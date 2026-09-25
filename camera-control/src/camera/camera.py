"""The Camera brain -- vision engine + coordinator (blueprint §5).

This is a CLOSED-LOOP brain (like the magnet): it runs a background *engine
thread* that, every frame, does the whole vision pipeline and -- when enabled --
closes the stabiliser and continuous-focus loops by commanding the stage and Z.

Per-frame pipeline (see :meth:`_process`):
    grab -> preprocess (clip/rotate/mirror) -> temporal average
         -> find spot (threshold + centre of mass)
         -> match template (if a reference is loaded and tracking is on)
         -> pin the scanning array to the template, compute selected-point<->spot
         -> stabiliser step (move XY to null the distance), if enabled
         -> continuous focus (dither-climb Z), if enabled
         -> publish a status snapshot + keep the processed frame for the GUI

Request-driven operations (snapshot, autofocus sweep, capture/load/save template,
click-to-go, objective calibration) are methods callable from the service/GUI.
The ones that need to grab many frames (autofocus) are handed to the engine
thread through a small request flag so only ONE thread ever touches the camera.

FIRE-AND-FORGET contract: setters return immediately; progress shows up in
``status()``.  ``status()`` must never raise.
"""

from __future__ import annotations

import inspect
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field

import cv2
import numpy as np

from . import objectives as OBJ
from . import vision as V
from .config import Config
from .template_io import BackupPattern, Reference, load_template, save_template

# Saved by save_config(), loaded by scripts/run_service.py when present, so a
# measured spot position and tuned thresholds survive a restart.
DEFAULT_CONFIG_NAME = "camera.ini"


class _AutofocusKilled(Exception):
    """Raised inside a sweep when Kill AF is pressed (or the engine stops)."""


# --------------------------------------------------------------------------- #
# Status snapshot (a plain dataclass; asdict -> the wire/GUI)
# --------------------------------------------------------------------------- #
@dataclass
class CameraStatus:
    connected: bool = False
    frame_number: int = 0
    fps: float = 0.0

    spot_found: bool = False          # DETECTED this frame (in the search box)
    spot_x: float = 0.0               # THE spot position = the calibrated one
    spot_y: float = 0.0
    spot_live_x: float = 0.0          # this frame's detected centroid (information)
    spot_live_y: float = 0.0
    spot_area: float = 0.0            # detected size this frame, px^2
    spot_bbox_x: int = 0
    spot_bbox_y: int = 0
    spot_bbox_w: int = 0
    spot_bbox_h: int = 0
    spot_holes: int = 0
    spot_orientation: float = 0.0
    spot_calibrated: bool = False     # True: spot_x/y are the CALIBRATED position, not detected

    pattern_loaded: bool = False
    match_found: bool = False
    template_w: int = 0               # size of the loaded template, px (0 = none)
    template_h: int = 0
    template_x: float = 0.0
    template_y: float = 0.0
    match_score: float = 0.0
    # Backup patterns: which one drives (0 = main), how many backups, the main
    # template's position derived from the driver (may be off-screen), and one
    # [x, y, w, h, matched, score] per pattern -- matched, or where it should be.
    pattern_driver: int = 0
    backups_n: int = 0
    anchor_x: float = 0.0
    anchor_y: float = 0.0
    pattern_boxes: list = field(default_factory=list)

    tracking_on: bool = False
    stabilize_on: bool = False
    continuous_focus_on: bool = False

    selected_index_x: int = 0
    selected_index_y: int = 0
    spot_at_index_x: int = 0
    spot_at_index_y: int = 0
    selected_point_x: float = 0.0
    selected_point_y: float = 0.0
    point_minus_spot_x: float = 0.0   # pixels (selected point - spot)
    point_minus_spot_y: float = 0.0
    distance_um: float = 0.0          # magnitude of the above, in um
    stable: bool = False
    # True only after a FULL averaged window AT THE CURRENTLY SELECTED POINT was
    # within stable_radius_um; cleared by selecting another point. `stable` is
    # not enough for a scan to wait on: between corrections it reports single
    # frames, so it can flicker True while the stage is still passing through.
    point_settled: bool = False
    # Where the laser spot is on the sample, in um, measured from the MAIN (first)
    # template -- still valid while a backup pattern drives and the main one is
    # off-screen. Image axes: +x right, +y down. NaN without a tracked template
    # or a calibrated spot (Python's json carries NaN; every consumer is Python).
    spot_from_template_x_um: float = float("nan")
    spot_from_template_y_um: float = float("nan")

    # Z position in the Z device's unit. The names say "voltage"/"_v" for
    # historical reasons (the piezo rig drives Z in volts); on the KIM rig these
    # carry MICROMETRES. `z_unit` says which, and the GUI labels follow it.
    z_voltage: float = 0.0
    af_running: bool = False
    af_error: str = "OK"
    # Number of the last REQUESTED autofocus (1, 2, ...). A scan waits for
    # "af_id == the number my request got AND not af_running" -- plain
    # "not af_running" can be read off a frame from before the request
    # (gotchas #2 and #17).
    af_id: int = 0
    best_focus_v: float = 0.0
    z_unit: str = "V"
    z_min: float = 0.0
    z_max: float = 75.0

    stage_x: float = 0.0
    stage_y: float = 0.0
    stage_moving: bool = False
    # The XY stage in its own terms: jog unit ("steps" on the KIM rig, whose um
    # are nominal; "um" on the piezo rig), the step counter where there is one,
    # whether it has a Datum, and the travel limits actually in force -- the
    # stage's own (`limits_from_stage`) or cfg.limits.
    xy_step_unit: str = "um"
    stage_steps_x: int = 0
    stage_steps_y: int = 0
    xy_has_datum: bool = False
    limits_from_stage: bool = False
    # Is the motion hardware answering? A stage behind a service (kim) can be
    # off or restarting; the GUI greys the stage controls and offers
    # "Reconnect stage" instead of letting every click wait out a timeout.
    stage_ok: bool = True
    stage_error: str = ""
    x_min: float = 0.0
    x_max: float = 0.0
    y_min: float = 0.0
    y_max: float = 0.0

    pixel_size_x: float = 0.413
    pixel_size_y: float = 0.413
    objective_name: str = ""

    #: Manifest revision, filled in by the service (see net/describe.py) so a
    #: client can tell its cached manifest went stale. None from the brain
    #: itself, which knows nothing about the wire.
    describe_rev: int | None = None


class Camera:
    def __init__(self, camera, xy_stage, zfocus, cfg: Config | None = None):
        self.backend = camera        # CameraBackend
        self.xy = xy_stage           # XYStage
        self.z = zfocus              # ZFocus
        self.cfg = cfg or Config()

        # Reference (template + array offset + meta); None until captured/loaded.
        self.reference: Reference | None = None

        # Live state (guarded by _lock).  Create the lock/status FIRST so the
        # objective application below (which touches them) is safe.
        self._lock = threading.RLock()
        self._status = CameraStatus()

        # AUTHORITATIVE control flags.  These must NOT live inside self._status:
        # the engine rebuilds a fresh CameraStatus every frame and replaces
        # self._status wholesale, so a setter that poked the old status object
        # would be clobbered (a lost-update race).  The engine copies these into
        # each frame's status; setters mutate these.
        self._tracking_on = False
        self._stabilize_on = False
        self._cf_on = False
        # (ix, iy) of the point the stabiliser last CONFIRMED with a full averaged
        # window, or None. Cleared by every setter that changes the target; the
        # engine stores the index it read at the START of its frame, so a frame
        # that straddles a set_selected_index marks the old point, not the new.
        self._settled_for: tuple | None = None

        # Objective table -> pixel size.
        self._objectives = OBJ.load_objectives(self.cfg.image.objectives_file)
        self._apply_objective(self.cfg.image.objective_name, quiet=True)
        self._last_frame: np.ndarray | None = None    # processed grayscale
        self._last_template_xy: tuple | None = None   # MAIN template position (anchor)
        self._driver = 0                               # which pattern drives (0 = main)
        self._driver_xy: tuple | None = None           # where the driver is in view
        self._avg_buf: deque = deque(maxlen=max(1, self.cfg.stabilizer.images_to_average))
        self._temporal: deque = deque(maxlen=max(1, self.cfg.camera.running_avg_frames))
        self._cf_dir = 1.0            # continuous-focus dither direction
        self._cf_last_metric: float | None = None
        # Autofocus state lives HERE, not in the status snapshot: the engine
        # replaces the snapshot every frame, and a flag written into it from
        # another thread can be lost (gotcha #1) -- a scan waiting on it would
        # then return early or hang. Every frame copies these in.
        self._af_id = 0               # last requested run
        self._af_busy = False         # a run is queued or running
        self._af_state = "OK"         # OK | queued | running | killed | no Z | <error>
        self._af_best = 0.0
        # Last autofocus sweep (for the GUI's focus-vs-Z plot).
        self._af_curve = {"z": [], "metric": [], "best": 0.0, "maximise": True}
        # Alignment-accuracy log: residual (dx_um, dy_um) per frame when enabled.
        self._acc_on = False
        self._acc_log: deque = deque(maxlen=256)
        self._measuring_spot = False  # calibrate_spot() searches the whole frame
        self._z_target: float | None = None   # last commanded Z (see step_z)
        self._z_target_t = 0.0
        self._stab_move_t = -1e9              # when the stabiliser last moved
        self._xy_target: tuple | None = None  # last commanded XY jog target (see step_xy)
        self._xy_target_t = 0.0
        self._xy_target_unit = ""
        # ... and the same target in STEPS, for a step-counting stage jogged in um:
        # repeated clicks must add up while the (open-loop) stage is still walking
        self._xy_steps_target: tuple = (0, 0)

        # Engine control.
        self._stop = threading.Event()
        self._engine: threading.Thread | None = None
        self._af_request: dict | None = None
        self._af_kill = threading.Event()
        self._frame_times: deque = deque(maxlen=10)

        # Event hook (the service replaces this).
        self._on_event = lambda level, msg: None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self.backend.open()
        exp = float(getattr(self.cfg.camera, "exposure_us", 0.0) or 0.0)
        if exp > 0:
            try:
                self.backend.set_feature("ExposureTime", exp)
            except Exception as exc:     # a sim/other camera may not have it
                self._emit("warn", f"could not apply exposure {exp:g} us: {exc}")
        self.xy.open()
        if self.cfg.hardware.use_z:
            self.z.open()
        with self._lock:
            self._status.connected = True
            self._status.objective_name = self.cfg.image.objective_name
            self._status.pixel_size_x = self.cfg.image.pixel_size_x_um
            self._status.pixel_size_y = self.cfg.image.pixel_size_y_um
        self._stop.clear()
        self._engine = threading.Thread(target=self._run, name="camera-engine", daemon=True)
        self._engine.start()
        self._emit("info", "camera engine started")

    def shutdown(self) -> None:
        self._stop.set()
        if self._engine is not None:
            self._engine.join(timeout=2.0)
        for dev, name in ((self.backend, "camera"), (self.xy, "xy"), (self.z, "z")):
            try:
                dev.close()
            except Exception:
                pass
        with self._lock:
            self._status.connected = False

    # ------------------------------------------------------------------ #
    # engine thread
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        period = 1.0 / max(1e-3, self.cfg.camera.frame_rate)
        while not self._stop.is_set():
            t0 = time.monotonic()
            # A queued autofocus sweep takes over the camera for its duration.
            with self._lock:
                req, self._af_request = self._af_request, None
            if req is not None:
                self._do_autofocus(req)
            else:
                try:
                    self._process()
                except Exception as exc:   # a bad frame must never kill the loop
                    self._emit("error", f"engine: {type(exc).__name__}: {exc}")
            dt = time.monotonic() - t0
            extra = self.cfg.camera.extra_delay_ms / 1000.0
            time.sleep(max(0.0, period - dt) + extra)

    def _grab_gray(self) -> np.ndarray:
        raw = self.backend.grab()
        img = self.cfg.image
        clip = None
        if img.clip_enabled:
            clip = (img.clip_left, img.clip_top, img.clip_right, img.clip_bottom)
        return V.preprocess(V.to_gray(raw), img.rotation_deg, img.symmetry, clip)

    def _process(self) -> None:
        gray = self._grab_gray()

        # Temporal (running) average across frames.
        self._temporal.append(gray.astype(np.float32))
        if len(self._temporal) > 1:
            gray = np.mean(self._temporal, axis=0).astype(np.uint8)

        st = CameraStatus()
        st.connected = True
        st.frame_number = self._status.frame_number + 1

        # -- spot ------------------------------------------------------- #
        # Two different things, kept apart on purpose (Lukas, 2026-09-13):
        #  * DETECTION, every frame: threshold only a search box around the
        #    calibrated position, to evaluate the spot's SIZE (area, extent) and
        #    whether it is there at all. Its centroid is information only.
        #  * POSITION: the laser spot is fixed in the image, so where it is is a
        #    user decision -- "Calibrate spot" stores it (calibrate_spot), and
        #    THAT position is what click-to-go and the stabiliser use. Not
        #    calibrated -> no position -> neither acts on the spot.
        sp = self.cfg.spot
        center = (sp.ref_x, sp.ref_y) if (sp.ref_set and not self._measuring_spot) else None
        det = V.find_spot(gray, sp.thr_lower, sp.thr_upper, sp.bright_spot,
                          sp.lookup_region_px if center else 0, center, sp.min_area_px,
                          sp.max_area_px, sp.reject_border, sp.search_shape,
                          sp.lookup_region_y_px, symmetric=bool(sp.symmetric and center))
        st.spot_found = det.found
        if det.found:
            st.spot_live_x, st.spot_live_y, st.spot_area = det.cx, det.cy, det.area
            st.spot_bbox_x, st.spot_bbox_y, st.spot_bbox_w, st.spot_bbox_h = det.bbox
            st.spot_holes = det.n_holes
            st.spot_orientation = det.orientation_deg
        spot_position_ok = bool(sp.ref_set)
        if spot_position_ok:
            st.spot_x, st.spot_y = float(sp.ref_x), float(sp.ref_y)
            st.spot_calibrated = True

        # -- pixel size / objective ------------------------------------- #
        px_x = self.cfg.image.pixel_size_x_um
        px_y = self.cfg.image.pixel_size_y_um
        st.pixel_size_x, st.pixel_size_y = px_x, px_y
        st.objective_name = self.cfg.image.objective_name

        # -- flags (from the authoritative attributes, race-free) ------- #
        st.tracking_on = self._tracking_on
        st.stabilize_on = self._stabilize_on
        st.continuous_focus_on = self._cf_on
        st.selected_index_x = self.cfg.scanning.selected_index_x
        st.selected_index_y = self.cfg.scanning.selected_index_y
        st.pattern_loaded = self.reference is not None

        # -- template match + scanning geometry ------------------------- #
        geo = None
        st.backups_n = 0 if self.reference is None else len(self.reference.backups)
        if self.reference is not None and st.tracking_on:
            anchor = self._track_patterns(gray, st)
            if anchor is not None:
                offs = V.scanning_array_pixel_offsets(
                    self.cfg.scanning.points_x, self.cfg.scanning.points_y,
                    self.cfg.scanning.dx_um, self.cfg.scanning.dy_um,
                    self.cfg.scanning.angle_deg, px_x, px_y)
                spot_xy = (st.spot_x, st.spot_y) if spot_position_ok else anchor
                # the array hangs off the MAIN template's (possibly off-screen)
                # position, whichever pattern is driving
                geo = V.pin_array_and_distance(
                    anchor, self.reference.array_center_offset_px, offs,
                    self.cfg.scanning.selected_index_x,
                    self.cfg.scanning.selected_index_y, spot_xy)
                st.selected_point_x, st.selected_point_y = geo.selected_point_px
                st.point_minus_spot_x, st.point_minus_spot_y = geo.point_minus_spot_px
                st.spot_at_index_x, st.spot_at_index_y = geo.spot_at_index
                if spot_position_ok:
                    st.spot_from_template_x_um, st.spot_from_template_y_um = V.pixels_to_um(
                        st.spot_x - anchor[0], st.spot_y - anchor[1], px_x, px_y)

        # -- stabiliser ------------------------------------------------- #
        # Stands down from the moment an autofocus is REQUESTED (not only once
        # it runs): a correction sent in the frame between would still be
        # walking when Z starts, and a point_settled from this frame would let
        # a scan go on before focus has even begun.
        af_pending = self._af_busy
        if (geo is not None and st.stabilize_on and spot_position_ok and st.match_found
                and not af_pending):
            stable = self._stabilise_step(geo, px_x, px_y, st)
            st.stable = stable
        else:
            self._avg_buf.clear()
            self._settled_for = None          # a lost match or a stopped loop is not settled
        st.point_settled = (st.stabilize_on and not af_pending and self._settled_for
                            == (st.selected_index_x, st.selected_index_y))

        # -- alignment-accuracy log (residual spot->point distance, um) -- #
        if geo is not None and spot_position_ok and self._acc_on:
            dux, duy = V.pixels_to_um(geo.point_minus_spot_px[0],
                                      geo.point_minus_spot_px[1], px_x, px_y)
            self._acc_log.append((dux, duy))

        # -- continuous focus ------------------------------------------- #
        if st.continuous_focus_on and self.cfg.hardware.use_z and not af_pending:
            self._continuous_focus_step(gray, st)

        # -- motion / z read-back --------------------------------------- #
        try:
            st.stage_x, st.stage_y = self.xy.read_xy()
            st.stage_moving = self.xy.moving()
            if callable(getattr(self.xy, "read_steps", None)):
                st.stage_steps_x, st.stage_steps_y = self.xy.read_steps()
        except Exception:
            pass
        st.stage_ok, st.stage_error = self.stage_state()
        st.xy_step_unit = self.xy_step_unit()
        st.xy_has_datum = callable(getattr(self.xy, "zero_counter", None))
        st.limits_from_stage = bool(getattr(self.xy, "owns_limits", False))
        xylim = self.xy_limits()
        if xylim is not None:
            (st.x_min, st.x_max), (st.y_min, st.y_max) = xylim
        if self.cfg.hardware.use_z:
            try:
                st.z_voltage = self.z.read_z()
            except Exception:
                pass
        st.z_unit = self.z_unit()
        zlim = self.z_limits()
        if zlim is not None:
            st.z_min, st.z_max = zlim

        # carry forward sticky fields (a normal frame must not clobber AF state)
        with self._lock:
            st.best_focus_v = self._af_best
            st.af_error = self._af_state
            st.af_running = self._af_busy
            st.af_id = self._af_id
            if self._af_busy:              # a request that arrived mid-frame
                st.point_settled = False
                st.stable = False

        # fps
        now = time.monotonic()
        self._frame_times.append(now)
        if len(self._frame_times) >= 2:
            span = self._frame_times[-1] - self._frame_times[0]
            if span > 0:
                st.fps = (len(self._frame_times) - 1) / span

        with self._lock:
            self._last_frame = gray
            self._status = st

    # ------------------------------------------------------------------ #
    # pattern tracking: the main template + backups
    # ------------------------------------------------------------------ #
    def _track_patterns(self, gray, st) -> tuple | None:
        """Match the main template and every backup; pick the DRIVER; return the
        main template's position (the ANCHOR the scan array hangs off), or None.

        Why backups: a long scan walks the sample so far that the main template
        leaves the screen and stabilisation stops (Lukáš, 2026-09-14). Each
        backup is stored with its offset from the main template, so ANY matched
        pattern tells where the main one is -- on screen or not:
            anchor = driver_position - driver_offset      (main offset = 0)

        * Every pattern is searched only in a box around where the anchor says it
          should be, and skipped when that place is off the frame (a template
          that does not fit cannot match -- and a full-frame search would find
          a false one). Nothing known yet (first frame, or `full_image`): whole
          frame, driver first; once one matches, the rest are predicted from it.
        * The driver keeps driving while it is matched and more than
          `edge_margin_px` inside the frame. Then the matched pattern with the
          most room takes over -- so two patterns side by side cannot flip-flop.
        * While others are in view with the driver, their offsets are refined
          slowly (`offset_learn_rate`), which absorbs a small rotation or drift;
          a disagreement beyond `offset_warn_px` is treated as a bad match.
        """
        ref, pat = self.reference, self.cfg.pattern
        H, W = gray.shape[:2]
        pats = [(ref.template, (0.0, 0.0))] + [(b.template, tuple(b.offset_px))
                                               for b in ref.backups]
        n = len(pats)
        if self._driver >= n:
            self._driver = 0
        anchor = self._last_template_xy
        reports: list = [None] * n
        order = [self._driver] + [k for k in range(n) if k != self._driver]
        for k in order:
            tpl, off = pats[k]
            th, tw = tpl.shape[:2]
            box = None
            if anchor is not None and not pat.full_image:
                ex, ey = anchor[0] + off[0], anchor[1] + off[1]
                if not (tw / 2 <= ex <= W - tw / 2 and th / 2 <= ey <= H - th / 2):
                    continue                       # expected off the frame: not visible
                s = pat.safety_area_px
                box = (int(ex - s - tw / 2), int(ey - s - th / 2),
                       int(2 * s + tw), int(2 * s + th))
            m = V.match_template(gray, tpl, pat.min_match_score,
                                 pat.angle_start, pat.angle_end, pat.angle_step, box)
            reports[k] = m
            if m.found and anchor is None:
                anchor = (m.x - off[0], m.y - off[1])   # relocked: predict the rest

        def room(k):
            m = reports[k]
            th, tw = pats[k][0].shape[:2]
            return min(m.x - tw / 2, W - m.x - tw / 2, m.y - th / 2, H - m.y - th / 2)

        found = [k for k in range(n) if reports[k] is not None and reports[k].found]
        margin = float(pat.edge_margin_px)
        if found and not (self._driver in found and room(self._driver) >= margin):
            best = max(found, key=lambda k: (room(k) >= margin, room(k)))
            if best != self._driver:
                why = "near the edge" if self._driver in found else "lost"
                self._emit("info", f"pattern {self._pattern_name(best)} now drives "
                                   f"({self._pattern_name(self._driver)} {why})")
                self._driver = best

        st.pattern_driver = self._driver
        st.template_h, st.template_w = pats[self._driver][0].shape[:2]
        boxes = []
        for k in range(n):
            m = reports[k]
            th, tw = pats[k][0].shape[:2]
            if m is not None and m.found:
                boxes.append([float(m.x), float(m.y), int(tw), int(th), True, float(m.score)])
            elif anchor is not None:               # where it should be (maybe off-screen)
                boxes.append([float(anchor[0] + pats[k][1][0]), float(anchor[1] + pats[k][1][1]),
                              int(tw), int(th), False, 0.0])
        st.pattern_boxes = boxes

        drv = reports[self._driver]
        if drv is None or not drv.found:
            st.match_found = False
            return None
        # published as "the template": the one actually in view, so another
        # module (kim's camera calibration) can track the same drawn feature
        st.match_found = True
        st.template_x, st.template_y, st.match_score = drv.x, drv.y, drv.score
        doff = pats[self._driver][1]
        anchor = (drv.x - doff[0], drv.y - doff[1])
        self._last_template_xy = anchor
        self._driver_xy = (drv.x, drv.y)
        st.anchor_x, st.anchor_y = anchor

        # refine the other visible backups' offsets against the driver
        rate = float(pat.offset_learn_rate)
        if rate > 0 and room(self._driver) >= 0:
            for k in found:
                if k == self._driver or k == 0 or room(k) < 0:
                    continue
                b = ref.backups[k - 1]
                meas = (reports[k].x - anchor[0], reports[k].y - anchor[1])
                err = float(np.hypot(meas[0] - b.offset_px[0], meas[1] - b.offset_px[1]))
                if err > pat.offset_warn_px:
                    self._warn_limited(f"backup{k}", f"pattern {self._pattern_name(k)} is "
                                       f"{err:.0f} px from where pattern "
                                       f"{self._pattern_name(self._driver)} puts it "
                                       f"-- a bad match? offset not updated")
                    continue
                b.offset_px = (b.offset_px[0] + rate * (meas[0] - b.offset_px[0]),
                               b.offset_px[1] + rate * (meas[1] - b.offset_px[1]))
            if self._driver != 0 and 0 in found and room(0) >= 0:
                # the MAIN template is in view while a backup drives: the backup's
                # offset is measured directly
                b = ref.backups[self._driver - 1]
                meas = (drv.x - reports[0].x, drv.y - reports[0].y)
                if float(np.hypot(meas[0] - b.offset_px[0],
                                  meas[1] - b.offset_px[1])) <= pat.offset_warn_px:
                    b.offset_px = (b.offset_px[0] + rate * (meas[0] - b.offset_px[0]),
                                   b.offset_px[1] + rate * (meas[1] - b.offset_px[1]))
        return anchor

    @staticmethod
    def _pattern_name(k: int) -> str:
        return "main" if k == 0 else f"backup {k}"

    def capture_backup(self, roi: tuple) -> str:
        """Add a BACKUP pattern from the last frame. The main template (or a
        backup) must be tracked right now: the new pattern's offset is measured
        against the main template's position in this same view."""
        if self.reference is None:
            raise RuntimeError("draw the main template first")
        with self._lock:
            frame = None if self._last_frame is None else self._last_frame.copy()
            tracked = self._status.match_found and self._tracking_on
        if frame is None:
            raise RuntimeError("no frame yet")
        if not tracked or self._last_template_xy is None:
            raise RuntimeError("turn tracking on and keep a pattern matched while "
                               "adding a backup (its offset is measured from it)")
        cx, cy, w, h = roi
        x0 = int(max(0, cx - w / 2)); y0 = int(max(0, cy - h / 2))
        x1 = int(min(frame.shape[1], cx + w / 2)); y1 = int(min(frame.shape[0], cy + h / 2))
        if x1 - x0 < 8 or y1 - y0 < 8:
            raise RuntimeError("backup region too small")
        tpl = frame[y0:y1, x0:x1].copy()
        center = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        ax, ay = self._last_template_xy
        self.reference.backups.append(BackupPattern(tpl, (center[0] - ax, center[1] - ay)))
        k = len(self.reference.backups)
        msg = (f"backup {k} captured: {x1 - x0}x{y1 - y0}px, "
               f"offset ({center[0] - ax:.1f}, {center[1] - ay:.1f})px from the main template")
        self._emit("info", msg)
        return msg

    def clear_backups(self) -> None:
        if self.reference is not None:
            self.reference.backups.clear()
        self._driver = 0
        self._emit("info", "backup patterns cleared")

    def list_backups(self) -> list:
        if self.reference is None:
            return []
        return [{"index": i + 1, "w": int(b.template.shape[1]), "h": int(b.template.shape[0]),
                 "offset_px": [float(b.offset_px[0]), float(b.offset_px[1])]}
                for i, b in enumerate(self.reference.backups)]

    # ------------------------------------------------------------------ #
    # stabiliser  (ports Stabilization_StableAtPixelOrCorrectV2.vi)
    # ------------------------------------------------------------------ #
    def _stabilise_step(self, geo, px_x, px_y, st) -> bool:
        """One stabiliser cycle: AVERAGE a full window of measurements, THEN
        correct once, then restart the window.

        Averaging *and* moving every frame would correct on stale (pre-move)
        error and limit-cycle; instead we collect ``images_to_average`` fresh
        measurements, act on their mean, and clear the buffer so the next window
        reflects the new position.  This is what makes it settle cleanly.
        """
        stb = self.cfg.stabilizer
        radius = max(0.0, float(stb.stable_radius_um))
        gain = min(max(float(stb.gain), 0.01), 2.0)   # > 1 overshoots; 2 = oscillates

        # live readout: instantaneous distance every frame
        inst = np.array(geo.point_minus_spot_px, dtype=float)
        inst_um = np.array(V.pixels_to_um(inst[0], inst[1], px_x, px_y))
        st.distance_um = float(np.hypot(inst_um[0], inst_um[1]))
        inst_stable = st.distance_um <= radius

        # After a correction: frames grabbed while the stage is still on its way
        # describe a position that is about to change, and averaging them
        # corrects on stale error (overshoot, limit cycle). Wait settle_s -- also
        # covers a status stream that has not yet noticed the move started
        # (gotcha #2) -- and, on an open-loop stage (KIM), until it has stopped.
        if time.monotonic() - self._stab_move_t < max(0.0, float(stb.settle_s)):
            self._avg_buf.clear()
            return inst_stable
        if getattr(self.xy, "open_loop", False):
            try:
                moving = self.xy.moving()
            except Exception:
                moving = False
            if moving:
                self._avg_buf.clear()
                return inst_stable

        self._avg_buf.append(inst)
        if len(self._avg_buf) < self._avg_buf.maxlen:
            # window not full yet -> report instantaneous stability, don't move
            return inst_stable

        avg = np.mean(self._avg_buf, axis=0)          # (dx_px, dy_px)
        dist_um = np.array(V.pixels_to_um(avg[0], avg[1], px_x, px_y))
        st.distance_um = float(np.hypot(dist_um[0], dist_um[1]))
        stable = st.distance_um <= radius

        if stable:
            self._avg_buf.clear()                     # start a fresh window
            # a whole averaged window agreed: THIS point is settled
            self._settled_for = (st.selected_index_x, st.selected_index_y)
            return stable

        self._settled_for = None                      # about to move: no longer settled

        # Move the sample to null the distance: the selected point is ON the
        # sample, so shifting the image by s moves it by s; to bring
        # (selected_point - spot) to zero, shift the image by -gain * distance.
        self._stab_move_t = time.monotonic()

        # KIM rig: ask the stage to shift the IMAGE, in pixels. Its camera
        # calibration knows how the stage is mounted (90 deg on the lab rig),
        # the direction-dependent step size and the crosstalk; no pixel size or
        # axis assumption is involved. Uncalibrated -> refused, and we do NOT
        # fall back to guessing: a wrong-sign stabiliser pushes the sample away.
        image_move = getattr(self.xy, "move_image_px", None)
        if image_move is not None:
            shift = (-gain * avg[0] if stb.move_with_x else 0.0,
                     -gain * avg[1] if stb.move_with_y else 0.0)
            try:
                image_move(shift[0], shift[1], context=self.image_context())
            except Exception as exc:
                self._warn_limited("stabiliser", f"stabiliser move refused: {exc}")
            self._avg_buf.clear()
            return stable

        # Piezo rig: stage axes assumed aligned with the image (+stage x -> +px x).
        try:
            cx, cy = self.xy.read_xy()
        except Exception:
            self._avg_buf.clear()
            return stable
        new_x, new_y = cx, cy
        if stb.move_with_x:
            new_x = cx - gain * dist_um[0]
        if stb.move_with_y:
            new_y = cy - gain * dist_um[1]
        new_x, new_y = self._clamp_xy(new_x, new_y)
        try:
            self.xy.move_xy(new_x, new_y)
        except Exception as exc:
            self._emit("warn", f"stabiliser move failed: {exc}")
        self._avg_buf.clear()                         # restart averaging post-move
        return stable

    # ------------------------------------------------------------------ #
    # continuous focus  (dither hill-climb on Z)
    # ------------------------------------------------------------------ #
    def _continuous_focus_step(self, gray, st) -> None:
        af = self.cfg.autofocus
        try:
            metric = self._focus_metric(gray)
        except RuntimeError:
            return
        if not np.isfinite(metric):
            return                       # spot not seen this frame: no step on noise
        maximise = V.focus_is_maximised(af.mechanism)
        if self._cf_last_metric is not None:
            delta = metric - self._cf_last_metric
            # Small hysteresis so sensor noise doesn't cause constant reversals.
            eps = 1e-6 + 0.002 * abs(self._cf_last_metric)
            worse = (delta < -eps) if maximise else (delta > eps)
            if worse:
                self._cf_dir *= -1.0   # went the wrong way -> turn around
        self._cf_last_metric = metric
        try:
            z = self.z.read_z()
            step = max(1e-3, af.continuous_gain)
            self.z.set_z(self._clamp_z(z + self._cf_dir * step))
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # autofocus sweep  (queued: runs inside the engine thread)
    # ------------------------------------------------------------------ #
    def autofocus(self) -> int:
        """Request an autofocus; runs on the engine thread. Returns its NUMBER.

        The run is finished when status shows ``af_id`` == that number and
        ``af_running`` False; ``af_error`` then says how it went ("OK").
        """
        with self._lock:
            self._af_id += 1
            self._af_request = {"kind": "sweep", "id": self._af_id}
            self._af_busy = True
            self._af_state = "queued"
            self._publish_af_locked()
            self._status.point_settled = False     # this snapshot too, not only the next
            self._status.stable = False
            return self._af_id

    def kill_af(self) -> None:
        self._af_kill.set()          # a sweep in progress stops at its next check
        with self._lock:
            if self._af_request is not None:     # queued, never started: cancel it
                self._af_request = None
                self._af_busy = False
                self._af_state = "killed"
                self._publish_af_locked()

    def _publish_af_locked(self) -> None:
        """Copy the AF state into the current snapshot (caller holds the lock),
        so a status read right now already sees it; the next frame copies it
        again from the attributes, so nothing is lost if this snapshot is."""
        self._status.af_id = self._af_id
        self._status.af_running = self._af_busy
        self._status.af_error = self._af_state
        self._status.best_focus_v = self._af_best

    def _af_finish(self, state: str, best: float | None = None,
                   z: float | None = None) -> None:
        """End a run: state, best focus and "not busy" in ONE critical section
        (gotcha #28), and still busy if another request queued up meanwhile --
        otherwise its waiter would see "not running" with its own id and go."""
        with self._lock:
            if best is not None:
                self._af_best = float(best)
            if z is not None:
                self._status.z_voltage = float(z)
            self._af_busy = self._af_request is not None
            self._af_state = "queued" if self._af_busy else state
            self._publish_af_locked()

    def _do_autofocus(self, req) -> None:
        """Run one autofocus with the image loops PAUSED.

        Lukas (2026-09-24): "while performing AF don't stabilize the image and
        don't track the pattern. They can wander off." They are paused anyway
        in the sense that the run owns the engine thread -- no frame is
        processed -- but their STATE would carry over: a stabiliser window
        half-filled before AF, a `point_settled` / `stable` that was true before
        Z moved, a running average of pre-AF frames. All of that is dropped at
        the start, and the stabiliser treats the end of AF like the end of a
        correction move (settle_s before it measures again). Tracking and the
        stabiliser stay SWITCHED ON: they resume by themselves afterwards and
        re-find the pattern wherever it wandered to.
        """
        self._pause_image_loops()
        try:
            self._run_autofocus(req)
        finally:
            if self._af_state == "running":      # left early (shutdown): never "busy" forever
                self._af_finish("stopped")
            self._pause_image_loops()            # nothing measured during AF counts after it
            self._stab_move_t = time.monotonic()

    def _pause_image_loops(self) -> None:
        self._avg_buf.clear()
        self._temporal.clear()
        self._settled_for = None
        self._cf_last_metric = None
        with self._lock:
            self._status.point_settled = False
            self._status.stable = False

    def _wait_xy_still(self, live, timeout_s: float = 10.0) -> None:
        """A stabiliser correction may still be walking (open-loop XY): let it
        arrive before Z starts, or the focus levels are scored on a moving image."""
        t_end = time.monotonic() + timeout_s
        while time.monotonic() < t_end:
            try:
                if not self.xy.moving():
                    return
            except Exception:
                return
            live()
            time.sleep(0.05)

    def _run_autofocus(self, req) -> None:
        af = self.cfg.autofocus
        with self._lock:
            self._af_state = "running"
            self._publish_af_locked()
        if not self.cfg.hardware.use_z:
            self._af_finish("no Z")
            return
        # Open-loop Z (KIM) walks rather than jumps, and its step size differs up
        # vs down. So: wait for each move to ARRIVE, and reach every target from
        # BELOW. The sweep climbs, so only the first level and the final park
        # (both usually downward moves) need the detour under the target.
        open_loop = bool(getattr(self.z, "open_loop", False))
        margin = max(0.0, float(af.approach_margin)) if open_loop else 0.0
        wait = getattr(self.z, "wait_settled", None)
        settle_s = self.cfg.hardware.z_step_time_ms / 1000.0
        unit = self.z_unit()
        self._af_kill.clear()
        self._af_curve = {"z": [], "metric": [], "best": 0.0,
                          "maximise": V.focus_is_maximised(af.mechanism)}

        # The sweep owns the engine thread, so nothing else publishes frames
        # while it runs: the view used to freeze for the whole sweep. live()
        # grabs and publishes a frame (+ Z) so you can watch focus change.
        def live(gray=None) -> np.ndarray:
            if self._af_kill.is_set() or self._stop.is_set():
                raise _AutofocusKilled()
            g = self._grab_gray() if gray is None else gray
            try:
                zr = float(self.z.read_z())
            except Exception:
                zr = self._status.z_voltage
            with self._lock:
                self._last_frame = g
                self._status.frame_number += 1
                self._status.z_voltage = zr
            return g

        try:
            wait_takes_tick = wait is not None and "tick" in inspect.signature(wait).parameters
        except (TypeError, ValueError):
            wait_takes_tick = False

        def go(zv: float) -> None:
            self.z.set_z(float(zv))
            if wait is not None:
                if wait_takes_tick:
                    wait(tick=live)
                else:
                    wait()
            t_end = time.monotonic() + settle_s    # mechanical settle, even after arrival
            live()
            while time.monotonic() < t_end:
                time.sleep(min(0.05, max(0.0, t_end - time.monotonic())))

        def approach(zv: float, current: float) -> None:
            if margin > 0.0 and zv < current:
                go(self._clamp_z(zv - margin))
            go(zv)

        # Where Z was before this run. A FAILED run goes back there (Lukas,
        # 2026-09-24: "if AF fails carry on with old Z"): a scan routine that
        # carries on after a failure must not measure the next row at whatever
        # sweep level the failure happened on -- that is a far worse focus than
        # the one it had. A KILLED run stays put: the operator stopped it there.
        try:
            z_start = float(self.z.read_z())
        except Exception:
            z_start = None
        try:
            if af.routine == "one_way":
                self._wait_xy_still(live)
                best, target, note = self._af_one_way(go, live)
                self._af_finish("OK", best, target)
                self._emit("info", f"autofocus (one way, from {af.approach_from}) -> best "
                                   f"{best:.3f} {unit}, parked {target:.3f} {unit}; {note}")
                return
            self._wait_xy_still(live)
            z0 = self.z.read_z()
            half = af.drive_amplitude_v / 2.0
            zmin, zmax = self.z.z_range()
            levels = np.linspace(max(zmin, z0 - half), min(zmax, z0 + half),
                                 max(3, int(af.steps)))
            maximise = V.focus_is_maximised(af.mechanism)
            metrics = []
            for i, zv in enumerate(levels):
                if self._stop.is_set():
                    return
                if i == 0:
                    approach(float(zv), z0)
                else:
                    go(float(zv))
                n = max(1, int(af.averages_per_level))
                samples = [self._focus_metric(live()) for _ in range(n)]
                finite = [v for v in samples if np.isfinite(v)]
                # a level where the spot was never seen is NaN: plotted as a gap
                # and left out of the fit, never scored as "area 0 = perfect"
                metrics.append(float(np.mean(finite)) if finite else float("nan"))
                # the focus plot fills in level by level
                self._af_curve = {"z": [float(v) for v in levels[:len(metrics)]],
                                  "metric": [float(v) for v in metrics],
                                  "best": None, "maximise": bool(maximise)}
            ok = np.isfinite(np.asarray(metrics, dtype=float))
            if ok.sum() == 0:
                raise RuntimeError("the spot was not seen at any Z level (threshold? "
                                   "search region? sweep range?)")
            best = V.best_focus_from_sweep(np.asarray(levels)[ok],
                                           np.asarray(metrics, dtype=float)[ok],
                                           maximise, af.fit_curve)
            self._af_curve = {"z": [float(v) for v in levels],
                              "metric": [float(v) for v in metrics],
                              "best": float(best), "maximise": bool(maximise)}
            target = self._clamp_z(best + af.offset_from_found_v)
            approach(target, float(levels[-1]))
            self._af_finish("OK", best, target)
            self._emit("info", f"autofocus -> best {best:.3f} {unit} "
                               f"(parked {target:.3f} {unit})")
        except _AutofocusKilled:
            self._af_finish("killed")
            self._emit("warn", "autofocus killed: Z left where it was")
        except Exception as exc:
            back = ""
            if z_start is not None and not self._stop.is_set():
                try:
                    # From below on an open-loop Z, like every other park.
                    approach(z_start, float(self.z.read_z()))
                    back = f"; Z back to {z_start:.3f} {unit}"
                except Exception as exc2:            # incl. a kill during the return
                    back = f"; could NOT return Z to {z_start:.3f} {unit}: {exc2!r}"
            # Finished only once Z is back: a scan waiting on this run must not
            # measure while Z is still walking home.
            self._af_finish(f"{type(exc).__name__}")
            self._emit("error", f"autofocus failed: {exc}{back}")

    # ------------------------------------------------------------------ #
    # autofocus routine "one_way"  (for a hysteretic, open-loop Z)
    # ------------------------------------------------------------------ #
    def _af_one_way(self, go, live) -> tuple[float, float, str]:
        """Find focus moving Z ONE way only; park by what the camera sees.

        Why (Lukáš, 2026-09-24): on the slip-stick PIA25 a step up is not a step
        down, so the sweep's "measure every level, fit, drive back to the best
        Z" lands somewhere else -- the step counter says focus, the image does
        not. Here every level whose metric counts is reached travelling in the
        approach direction ``d``, and the final position is chosen by the
        metric itself, never by the counter alone. Three phases:

        1. COARSE -- follow the slope. Probe one coarse step in ``d``; if that
           got worse, the minimum is BEHIND, so walk the other way (that walk
           only brackets, its levels are not trusted for the park). While the
           metric keeps improving the step grows: for spot_area the spot RADIUS
           (sqrt of the area) is ~linear in defocus far from focus, so two
           levels predict how far focus still is; other metrics just grow the
           step x1.5. Stop at the first level that is worse than the best.
           A spot too blurred to be detected (NaN) is searched for first.
        2. FINE -- from the approach side of the bracket, walk through it in
           ``fine_step_v`` until the metric has been worse than the best for
           ``rise_levels`` levels. Its best (parabola vertex if fit_curve) is
           the focus.
        3. PARK -- back off past the best by approach_margin (the only reverse
           move), walk in again in fine steps and stop at the first level whose
           metric is within ``park_tolerance`` of the fine walk's best. If the
           walk passes focus without getting there, back off further and retry;
           after three tries park by the counter and say so.

        Returns (best_z, parked_z, note). The focus plot shows the three phases.
        """
        af = self.cfg.autofocus
        maximise = V.focus_is_maximised(af.mechanism)
        d = 1.0 if af.approach_from == "below" else -1.0
        coarse = max(1e-6, abs(float(af.coarse_step_v)))
        fine = max(1e-6, abs(float(af.fine_step_v)))
        max_step = 8.0 * coarse
        rise = max(0.0, float(af.rise_fraction))
        n_rise = max(1, int(af.rise_levels))
        margin = max(fine, abs(float(af.approach_margin)))
        zmin, zmax = self.z.z_range()

        def clampz(v: float) -> float:
            return float(min(max(self._clamp_z(v), zmin), zmax))

        # "score" is smaller-is-better whatever the metric's own sense
        def score(m: float) -> float:
            return -m if maximise else m

        def worse(m: float, ref: float, frac: float = rise) -> bool:
            """m is meaningfully worse than ref (NaN = spot lost = worse)."""
            if not np.isfinite(m):
                return True
            return score(m) > score(ref) + frac * abs(ref)

        phases = {"coarse": ([], []), "fine": ([], []), "park": ([], [])}

        def publish(best=None):
            self._af_curve = {
                "z": list(phases["fine"][0]), "metric": list(phases["fine"][1]),
                "best": best, "maximise": bool(maximise),
                "phases": {k: {"z": list(v[0]), "metric": list(v[1])}
                           for k, v in phases.items()}}

        pos = {"z": float(self.z.read_z())}     # the COMMANDED position (counter)

        def measure(zv: float, phase: str) -> float:
            zv = clampz(zv)
            if zv != pos["z"]:
                go(zv)
                pos["z"] = zv
            n = max(1, int(af.averages_per_level))
            vals = [self._focus_metric(live()) for _ in range(n)]
            vals = [v for v in vals if np.isfinite(v)]
            m = float(np.mean(vals)) if vals else float("nan")
            phases[phase][0].append(zv)
            phases[phase][1].append(m)
            publish()
            return m

        z0 = pos["z"]

        # ---- 1. COARSE ------------------------------------------------------
        m = measure(z0, "coarse")
        if not np.isfinite(m):
            # Heavily out of focus: the spot is not even detected. Look for it
            # in coarse steps, the approach direction first.
            found = False
            for sgn in (d, -d):
                z = z0
                while abs(z - z0) < af.max_travel_v:
                    zn = clampz(z + sgn * coarse)
                    if zn == z:
                        break                           # at the Z limit
                    z = zn
                    m = measure(z, "coarse")
                    if np.isfinite(m):
                        found = True
                        break
                if found:
                    break
            if not found:
                raise RuntimeError(f"the spot was not seen within +-{af.max_travel_v:g} of "
                                   f"the start (threshold? search region? max_travel?)")
        z_best, m_best = pos["z"], m

        # probe one step in the approach direction: which side is focus on?
        zp = clampz(z_best + d * coarse)
        mp = measure(zp, "coarse") if zp != z_best else float("nan")
        if zp != z_best and not worse(mp, m_best) and score(mp) <= score(m_best):
            walk = d                                   # better ahead: keep going
            z_prev, m_prev, z_best, m_best = z_best, m_best, zp, mp
        elif zp != z_best and not worse(mp, m_best):
            walk = 0.0                                 # flat: already at the bottom
        else:
            walk = -d                                  # worse ahead: focus is behind
            z_prev, m_prev = zp, mp

        gaps = [coarse]                                # step sizes around the best
        if walk != 0.0:
            step = coarse
            z = z_best
            while True:
                # Follow the slope: how big may the next step be?
                if (af.mechanism == "spot_area" and np.isfinite(m_prev)
                        and m_prev > 0 and m_best > 0):
                    r_prev, r_now = np.sqrt(m_prev), np.sqrt(m_best)
                    k = (r_prev - r_now) / max(abs(z_best - z_prev), 1e-9)  # radius per unit Z
                    r_ref = np.sqrt(self.cfg.spot.ref_area) if self.cfg.spot.ref_area > 0 else 0.0
                    if k > 0:
                        remaining = max(0.0, r_now - r_ref) / k
                        step = float(np.clip(0.7 * remaining, coarse, max_step))
                    else:
                        step = coarse
                elif np.isfinite(m_prev) and score(m_best) < score(m_prev) - 2 * rise * abs(m_prev):
                    step = min(step * 1.5, max_step)   # still improving fast: far away
                else:
                    step = coarse
                zn = clampz(z + walk * step)
                if zn == z:
                    break                              # hit the Z limit
                if abs(zn - z0) > af.max_travel_v:
                    raise RuntimeError(f"no focus within {af.max_travel_v:g} of the start "
                                       f"(the metric was still improving)")
                mn = measure(zn, "coarse")
                gaps.append(abs(zn - z))
                if worse(mn, m_best):
                    break                              # passed the minimum: bracketed
                if score(mn) < score(m_best):          # (z_prev, m_prev) = level before the best

                    z_prev, m_prev = z_best, m_best
                    z_best, m_best = zn, mn
                z = zn
        # How far before the best the fine walk must start: up to the nearest
        # coarse level on the APPROACH side (focus lies between the best and
        # it). The largest jump would do too, but after a slope-following jump
        # that is many fine levels too far -- each one a slow open-loop move.
        cz = phases["coarse"][0]
        side = [abs(zc - z_best) for zc in cz if (zc - z_best) * d < 0]
        bracket = min(side) if side else (max(gaps[-2:]) if len(gaps) > 1 else gaps[-1])

        # ---- 2. FINE ---------------------------------------------------------
        # Start on the approach side of the bracket, a bit beyond it, and reach
        # that start from further back so the first fine levels are already
        # travelling in d (a reversal's first steps are the unreliable ones).
        # The walk is bounded by what the camera sees (the metric rising again),
        # NOT by a counted distance: after the coarse walk the counter and the
        # true Z have drifted apart (that is the hysteresis), so the bracket in
        # counter units is only a hint. If the walk starts already PAST focus
        # (it only ever gets worse), back off further and walk again.
        start = clampz(z_best - d * (bracket + 2 * fine))
        fz, fm = phases["fine"]
        best_f = None
        for attempt in range(3):
            fz.clear(); fm.clear()
            if (start - pos["z"]) * d < 0:             # a reverse move: overshoot it
                go(clampz(start - d * margin)); pos["z"] = clampz(start - d * margin)
            best_f, i_best, n_worse = None, 0, 0
            z = start
            while True:
                mf = measure(z, "fine")
                if np.isfinite(mf) and (best_f is None or score(mf) < score(best_f[1])):
                    best_f, i_best, n_worse = (z, mf), len(fz) - 1, 0
                elif best_f is not None and worse(mf, best_f[1]):
                    n_worse += 1
                if n_worse >= n_rise:
                    break
                zn = clampz(z + d * fine)
                if zn == z or abs(zn - start) > af.max_travel_v:
                    break
                z = zn
            if best_f is not None and i_best == 0 and n_worse >= n_rise:
                start = clampz(start - d * (2 * bracket + margin))   # focus is behind
                continue
            break
        if best_f is None:
            raise RuntimeError("the spot was lost during the fine walk")
        zs = np.asarray(fz, dtype=float); ms = np.asarray(fm, dtype=float)
        ok = np.isfinite(ms)
        best = V.best_focus_from_sweep(zs[ok], ms[ok], maximise, af.fit_curve)
        m_goal = best_f[1]
        publish(best)

        # ---- 3. PARK: by the image ------------------------------------------
        note = ""
        parked = None
        for attempt in range(1, 4):
            back = clampz(best - d * margin * attempt)
            go(back); pos["z"] = back
            z = back
            best_p, n_worse = None, 0
            walk_limit = margin * attempt + 2 * bracket + af.max_travel_v / 4
            while True:
                mp = measure(z, "park")
                if np.isfinite(mp) and not worse(mp, m_goal, af.park_tolerance):
                    parked = z
                    break
                if np.isfinite(mp) and (best_p is None or score(mp) < score(best_p)):
                    best_p, n_worse = mp, 0
                elif best_p is not None and worse(mp, best_p):
                    n_worse += 1
                if n_worse >= n_rise:
                    break                              # walked past focus: try again
                zn = clampz(z + d * fine)
                if zn == z or abs(zn - back) > walk_limit + 1e-9:
                    break
                z = zn
            if parked is not None:
                note = (f"parked by the image (attempt {attempt}), "
                        f"{parked - best:+.3f} from the fine walk's best by the counter")
                break
        if parked is None:
            parked = clampz(best)
            go(clampz(parked - d * margin)); go(parked)
            note = "WARNING: never back within park_tolerance -- parked by the step counter"
            self._emit("warn", "autofocus: the image never got back within park_tolerance "
                               "of the best focus; parked by the step counter")
        pos["z"] = parked
        # a requested offset from focus is a deliberate move away: by the counter
        if af.offset_from_found_v:
            target = clampz(parked + af.offset_from_found_v)
            if (target - parked) * d < 0:
                go(clampz(target - d * margin))
            go(target)
            parked = target
        publish(best)
        return float(best), float(parked), note

    def _focus_metric(self, gray) -> float:
        """The focus score of one frame; NaN when the spot_area metric sees no spot.

        spot_area = the area of the LASER SPOT only, found exactly like the
        per-frame spot check: in the search region around the calibrated
        position, with the min/max area, edge and symmetry rules. Until
        2026-09-14 it counted every thresholded pixel in the whole frame -- on
        the 63x rig that was ~136 000 px of bright illumination against an
        885 px spot, so the sweep focused on the background (Lukáš noticed).
        """
        af, sp = self.cfg.autofocus, self.cfg.spot
        if af.mechanism == "spot_area":
            if not sp.ref_set:
                raise RuntimeError("the spot_area focus metric needs a calibrated spot "
                                   "(Spot tab -> Calibrate spot)")
            det = V.find_spot(gray, sp.thr_lower, sp.thr_upper, sp.bright_spot,
                              sp.lookup_region_px, (sp.ref_x, sp.ref_y), sp.min_area_px,
                              sp.max_area_px, sp.reject_border, sp.search_shape,
                              sp.lookup_region_y_px, symmetric=bool(sp.symmetric))
            return float(det.area) if det.found else float("nan")
        roi = self._safety_roi(self._status) if af.focus_from_safety_area else None
        return float(V.focus_metric(gray, af.mechanism, roi,
                                    (sp.thr_lower, sp.thr_upper)))

    def _safety_roi(self, st) -> tuple | None:
        """A box around the template (the 'safety area') for focus scoring."""
        if self._driver_xy is None or self.reference is None:
            return None
        s = self.cfg.pattern.safety_area_px
        tx, ty = self._driver_xy               # the pattern in view, not an off-screen anchor
        return (int(tx - s), int(ty - s), int(2 * s), int(2 * s))

    # ------------------------------------------------------------------ #
    # reference (template) capture / load / save
    # ------------------------------------------------------------------ #
    def capture_reference(self, roi: tuple, array_center_px: tuple | None = None) -> str:
        """Grab the template patch from the last frame and pin the scan array.

        ``roi`` = (cx, cy, w, h) in pixels marks the template.  ``array_center_px``
        is where the scanning-array centre should sit (defaults to the current
        spot, so the centre point starts on the spot = 'lock image to pattern').
        """
        with self._lock:
            frame = None if self._last_frame is None else self._last_frame.copy()
        spot_xy = self.spot_position()
        if frame is None:
            raise RuntimeError("no frame yet")
        cx, cy, w, h = roi
        x0 = int(max(0, cx - w / 2)); y0 = int(max(0, cy - h / 2))
        x1 = int(min(frame.shape[1], cx + w / 2)); y1 = int(min(frame.shape[0], cy + h / 2))
        tpl = frame[y0:y1, x0:x1].copy()
        tpl_center = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        if array_center_px is None:
            array_center_px = spot_xy if spot_xy is not None else tpl_center
        offset = (array_center_px[0] - tpl_center[0], array_center_px[1] - tpl_center[1])
        self.reference = Reference(template=tpl, array_center_offset_px=offset,
                                   meta=self._reference_meta())
        self._last_template_xy = tpl_center
        self._driver, self._driver_xy = 0, tpl_center   # a new main template: no backups
        self._emit("info", f"reference captured: {self.reference.describe()}")
        return self.reference.describe()

    def _reference_meta(self) -> dict:
        sc, im = self.cfg.scanning, self.cfg.image
        return {
            # the WHOLE scanning definition, so a loaded pattern restores every
            # array parameter (Lukáš, 2026-09-14); the flat keys below stay for
            # pattern files written before this
            "scanning": asdict(sc),
            "points_x": sc.points_x, "points_y": sc.points_y,
            "dx_um": sc.dx_um, "dy_um": sc.dy_um, "angle_deg": sc.angle_deg,
            "selected_index_x": sc.selected_index_x,
            "selected_index_y": sc.selected_index_y,
            "pixel_size_x_um": im.pixel_size_x_um,
            "pixel_size_y_um": im.pixel_size_y_um,
            "objective_name": im.objective_name,
        }

    def save_pattern(self, path: str) -> None:
        if self.reference is None:
            raise RuntimeError("no reference to save")
        self.reference.meta = self._reference_meta()
        save_template(path, self.reference)
        self._emit("info", f"pattern saved -> {path}")

    def load_pattern(self, path: str, load_arrays: bool = True) -> str:
        ref = load_template(path)
        self.reference = ref
        self._last_template_xy = None
        self._driver, self._driver_xy = 0, None
        if load_arrays and ref.meta:
            m, sc, im = ref.meta, self.cfg.scanning, self.cfg.image
            saved = dict(m.get("scanning") or {})
            for k in ("points_x", "points_y", "dx_um", "dy_um", "angle_deg",
                      "selected_index_x", "selected_index_y"):
                saved.setdefault(k, m.get(k, getattr(sc, k)))    # older files: flat keys
            for k, v in saved.items():
                cur = getattr(sc, k, None)
                if cur is None and not hasattr(sc, k):
                    continue                                      # unknown key: ignore
                try:                                              # keep each field's type
                    setattr(sc, k, type(cur)(v) if not isinstance(cur, bool) else bool(v))
                except (TypeError, ValueError):
                    pass
            self._avg_buf.clear()
            if "pixel_size_x_um" in m:
                im.pixel_size_x_um = float(m["pixel_size_x_um"])
                im.pixel_size_y_um = float(m.get("pixel_size_y_um", im.pixel_size_x_um))
            if "objective_name" in m:
                im.objective_name = m["objective_name"]
        self._emit("info", f"pattern loaded <- {path} ({ref.describe()})")
        return ref.describe()

    # ------------------------------------------------------------------ #
    # snapshot
    # ------------------------------------------------------------------ #
    def snapshot(self, path: str | None = None) -> str:
        with self._lock:
            frame = None if self._last_frame is None else self._last_frame.copy()
        if frame is None:
            raise RuntimeError("no frame yet")
        if path is None:
            folder = self.cfg.image.save_path
            os.makedirs(folder, exist_ok=True)
            i = 0
            while True:
                path = os.path.join(folder, f"snapshot_{i:04d}.png")
                if not os.path.exists(path):
                    break
                i += 1
        cv2.imwrite(path, frame)
        self._emit("info", f"snapshot -> {path}")
        return path

    def get_frame_png(self) -> bytes:
        with self._lock:
            frame = None if self._last_frame is None else self._last_frame
            if frame is None:
                return b""
            ok, buf = cv2.imencode(".png", frame)
        return buf.tobytes() if ok else b""

    def latest_frame(self) -> np.ndarray | None:
        with self._lock:
            return None if self._last_frame is None else self._last_frame.copy()

    # ------------------------------------------------------------------ #
    # immediate control verbs (safe from the command thread)
    # ------------------------------------------------------------------ #
    def set_tracking(self, on: bool) -> bool:
        self._tracking_on = bool(on)
        if not on:
            self._last_template_xy = None
            self._driver_xy = None
        return bool(on)

    def set_stabilize(self, on: bool) -> bool:
        self._stabilize_on = bool(on)
        self._avg_buf.clear()
        self._settled_for = None
        return bool(on)

    def set_continuous_focus(self, on: bool) -> bool:
        self._cf_on = bool(on)
        self._cf_last_metric = None
        return bool(on)

    def set_selected_index(self, ix: int | None = None, iy: int | None = None) -> tuple:
        """Select the scan-array point the stabiliser brings under the laser.

        Either index may be None = keep the current one, so a scan can sweep X
        and Y as two independent axes. Clamped to the array (0..points-1).
        """
        sc = self.cfg.scanning
        if ix is not None:
            sc.selected_index_x = min(max(int(round(float(ix))), 0), max(0, sc.points_x - 1))
        if iy is not None:
            sc.selected_index_y = min(max(int(round(float(iy))), 0), max(0, sc.points_y - 1))
        self._avg_buf.clear()   # restart averaging on a new target
        self._settled_for = None
        return (sc.selected_index_x, sc.selected_index_y)

    def move_xy(self, x_um: float, y_um: float) -> list:
        x, y = self._clamp_xy(float(x_um), float(y_um))
        self.xy.move_xy(x, y)
        return [x, y]

    def read_xy(self) -> list:
        return list(self.xy.read_xy())

    def xy_step_unit(self) -> str:
        """"steps" when the stage counts steps (KIM) and hardware.xy_unit asks for
        them; otherwise "um"."""
        has_steps = callable(getattr(self.xy, "move_to_steps", None))
        return "steps" if has_steps and self.cfg.hardware.xy_unit == "steps" else "um"

    def step_xy(self, dx: float, dy: float) -> list:
        """Jog XY by (dx, dy) in ``xy_step_unit()``; returns the new target.

        Same rule as step_z: from the last COMMANDED jog target while it is
        fresh (an open-loop stage is still walking, and quick clicks must add
        up), from the live reading after that.
        """
        unit = self.xy_step_unit()
        fresh = (self._xy_target is not None and self._xy_target_unit == unit
                 and time.monotonic() - self._xy_target_t < self.Z_STEP_FRESH_S)
        if unit == "steps":
            bx, by = self._xy_target if fresh else self.xy.read_steps()
            # kim clamps to its own limits and tells us what it accepted
            target = list(self.xy.move_to_steps(int(round(bx + dx)), int(round(by + dy))))
        elif callable(getattr(self.xy, "steps_for_um", None)):
            # A step-counting stage (KIM) asked for in um: convert HERE, with the
            # step size of the direction this jog travels, and command steps --
            # an absolute um target would be converted by the mean of the two
            # directions, which is ~20 % out on this rig's X.
            bsx, bsy = self._xy_steps_target if fresh else self.xy.read_steps()
            steps = self.xy.move_to_steps(bsx + self.xy.steps_for_um(0, dx),
                                          bsy + self.xy.steps_for_um(1, dy))
            self._xy_steps_target = tuple(steps)
            target = [steps[a] * self.xy.um_per_step(a) for a in range(2)]
        else:
            bx, by = self._xy_target if fresh else self.xy.read_xy()
            target = self.move_xy(bx + float(dx), by + float(dy))
        self._xy_target, self._xy_target_t = tuple(target), time.monotonic()
        self._xy_target_unit = unit       # a target in the other unit is not "fresh"
        self._emit("info", f"XY jog ({float(dx):+g}, {float(dy):+g}) {unit} "
                           f"-> ({target[0]:g}, {target[1]:g}) {unit}")
        return target

    def stage_state(self) -> tuple:
        """(ok, why) for the motion hardware, from its cached state only.

        Stages without an ``available()`` (the simulator, a local driver) are
        always ok. XY and Z are asked separately: on the lab rig they share
        one kim link, so they agree; on the piezo rig they are two services.
        """
        for dev in (self.xy, self.z if self.cfg.hardware.use_z else None):
            fn = getattr(dev, "available", None)
            if callable(fn):
                try:
                    ok, why = fn()
                except Exception as exc:          # never let a probe kill the frame
                    ok, why = False, f"stage state unknown: {exc}"
                if not ok:
                    return False, why
        return True, ""

    def reconnect_stage(self) -> dict:
        """Rebuild the connection to the stage service(s) and try it once."""
        seen, results = set(), []
        for dev in (self.xy, self.z):
            fn = getattr(dev, "reconnect", None)
            link = getattr(dev, "link", dev)
            if not callable(fn) or id(link) in seen:
                continue
            seen.add(id(link))
            results.append(fn())
        if not results:
            return {"stage_ok": True, "stage_error": "", "note": "nothing to reconnect"}
        ok = all(r[0] for r in results)
        why = "; ".join(r[1] for r in results if r[1])
        self._xy_target = None              # positions from before may be stale
        self._emit("info" if ok else "warn",
                   "stage reconnected" if ok else f"stage reconnect failed: {why}")
        return {"stage_ok": ok, "stage_error": why}

    def datum_xy(self) -> None:
        """Datum: make the current XY position the stage's 0 (KIM step counters).

        Every stored stage coordinate from before (kim's leash box, positions
        you wrote down) now refers to the old origin -- hence the warning.
        """
        fn = getattr(self.xy, "zero_counter", None)
        if not callable(fn):
            raise RuntimeError("this XY stage has no datum (its zero is fixed by the hardware)")
        if self._stabilize_on:
            raise RuntimeError("switch the stabiliser off before setting the datum")
        fn()
        self._xy_target = None
        self._emit("warn", "XY datum set: step counters are 0 here; "
                           "older stage coordinates now refer to the old origin")

    def set_z(self, volts: float) -> float:
        v = self._clamp_z(float(volts))
        self.z.set_z(v)
        self._z_target, self._z_target_t = v, time.monotonic()
        return v

    # A step is taken from the last COMMANDED target while that command is
    # recent: an open-loop Z (KIM) walks, so reading Z right after a click would
    # return a point still on the way and quick repeated clicks would lose steps.
    # After Z_STEP_FRESH_S the live reading wins again, so a move made by another
    # client (e.g. the kim GUI) is never undone by a stale target.
    Z_STEP_FRESH_S = 3.0

    def step_z(self, delta: float) -> float:
        """Move focus by ``delta`` (in the Z unit) from where it is heading; returns
        the new, clamped target."""
        fresh = (self._z_target is not None
                 and time.monotonic() - self._z_target_t < self.Z_STEP_FRESH_S)
        base = self._z_target if fresh else float(self.z.read_z())
        target = self.set_z(base + float(delta))
        self._emit("info", f"focus step {float(delta):+g} {self.z_unit()} -> {target:.3f}")
        return target

    def read_z(self) -> float:
        return float(self.z.read_z())

    def read_position_px(self) -> dict:
        s = self._status
        return {
            "spot": [s.spot_x, s.spot_y],
            "template": [s.template_x, s.template_y],
            "selected_point": [s.selected_point_x, s.selected_point_y],
        }

    def image_context(self) -> dict:
        """The image geometry an image-frame calibration is valid for.

        Must be built by the same rules as kim's ``calibration.image_context``
        (objective, rotation, symmetry, clip, processed frame size): kim refuses
        a move whose context differs from the one it calibrated under.
        """
        img = self.cfg.image
        ctx = {
            "objective": img.objective_name,
            "rotation_deg": float(img.rotation_deg),
            "symmetry": str(img.symmetry),
            "clip": [bool(img.clip_enabled), int(img.clip_left), int(img.clip_top),
                     int(img.clip_right), int(img.clip_bottom)],
        }
        with self._lock:
            if self._last_frame is not None:
                ctx["frame"] = [int(self._last_frame.shape[0]), int(self._last_frame.shape[1])]
        return ctx

    def _warn_limited(self, key: str, msg: str, every_s: float = 5.0) -> None:
        """An event at most every ``every_s`` per key: a loop that fails every
        window must not flood the log."""
        now = time.monotonic()
        last = getattr(self, "_warned", {})
        if now - last.get(key, -1e9) >= every_s:
            last[key] = now
            self._warned = last
            self._emit("warn", msg)

    def set_position_px(self, x: float, y: float) -> list:
        """Move the stage so the tracked template sits at pixel (x, y)."""
        if self._last_template_xy is None:
            raise RuntimeError("no template tracked yet")
        dx_px = x - self._last_template_xy[0]
        dy_px = y - self._last_template_xy[1]
        if hasattr(self.xy, "move_image_px"):       # KIM rig: move the image directly
            return self.xy.move_image_px(dx_px, dy_px, context=self.image_context())
        dux, duy = V.pixels_to_um(dx_px, dy_px,
                                  self.cfg.image.pixel_size_x_um,
                                  self.cfg.image.pixel_size_y_um)
        cx, cy = self.xy.read_xy()
        return self.move_xy(cx + dux, cy + duy)

    def click_to_go(self, px: float, py: float) -> list:
        """Move the sample so the clicked image point goes under the laser spot."""
        spot = self.spot_position()
        if spot is None:
            raise RuntimeError("no spot to go to: calibrate the spot position first (Spot tab)")
        if hasattr(self.xy, "move_image_px"):       # KIM rig: move the image directly
            return self.xy.move_image_px(spot[0] - px, spot[1] - py,
                                         context=self.image_context())
        dux, duy = V.pixels_to_um(spot[0] - px, spot[1] - py,
                                  self.cfg.image.pixel_size_x_um,
                                  self.cfg.image.pixel_size_y_um)
        cx, cy = self.xy.read_xy()
        return self.move_xy(cx + dux, cy + duy)

    def set_objective(self, name: str) -> dict:
        self._apply_objective(name)
        return {"objective": self.cfg.image.objective_name,
                "pixel_size_x_um": self.cfg.image.pixel_size_x_um,
                "pixel_size_y_um": self.cfg.image.pixel_size_y_um}

    def set_scan_area(self, cx: float, cy: float, w: float, h: float,
                      angle: float | None = None) -> dict:
        """Define the scanning grid from a drawn rectangle (px), LabVIEW-style.

        The rectangle's local size sets the total span; the per-point pitch is
        span/(points-1) for the current points_x/points_y.  ``angle`` (deg) tilts
        the whole array.  If a template is being tracked, the array centre is
        re-pinned to the rectangle centre (the 'Template-array distance').  Ports
        'Define scanning area' + 'Accept scanning ROI'.
        """
        sc = self.cfg.scanning
        px_x = self.cfg.image.pixel_size_x_um
        px_y = self.cfg.image.pixel_size_y_um
        size_x = abs(w) * px_x
        size_y = abs(h) * px_y
        if sc.points_x > 1:
            sc.dx_um = size_x / (sc.points_x - 1)
        if sc.points_y > 1:
            sc.dy_um = size_y / (sc.points_y - 1)
        if angle is not None:
            sc.angle_deg = float(angle)
        repinned = False
        if self.reference is not None and self._last_template_xy is not None:
            self.reference.array_center_offset_px = (
                cx - self._last_template_xy[0], cy - self._last_template_xy[1])
            repinned = True
        self._avg_buf.clear()
        self._emit("info", f"scan area {size_x:.2f}x{size_y:.2f} um @ "
                           f"{sc.angle_deg:.1f} deg -> pitch "
                           f"({sc.dx_um:.3f}, {sc.dy_um:.3f}) um"
                           + ("" if repinned else " (no template pinned yet)"))
        return {"size_x_um": size_x, "size_y_um": size_y, "dx_um": sc.dx_um,
                "dy_um": sc.dy_um, "angle_deg": sc.angle_deg, "repinned": repinned}

    def set_scan_size_um(self, size_x_um: float, size_y_um: float) -> dict:
        """Set the array by its total SIZE (um); the pitch adapts to the points.

        The alternative to entering a pitch: give the span and let dx/dy =
        span/(points-1) fall out.
        """
        sc = self.cfg.scanning
        if sc.points_x > 1:
            sc.dx_um = abs(size_x_um) / (sc.points_x - 1)
        if sc.points_y > 1:
            sc.dy_um = abs(size_y_um) / (sc.points_y - 1)
        self._avg_buf.clear()
        self._emit("info", f"array size {size_x_um:.2f}x{size_y_um:.2f} um -> "
                           f"pitch ({sc.dx_um:.3f}, {sc.dy_um:.3f}) um")
        return {"dx_um": sc.dx_um, "dy_um": sc.dy_um}

    def get_scan_rect(self) -> dict | None:
        """Reconstruct the scan-area rectangle from the known ROI (for 'Recall').

        Returns {cx, cy, w, h, angle} in pixels/deg from the current template
        position + stored array offset + scanning pitch, or None if there is no
        template/reference yet.
        """
        if self.reference is None or self._last_template_xy is None:
            return None
        sc = self.cfg.scanning
        px_x = self.cfg.image.pixel_size_x_um
        px_y = self.cfg.image.pixel_size_y_um
        ox, oy = self.reference.array_center_offset_px
        cx = self._last_template_xy[0] + ox
        cy = self._last_template_xy[1] + oy
        w = (sc.points_x - 1) * sc.dx_um / px_x if sc.points_x > 1 else 0.0
        h = (sc.points_y - 1) * sc.dy_um / px_y if sc.points_y > 1 else 0.0
        return {"cx": cx, "cy": cy, "w": w, "h": h, "angle": sc.angle_deg}

    def get_af_curve(self) -> dict:
        """The last autofocus sweep: {z, metric, best, maximise}."""
        return dict(self._af_curve)

    def set_accuracy_logging(self, on: bool) -> bool:
        """Start/stop logging the residual spot->point distance (um) per frame."""
        self._acc_on = bool(on)
        if on:
            self._acc_log.clear()
        return self._acc_on

    def get_accuracy(self) -> dict:
        """The alignment-accuracy log as {dx: [...], dy: [...]} in um."""
        return {"dx": [p[0] for p in self._acc_log],
                "dy": [p[1] for p in self._acc_log]}

    def list_objectives(self) -> list:
        return list(self._objectives.keys())

    # -- live camera parameters (GenICam features via the backend) --------- #
    def camera_features(self) -> list:
        """Descriptors for the camera's controllable parameters (for the GUI)."""
        try:
            return self.backend.features()
        except Exception as exc:
            self._emit("warn", f"camera features unavailable: {exc}")
            return []

    def get_camera_feature(self, name: str):
        return self.backend.get_feature(name)

    def set_camera_feature(self, name: str, value):
        """Set one camera parameter; returns its read-back value."""
        self.backend.set_feature(name, value)
        try:
            v = self.backend.get_feature(name)
        except Exception:
            v = value
        if name == "ExposureTime":
            try:
                self.cfg.camera.exposure_us = float(v)   # re-applied on the next open
            except (TypeError, ValueError):
                pass
        self._emit("info", f"camera {name} = {v}")
        return v

    def _apply_objective(self, name: str, quiet: bool = False) -> None:
        obj = OBJ.resolve(self._objectives, name)
        if obj is None:
            if not quiet:
                self._emit("warn", f"unknown objective {name!r}; pixel size unchanged")
            return
        self.cfg.image.objective_name = name
        self.cfg.image.pixel_size_x_um = obj.pixel_size_x_um
        self.cfg.image.pixel_size_y_um = obj.pixel_size_y_um
        with self._lock:
            self._status.objective_name = name
            self._status.pixel_size_x = obj.pixel_size_x_um
            self._status.pixel_size_y = obj.pixel_size_y_um
        if not quiet:
            self._emit("info", f"objective {name}: {obj.pixel_size_x_um:.4f} um/px")

    # ------------------------------------------------------------------ #
    # clamps to the safety envelope
    # ------------------------------------------------------------------ #
    # A backend that sets ``owns_limits`` (the KIM rig) reports its own LIVE
    # travel range: kim's positions are centred on the Datum and go negative,
    # and its leash can change at any time, so the camera's fixed cfg.limits
    # envelope (0..130 um / 0..75 V, written for the piezo rig) would be wrong.
    def xy_limits(self) -> tuple | None:
        """((x_min, x_max), (y_min, y_max)) in um, or None if unknown right now."""
        if getattr(self.xy, "owns_limits", False):
            try:
                return self.xy.xy_range()
            except Exception:
                return None   # stage unreachable: its own service still clamps
        lim = self.cfg.limits
        return ((lim.motor_x_min, lim.motor_x_max), (lim.motor_y_min, lim.motor_y_max))

    def z_limits(self) -> tuple | None:
        """(z_min, z_max) in the Z unit, or None if unknown right now."""
        if getattr(self.z, "owns_limits", False):
            try:
                return self.z.z_range()
            except Exception:
                return None
        return (self.cfg.limits.z_min_v, self.cfg.limits.z_max_v)

    def z_unit(self) -> str:
        """The unit Z is driven in: "V" (piezo rig) or "um" (KIM rig)."""
        fn = getattr(self.z, "z_unit", None)
        return fn() if callable(fn) else "V"

    def _clamp_xy(self, x: float, y: float) -> tuple:
        lims = self.xy_limits()
        if not self.cfg.limits.enforce or lims is None:
            return (x, y)
        (x0, x1), (y0, y1) = lims
        return (min(max(x, x0), x1), min(max(y, y0), y1))

    def _clamp_z(self, v: float) -> float:
        lims = self.z_limits()
        if not self.cfg.limits.enforce or lims is None:
            return v
        return min(max(v, lims[0]), lims[1])

    # ------------------------------------------------------------------ #
    # blueprint surface: status / config / events
    # ------------------------------------------------------------------ #
    def status(self) -> CameraStatus:
        with self._lock:
            return self._status

    def get_config(self) -> Config:
        return self.cfg

    # ------------------------------------------------------------------ #
    # spot position + persistence
    # ------------------------------------------------------------------ #
    def spot_position(self) -> tuple | None:
        """THE spot position (the calibrated one), or None before calibration.

        Read straight from the config rather than from the last processed frame,
        so a command right after calibrate_spot() already sees the new value (a
        per-frame copy lagged one frame behind and broke capture_reference).
        """
        sp = self.cfg.spot
        return (float(sp.ref_x), float(sp.ref_y)) if sp.ref_set else None

    def calibrate_spot(self, frames: int = 20, timeout_s: float = 2.5) -> dict:
        """Find the spot with the current threshold, averaged over ``frames`` new
        frames, and store it as THE spot position used from now on.

        The spread of the centroid (``jitter``) is stored with it: the noise
        floor of this calibration. Blocks for about frames / fps (capped by
        ``timeout_s``, which must stay below the client's REQ timeout).
        """
        frames = max(2, int(frames))
        self._measuring_spot = True     # whole-frame search: the beam may have moved
        xs, ys, areas = [], [], []
        try:
            # the frame in flight when the flag went up still used the old
            # search box, so only frames numbered start+2 onwards count
            start = last = self._status.frame_number
            t_end = time.monotonic() + timeout_s
            while len(xs) < frames and time.monotonic() < t_end:
                s = self._status
                if s.frame_number != last:
                    last = s.frame_number
                    if s.frame_number >= start + 2 and s.spot_found:
                        xs.append(s.spot_live_x); ys.append(s.spot_live_y)
                        areas.append(s.spot_area)
                time.sleep(0.005)
        finally:
            self._measuring_spot = False
        if len(xs) < 2:
            raise RuntimeError("no spot detected while calibrating: adjust the threshold "
                               "(Spot tab) so exactly the laser spot is selected")
        sp = self.cfg.spot
        sp.ref_x, sp.ref_y = float(np.mean(xs)), float(np.mean(ys))
        sp.ref_area = float(np.mean(areas))
        sp.ref_jitter_px = float(np.hypot(np.std(xs), np.std(ys)))
        sp.ref_set = True
        self._emit("info", f"spot calibrated: ({sp.ref_x:.2f}, {sp.ref_y:.2f}) px "
                           f"+/- {sp.ref_jitter_px:.2f} px, area {sp.ref_area:.0f} px2, "
                           f"{len(xs)} frames")
        return {"x": sp.ref_x, "y": sp.ref_y, "area": sp.ref_area,
                "jitter_px": sp.ref_jitter_px, "frames": len(xs)}

    def set_spot_position(self, x: float, y: float) -> dict:
        """Enter THE spot position by hand (px, processed-frame coordinates).

        For when the spot cannot be thresholded (laser off, dim against the
        illumination) or you simply know where it is. Nothing is measured, so
        the jitter is recorded as 0 and the calibrated area as unknown (0).
        """
        x, y = float(x), float(y)
        with self._lock:
            shape = None if self._last_frame is None else self._last_frame.shape
        if shape is not None and not (0 <= x < shape[1] and 0 <= y < shape[0]):
            raise ValueError(f"({x:.1f}, {y:.1f}) is outside the {shape[1]}x{shape[0]} frame")
        sp = self.cfg.spot
        sp.ref_x, sp.ref_y = x, y
        sp.ref_area, sp.ref_jitter_px, sp.ref_set = 0.0, 0.0, True
        self._emit("info", f"spot position entered by hand: ({x:.2f}, {y:.2f}) px")
        return {"x": x, "y": y, "area": 0.0, "jitter_px": 0.0, "frames": 0}

    def clear_spot_position(self) -> None:
        """Forget the spot position: click-to-go and the stabiliser stop using one."""
        self.cfg.spot.ref_set = False
        self._emit("warn", "spot position cleared (not calibrated)")

    def config_file(self) -> str:
        """Where save_config() writes by default, and run_service.py loads from."""
        from pathlib import Path
        return str(Path(__file__).resolve().parents[2] / DEFAULT_CONFIG_NAME)

    def save_config(self, path: str | None = None) -> str:
        """Write the whole config to an INI (default: camera-control/camera.ini)."""
        from .config import save_config
        path = path or self.config_file()
        save_config(self.cfg, path)
        self._emit("info", f"config saved -> {path}")
        return path

    def set_config(self, config: dict) -> None:
        """Apply a nested {group: {field: value}} dict in place, then re-derive.

        Mirrors the service's set_config so a LOCAL GUI (handed the brain) and a
        REMOTE GUI (handed the client) have the identical surface.
        """
        groups = {
            "camera": self.cfg.camera, "image": self.cfg.image,
            "spot": self.cfg.spot, "pattern": self.cfg.pattern,
            "autofocus": self.cfg.autofocus, "scanning": self.cfg.scanning,
            "stabilizer": self.cfg.stabilizer, "limits": self.cfg.limits,
            "hardware": self.cfg.hardware, "ui": self.cfg.ui,
        }
        for gname, values in (config or {}).items():
            obj = groups.get(gname)
            if obj is None or not isinstance(values, dict):
                continue
            for k, v in values.items():
                if hasattr(obj, k):
                    setattr(obj, k, v)
        self.apply_config()

    def apply_config(self) -> None:
        """Re-derive anything cached from cfg after an in-place edit."""
        self._objectives = OBJ.load_objectives(self.cfg.image.objectives_file)
        # keep pixel size consistent with the (possibly changed) objective
        self._apply_objective(self.cfg.image.objective_name, quiet=True)
        self._avg_buf = deque(maxlen=max(1, self.cfg.stabilizer.images_to_average))
        self._temporal = deque(maxlen=max(1, self.cfg.camera.running_avg_frames))

    def _emit(self, level: str, msg: str) -> None:
        try:
            self._on_event(level, msg)
        except Exception:
            pass


def status_to_dict(s: CameraStatus) -> dict:
    return asdict(s)
