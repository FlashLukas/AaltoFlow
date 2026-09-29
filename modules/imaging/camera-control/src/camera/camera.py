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
import json
import math
import os
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

import cv2
import numpy as np

from . import objectives as OBJ
from . import vision as V
from .config import CALIB_MODES, LOCATE_MODES, Config
from .stream import StreamRecorder
from .template_io import BackupPattern, Reference, load_template, save_template

# Saved by save_config(), loaded by scripts/run_service.py when present, so a
# measured spot position and tuned thresholds survive a restart.
DEFAULT_CONFIG_NAME = "camera.ini"


# The autofocus mechanisms measured on the spot around its centre (not the
# whole image): the threshold-free sizes and the peak.
SIZE_FOCUS = ("spot_d4sigma", "spot_relative", "spot_encircled", "spot_gauss", "spot_peak")


@contextmanager
def _nothing():
    """An empty context (the "no exposure switch" branch of calibrate_spot)."""
    yield False


class _AutofocusKilled(Exception):
    """Raised inside a sweep when Kill AF is pressed (or the engine stops)."""


class AutofocusFailed(RuntimeError):
    """A run that must not be trusted, with a SHORT reason for af_error.

    Any exception fails a run (af_error = its type name, the old behaviour,
    which scan waits and tests rely on). This one carries a readable state
    instead -- e.g. "park failed: never within park_tolerance" -- because
    "RuntimeError" alone does not tell an operator reading the scan log what
    went wrong. The full detail goes into the error event.
    """

    def __init__(self, state: str, detail: str = ""):
        super().__init__(f"{state}: {detail}" if detail else state)
        self.state = state


# --------------------------------------------------------------------------- #
# Status snapshot (a plain dataclass; asdict -> the wire/GUI)
# --------------------------------------------------------------------------- #
#: The fly-scan stream: where the laser is on the sample, in um from the MAIN
#: template (= spot_from_template_x/y_um), one sample per processed frame.
STREAM_CHANNELS = ("laser_x", "laser_y")


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
    # The spot's SIZE without a fixed threshold (2026-09-28, vision.py "Spot
    # SIZE"), measured every frame in the search region. Information and
    # autofocus metrics only -- the position used for motion stays spot_x/y.
    spot_size_method: str = "relative"    # cfg.spot.size_method, echoed
    spot_size: float = float("nan")       # that method's number (px^2, or D4sigma px)
    spot_rel_area: float = float("nan")   # px^2 above rel_level x (peak - background)
    spot_d4sigma_px: float = float("nan") # 4 sqrt(sigma^2): ISO beam diameter, px
    spot_sigma2_px2: float = float("nan") # second moment, mean of x and y, px^2
    spot_centroid_x: float = float("nan") # intensity centroid (first moment), px
    spot_centroid_y: float = float("nan")
    spot_peak: float = 0.0                # brightest pixel above background, counts
    spot_saturated: bool = False          # a pixel of the spot at the camera's maximum
    # 2026-09-29: WHERE the size was measured (Spot.locate: the calibrated
    # position, or the spot found in the search region) -- information only,
    # motion keeps spot_x/y -- how far that is from the calibration, and in
    # words why nothing was measured (e.g. "the brightest light is 95 px from
    # the calibrated position ..."). "" = measured.
    spot_found_x: float = float("nan")
    spot_found_y: float = float("nan")
    spot_offset_px: float = float("nan")
    spot_size_why: str = ""
    # the box the size was integrated over (x0, y0, x1, y1; the second
    # moment's), for the overlay; (0, 0, 0, 0) = none
    spot_size_box: tuple = (0, 0, 0, 0)
    # ... and the new sizes: encircled-energy diameter (encircled_fraction,
    # D86 by default), a 2-D Gaussian fit's sigma^2 and its R^2, the spot's
    # 3x3-averaged peak above background (counts)
    spot_d86_px: float = float("nan")
    spot_gauss_sigma2_px2: float = float("nan")
    spot_gauss_r2: float = float("nan")
    spot_peak_avg: float = float("nan")
    # saturation as INFORMATION (Lukas: a saturated spot is still a spot):
    # the fraction of the spot's pixels at full scale, and the exposure factor
    # that would bring its peak to ~80 % of full scale (an estimate; NaN when
    # not saturated)
    spot_sat_fraction: float = 0.0
    spot_exposure_hint: float = float("nan")
    # the autofocus exposure (autofocus.exposure_us) is on the camera NOW
    af_exposure_active: bool = False
    # Bit depth of the frame the sizes above were measured on (2026-09-28): 8,
    # or 10/12 when the camera delivers its full depth (then spot_peak is in
    # those counts, 0..4095 for 12 bit). The display, the template matching
    # and the fixed-threshold spot_area stay 8-bit whatever this says.
    spot_bit_depth: int = 8
    # Why the frame is 8 bit when the camera could give more (2026-09-29), from
    # the backend's deep_note(): e.g. "the camera's PixelFormat is Mono8 ...".
    # "" = a deep frame came, or the backend has nothing to say (simulator).
    spot_bit_note: str = ""

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
    # PLACING THE LASER at a point on the sample given in those same template
    # coordinates (set_laser_target): the target, whether the placement loop is
    # still correcting, and whether the laser is there. `laser_settled` is
    # evaluated EVERY frame -- a finished placement AND the measured position
    # within stable_radius_um of the target -- so a frame from after the stage
    # has flown away can never say "settled" at the old target.
    laser_target_x_um: float = float("nan")
    laser_target_y_um: float = float("nan")
    laser_goto: bool = False
    laser_settled: bool = False
    # a fly scan is recording the laser position (stream): the stabiliser and
    # the placement loop stand down, or they would fight the flying stage
    streaming: bool = False
    # FAULT (2026-09-28): "" when fine, else why the camera cannot be trusted to
    # hold the sample -- today: the tracked pattern was LOST (out of image, spot
    # on the pattern, out of focus). LATCHED: it stays until the user sends
    # `clear_fault`, even if the pattern is found again, because whatever lost
    # it needs a human look. While it is set the stabiliser and the laser
    # placement hold the stage, and point_settled / laser_settled are False, so
    # a scan waiting on them does not measure; scan-core pauses on it.
    fault: str = ""
    # A failed hardware read this frame ("" when fine; the suite's convention):
    # the camera grab, or reading the stage / Z back.
    hw_error: str = ""

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
    # A warning about the chosen focus metric, "" when there is none. Today:
    # spot_area on a spot that is NOT saturated (its thresholded area is then
    # LARGEST at focus, but spot_area is minimised -- rig 2026-09-28).
    af_hint: str = ""
    # Z STEP CALIBRATION by the camera (calibrate_z_steps), numbered like the
    # autofocus: a scan waits for zcal_id == its number and not zcal_running,
    # then requires zcal_state "OK". The result: step-size ratio up/down, the
    # two fits' R^2, and the two sizes written to the Z stage (Z unit/step).
    zcal_id: int = 0
    zcal_running: bool = False
    zcal_state: str = "OK"
    zcal_ratio: float = float("nan")
    zcal_ratio_err: float = float("nan")
    zcal_r2_up: float = float("nan")
    zcal_r2_down: float = float("nan")
    zcal_up_um: float = float("nan")
    zcal_down_um: float = float("nan")
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
        # Laser placement (set_laser_target): the target in template um, the
        # loop running, and a placement finished since the last request. Brain
        # attributes, copied into each frame (gotcha #1).
        self._laser_target: tuple | None = None
        self._laser_goto = False
        self._laser_done = False
        # set_laser_target (request thread) vs _laser_step (engine thread)
        self._laser_lock = threading.Lock()
        # The fly-scan record of the laser position on the sample (stream.py).
        self.stream = StreamRecorder(STREAM_CHANNELS, delay_fn=self.stream_delays)

        # Objective table -> pixel size.
        self._objectives = OBJ.load_objectives(self.cfg.image.objectives_file)
        self._apply_objective(self.cfg.image.objective_name, quiet=True)
        self._last_frame: np.ndarray | None = None    # processed grayscale
        self._last_template_xy: tuple | None = None   # MAIN template position (anchor)
        self._driver = 0                               # which pattern drives (0 = main)
        self._driver_xy: tuple | None = None           # where the driver is in view
        # Bumped (under _lock) by every setter that resets the tracking; a frame
        # that was matching meanwhile must not write its stale anchor back.
        self._anchor_gen = 0
        # Pattern loss (see _update_loss). Brain attributes, copied into every
        # frame's snapshot (gotcha #1). _fault and _recovery are written under
        # _lock: the engine sets them, clear_fault (request thread) clears them.
        self._fault = ""
        self._lost_count = 0          # frames in a row without the pattern
        self._recovery: dict | None = None   # a running autofocus_on_loss attempt
        # when the last recovery autofocus started (engine thread only);
        # -inf = never, so the first loss may always try
        self._last_recovery_t = float("-inf")
        self._avg_buf: deque = deque(maxlen=max(1, self.cfg.stabilizer.images_to_average))
        self._temporal: deque = deque(maxlen=max(1, self.cfg.camera.running_avg_frames))
        self._temporal_deep: deque = deque(maxlen=max(1, self.cfg.camera.running_avg_frames))
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
        # spot_area needs a SATURATED spot (see _note_area_saturation): the hint
        # text, and per run [levels scored, levels with a saturated spot]
        self._af_hint = ""
        # the backend's "why no deep frame" (deep_note), and the last one SAID:
        # one info event per service start and per change, not one per frame
        self._deep_note = ""
        self._deep_note_said = ""
        # saturation is SAID once per episode (a run of saturated frames),
        # not per frame: Lukas saw the log repeat it (2026-09-29)
        self._sat_episode = False
        self._unsat_run = 0
        # AUTOFOCUS EXPOSURE (autofocus.exposure_us): the working exposure to
        # put back (None = nothing switched), and whether the AF one is on.
        # _expo_hold: the engine keeps the image loops (pattern tracking,
        # stabiliser, laser placement, continuous focus) standing down while
        # the exposure is switched, and for this many frames after (frames
        # already exposed with the wrong value -- see _exposure_for).
        self._af_expo_saved: float | None = None
        self._af_expo_active = False
        self._expo_hold_frames = 0
        # The "spot is N px from its calibration" warning (_measure_size):
        # consecutive far frames so far, and how many more frames to ignore
        # after ANY exposure change (_note_exposure_change).
        self._offset_run = 0
        self._offset_quiet = 0
        self._af_area_sat = [0, 0]
        # Z step calibration (calibrate_z_steps): same pattern as the autofocus
        # state above -- brain attributes, copied into every frame (gotcha #1)
        self._zcal_id = 0
        self._zcal_busy = False
        self._zcal_state = "OK"
        self._zcal_request: dict | None = None
        self._zcal_result: dict = {}
        self._zcal_curve: dict = {}
        # The full-depth copy of the frame _grab_gray returned last, as
        # (that 8-bit frame, deep frame, bits), or None. Engine thread only.
        self._deep: tuple | None = None
        # Last autofocus sweep (for the GUI's focus-vs-Z plot).
        self._af_curve = {"z": [], "metric": [], "best": 0.0, "maximise": True}
        # Alignment-accuracy log: residual (dx_um, dy_um) per frame when enabled.
        self._acc_on = False
        self._acc_log: deque = deque(maxlen=256)
        self._measuring_spot = False  # calibrate_spot() is running
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
        # The camera's ExposureTime as last read from it (None = not started).
        # Brain attribute, not status (gotcha #1); see _adopt_exposure.
        self._exposure_known: float | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self.backend.open()
        # ADOPT the camera's exposure, never push ours (Lukas, 2026-09-27: every
        # module reads the instrument's state at start and changes nothing).
        # Until then the .ini's exposure_us was WRITTEN to the camera here. Now
        # it is only a stored value: the camera's actual ExposureTime replaces
        # it, so the GUI, get_config and a later "Save" all show what the camera
        # is really doing. It reaches the camera again only when the user sets
        # it explicitly (the live parameter panel, or set_config with a new
        # value -- see apply_config).
        self._adopt_exposure()
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

    def _adopt_exposure(self) -> None:
        """Read ExposureTime from the camera into cfg.camera.exposure_us (read only)."""
        self._exposure_known = None
        try:
            v = self.backend.get_feature("ExposureTime")
        except Exception as exc:          # a camera without that feature
            self._emit("info", f"camera exposure not readable ({exc}); left as it is")
            return
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        if v > 0:
            self.cfg.camera.exposure_us = v
            self._exposure_known = v
            self._emit("info", f"camera exposure adopted: {v:g} us")

    def shutdown(self) -> None:
        self._stop.set()
        if self._engine is not None:
            self._engine.join(timeout=2.0)
        # A run interrupted by the shutdown restores the working exposure in
        # its own finally; if the engine did not get that far (stuck in a
        # grab), do it here, before the camera is closed -- the camera must end
        # as it was found (adopt rule).
        if self._af_expo_saved is not None:
            try:
                self.backend.set_feature("ExposureTime", self._af_expo_saved)
                self._emit("info", f"exposure restored to {self._af_expo_saved:g} us "
                                   f"at shutdown")
            except Exception as exc:
                self._emit("error", f"could not restore the exposure at shutdown: {exc}")
            self._af_expo_saved = None
            self._af_expo_active = False
        for dev, name in ((self.backend, "camera"), (self.xy, "xy"), (self.z, "z")):
            try:
                dev.close()
            except Exception:
                pass
        self._exposure_known = None     # closed: nothing to compare against
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
                if req is None:
                    # a Z step calibration takes the camera and Z the same way
                    req, self._zcal_request = self._zcal_request, None
                if req is not None:
                    # Arm Kill AF for THIS run here, in the same critical
                    # section that takes the request (kill_af sets the event
                    # under this lock too). It used to be cleared later, at the
                    # start of the run -- a Kill pressed in between was wiped
                    # out and the run went ahead (deep cleaning 2026-09-28).
                    # A Kill pressed while nothing was queued is dropped here.
                    self._af_kill.clear()
            if req is not None:
                # A routine that CRASHES (an exception its own handler did not
                # expect) must not take the engine thread with it: no frame
                # would ever be processed again. Its finally has restored the
                # exposure and cleared "busy"; here it is logged, and the loop
                # goes on (2026-09-29, found testing the AF exposure).
                try:
                    if req.get("kind") == "zcal":
                        self._do_zcal(req)
                    else:
                        self._do_autofocus(req)
                except Exception as exc:
                    self._emit("error", f"engine: {req.get('kind', 'autofocus')} crashed: "
                                        f"{type(exc).__name__}: {exc}")
            else:
                try:
                    self._process()
                except Exception as exc:   # a bad frame must never kill the loop
                    self._emit("error", f"engine: {type(exc).__name__}: {exc}")
            dt = time.monotonic() - t0
            extra = self.cfg.camera.extra_delay_ms / 1000.0
            time.sleep(max(0.0, period - dt) + extra)

    def _grab_gray(self) -> np.ndarray:
        """Grab one frame; return it as the processed 8-bit image.

        A camera that runs deeper than 8 bit (backend ``last_deep()``, the
        SAME buffer) also leaves its full-depth copy in ``self._deep``, put
        through the same clip / rotation / mirror so its pixels line up with
        the 8-bit ones. Only the spot SIZE metrics use it (_spot_source); the
        display, template matching and the fixed threshold keep the 8-bit
        frame, exactly as before (2026-09-28, 12-bit frames).
        """
        raw = self.backend.grab()
        deep = None
        get_deep = getattr(self.backend, "last_deep", None)
        if callable(get_deep):
            try:
                deep = get_deep()
            except Exception:
                deep = None
        img = self.cfg.image
        clip = None
        if img.clip_enabled:
            clip = (img.clip_left, img.clip_top, img.clip_right, img.clip_bottom)
        gray = V.preprocess(V.to_gray(raw), img.rotation_deg, img.symmetry, clip)
        self._deep = None
        self._note_deep(deep)
        if deep is not None:
            arr, bits = deep
            if getattr(arr, "ndim", 0) == 2 and arr.shape == raw.shape[:2]:
                self._deep = (gray, V.preprocess(arr, img.rotation_deg, img.symmetry, clip),
                              int(bits))
        return gray

    def _note_deep(self, deep) -> None:
        """Tell the operator ONCE why the spot metrics run on 8 bit.

        The rig (2026-09-29) ran Mono8 and nothing said so: the deep-frame path
        simply found nothing to convert. The backend knows why (deep_note);
        this says it as one info event when it first appears and again only
        when it CHANGES (another PixelFormat chosen in the live panel), and
        keeps it in status for the GUI. Runs on the grabbing thread; status
        copies the attribute (gotcha #1).
        """
        note = ""
        if deep is None:
            get_note = getattr(self.backend, "deep_note", None)
            if callable(get_note):
                try:
                    note = str(get_note() or "")
                except Exception:
                    note = ""
        self._deep_note = note
        if note and note != self._deep_note_said:
            self._emit("info", f"spot size: {note}")
        self._deep_note_said = note

    def _spot_source(self, gray) -> tuple:
        """(frame, full scale, bits) the spot SIZE is measured on for ``gray``.

        The full-depth copy of that very frame when there is one -- matched by
        IDENTITY, so a frame averaged or grabbed elsewhere never gets another
        frame's deep data -- otherwise ``gray`` itself (8 bit, full scale 255).
        """
        d = self._deep
        if d is not None and d[0] is gray:
            return d[1], float((1 << d[2]) - 1), d[2]
        return gray, 255.0, 8

    def _process(self) -> None:
        try:
            gray = self._grab_gray()
        except Exception as exc:
            # No new frame. Until 2026-09-28 the exception went up to _run, was
            # logged every frame, and the LAST snapshot stayed published as it
            # was -- including a point_settled=True from before the camera
            # failed, which a scan would believe. Now: say so in hw_error, and
            # withdraw every "settled" flag from the snapshot a scan reads.
            why = f"camera grab failed: {type(exc).__name__}: {exc}"
            with self._lock:
                self._status.hw_error = why
                self._status.point_settled = False
                self._status.stable = False
                self._status.laser_settled = False
            self._settled_for = None
            self._avg_buf.clear()
            self._warn_limited("grab", why)
            return
        # When this frame was taken, on the WALL clock (a fly scan lines the
        # stream up with other instruments'). The grab returns once the frame
        # is exposed and read out; the exposure is ~1 ms here, so "now" is it.
        t_frame = time.time()

        # Temporal (running) average across frames. The full-depth copy is
        # averaged alongside (as float: the average IS finer than a count), and
        # dropped as soon as one frame in the window lacks it.
        deep = self._deep if (self._deep is not None and self._deep[0] is gray) else None
        self._temporal.append(gray.astype(np.float32))
        if deep is not None:
            self._temporal_deep.append(deep[1].astype(np.float32))
        else:
            self._temporal_deep.clear()
        if len(self._temporal) > 1:
            gray = np.mean(self._temporal, axis=0).astype(np.uint8)
            if deep is not None and len(self._temporal_deep) == len(self._temporal):
                self._deep = (gray, np.mean(self._temporal_deep, axis=0), deep[2])
            else:
                self._deep = None

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
        # never the whole frame (Lukas 2026-09-29): uncalibrated -> the region
        # around the frame centre, like every other spot search
        where = center if center is not None else (gray.shape[1] / 2.0, gray.shape[0] / 2.0)
        det = V.find_spot(gray, sp.thr_lower, sp.thr_upper, sp.bright_spot,
                          sp.lookup_region_px, where, sp.min_area_px,
                          self._max_area_px(gray.shape, where), sp.reject_border,
                          sp.search_shape,
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
        # the threshold-free sizes, around the calibrated position (or, while
        # calibrating / before calibration, around what the threshold found)
        guess = center if center is not None else ((det.cx, det.cy) if det.found else None)
        self._measure_size(gray, guess, st)

        # -- pixel size / objective ------------------------------------- #
        px_x = self.cfg.image.pixel_size_x_um
        px_y = self.cfg.image.pixel_size_y_um
        st.pixel_size_x, st.pixel_size_y = px_x, px_y
        st.objective_name = self.cfg.image.objective_name

        # -- flags (from the authoritative attributes, race-free) ------- #
        with self._lock:
            # read together: a tracking reset (set_tracking, a new or loaded
            # pattern) bumps _anchor_gen under this lock -- see _track_patterns
            st.tracking_on = self._tracking_on
            anchor_gen = self._anchor_gen
        st.stabilize_on = self._stabilize_on
        st.continuous_focus_on = self._cf_on
        st.selected_index_x = self.cfg.scanning.selected_index_x
        st.selected_index_y = self.cfg.scanning.selected_index_y
        st.pattern_loaded = self.reference is not None

        # -- template match + scanning geometry ------------------------- #
        geo = None
        st.backups_n = 0 if self.reference is None else len(self.reference.backups)
        expo_switched = self._af_expo_active or self._expo_hold_frames > 0
        if self.reference is not None and st.tracking_on and not expo_switched:
            anchor = self._track_patterns(gray, st, anchor_gen)
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

        # -- laser placement + stabiliser --------------------------------- #
        # Both stand down from the moment an autofocus is REQUESTED (not only
        # once it runs): a correction sent in the frame between would still be
        # walking when Z starts, and a point_settled from this frame would let
        # a scan go on before focus has even begun. Both also stand down while
        # a fly scan records the camera: the stage is being flown on purpose.
        # (a Z step calibration moves Z just the same: both loops stand down)
        af_pending = self._af_busy or self._zcal_busy
        # The AF exposure is on (or just came off: frames in flight): the image
        # is not the working one, so the loops stand down exactly as during an
        # autofocus -- and a pattern not matched on a dark frame is not "lost".
        if self._af_expo_active or self._expo_hold_frames > 0:
            if not self._af_expo_active:
                self._expo_hold_frames -= 1
            af_pending = True
        # A lost pattern: count it, and after lost_frames raise the fault (and
        # maybe start the autofocus recovery). Done BEFORE the loops below, so
        # the frame that declares the loss already holds the stage.
        self._update_loss(st, gray.shape[:2], af_pending)
        af_pending = (self._af_busy or self._zcal_busy     # the recovery may just have queued one
                      or self._af_expo_active or af_pending)
        with self._lock:
            faulted = bool(self._fault)
        streaming = self.stream.running
        st.streaming = streaming
        anchor_now = self._last_template_xy if (self.reference is not None
                                                 and st.tracking_on and st.match_found) else None
        # While FAULTED neither loop moves the stage, even when the pattern is
        # matched again: whatever lost it (a spurious match, a half-defocused
        # image) has not been looked at by anyone yet.
        goto = (self._laser_goto and anchor_now is not None and spot_position_ok
                and not af_pending and not streaming and not faulted)
        if goto:
            self._laser_step(anchor_now, px_x, px_y, st)
        elif (geo is not None and st.stabilize_on and spot_position_ok and st.match_found
                and not af_pending and not streaming and not self._laser_goto
                and not faulted):
            stable = self._stabilise_step(geo, px_x, px_y, st)
            st.stable = stable
        else:
            self._avg_buf.clear()
            self._settled_for = None          # a lost match or a stopped loop is not settled
        st.point_settled = (st.stabilize_on and not af_pending and not streaming
                            and not faulted
                            and not self._laser_goto and self._settled_for
                            == (st.selected_index_x, st.selected_index_y))

        # -- where the laser is, against where it was asked to be ---------- #
        tgt = self._laser_target
        if tgt is not None:
            st.laser_target_x_um, st.laser_target_y_um = tgt
        st.laser_goto = bool(self._laser_goto)
        here = (st.spot_from_template_x_um, st.spot_from_template_y_um)
        st.laser_settled = bool(
            tgt is not None and self._laser_done and not self._laser_goto
            and math.isfinite(here[0]) and math.isfinite(here[1])
            and math.hypot(here[0] - tgt[0], here[1] - tgt[1])
            <= max(0.0, float(self.cfg.stabilizer.stable_radius_um)))
        # every processed frame goes into a running fly-scan stream; NaN when
        # there is no position this frame (template lost, spot not calibrated)
        self.stream.append(t_frame, here)

        # -- alignment-accuracy log (residual spot->point distance, um) -- #
        if geo is not None and spot_position_ok and self._acc_on:
            dux, duy = V.pixels_to_um(geo.point_minus_spot_px[0],
                                      geo.point_minus_spot_px[1], px_x, px_y)
            self._acc_log.append((dux, duy))

        # -- continuous focus ------------------------------------------- #
        # Not while FAULTED (Lukas 2026-09-28): the metric is then read off a
        # scene nobody has checked, and a Z that walks away makes the user's
        # correction harder. It resumes by itself once the fault is cleared.
        if (st.continuous_focus_on and self.cfg.hardware.use_z and not af_pending
                and not faulted):
            self._continuous_focus_step(gray, st)

        # -- motion / z read-back --------------------------------------- #
        # A failed read-back is not fatal for the frame, but it is published as
        # hw_error (it used to vanish in a bare `pass`).
        hw_errors = []
        try:
            st.stage_x, st.stage_y = self.xy.read_xy()
            st.stage_moving = self.xy.moving()
            if callable(getattr(self.xy, "read_steps", None)):
                st.stage_steps_x, st.stage_steps_y = self.xy.read_steps()
        except Exception as exc:
            hw_errors.append(f"stage read failed: {type(exc).__name__}: {exc}")
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
            except Exception as exc:
                hw_errors.append(f"Z read failed: {type(exc).__name__}: {exc}")
        st.hw_error = "; ".join(hw_errors)
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
            # spot_area's hint only while spot_area is the metric in use
            st.af_hint = self._af_hint if self.cfg.autofocus.mechanism == "spot_area" else ""
            st.spot_bit_note = self._deep_note
            st.af_exposure_active = self._af_expo_active
            st.zcal_id, st.zcal_running = self._zcal_id, self._zcal_busy
            st.zcal_state = self._zcal_state
            for k, v in self._zcal_result.items():
                setattr(st, k, v)
            if self._af_busy or self._zcal_busy:   # a request that arrived mid-frame
                st.point_settled = False
                st.stable = False
            # the fault as it is NOW (clear_fault may have run mid-frame); a
            # fault never goes out together with a "settled" flag
            st.fault = self._fault
            if st.fault:
                st.point_settled = False
                st.stable = False
                st.laser_settled = False

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
    def _track_patterns(self, gray, st, gen: int | None = None) -> tuple | None:
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
          "Nothing known" happens only after a RESET (tracking switched on, a
          pattern drawn or loaded) -- never after a loss: a lost pattern keeps
          its last anchor, is searched only there, and raises a fault
          (_update_loss explains why there is no whole-frame relock).
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
        # The driver is worked on as a LOCAL and written back only at the end,
        # together with the anchor, and only if no setter reset the tracking
        # meanwhile (see _commit_tracking).
        driver = self._driver if self._driver < n else 0
        anchor = self._last_template_xy
        reports: list = [None] * n
        order = [driver] + [k for k in range(n) if k != driver]
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
        if found and not (driver in found and room(driver) >= margin):
            best = max(found, key=lambda k: (room(k) >= margin, room(k)))
            if best != driver:
                why = "near the edge" if driver in found else "lost"
                self._emit("info", f"pattern {self._pattern_name(best)} now drives "
                                   f"({self._pattern_name(driver)} {why})")
                driver = best

        st.pattern_driver = driver
        st.template_h, st.template_w = pats[driver][0].shape[:2]
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

        drv = reports[driver]
        if drv is None or not drv.found:
            self._commit_tracking(gen, driver)
            st.match_found = False
            return None
        # published as "the template": the one actually in view, so another
        # module (kim's camera calibration) can track the same drawn feature
        st.match_found = True
        st.template_x, st.template_y, st.match_score = drv.x, drv.y, drv.score
        doff = pats[driver][1]
        anchor = (drv.x - doff[0], drv.y - doff[1])
        if not self._commit_tracking(gen, driver, anchor, (drv.x, drv.y)):
            st.match_found = False
            return None
        st.anchor_x, st.anchor_y = anchor

        # refine the other visible backups' offsets against the driver
        rate = float(pat.offset_learn_rate)
        if rate > 0 and room(driver) >= 0:
            for k in found:
                if k == driver or k == 0 or room(k) < 0:
                    continue
                b = ref.backups[k - 1]
                meas = (reports[k].x - anchor[0], reports[k].y - anchor[1])
                err = float(np.hypot(meas[0] - b.offset_px[0], meas[1] - b.offset_px[1]))
                if err > pat.offset_warn_px:
                    self._warn_limited(f"backup{k}", f"pattern {self._pattern_name(k)} is "
                                       f"{err:.0f} px from where pattern "
                                       f"{self._pattern_name(driver)} puts it "
                                       f"-- a bad match? offset not updated")
                    continue
                b.offset_px = (b.offset_px[0] + rate * (meas[0] - b.offset_px[0]),
                               b.offset_px[1] + rate * (meas[1] - b.offset_px[1]))
            if driver != 0 and 0 in found and room(0) >= 0:
                # the MAIN template is in view while a backup drives: the backup's
                # offset is measured directly
                b = ref.backups[driver - 1]
                meas = (drv.x - reports[0].x, drv.y - reports[0].y)
                if float(np.hypot(meas[0] - b.offset_px[0],
                                  meas[1] - b.offset_px[1])) <= pat.offset_warn_px:
                    b.offset_px = (b.offset_px[0] + rate * (meas[0] - b.offset_px[0]),
                                   b.offset_px[1] + rate * (meas[1] - b.offset_px[1]))
        return anchor

    def _commit_tracking(self, gen, driver, anchor=None, driver_xy=None) -> bool:
        """Store this frame's tracking result -- unless it went stale meanwhile.

        Matching takes a good part of a frame. If set_tracking / capture_reference
        / load_pattern / clear_backups reset the tracking while this frame was
        matching, the frame's anchor belongs to the OLD state: writing it back
        would undo the reset, and the next frames would search only a small box
        around that stale position -- a new pattern elsewhere, or a sample moved
        while tracking was off, then never relocks (deep cleaning 2026-09-28;
        gotcha #1 in another shape). Returns False when the result was dropped.
        """
        with self._lock:
            if gen is not None and gen != self._anchor_gen:
                return False
            self._driver = driver
            if anchor is not None:
                self._last_template_xy = anchor
                self._driver_xy = driver_xy
            return True

    def _reset_tracking(self, anchor=None, driver_xy=None) -> None:
        """Forget where the pattern was (setters); stale in-flight frames lose."""
        with self._lock:
            self._anchor_gen += 1
            self._last_template_xy = anchor
            self._driver, self._driver_xy = 0, driver_xy

    # ------------------------------------------------------------------ #
    # losing the pattern: a latched fault, maybe one autofocus (2026-09-28)
    # ------------------------------------------------------------------ #
    # WHY THERE IS NO WHOLE-FRAME RELOCK. When the pattern is not found in its
    # box, the tempting fix is to search the whole frame and carry on from the
    # best match there. Lukas (2026-09-28): "A whole-frame relock is dangerous
    # for spurious templates. The template never changes abruptly, so if it is
    # lost it is out of focus, out of image, the spot is in the pattern, or
    # something else happened that is terrible. All need correction." A sample
    # full of similar structures (an array of discs, a grating) matches the
    # template in many places at a score close to the real one; relocking on
    # one of them would pin the scan array to the wrong structure and the
    # stabiliser would then DRIVE the sample there -- a scan would carry on,
    # measuring the wrong place, with every flag green. So a loss stops the
    # loops (fault) and waits for a human. The only automatic attempt is ONE
    # autofocus (autofocus_on_loss), after which the pattern is looked for
    # again ONLY at its last place. (Switching tracking off and on still
    # searches the whole frame once: that is the user, watching, asking for it.)
    def _update_loss(self, st, frame_hw: tuple, af_pending: bool) -> None:
        """Engine thread, once per frame, after tracking: count frames without
        the pattern, declare a loss, run / judge the autofocus recovery."""
        pat = self.cfg.pattern
        n_lost = max(1, int(pat.lost_frames))
        with self._lock:
            rec = self._recovery
        if rec is not None:
            if self._af_busy:
                return                          # the recovery autofocus is still to come
            if rec["phase"] == "af":
                state = self._af_state
                if state != "OK":
                    self._latch(f"{rec['why']}; the autofocus recovery failed ({state})")
                    return
                rec["phase"], rec["frames"] = "relock", 0
            if st.match_found:
                with self._lock:
                    self._recovery = None
                    self._fault = ""
                self._lost_count = 0
                self._emit("warn", "pattern recovered by autofocus")
                return
            rec["frames"] += 1
            if rec["frames"] >= n_lost:
                self._latch(f"{rec['why']}; not found again after the autofocus recovery")
            return

        # Lost = tracking on, a pattern that WAS locked (an anchor is known), and
        # no match now. Right after a reset nothing is known yet and nothing is
        # "lost": that first search is the user's own (whole frame).
        lost_now = (self.reference is not None and st.tracking_on and not st.match_found
                    and self._last_template_xy is not None)
        if not lost_now or af_pending:
            # (a queued autofocus is about to move Z anyway; judge afterwards)
            if not lost_now:
                self._lost_count = 0
            return
        self._lost_count += 1
        with self._lock:
            if self._fault or self._lost_count < n_lost:
                return
        kind, why = self._loss_cause(frame_hw)
        if (pat.autofocus_on_loss and kind == "focus" and self.cfg.hardware.use_z):
            # Rate limit (Lukas 2026-09-28): a pattern that keeps flickering
            # out must not start an autofocus every few seconds -- a second
            # loss soon after a recovery is itself a sign something is wrong.
            since = time.monotonic() - self._last_recovery_t
            if since < float(pat.recovery_min_interval_s):
                self._latch(f"{why}; lost again {since:.0f} s after the last "
                            f"autofocus recovery (no new attempt within "
                            f"{float(pat.recovery_min_interval_s):.0f} s)")
                return
            self._last_recovery_t = time.monotonic()
            with self._lock:
                self._recovery = {"phase": "af", "why": why, "frames": 0}
                self._fault = f"{why}; autofocus recovery running"
            self._emit("warn", f"{why}: trying ONE autofocus (autofocus_on_loss)")
            self.autofocus()
            return
        self._latch(why)

    def _latch(self, why: str) -> None:
        with self._lock:
            self._recovery = None
            self._fault = why
        self._emit("error", f"FAULT: {why}. The stabiliser holds; correct it, "
                            f"then Clear fault.")

    def _loss_cause(self, frame_hw: tuple) -> tuple[str, str]:
        """(kind, message) from where the pattern was LAST seen in the image.

        kind: "edge" (out of image), "spot" (the laser spot is on the pattern)
        or "focus" (neither -- out of focus, or something unknown).
        """
        pat = self.cfg.pattern
        H, W = frame_hw
        xy = self._driver_xy
        if xy is None or self.reference is None:
            return "focus", "pattern lost: out of focus or unknown cause"
        k = self._driver if self._driver <= len(self.reference.backups) else 0
        tpl = (self.reference.template if k == 0
               else self.reference.backups[k - 1].template)
        th, tw = tpl.shape[:2]
        x, y = xy
        room = min(x - tw / 2, W - x - tw / 2, y - th / 2, H - y - th / 2)
        if room <= float(pat.loss_edge_margin_px):
            return "edge", (f"pattern lost: out of image (last seen {max(0.0, room):.0f} px "
                            f"from the image edge)")
        sp = self.cfg.spot
        if sp.ref_set:
            # distance from the spot to the pattern's BOX (0 = spot inside it)
            dx = max(0.0, abs(sp.ref_x - x) - tw / 2)
            dy = max(0.0, abs(sp.ref_y - y) - th / 2)
            d = float(np.hypot(dx, dy))
            if d <= float(pat.loss_spot_margin_px):
                return "spot", (f"pattern lost: the laser spot is on the pattern "
                                f"(last seen {d:.0f} px from the spot)")
        return "focus", (f"pattern lost: out of focus or unknown cause "
                         f"(last seen at {x:.0f}, {y:.0f} px)")

    def clear_fault(self) -> str:
        """The user has looked and corrected: resume the loops.

        Refused while tracking is on and the pattern is still not matched --
        clearing then would only raise the same fault again, or (worse) let the
        loops act on nothing. With tracking OFF there is nothing to lose, so
        clearing is allowed (the user has decided to go on without it).
        """
        with self._lock:
            if not self._fault:
                return "no fault"
            if self._recovery is not None and self._tracking_on:
                raise RuntimeError("the autofocus recovery is still running: wait for "
                                   "it (or Kill AF), then clear")
            if (self._tracking_on and self.reference is not None
                    and not self._status.match_found):
                raise RuntimeError("the pattern is still not found: correct it first "
                                   "(focus, move it back into view, or switch tracking "
                                   "off and on to search the whole frame), then clear")
            was = self._fault
            self._fault = ""
            self._recovery = None
            self._lost_count = 0
            self._status.fault = ""
        self._emit("info", f"fault cleared by the user (was: {was})")
        return "cleared"

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
        self._reset_tracking(self._last_template_xy, self._driver_xy)   # drive with main again
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
        self._correct(avg, dist_um, gain, stb.move_with_x, stb.move_with_y, "stabiliser")
        return stable

    def _correct(self, avg, dist_um, gain, move_x, move_y, who) -> None:
        """Move the sample to null a measured (point - spot) distance.

        Shared by the stabiliser and the laser placement loop. `avg` is the
        averaged distance in px, `dist_um` the same in um.
        """
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
            shift = (-gain * avg[0] if move_x else 0.0,
                     -gain * avg[1] if move_y else 0.0)
            try:
                image_move(shift[0], shift[1], context=self.image_context())
            except Exception as exc:
                self._warn_limited(who, f"{who} move refused: {exc}")
            self._avg_buf.clear()
            return

        # Piezo rig: stage axes assumed aligned with the image (+stage x -> +px x).
        try:
            cx, cy = self.xy.read_xy()
        except Exception:
            self._avg_buf.clear()
            return
        new_x, new_y = cx, cy
        if move_x:
            new_x = cx - gain * dist_um[0]
        if move_y:
            new_y = cy - gain * dist_um[1]
        new_x, new_y = self._clamp_xy(new_x, new_y)
        try:
            self.xy.move_xy(new_x, new_y)
        except Exception as exc:
            self._emit("warn", f"{who} move failed: {exc}")
        self._avg_buf.clear()                         # restart averaging post-move

    def _laser_step(self, anchor, px_x, px_y, st) -> None:
        """One cycle of PLACING THE LASER at the target (set_laser_target).

        The stabiliser's own recipe -- wait out a move, average a window of
        fresh frames, correct once on the mean -- aimed at a free point of the
        sample instead of an array point: the point `target` um from the main
        template. When a whole averaged window is within stable_radius_um the
        placement is DONE and the loop lets go of the stage, so a fly scan can
        move it without a fight.
        """
        stb = self.cfg.stabilizer
        tgt = self._laser_target
        if tgt is None:
            return
        tx, ty = tgt
        point = (anchor[0] + tx / px_x, anchor[1] + ty / px_y)
        inst = np.array([point[0] - st.spot_x, point[1] - st.spot_y], dtype=float)
        st.distance_um = float(np.hypot(*V.pixels_to_um(inst[0], inst[1], px_x, px_y)))
        if self._stage_settling():
            self._avg_buf.clear()
            return
        # A new target can arrive (request thread) while this frame is being
        # worked on. Without the lock and the identity check, this frame's
        # distance -- measured to the OLD target -- could land in the new
        # target's fresh window, or "done" be stamped on the new target, which
        # then never moves (a scan waiting for laser_settled times out).
        # set_laser_target makes a NEW tuple every call, so `is` also tells a
        # repeated request for the same point apart.
        with self._laser_lock:
            if self._laser_target is not tgt:
                return
            self._avg_buf.append(inst)
            if len(self._avg_buf) < self._avg_buf.maxlen:
                return
            avg = np.mean(self._avg_buf, axis=0)
            dist_um = np.array(V.pixels_to_um(avg[0], avg[1], px_x, px_y))
            st.distance_um = float(np.hypot(dist_um[0], dist_um[1]))
            if st.distance_um <= max(0.0, float(stb.stable_radius_um)):
                self._avg_buf.clear()
                self._laser_goto = False
                self._laser_done = True
                return
        gain = min(max(float(stb.gain), 0.01), 2.0)
        self._correct(avg, dist_um, gain, True, True, "laser placement")

    def _stage_settling(self) -> bool:
        """True while a correction is still arriving: within settle_s of the last
        move, or (open-loop stage) while it reports moving. Frames from then
        describe a position about to change."""
        if time.monotonic() - self._stab_move_t < max(0.0, float(self.cfg.stabilizer.settle_s)):
            return True
        if getattr(self.xy, "open_loop", False):
            try:
                return bool(self.xy.moving())
            except Exception:
                return False
        return False

    # ------------------------------------------------------------------ #
    # spot size without a fixed threshold (2026-09-28)
    # ------------------------------------------------------------------ #
    def _measure_size(self, gray, guess, st) -> None:
        """Fill the threshold-free size fields of ``st`` for this frame.

        ``guess`` is the calibrated position (or, before calibration, what the
        threshold found). WHERE the size is measured is Spot.locate
        (2026-09-29): around ``guess`` itself ("calibrated", the default), or
        around the spot found in the search region ("peak" / "blob") -- the rig
        had a spot 100 px off its calibration, and every size read "no spot".
        Everything is computed around ONE centre, in the search region only (a
        few hundred pixels square, not the 2-megapixel frame): the second
        moment, the relative area, the encircled energy, the Gaussian fit.
        A saturated spot is measured anyway and said ONCE per episode, with
        which sizes it affects (Lukas: a saturated spot is still a spot).
        """
        sp = self.cfg.spot
        st.spot_size_method = sp.size_method
        if guess is None:
            st.spot_size_why = "no spot position yet: calibrate the spot (Spot tab)"
            return
        # the camera's full-depth frame when it delivers one (12 bit: the far
        # wings are no longer rounded away), else this 8-bit frame
        src, top, bits = self._spot_source(gray)
        st.spot_bit_depth = bits
        centre, loc = self._size_centre(src, top, guess)
        # Frames in flight after an exposure change (the AF exposure's switch
        # and restore, or a new ExposureTime from the live panel / settings)
        # were exposed with the OLD value: whatever is located on them says
        # nothing about the laser. Rig 2026-09-29: one false "N px from its
        # calibrated position" right at a 65 -> 2480 us switch, locate = blob.
        expo_in_flight = (self._af_expo_active or self._expo_hold_frames > 0
                          or self._offset_quiet > 0)
        if self._offset_quiet > 0:
            self._offset_quiet -= 1
        if sp.ref_set and centre is not None:
            st.spot_offset_px = float(np.hypot(centre[0] - sp.ref_x, centre[1] - sp.ref_y))
        # The warning needs the offset to PERSIST for offset_warn_frames
        # consecutive working frames: a moved laser / stale calibration stays
        # put, a single odd frame does not.
        far = (sp.ref_set and centre is not None and sp.locate != "calibrated"
               and float(sp.offset_warn_px) > 0 and st.spot_offset_px > float(sp.offset_warn_px))
        if expo_in_flight or not far:
            self._offset_run = 0
        else:
            self._offset_run += 1
            if self._offset_run >= max(1, int(sp.offset_warn_frames)):
                self._warn_limited(
                    "spot_offset", f"the spot is {st.spot_offset_px:.0f} px from its calibrated "
                    f"position (for {self._offset_run} frames): the laser moved or the "
                    f"calibration is stale -- recalibrate the spot (Spot tab); motion still "
                    f"uses the calibrated position", 60.0)
        if centre is None:
            st.spot_size_why = self._why_no_spot(src, top, guess, loc.why)
            return
        st.spot_found_x, st.spot_found_y = float(centre[0]), float(centre[1])
        try:
            mom = V.spot_second_moment(src, centre, sp, max_value=top)
            rel = V.spot_relative_area(src, centre, sp, max_value=top)
            enc = V.spot_encircled(src, centre, sp, max_value=top, moments=mom)
            gau = V.spot_gauss_fit(src, centre, sp, max_value=top, moments=mom)
        except Exception as exc:                 # never let a size kill the frame
            self._warn_limited("size", f"spot size: {type(exc).__name__}: {exc}", 30.0)
            return
        if mom.ok:
            st.spot_d4sigma_px, st.spot_sigma2_px2 = mom.d4sigma, mom.sigma2
            st.spot_centroid_x, st.spot_centroid_y = mom.cx, mom.cy
            st.spot_peak = mom.peak
            st.spot_size_box = tuple(int(v) for v in mom.box)
        else:
            st.spot_size_why = self._why_no_spot(src, top, centre, mom.why)
        if rel.ok:
            st.spot_rel_area = rel.area
            st.spot_peak_avg = rel.peak
        if enc.ok:
            st.spot_d86_px = enc.d_px
        if gau.ok:
            st.spot_gauss_sigma2_px2, st.spot_gauss_r2 = gau.sigma2, gau.r2
        st.spot_saturated = bool(mom.saturated or rel.saturated)
        if st.spot_saturated:
            st.spot_sat_fraction, st.spot_exposure_hint = V.saturation_info(
                src, centre, sp, max_value=top)
        st.spot_size = {"threshold": st.spot_area if st.spot_found else float("nan"),
                        "relative": st.spot_rel_area,
                        "d4sigma": st.spot_d4sigma_px,
                        "encircled": st.spot_d86_px,
                        "gauss": st.spot_gauss_sigma2_px2,
                        "peak": st.spot_peak_avg}.get(sp.size_method, float("nan"))
        self._note_saturation(st.spot_saturated, st.spot_sat_fraction, st.spot_exposure_hint)

    def _size_centre(self, src, top, guess) -> tuple:
        """(centre or None, SpotLocation or None): where the size is measured.

        "calibrated" (or no calibration yet): ``guess`` itself, exactly as
        before. "peak" / "blob": vision.locate_spot in the search region
        around ``guess`` -- the measurement box AND the mirror test of
        reject_asymmetric then use the LOCATED centre; mirroring a spot that is
        not at the calibrated centre through that centre threw all of it away
        on the rig."""
        sp = self.cfg.spot
        mode = sp.locate if sp.locate in LOCATE_MODES else "calibrated"
        if mode == "calibrated" or guess is None:
            return guess, None
        loc = V.locate_spot(src, guess, sp, mode, max_value=top)
        return ((loc.x, loc.y) if loc.ok else None), loc

    def _why_no_spot(self, src, top, centre, why: str) -> str:
        """In words: why no size was measured, and WHERE the light is instead.

        The rig's screenshot said only "no spot above the noise" while the spot
        sat, plainly visible, 100 px from the calibrated position: the useful
        news is where the light is and what to do about it.

        ONLY in the search region around the calibrated laser, and only light
        that looks like a spot (vision.locate_spot's candidate rules). Rig
        check 2026-09-29: the whole-frame "brightest light" was the top-left
        corner (236, 0) of a saturated illuminated block 915 px away, while
        the laser sat 50 px from the calibration; and Lukas's rule the same
        day: "Always look for the laser spot in the safety area around the
        laser only!"."""
        sp = self.cfg.spot
        why = why or "no spot"
        calib = (float(sp.ref_x), float(sp.ref_y)) if sp.ref_set else None
        try:
            loc = V.locate_spot(src, calib, sp, "blob", max_value=top)
        except Exception:
            return why
        if not loc.ok:
            # With locate = blob the caller's ``why`` IS this same look's
            # answer: joining the two printed one sentence twice (rig,
            # 2026-09-29). Add only what is new.
            if not loc.why or loc.why in why:
                return why
            return f"{why}; {loc.why}"
        d = float(np.hypot(loc.x - centre[0], loc.y - centre[1]))
        # a few spot radii away = somewhere else; closer = the spot itself is dim
        near = max(10.0, 1.5 * float(sp.ref_d4sigma_px or 0.0))
        if d <= near:
            return why
        where = ("calibrated position" if sp.locate == "calibrated" else "measured centre")
        half = int(sp.lookup_region_px or 0)
        region = f" in the {half} px search region" if half > 0 else ""
        return (f"{why}; the brightest spot-like light{region} is {d:.0f} px from the "
                f"{where} ({loc.x:.0f}, {loc.y:.0f}) -- pick locate = peak or blob, or "
                f"recalibrate the spot")

    def _note_saturation(self, saturated: bool, frac: float = float("nan"),
                         factor: float = float("nan")) -> None:
        """Say it ONCE per saturation episode, as information.

        Lukas (2026-09-29): "I am fine with measuring a saturated spot as a
        spot" -- nothing is refused because of it. What saturation does to each
        size: locating it (peak / blob) and the fixed-threshold area are
        unaffected; D4sigma and the encircled energy read too LARGE (the
        clipped core holds too little of the light) but keep their minimum near
        focus -- usable for autofocus, just shallower; the Gaussian fit and the
        peak brightness are meaningless on a flat top. An episode ends after 15
        unsaturated frames in a row (one frame dipping under full scale must
        not start a new message).

        Frames taken at the AF exposure (and the ones still in flight after
        the restore) are NOT the working image: they neither end nor start an
        episode. Rig 2026-09-29: the unsaturated AF frames ended the episode,
        the restored working exposure started a new one -- the line came
        twice per autofocus run."""
        if self._af_expo_active or self._expo_hold_frames > 0:
            return
        if not saturated:
            self._unsat_run += 1
            if self._unsat_run >= 15:
                self._sat_episode = False
            return
        self._unsat_run = 0
        if self._sat_episode:
            return
        self._sat_episode = True
        how = f" ({100 * frac:.0f} % of its pixels at full scale)" if np.isfinite(frac) else ""
        msg = (f"the laser spot is SATURATED{how}. Unaffected: locating it (peak / blob) "
               f"and the fixed-threshold area. D4sigma and encircled read too large "
               f"(clipped core) but keep their minimum near focus -- usable for autofocus, "
               f"shallower. The Gaussian fit and the peak are not usable while saturated.")
        if np.isfinite(factor) and 0 < factor < 1:
            msg += (f" Optional: exposure x{factor:.2f} would bring the peak to ~80 % of "
                    f"full scale (estimate; or set autofocus.exposure_us for the autofocus "
                    f"only).")
        self._emit("info", msg)

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
        with self._lock:
            # under the lock: the engine takes a request and re-arms the kill
            # event in one critical section, so this can land neither between
            # the two nor be wiped out afterwards
            self._af_kill.set()      # a sweep in progress stops at its next check
            if self._af_request is not None:     # queued, never started: cancel it
                self._af_request = None
                self._af_busy = False
                self._af_state = "killed"
                self._publish_af_locked()
            if self._zcal_request is not None:   # the same for a Z step calibration
                self._zcal_request = None
                self._zcal_busy = False
                self._zcal_state = "killed"
                self._publish_zcal_locked()

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
        # the run moved Z itself: a focus step must start from where Z IS, not
        # from a set_z target of before the run (see step_z)
        self._z_target = None

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
        look for the pattern where it was before the run (its safety box only,
        never the whole frame -- see _update_loss). A pattern that wandered
        further than that is LOST: a fault, not a silent relock elsewhere.
        """
        self._pause_image_loops()
        try:
            # the AF exposure (if set) is switched on here and ALWAYS back off
            # before the loops resume below -- normal end, failure, kill, crash
            with self._exposure_for("autofocus"):
                self._run_autofocus(req)
        except Exception as exc:
            # crashed (not a handled failure): never "busy" forever, and say so
            if self._af_state in ("queued", "running"):
                self._af_finish(f"crashed: {type(exc).__name__}")
            raise
        finally:
            if self._af_state in ("queued", "running"):   # left early (shutdown)
                self._af_finish("stopped")
            self._pause_image_loops()            # nothing measured during AF counts after it
            self._stab_move_t = time.monotonic()

    @contextmanager
    def _exposure_for(self, what: str, engine: bool = True):
        """Run the block at autofocus.exposure_us, then restore the camera's own.

        Why (Lukas 2026-09-29): "if I lower the exposure I don't see the main
        image". The working exposure stays bright -- pattern tracking needs the
        sample -- and the spot saturates; the autofocus (and the Z step
        calibration, and optionally the spot calibration) may use a shorter
        one. The camera's ExposureTime is READ first (not the config: that is
        the truth), set, and put back in a finally -- a failed, killed or
        crashed run ends at the working exposure too; shutdown() catches a run
        interrupted before its finally. The adopt rule is kept: this is an
        explicit action, and the camera ends as it was.

        Frames in flight: buffers already exposed with the old value arrive
        after the write. ``engine`` = this runs on the engine thread (AF, zcal):
        it grabs and drops autofocus.exposure_discard_frames frames itself after
        each switch. Otherwise (calibrate_spot, a request thread) the engine
        keeps grabbing: it is told to hold the image loops for that many frames
        after the restore, and the caller skips frames by number.
        # VERIFY on the IDS camera: how many frames lag behind an ExposureTime
        # write (NumBuffersAnnouncedMinRequired buffers are queued); the
        # simulator applies it to the very next grab.
        """
        af = self.cfg.autofocus
        want = float(af.exposure_us or 0.0)
        n_drop = max(0, int(af.exposure_discard_frames))
        if want <= 0:
            yield False
            return
        try:
            old = float(self.backend.get_feature("ExposureTime"))
        except Exception as exc:
            self._emit("warn", f"{what}: cannot read the exposure ({exc}); running at the "
                               f"working exposure")
            yield False
            return
        try:
            self._note_exposure_change()
            self.backend.set_feature("ExposureTime", want)
        except Exception as exc:
            self._emit("warn", f"{what}: cannot set the autofocus exposure {want:g} us "
                               f"({exc}); running at the working exposure")
            yield False
            return
        self._af_expo_saved = old
        with self._lock:
            self._af_expo_active = True
            self._status.af_exposure_active = True
        self._emit("info", f"{what}: exposure {old:g} -> {want:g} us (autofocus.exposure_us)")
        try:
            if engine:
                self._drop_frames(n_drop)
            yield True
        finally:
            try:
                self._note_exposure_change()
                self.backend.set_feature("ExposureTime", old)
                self._emit("info", f"{what}: exposure restored to {old:g} us")
            except Exception as exc:
                self._emit("error", f"{what}: could NOT restore the exposure {old:g} us: "
                                    f"{exc} -- set it by hand (Camera settings)")
            self._af_expo_saved = None
            # the loops stay down for the frames still exposed short
            self._expo_hold_frames = n_drop + 1
            with self._lock:
                self._af_expo_active = False
                self._status.af_exposure_active = False
            if engine:
                try:
                    self._drop_frames(n_drop)
                except Exception:
                    pass

    def _drop_frames(self, n: int) -> None:
        """Grab and throw away ``n`` frames (engine thread only)."""
        for _ in range(max(0, int(n))):
            if self._stop.is_set():
                return
            try:
                self.backend.grab()
            except Exception:
                return

    def auto_exposure_once(self, timeout_s: float = 2.5) -> dict:
        """Set ExposureTime ONCE so the image is well exposed (an explicit action).

        Lukas 2026-09-29 ("Auto exposure (once)" in Camera settings). The
        camera's own ExposureAuto = "Once" when it offers it (GenICam SFNC:
        it adjusts, then returns to "Off" by itself -- # VERIFY both on the IDS
        camera); otherwise a few software steps on the IMAGE: the
        ``auto_exposure_percentile`` of the frame with the spot's search
        region left out (the spot is meant to be bright; the sample is what
        must be visible) brought to ``auto_exposure_target`` of full scale,
        within the feature's min / max. Only ExposureTime changes; cfg and the
        adopted exposure follow, so a later settings round trip does not undo it.
        """
        if self._af_busy or self._zcal_busy:
            raise RuntimeError("autofocus / Z calibration is running: set the exposure "
                               "once it has finished")
        cam = self.cfg.camera
        old = float(self.backend.get_feature("ExposureTime"))
        feats = {}
        try:
            feats = {f.get("name"): f for f in (self.backend.features() or [])}
        except Exception:
            feats = {}
        ea = feats.get("ExposureAuto")
        t_end = time.monotonic() + max(0.5, float(timeout_s))
        level = float("nan")
        if ea is not None and "Once" in [str(o) for o in (ea.get("options") or [])] \
                and ea.get("writable", True):
            method = "camera ExposureAuto=Once"
            self._note_exposure_change()
            self.backend.set_feature("ExposureAuto", "Once")
            done = False
            while time.monotonic() < t_end:
                if str(self.backend.get_feature("ExposureAuto")) == "Off":
                    done = True
                    break
                time.sleep(0.05)
            if not done:
                try:
                    self.backend.set_feature("ExposureAuto", "Off")
                except Exception:
                    pass
                raise RuntimeError("the camera's ExposureAuto=Once did not finish in time; "
                                   "switched back to Off")
            # the camera changed its exposure all the time it ran: frames in
            # flight from the last change still arrive
            self._note_exposure_change()
            new = float(self.backend.get_feature("ExposureTime"))
        else:
            method = "software"
            ft = feats.get("ExposureTime") or {}
            lo = float(ft.get("min") or 1.0)
            hi = float(ft.get("max") or 1e7)
            goal = min(0.98, max(0.05, float(cam.auto_exposure_target))) * 255.0
            pct = min(100.0, max(50.0, float(cam.auto_exposure_percentile)))
            wait_n = max(0, int(self.cfg.autofocus.exposure_discard_frames)) + 2
            cur = old
            for _ in range(max(1, int(cam.auto_exposure_iterations))):
                gray = self._frame_after(wait_n, t_end)
                if gray is None:
                    break
                level = self._image_level(gray, pct)
                if level >= 254.0:
                    factor = 0.5                       # clipped: the level is unknown
                else:
                    factor = goal / max(level, 1.0)
                factor = float(np.clip(factor, 0.25, 4.0))
                if abs(factor - 1.0) < 0.03:
                    break
                want = float(np.clip(cur * factor, lo, hi))
                if abs(want - cur) <= 1e-6 * max(1.0, cur):
                    break                              # at a limit
                self._note_exposure_change()
                self.backend.set_feature("ExposureTime", want)
                cur = float(self.backend.get_feature("ExposureTime"))
            new = cur
        self.cfg.camera.exposure_us = new
        self._exposure_known = new
        self._emit("info", f"auto exposure (once, {method}): {old:g} -> {new:g} us"
                   + (f" (image {pct:g}th percentile {level:.0f} of 255)"
                      if method == "software" and np.isfinite(level) else ""))
        return {"method": method, "old_us": old, "new_us": new, "level": level}

    def _frame_after(self, n: int, t_end: float):
        """The processed frame at least ``n`` frames from now (None on timeout)."""
        start = self._status.frame_number
        while time.monotonic() < t_end:
            if self._status.frame_number >= start + n:
                with self._lock:
                    return self._last_frame
            time.sleep(0.005)
        return None

    def _image_level(self, gray, pct: float) -> float:
        """The ``pct`` percentile of the frame WITHOUT the spot's search region."""
        sp = self.cfg.spot
        mask = np.ones(gray.shape[:2], bool)
        if sp.ref_set:
            x0, y0, x1, y1 = V._limit_box(gray.shape, (sp.ref_x, sp.ref_y), sp)
            mask[y0:y1, x0:x1] = False
        vals = gray[mask] if mask.any() else gray.ravel()
        return float(np.percentile(vals, pct))

    def _pause_image_loops(self) -> None:
        self._avg_buf.clear()
        self._temporal.clear()
        self._temporal_deep.clear()
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
        # (the kill event was re-armed when the engine took this request: see _run)
        self._af_curve = {"z": [], "metric": [], "best": 0.0,
                          "maximise": V.focus_is_maximised(af.mechanism)}
        self._af_area_sat = [0, 0]        # spot_area: levels scored / saturated

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
                self._check_area_saturation()
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
                                           maximise, af.fit_curve,
                                           rel_window=self._fit_rel_window())
            self._af_curve = {"z": [float(v) for v in levels],
                              "metric": [float(v) for v in metrics],
                              "best": float(best), "maximise": bool(maximise)}
            target = self._clamp_z(best + af.offset_from_found_v)
            approach(target, float(levels[-1]))
            self._af_finish("OK", best, target)
            self._emit("info", f"autofocus -> best {best:.3f} {unit} "
                               f"(parked {target:.3f} {unit})")
            self._check_area_saturation()
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
            # measure while Z is still walking home. af_error = the exception's
            # type, or -- for a run that knows WHY it failed -- that reason.
            state = exc.state if isinstance(exc, AutofocusFailed) else type(exc).__name__
            self._af_finish(state)
            self._emit("error", f"autofocus failed: {exc}{back}")
            self._check_area_saturation()

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
        fine_sem: list = []       # frame-to-frame error of each fine level's mean
        park_info: dict = {}      # the park target + tolerance, for the plot / wire

        def publish(best=None):
            self._af_curve = {
                "z": list(phases["fine"][0]), "metric": list(phases["fine"][1]),
                "best": best, "maximise": bool(maximise),
                "phases": {k: {"z": list(v[0]), "metric": list(v[1])}
                           for k, v in phases.items()},
                **park_info}

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
            if phase == "fine" and len(vals) > 1:
                fine_sem.append(float(np.std(vals, ddof=1)) / np.sqrt(len(vals)))
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
                # For every SIZE metric the square root is ~linear in the
                # defocus far from focus: sqrt(area) is a radius, and
                # sqrt(sigma^2) = sigma is exactly the hyperbola
                # sqrt(sigma0^2 + c dz^2) -> sqrt(c)|dz| of a coherent beam.
                # So the last two levels and the calibrated in-focus size tell
                # how far focus still is.
                r_ref = self._ref_size_root()
                aimed = (self._coarse_step(m_prev, m_best, abs(z_best - z_prev), r_ref,
                                           coarse, max_step, rise)
                         if r_ref is not None else None)
                if aimed is not None:
                    step = aimed
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
            fz.clear(); fm.clear(); fine_sem.clear()
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
        best = V.best_focus_from_sweep(zs[ok], ms[ok], maximise, af.fit_curve,
                                       rel_window=self._fit_rel_window())
        # What the park aims for (rig 2026-09-29). It used to be the single
        # best fine level, best_f[1] -- the lowest of ~10 noisy numbers, biased
        # low by construction; on the rig a 9 % dip that no later frame could
        # match, so every park walk "failed". Now the fitted extremum of the
        # whole fine walk with an outlying level dropped (vision docstring).
        tgt = V.robust_focus_target(zs[ok], ms[ok], maximise,
                                    rel_window=self._fit_rel_window(),
                                    repeat_noise=float(np.median(fine_sem)) if fine_sem else 0.0)
        m_goal = tgt.value if np.isfinite(tgt.value) else best_f[1]
        if af.fit_curve and tgt.method == "parabola":
            best = tgt.z                     # the same fit, without the outlier
        tol, tol_why = self.effective_park_tolerance(m_goal, tgt.noise)
        park_info.update(park_target=float(m_goal), park_tolerance=float(tol),
                         park_target_method=tgt.method)
        publish(best)
        # Centring (park_centre, sigma^2 only): the band "within tol of the
        # minimum" is |dz| <= w = sqrt(tol x min / a) wide on either side of
        # focus, and the walk enters it at its APPROACH-SIDE EDGE -- for a 4 %
        # band on the sim's spot that is ~0.8 units (a quarter of a Rayleigh
        # range) before focus. The fitted curvature a (per counter unit^2 of a
        # walk in the same direction as the park walk) turns the metric of the
        # entry level into the distance still to go: dz = sqrt((m - min) / a).
        centre = (bool(af.park_centre) and tgt.method == "parabola"
                  and af.mechanism in V.PARABOLIC_FOCUS and tgt.curvature > 0)
        w_band = (float(np.sqrt(tol * abs(m_goal) / tgt.curvature)) if centre else 0.0)

        # ---- 3. PARK: by the image ------------------------------------------
        note = ""
        parked = None
        centred = 0.0
        closest = None                       # the best park level seen, for the report
        for attempt in range(1, 4):
            back = clampz(best - d * margin * attempt)
            go(back); pos["z"] = back
            z = back
            best_p, n_worse = None, 0
            walk_limit = margin * attempt + 2 * bracket + af.max_travel_v / 4
            while True:
                mp = measure(z, "park")
                if np.isfinite(mp) and (closest is None or score(mp) < score(closest)):
                    closest = mp
                if np.isfinite(mp) and not worse(mp, m_goal, tol):
                    step_on = 0.0
                    if centre:
                        excess = max(0.0, score(mp) - score(m_goal))
                        step_on = min(float(np.sqrt(excess / tgt.curvature)), w_band)
                    if step_on < fine / 4:
                        parked = z
                        break
                    # on towards the centre -- the same direction, so no
                    # reversal -- and CONFIRM there on the image
                    zc = clampz(z + d * step_on)
                    mc = measure(zc, "park")
                    if np.isfinite(mc) and not worse(mc, m_goal, tol):
                        parked, centred = zc, zc - z
                        break
                    # passed the band (or a noisy read): the next walk parks
                    # at the first level inside it, uncentred
                    centre = False
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
                note = (f"parked by the image (attempt {attempt}, tolerance "
                        f"{100 * tol:.0f} %: {tol_why}), {parked - best:+.3f} from the fine "
                        f"walk's best by the counter"
                        + (f" (centred {centred:+.3f} by the fit)" if centred else ""))
                break
        target_txt = (f"target {m_goal:.4g} ({tgt.method} over {tgt.n} levels"
                      + (f", {tgt.n_dropped} outlying dropped" if tgt.n_dropped else "")
                      + f"; single best level {best_f[1]:.4g})")
        note = f"{note}; {target_txt}" if note else target_txt
        if parked is None:
            # A FAILED run (rig test 2026-09-28, Lukas). Until then this parked
            # by the step counter at the fine walk's best and reported "OK",
            # with only an event saying so -- on the rig up to 3.7 focal depths
            # off (5 times in ~90 runs) while a waiting scan measured on. The
            # counter is exactly what this routine exists NOT to trust. As a
            # failure, af_error says so, describe's wait.check makes a scan
            # routine raise (its on_error decides), and _run_autofocus puts Z
            # back where the run started -- the failed-run rule.
            got = ("nothing measurable" if closest is None else
                   f"closest {100 * (score(closest) - score(m_goal)) / max(abs(m_goal), 1e-12):+.0f} %")
            raise AutofocusFailed(
                "park failed: never within park_tolerance",
                f"after 3 park walks the image never got back within {100 * tol:.0f} % "
                f"({tol_why}) of the fine walk's {target_txt} ({got}); not parked by the "
                f"step counter")
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

    @staticmethod
    def _coarse_step(m_prev: float, m_now: float, dz: float, r_ref: float | None,
                     coarse: float, max_step: float, rise: float) -> float | None:
        """one_way's AIMED coarse step for a size metric, or None (not aimable).

        The square root of every size metric is ~linear in the defocus far
        from focus (a radius; sigma = sqrt(sigma^2) is the hyperbola
        sqrt(sigma0^2 + c dz^2)), so the last two levels and the calibrated
        in-focus root r_ref tell how far focus still is. Aim at 0.7 of it.

        ONLY while the last step improved the metric by more than
        ``rise`` (rise_fraction): near the bottom the two levels read nearly
        the same, the slope is ~0 and "distance / slope" explodes -- the
        benchmark of 2026-09-29 caught it jumping the maximum AWAY from focus
        and ending 8.6 units off. There the walk takes plain coarse steps.
        """
        if r_ref is None:
            return None
        if not (np.isfinite(m_prev) and np.isfinite(m_now) and m_prev > 0 and m_now > 0):
            return None                        # nothing to aim with: the caller decides
        if m_now >= m_prev * (1.0 - max(0.0, rise)):
            return coarse                      # flat bottom (or noise): no aiming
        r_prev, r_now = np.sqrt(m_prev), np.sqrt(m_now)
        k = (r_prev - r_now) / max(abs(dz), 1e-9)      # radius per unit Z
        if k <= 0:
            return coarse
        remaining = max(0.0, r_now - r_ref) / k
        return float(np.clip(0.7 * remaining, coarse, max_step))

    # ------------------------------------------------------------------ #
    # Z STEP CALIBRATION by the camera (2026-09-28)
    # ------------------------------------------------------------------ #
    # WHY. The kim Z (PIA25, slip-stick) steps UP much smaller than DOWN: in
    # the D4sigma rig test the step counter at focus climbed from -1.5 to +344
    # um while the image stayed in focus. Every routine that goes "back to a
    # Z" by the counter -- the sweep's park, a failed run's return -- then
    # lands elsewhere. Lukas chose to MEASURE the two step sizes with the
    # camera (2026-09-28).
    #
    # HOW. sigma^2 of the spot (the D4sigma metric) is exactly a parabola in
    # the TRUE Z: sigma^2 = s0 + K (z - z0)^2. Walk Z up through focus in equal
    # COUNTER steps: the true Z advances s_up per counter unit, so in counter
    # units the parabola's curvature is K s_up^2. Walk down through focus the
    # same way: K s_down^2. Their ratio is (s_up / s_down)^2 -- K (the optics)
    # and z0 (where focus is on the counter, which drifts) drop out. What does
    # NOT come out is the absolute scale (a stage twice as coarse both ways
    # gives the same ratio), so the geometric mean of the two sizes is kept at
    # the stage's current step (or autofocus.zcal_step_um when that is known).
    #
    # Each walk is approached from beyond its start (so every counted level is
    # reached moving in the walk's direction -- the first steps after a
    # reversal are the unreliable ones) and ends by the IMAGE: once sigma^2 has
    # passed its minimum and climbed back to zcal_fit_window x that minimum.
    # Only levels within that window are fitted: far out the faint wings sink
    # below the camera's grey levels and sigma^2 reads low (rig: up to 50 % in
    # 8 bit). A poor fit (R^2), a minimum not bracketed, or no spot: REFUSED,
    # nothing written, Z back where it started -- no guess.
    def calibrate_z_steps(self) -> int:
        """Queue a Z step calibration; runs on the engine thread. Returns its NUMBER.

        Finished when status shows ``zcal_id`` == that number and not
        ``zcal_running``; ``zcal_state`` then says how it went ("OK").
        Needs a calibrated spot, roughly in focus, and an open-loop Z with a
        step counter (kim). Kill AF stops it (Z stays where it is).
        """
        with self._lock:
            self._zcal_id += 1
            self._zcal_request = {"kind": "zcal", "id": self._zcal_id}
            self._zcal_busy = True
            self._zcal_state = "queued"
            self._publish_zcal_locked()
            self._status.point_settled = False
            self._status.stable = False
            return self._zcal_id

    def _publish_zcal_locked(self) -> None:
        self._status.zcal_id = self._zcal_id
        self._status.zcal_running = self._zcal_busy
        self._status.zcal_state = self._zcal_state
        for k, v in self._zcal_result.items():
            setattr(self._status, k, v)

    def _zcal_finish(self, state: str) -> None:
        """End a run: state + not busy in ONE critical section (gotcha #28)."""
        with self._lock:
            self._zcal_busy = self._zcal_request is not None
            self._zcal_state = "queued" if self._zcal_busy else state
            self._publish_zcal_locked()
        self._z_target = None           # Z moved: a focus step starts from where it IS

    def get_zcal_curve(self) -> dict:
        """The last Z step calibration's two walks, for a plot: sigma^2 against
        the COUNTER (in the Z unit), and each fit's vertex."""
        return dict(self._zcal_curve)

    def _do_zcal(self, req) -> None:
        """Run one Z step calibration with the image loops PAUSED (as for AF)."""
        self._pause_image_loops()
        try:
            with self._exposure_for("Z step calibration"):
                self._run_zcal(req)
        finally:
            if self._zcal_state == "running":     # left early (shutdown)
                self._zcal_finish("stopped")
            self._pause_image_loops()
            self._stab_move_t = time.monotonic()

    def _live_frame(self) -> np.ndarray:
        """Grab and publish a frame while a routine owns the engine (the view
        keeps moving); raises _AutofocusKilled on Kill AF or shutdown."""
        if self._af_kill.is_set() or self._stop.is_set():
            raise _AutofocusKilled()
        g = self._grab_gray()
        try:
            zr = float(self.z.read_z())
        except Exception:
            zr = self._status.z_voltage
        with self._lock:
            self._last_frame = g
            self._status.frame_number += 1
            self._status.z_voltage = zr
        return g

    def _zcal_metric(self, gray) -> float:
        """sigma^2 (px^2) of the spot around the same centre as the live sizes
        (Spot.locate), measured on the full-depth frame when the camera
        delivers one; NaN = not measurable."""
        sp = self.cfg.spot
        src, top, _bits = self._spot_source(gray)
        centre, _loc = self._size_centre(src, top, (sp.ref_x, sp.ref_y))
        if centre is None:
            return float("nan")
        m = V.spot_second_moment(src, centre, sp, max_value=top)
        return float(m.sigma2) if m.ok else float("nan")

    def _run_zcal(self, req) -> None:
        af = self.cfg.autofocus
        z = self.z
        unit = self.z_unit()
        nan = float("nan")
        with self._lock:
            self._zcal_state = "running"
            self._zcal_result = {k: nan for k in ("zcal_ratio", "zcal_ratio_err", "zcal_r2_up",
                                                  "zcal_r2_down", "zcal_up_um", "zcal_down_um")}
            self._publish_zcal_locked()
        self._zcal_curve = {}
        need = ("counter_steps", "move_counter", "step_sizes", "set_step_sizes")
        why = None
        if not self.cfg.hardware.use_z:
            why = "failed: no Z"
        elif not all(callable(getattr(z, n, None)) for n in need):
            why = ("failed: needs an open-loop Z with a step counter (kim); "
                   "a closed-loop Z has nothing to calibrate")
        elif not self.cfg.spot.ref_set:
            why = "failed: needs a calibrated spot (Spot tab -> Calibrate spot)"
        if why:
            self._zcal_finish(why)
            self._emit("error", f"Z step calibration: {why}")
            return

        up0, down0 = (float(v) for v in z.step_sizes())
        per_step = 0.5 * (up0 + down0)             # the stage's own reading per step
        step = max(1e-9, abs(float(af.zcal_step_v)) / per_step)       # counter steps
        margin = max(2.0 * step, abs(float(af.approach_margin)) / per_step)
        max_n = abs(float(af.zcal_max_travel_v)) / per_step
        window = max(1.2, float(af.zcal_fit_window))
        n_side = max(2, int(af.zcal_min_side_levels))
        n_avg = max(1, int(af.zcal_averages))
        wait = getattr(z, "wait_settled", None)
        try:
            wait_takes_tick = wait is not None and "tick" in inspect.signature(wait).parameters
        except (TypeError, ValueError):
            wait_takes_tick = False
        settle_s = self.cfg.hardware.z_step_time_ms / 1000.0
        walks = {"up": ([], []), "down": ([], [])}

        def publish(fits=None):
            self._zcal_curve = {
                "unit": unit, "per_step": per_step,
                "up": {"z": [n * per_step for n in walks["up"][0]], "metric": list(walks["up"][1])},
                "down": {"z": [n * per_step for n in walks["down"][0]],
                         "metric": list(walks["down"][1])},
                "fits": fits or {}}

        def go(n: float) -> None:
            z.move_counter(float(n))
            if wait is not None:
                if wait_takes_tick:
                    wait(tick=self._live_frame)
                else:
                    wait()
            t_end = time.monotonic() + settle_s
            self._live_frame()
            while time.monotonic() < t_end:
                time.sleep(min(0.05, max(0.0, t_end - time.monotonic())))

        def measure() -> float:
            vals = [self._zcal_metric(self._live_frame()) for _ in range(n_avg)]
            vals = [v for v in vals if np.isfinite(v)]
            return float(np.mean(vals)) if vals else float("nan")

        def walk(d: float, start: float, name: str):
            """Levels from ``start`` in direction d until the image says the
            minimum is behind us (sigma^2 back up to window x its minimum)."""
            go(start - d * margin)                 # approach the first level moving in d
            go(start)
            ns, ms = walks[name]
            n = start
            while True:
                ns.append(n); ms.append(measure())
                publish()
                fin = [i for i, v in enumerate(ms) if np.isfinite(v)]
                if fin:
                    i_min = min(fin, key=lambda i: ms[i])
                    after = [i for i in fin if i > i_min]
                    if (len(after) >= n_side and fin[-1] == len(ms) - 1
                            and ms[-1] >= window * ms[i_min]):
                        return np.asarray(ns, float), np.asarray(ms, float)
                n += d * step
                if abs(n - start) > max_n:
                    raise AutofocusFailed(
                        f"failed: minimum not bracketed ({name} walk)",
                        f"sigma^2 did not pass a minimum and climb back to {window:g} x it "
                        f"within zcal_max_travel_v = {af.zcal_max_travel_v:g} {unit}: start "
                        f"closer to focus (below it) or widen the travel")
                go(n)

        def fit(ns, ms, name):
            """Parabola in COUNTER steps over the levels within the window."""
            ok = np.isfinite(ms)
            if ok.sum() < 2 * n_side + 1:
                raise AutofocusFailed(f"failed: too few measurable levels ({name} walk)")
            i_min = int(np.nanargmin(np.where(ok, ms, np.nan)))
            # the CONTIGUOUS run of levels around the minimum that stay within
            # the window: far out on the side where the light sits in a faint
            # outer ring, 8-bit sigma^2 reads low and can dip back INTO the
            # window (simulator: 2 Rayleigh ranges below focus) -- those levels
            # are not on the parabola and must not be fitted
            inside = ok & (ms <= window * ms[i_min])
            sel = np.zeros_like(ok)
            for rng in (range(i_min, -1, -1), range(i_min, len(ms))):
                for i in rng:
                    if not inside[i]:
                        break
                    sel[i] = True
            x = ns - ns[i_min]                      # centred: well-conditioned
            below, above = int((sel & (x < 0)).sum()), int((sel & (x > 0)).sum())
            if min(below, above) < n_side:
                raise AutofocusFailed(
                    f"failed: minimum not bracketed ({name} walk)",
                    f"only {below} level(s) before and {above} after the smallest sigma^2 "
                    f"within {window:g} x it (need {n_side} each side): start further below "
                    f"focus, or smaller zcal_step_v")
            p, cov = np.polyfit(x[sel], ms[sel], 2, cov=True)
            res = ms[sel] - np.polyval(p, x[sel])
            ss = float(((ms[sel] - ms[sel].mean()) ** 2).sum())
            r2 = 1.0 - float((res ** 2).sum()) / ss if ss > 0 else 0.0
            if p[0] <= 0:
                raise AutofocusFailed(f"failed: no parabola ({name} walk opens downwards)")
            if r2 < float(af.zcal_min_r2):
                raise AutofocusFailed(
                    f"failed: poor fit ({name} walk R^2 {r2:.3f} < {af.zcal_min_r2:g})",
                    "noise, a moving sample, or light other than the spot in the search region")
            vert = float(ns[i_min] - p[1] / (2.0 * p[0]))
            vmin = float(np.polyval(p, -p[1] / (2.0 * p[0])))
            return {"a": float(p[0]), "var_a": float(cov[0, 0]), "r2": r2,
                    "vertex": vert, "min": vmin, "n": int(sel.sum())}

        n_start = float(z.counter_steps())
        try:
            self._wait_xy_still(lambda: self._live_frame())
            # 1. UP through focus (from below), 2. DOWN through it (from above)
            ns_u, ms_u = walk(+1.0, n_start - abs(float(af.zcal_start_offset_v)) / per_step, "up")
            f_up = fit(ns_u, ms_u, "up")
            ns_d, ms_d = walk(-1.0, float(ns_u[-1]), "down")
            f_dn = fit(ns_d, ms_d, "down")
            # 3. the ratio, and the two sizes around the kept geometric mean
            q = math.sqrt(f_up["a"] / f_dn["a"])
            q_err = 0.5 * q * math.sqrt(f_up["var_a"] / f_up["a"] ** 2
                                        + f_dn["var_a"] / f_dn["a"] ** 2)
            g = float(af.zcal_step_um) if af.zcal_step_um > 0 else math.sqrt(up0 * down0)
            up, down = g * math.sqrt(q), g / math.sqrt(q)
            z.set_step_sizes(up, down)
            publish({"up": f_up, "down": f_dn})
            with self._lock:
                self._zcal_result = {"zcal_ratio": q, "zcal_ratio_err": q_err,
                                     "zcal_r2_up": f_up["r2"], "zcal_r2_down": f_dn["r2"],
                                     "zcal_up_um": up, "zcal_down_um": down}
            spread = abs(f_up["min"] - f_dn["min"]) / max(1e-12, min(f_up["min"], f_dn["min"]))
            if spread > 0.25:
                # the in-focus sigma^2 is a property of the beam, not of the
                # direction: very different minima = something moved meanwhile
                self._emit("warn", f"Z step calibration: the two walks' smallest sigma^2 "
                                   f"differ by {100 * spread:.0f} % ({f_up['min']:.1f} vs "
                                   f"{f_dn['min']:.1f} px2) -- check the result")
            # 4. back to focus BY THE IMAGE. The down walk ended below focus; its
            # vertex is where focus was on the counter moving DOWN. Going UP the
            # steps are q x bigger, so focus is 1/q of that counter distance
            # away. Approach it from below and stop on the image.
            n_end = float(ns_d[-1])
            n_focus = n_end + (f_dn["vertex"] - n_end) / q
            note = self._zcal_park(go, measure, n_end, n_focus, step,
                                   min(f_up["min"], f_dn["min"]))
            self._zcal_finish("OK")
            self._emit("info", (
                f"Z step calibration: ratio up/down {q:.3f} +- {q_err:.3f} (R^2 up "
                f"{f_up['r2']:.4f}, down {f_dn['r2']:.4f}; {f_up['n']}/{f_dn['n']} levels); "
                f"steps written: up {up:.5g}, down {down:.5g} {unit}/step (geometric mean "
                f"{g:.5g}{', given' if af.zcal_step_um > 0 else ', kept'}); {note}"))
        except _AutofocusKilled:
            self._zcal_finish("killed")
            self._emit("warn", "Z step calibration killed: nothing written, Z left where it was")
        except Exception as exc:
            # the failed-run rule: Z back where it started, by the counter
            # (nothing else is known -- the calibration is what was missing)
            back = ""
            if not self._stop.is_set():
                try:
                    here = float(z.counter_steps())
                    if n_start < here:
                        go(n_start - margin)           # from below, like every park
                    go(n_start)
                    back = f"; Z back to the start (counter {n_start * per_step:.3f} {unit})"
                except Exception as exc2:
                    back = f"; could NOT return Z: {exc2!r}"
            state = exc.state if isinstance(exc, AutofocusFailed) else f"failed: {type(exc).__name__}"
            self._zcal_finish(state)
            self._emit("error", f"Z step calibration: {exc}{back}")

    def _zcal_park(self, go, measure, n_now, n_focus, step, goal) -> str:
        """Go UP to the focus the calibration predicts, and CHECK it on the image
        (sigma^2 within the d4sigma park tolerance of ``goal``). If the image
        disagrees, walk on up in half levels until it agrees; say how it ended.

        Why the prediction first: a walk that stops at the first level within
        tolerance stops EARLY on its approach side (sigma^2 is flat at the
        bottom: 4 % is ~0.27 Rayleigh ranges in the simulator); the predicted
        counter value is the parabola's vertex itself.
        """
        tol = self.cfg.autofocus.park_tolerance_d4sigma or self.cfg.autofocus.park_tolerance
        if n_focus > n_now:                        # below it, as planned: straight up
            go(n_focus)
            m = measure()
            if np.isfinite(m) and m <= (1.0 + tol) * goal:
                return (f"Z parked at the predicted focus, confirmed by the image "
                        f"(sigma^2 within {100 * tol:.0f} %)")
        n = max(n_now, n_focus) + 0.5 * step
        best, n_worse = None, 0
        limit = n_focus + 4.0 * step
        while n <= limit:
            go(n)
            m = measure()
            if np.isfinite(m) and m <= (1.0 + tol) * goal:
                return f"Z parked in focus by the image (sigma^2 within {100 * tol:.0f} %)"
            if np.isfinite(m) and (best is None or m < best):
                best, n_worse = m, 0
            elif best is not None:
                n_worse += 1
                if n_worse >= max(1, int(self.cfg.autofocus.rise_levels)):
                    break
            n += 0.5 * step
        self._emit("warn", "Z step calibration: the image never got back within the park "
                           "tolerance -- Z left near focus by the counter; run an autofocus")
        return "Z left NEAR focus by the counter (run an autofocus)"

    def park_tolerance(self) -> float:
        """The park tolerance for the focus metric in use (a fraction).

        The threshold-free sizes have their own (autofocus.park_tolerance_d4sigma
        / _relative, 0.04 from the rig test): sigma^2 is a parabola, flat at
        the bottom, so the generic 10 % parks ~0.3 Rayleigh ranges early. 0 in
        one of them = use the generic autofocus.park_tolerance.
        """
        af = self.cfg.autofocus
        # the squared sizes (encircled r86^2, the Gaussian's sigma^2) are flat
        # at the bottom like sigma^2: they share its tolerance (2026-09-29)
        own = {"spot_d4sigma": af.park_tolerance_d4sigma,
               "spot_encircled": af.park_tolerance_d4sigma,
               "spot_gauss": af.park_tolerance_d4sigma,
               "spot_relative": af.park_tolerance_relative}.get(af.mechanism, 0.0)
        return float(own) if own and own > 0 else float(af.park_tolerance)

    def effective_park_tolerance(self, target: float, noise: float) -> tuple[float, str]:
        """(tolerance as a fraction, why) for a park aiming at ``target``.

        The configured tolerance (park_tolerance()), but never tighter than
        ``park_noise_k`` x the fine walk's measured noise per level (rig
        2026-09-29: 4 % against ~1-2 % frame noise AND a biased-low target was
        unreachable). Capped at rise_fraction: the fine walk itself calls a
        level "worse" only beyond that, so a noise estimate bigger than it
        means the walk could not tell up from down either -- parking anywhere
        in a band that wide would hide that, not fix it.
        """
        af = self.cfg.autofocus
        base = self.park_tolerance()
        k = max(0.0, float(af.park_noise_k))
        frac = (abs(float(noise)) / abs(float(target))
                if np.isfinite(noise) and np.isfinite(target) and target else 0.0)
        tol, why = base, f"{100 * base:.0f} % configured"
        if k > 0 and frac > 0:
            why += f", noise {100 * frac:.1f} %/level"
            if k * frac > base:
                tol = k * frac
                why = (f"{k:g} x noise {100 * frac:.1f} %/level "
                       f"(configured {100 * base:.0f} %)")
        cap = max(base, float(af.rise_fraction))
        if tol > cap:
            tol = cap
            why += f", capped at rise_fraction {100 * cap:.0f} %"
        return float(tol), why

    # spot_area needs a SATURATED spot. The routine MINIMISES the fixed-threshold
    # area, which is right only while the spot is clipped at the camera's
    # maximum: then defocus only spreads the clipped plateau. An unsaturated
    # spot is the other way round -- defocus DIMS it below the threshold, so its
    # thresholded area is LARGEST at focus (rig test 2026-09-28: 0/15 good runs,
    # 14 parked 1.2 focal depths off). The direction is NOT flipped silently (a
    # spot can saturate at focus only, and on a camera nobody watches a flip
    # would be a surprise); the operator is told, with the metric to use.
    def _check_area_saturation(self) -> None:
        """After an autofocus run on spot_area: was the spot ever saturated?"""
        if self.cfg.autofocus.mechanism == "spot_area":
            seen, sat = self._af_area_sat
            self._note_area_saturation(seen, sat, "during the autofocus")

    def _note_area_saturation(self, seen: int, saturated: int, where: str) -> None:
        if seen <= 0:
            return
        if saturated > 0:
            self._af_hint = ""
            return
        self._af_hint = ("spot_area expects a SATURATED spot (its area is smallest at focus "
                         f"only then); this spot never saturated {where}, so its thresholded "
                         "area is LARGEST at focus -- use spot_d4sigma")
        if self.cfg.autofocus.mechanism == "spot_area":
            self._emit("warn", f"autofocus: {self._af_hint}")

    def _fit_rel_window(self) -> float | None:
        """For sigma^2 the parabola is the true curve, so the fit may use every
        level up to 2 x the smallest sigma^2 (|dz| up to ~1 Rayleigh range of
        the second-moment beam) instead of only +-2 levels -- more points, a
        better vertex. Not further: far out the spot is dim, its faint wings
        sink below the camera's noise and its 8-bit steps, and sigma^2 comes
        out too SMALL there (in the coherent simulator 15-50 % at 2-3 Rayleigh
        ranges on the side where the light sits in a faint outer ring) --
        with a 3 x window the sweep's vertex moved by 0.7 units, with 2 x by
        < 0.1."""
        return 2.0 if self.cfg.autofocus.mechanism == "spot_d4sigma" else None

    def _ref_size_root(self) -> float | None:
        """sqrt of the in-focus value of the current SIZE metric (from the spot
        calibration; 0 when not measured), or None for a non-size metric."""
        sp, mech = self.cfg.spot, self.cfg.autofocus.mechanism
        if mech == "spot_area":
            return float(np.sqrt(sp.ref_area)) if sp.ref_area > 0 else 0.0
        if mech == "spot_d4sigma":          # metric = sigma^2, D4sigma = 4 sigma
            return float(sp.ref_d4sigma_px) / 4.0 if sp.ref_d4sigma_px > 0 else 0.0
        if mech == "spot_relative":
            return float(np.sqrt(sp.ref_rel_area)) if sp.ref_rel_area > 0 else 0.0
        if mech == "spot_encircled":        # metric = r86^2, D86 = 2 r86
            return float(sp.ref_d86_px) / 2.0 if sp.ref_d86_px > 0 else 0.0
        if mech == "spot_gauss":            # metric = the fitted sigma^2
            return float(np.sqrt(sp.ref_gauss_sigma2)) if sp.ref_gauss_sigma2 > 0 else 0.0
        return None

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
        if af.mechanism in SIZE_FOCUS:
            # No threshold (2026-09-28/29): the second moment sigma^2 (px^2; for
            # a coherent beam EXACTLY a parabola in Z, so the sweep's parabola
            # fit is the right model), the area above a fraction of the spot's
            # own peak, the encircled-energy radius^2, a Gaussian fit's sigma^2,
            # or the spot's peak (maximised). Around the same centre as the
            # live sizes (Spot.locate: calibrated, or located in the region).
            if not sp.ref_set:
                raise RuntimeError(f"the {af.mechanism} focus metric needs a calibrated spot "
                                   f"(Spot tab -> Calibrate spot)")
            src, top, _bits = self._spot_source(gray)      # 12 bit when there is one
            centre, _loc = self._size_centre(src, top, (sp.ref_x, sp.ref_y))
            if centre is None:
                return float("nan")
            nan = float("nan")
            if af.mechanism == "spot_relative":
                r = V.spot_relative_area(src, centre, sp, max_value=top)
                self._note_saturation(r.saturated)
                return float(r.area) if r.ok else nan
            if af.mechanism == "spot_peak":
                r = V.spot_relative_area(src, centre, sp, max_value=top)
                self._note_saturation(r.saturated)
                return float(r.peak) if r.ok else nan
            m = V.spot_second_moment(src, centre, sp, max_value=top)
            self._note_saturation(m.saturated)
            if af.mechanism == "spot_d4sigma":
                return float(m.sigma2) if m.ok else nan
            if af.mechanism == "spot_encircled":
                e = V.spot_encircled(src, centre, sp, max_value=top, moments=m)
                return float(e.r_px) ** 2 if e.ok else nan     # r86^2: a size, like sigma^2
            g = V.spot_gauss_fit(src, centre, sp, max_value=top, moments=m)
            return float(g.sigma2) if g.ok else nan
        if af.mechanism == "spot_area":
            if not sp.ref_set:
                raise RuntimeError("the spot_area focus metric needs a calibrated spot "
                                   "(Spot tab -> Calibrate spot)")
            det = V.find_spot(gray, sp.thr_lower, sp.thr_upper, sp.bright_spot,
                              sp.lookup_region_px, (sp.ref_x, sp.ref_y), sp.min_area_px,
                              self._max_area_px(gray.shape, (sp.ref_x, sp.ref_y)),
                              sp.reject_border, sp.search_shape,
                              sp.lookup_region_y_px, symmetric=bool(sp.symmetric))
            if det.found:
                # was this level's spot SATURATED? (spot_area only makes sense
                # for a saturated spot -- see _note_area_saturation)
                x, y, w, h = det.bbox
                box = gray[max(0, y):y + h, max(0, x):x + w]
                if box.size:
                    self._af_area_sat[0] += 1
                    self._af_area_sat[1] += int(box.max() >= 255 if sp.bright_spot
                                                else box.min() <= 0)
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
        self._reset_tracking(tpl_center, tpl_center)    # a new main template: no backups
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
        self._reset_tracking()               # found afresh, wherever it is in the frame
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

    # -- saving next to a measurement (scan routines) ---------------------- #
    # Two actions a scan can run before / after (or during) a measurement, with
    # folder and name filled in by scan-core from where the data is written
    # (describe: defaults "{data_dir}" / "{data_stem}_{moment}_..."). Each
    # writes its file AND a readable .json beside it: what the camera, the
    # pattern, the scan array and the stage were at that moment.
    def _out_path(self, folder: str, name: str, default_name: str, ext: str) -> str:
        folder = (folder or "").strip() or self.cfg.image.save_path or "captures"
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", (name or "").strip()).strip("_") \
            or default_name
        os.makedirs(folder, exist_ok=True)
        base = os.path.join(folder, name)
        path, k = base + ext, 2
        while os.path.exists(path):          # never overwrite an earlier save
            path, k = f"{base}_{k}{ext}", k + 1
        return path

    def _record(self, kind: str, path: str) -> str:
        st = self.status()
        rec = {
            "kind": kind,
            "file": os.path.basename(path),
            "saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "scanning": self._reference_meta(),
            "status": {k: getattr(st, k) for k in (
                "frame_number", "spot_x", "spot_y", "spot_calibrated", "spot_found",
                "spot_area", "match_found", "match_score", "template_x", "template_y",
                "template_w", "template_h", "selected_index_x", "selected_index_y",
                "stage_x", "stage_y", "stage_steps_x", "stage_steps_y", "xy_step_unit",
                "z_voltage", "z_unit", "stage_ok") if hasattr(st, k)},
        }
        info = os.path.splitext(path)[0] + ".json"
        with open(info, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2, default=str)
        return info

    def save_scan_pattern(self, folder: str = "", name: str = "") -> dict:
        """Save the pattern (template + backups, with the WHOLE scan-array
        definition inside, as Load pattern restores it) plus a .json record."""
        if self.reference is None:
            raise RuntimeError("no pattern to save: draw or load one first")
        path = self._out_path(folder, name, time.strftime("pattern_%Y%m%d_%H%M%S"), ".png")
        self.save_pattern(path)
        return {"path": path, "info": self._record("pattern", path)}

    def save_picture(self, folder: str = "", name: str = "") -> dict:
        """Save the current camera frame (full resolution) plus a .json record."""
        frame = self.latest_frame()
        if frame is None:
            raise RuntimeError("no camera frame yet")
        path = self._out_path(folder, name, time.strftime("camera_%Y%m%d_%H%M%S"), ".png")
        if not cv2.imwrite(path, frame):
            raise RuntimeError(f"could not write {path}")
        info = self._record("picture", path)
        self._emit("info", f"picture -> {path}")
        return {"path": path, "info": info}

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
        with self._lock:
            self._tracking_on = bool(on)
        if not on:
            # forget the position: switched on again, the pattern is searched in
            # the WHOLE frame (the sample may have been moved meanwhile)
            self._reset_tracking()
        return bool(on)

    def set_stabilize(self, on: bool) -> bool:
        if on:
            self._laser_goto = False          # one target at a time (set_laser_target)
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

    def set_laser_target(self, x: float | None = None, y: float | None = None) -> list:
        """Put the laser at (x, y) um from the main template -- a point ON the
        sample, not on the stage -- and keep correcting until it is there.

        Either coordinate may be None = keep the current target (or, with no
        target yet, stay where the laser is now), so a scan can step y and fly
        x as two axes. Needs a tracked template and a calibrated spot, like the
        stabiliser. The loop lets go once the laser is within stable_radius_um;
        `laser_settled` then says so.
        """
        if self.reference is None or not self._tracking_on:
            raise RuntimeError("no template tracked: load or capture a pattern and "
                               "switch tracking on first")
        if not self.cfg.spot.ref_set:
            raise RuntimeError("no spot position: calibrate the spot first (Spot tab)")
        st = self._status
        old = self._laser_target
        here = (st.spot_from_template_x_um, st.spot_from_template_y_um)

        def pick(v, i):
            if v is not None:
                v = float(v)
                if not math.isfinite(v):
                    raise ValueError("laser target must be a finite number")
                return v
            if old is not None:
                return old[i]
            if not math.isfinite(here[i]):
                raise RuntimeError("the laser position is not known (template lost?)")
            return float(here[i])

        target = (pick(x, 0), pick(y, 1))
        if self._stabilize_on:
            # Holding an ARRAY point and placing the laser at a free point are
            # two targets for one stage: the stabiliser would pull the laser
            # back as soon as the placement let go (between two fly rows, say).
            self._stabilize_on = False
            self._settled_for = None
            self._emit("info", "stabiliser off: the laser is now placed by coordinate")
        self._commit_laser_target(target)
        return list(target)

    def _commit_laser_target(self, target: tuple) -> None:
        """Hand a new target to the placement loop, atomically w.r.t. _laser_step."""
        with self._laser_lock:
            self._laser_done = False          # before the target: no frame may pair
            self._laser_target = target       # the new target with an old "done"
            self._avg_buf.clear()
            self._laser_goto = True

    def cancel_laser_target(self) -> None:
        """Stop the placement loop where it is (the target is kept)."""
        self._laser_goto = False

    def stream_delays(self) -> dict:
        """How late the laser position is: half the span of the running
        average (running_avg_frames), whose centroid lags the newest frame."""
        n = max(1, int(self.cfg.camera.running_avg_frames))
        fps = float(self._status.fps or self.cfg.camera.frame_rate or 0.0)
        d = 0.5 * (n - 1) / fps if fps > 0 else 0.0
        return {c: d for c in STREAM_CHANNELS}

    def move_xy(self, x_um: float, y_um: float) -> list:
        x, y = self._clamp_xy(float(x_um), float(y_um))
        # An absolute move makes the last JOG target meaningless: a jog within
        # Z_STEP_FRESH_S would otherwise start from it and undo this move
        # (deep cleaning 2026-09-28). step_xy sets its own target again after.
        self._xy_target = None
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
        self._xy_target = None              # not a jog: see move_xy
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
        self._xy_target = None              # not a jog: see move_xy
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

    def _max_area_px(self, shape, centre) -> int:
        """The area limit of the fixed-threshold spot search around ``centre``:
        Spot.max_area_px, or (0, the default since 2026-09-29 late) a quarter
        of the search region -- the saturated laser with its rings is several
        thousand px^2 at the working exposure (vision.AUTO_MAX_AREA_FRACTION)."""
        sp = self.cfg.spot
        reg = V.search_region(centre, sp.lookup_region_px, sp.lookup_region_y_px,
                              sp.search_shape, (shape[1], shape[0]))
        return int(V.max_area_limit(sp, reg))

    def _note_exposure_change(self) -> None:
        """An exposure write is about to happen: the next frames were (partly)
        exposed with the old value. Nothing located on them may count toward
        the spot-offset warning. n_drop + 2 = the frames the camera still
        delivers with the old exposure (autofocus.exposure_discard_frames,
        # VERIFY on the IDS) + 1 grabbed before the write + 1 margin."""
        self._offset_run = 0
        self._offset_quiet = max(0, int(self.cfg.autofocus.exposure_discard_frames)) + 2

    def set_camera_feature(self, name: str, value):
        """Set one camera parameter; returns its read-back value."""
        if name == "ExposureTime":
            self._note_exposure_change()           # BEFORE the write: no frame slips past
        self.backend.set_feature(name, value)
        try:
            v = self.backend.get_feature(name)
        except Exception:
            v = value
        if name == "ExposureTime":
            try:
                # keep cfg in step with the camera, so a set_config round trip of
                # the whole config (the Settings dialog) does not look like a change
                self.cfg.camera.exposure_us = float(v)
                self._exposure_known = float(v)
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

    def z_resolution(self) -> float | None:
        """The Z device's smallest step in the Z unit (kim: one step in um), or
        None when it has no fixed quantum (a piezo voltage) or cannot say now."""
        fn = getattr(self.z, "resolution", None)
        if not callable(fn):
            return None
        try:
            r = float(fn())
        except Exception:
            return None
        return r if math.isfinite(r) and r > 0 else None

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
        """Find the spot ANYWHERE in the frame, averaged over ``frames`` new
        frames, and store it as THE spot position used from now on.

        Spot.calib_mode (2026-09-29, Lukas): "saturated" = the fixed
        threshold's largest blob (a flat top -- the old method, now refused
        when a second blob is nearly as large); "unsaturated" = the brightest
        smoothed blob, refined to its intensity centroid (a peaked spot the
        threshold cannot select). Searched ONLY in the search region around
        the previous calibration (the frame centre for the first one) -- Lukas
        2026-09-29: "Always look for the laser spot in the safety area around
        the laser only!" (a whole-frame search, the day's first version, locked
        onto a saturated illuminated block on the rig). A spot outside the
        region is refused with the way out: enlarge the region or set the
        position by hand. Of several spot-like candidates the one nearest the
        previous calibration is taken, and the per-frame positions must agree
        within Spot.calib_max_jitter_px (else refused: something competes). With
        Spot.calib_at_af_exposure and autofocus.exposure_us set, the frames are
        taken at the autofocus exposure (switched and restored like an
        autofocus run) -- for a spot that saturates at the working exposure.

        The spread of the centroid (``jitter``) is stored with it: the noise
        floor of this calibration. The result says where the spot was found and
        how far that is from the previous calibration. Blocks for about
        frames / fps (capped by ``timeout_s``, which must stay below the
        client's REQ timeout). Refuses with the reason (RuntimeError) when no
        frame gave a clean single candidate.
        """
        frames = max(2, int(frames))
        # Not during an autofocus: it owns the engine, so no frame is ANALYSED
        # while it runs -- but its live view still advances frame_number on the
        # last analysed snapshot. The loop below then took that one stale
        # frame N times and stored it as a perfect calibration (jitter 0)
        # (deep cleaning 2026-09-28).
        if self._af_busy or self._zcal_busy:
            raise RuntimeError("autofocus / Z calibration is running: calibrate the spot "
                               "once it has finished")
        sp = self.cfg.spot
        mode = sp.calib_mode if sp.calib_mode in CALIB_MODES else "saturated"
        at_af = bool(sp.calib_at_af_exposure) and float(self.cfg.autofocus.exposure_us or 0) > 0
        # "at the autofocus exposure" asked for, but there is none: it used to
        # calibrate at the working exposure WITHOUT A WORD (2026-09-29 late) --
        # a saturated spot where the user expected an unsaturated one. Say it
        # (event + reply); calibrating anyway is still the right thing: the
        # user pressed Calibrate.
        warning = ""
        if bool(sp.calib_at_af_exposure) and not at_af:
            warning = ("no autofocus exposure set (autofocus.exposure_us = 0) -- calibrated "
                       "at the working exposure")
            self._emit("warn", f"spot calibration: {warning}")
        before = (float(sp.ref_x), float(sp.ref_y)) if sp.ref_set else None
        xs, ys, areas, whys, sizes, sats = [], [], [], [], [], []
        skip = 2 + (max(0, int(self.cfg.autofocus.exposure_discard_frames)) if at_af else 0)
        with self._exposure_for("spot calibration", engine=False) if at_af \
                else _nothing():
            # the frame in flight when the request came (and, at the AF
            # exposure, the frames still exposed with the old value) do not count
            start = last = self._status.frame_number
            t_end = time.monotonic() + timeout_s
            while len(xs) < frames and time.monotonic() < t_end:
                s = self._status
                if s.frame_number != last:
                    last = s.frame_number
                    if self._af_busy:       # an autofocus started meanwhile: stale frames
                        raise RuntimeError("autofocus started during the spot calibration; "
                                           "calibrate again once it has finished")
                    if s.frame_number >= start + skip:
                        with self._lock:
                            gray = self._last_frame
                        if gray is not None:
                            self._calib_frame(gray, mode, xs, ys, areas, whys, sizes, sats,
                                              before)
                time.sleep(0.005)
        if len(xs) < 2:
            why = max(set(whys), key=whys.count) if whys else "no new frames from the camera"
            raise RuntimeError(f"spot calibration ({mode} spot) refused: {why}")
        # The per-frame positions must AGREE. Rig 2026-09-29 (saturated mode,
        # working exposure): the threshold caught the laser in some frames and
        # a saturated illuminated block in others; the mean (a point on
        # neither) was stored with "+/- 277 px". A real spot sits still to a
        # fraction of a pixel (the unsaturated calibration: +/- 0.12 px).
        jitter = float(np.hypot(np.std(xs), np.std(ys)))
        max_jit = float(sp.calib_max_jitter_px or 0.0)
        if max_jit > 0 and jitter > max_jit:
            span = float(np.hypot(np.ptp(xs), np.ptp(ys)))
            raise RuntimeError(
                f"spot calibration ({mode} spot) refused: the found position jumps by "
                f"{span:.0f} px between frames (+/- {jitter:.1f} px, allowed "
                f"{max_jit:g} px): another bright object competes with the laser in the "
                f"search region -- calibrate as an 'unsaturated spot' (at the autofocus "
                f"exposure), raise the threshold, shrink the search region, or set the "
                f"position by hand")
        sp.ref_x, sp.ref_y = float(np.mean(xs)), float(np.mean(ys))
        sp.ref_area = float(np.mean(areas))
        sp.ref_jitter_px = jitter

        # the threshold-free sizes of the same frames, at the found position
        # (0 = not measurable): the one_way autofocus aims with them, the Spot
        # tab's trace draws them as the reference line
        def mean_of(k):
            v = [d[k] for d in sizes if np.isfinite(d[k])]
            return float(np.mean(v)) if v else 0.0
        sp.ref_d4sigma_px = mean_of("d4")
        sp.ref_rel_area = mean_of("rel")
        sp.ref_d86_px = mean_of("d86")
        sp.ref_gauss_sigma2 = mean_of("g2")
        sp.ref_set = True
        # Wait until a frame STARTED after this has been published (at most
        # 0.5 s): the snapshot otherwise still shows the old position for a
        # frame or two, and a client acting on status right after the reply
        # (capture the pattern, read spot_x) would see the old one.
        f_now = self._status.frame_number
        t_pub = time.monotonic() + 0.5
        while self._status.frame_number < f_now + 2 and time.monotonic() < t_pub:
            time.sleep(0.005)
        moved = (float(np.hypot(sp.ref_x - before[0], sp.ref_y - before[1]))
                 if before is not None else float("nan"))
        # calibrated IN FOCUS: an unsaturated spot here makes spot_area the
        # wrong focus metric (see _note_area_saturation)
        self._note_area_saturation(len(sats), sum(sats), "at the spot calibration")
        self._emit("info", f"spot calibrated ({mode} spot"
                           + (", at the autofocus exposure" if at_af else "")
                           + f"): ({sp.ref_x:.2f}, {sp.ref_y:.2f}) px "
                           f"+/- {sp.ref_jitter_px:.2f} px, "
                           + (f"{moved:.1f} px from the previous calibration, "
                              if np.isfinite(moved) else "first calibration, ")
                           + f"area {sp.ref_area:.0f} px2, D4sigma {sp.ref_d4sigma_px:.1f} px, "
                           f"relative area {sp.ref_rel_area:.0f} px2, {len(xs)} frames")
        return {"x": sp.ref_x, "y": sp.ref_y, "area": sp.ref_area,
                "d4sigma_px": sp.ref_d4sigma_px, "rel_area": sp.ref_rel_area,
                "d86_px": sp.ref_d86_px, "gauss_sigma2": sp.ref_gauss_sigma2,
                "jitter_px": sp.ref_jitter_px, "frames": len(xs), "mode": mode,
                "moved_px": moved, "previous": list(before) if before else None,
                "at_af_exposure": at_af, "warning": warning}

    def _calib_frame(self, gray, mode, xs, ys, areas, whys, sizes, sats, before=None) -> None:
        """One frame of calibrate_spot: find, then measure the sizes there.

        ``before`` = the previous calibration: the search region is around it
        (around the frame centre when there is none) and, of several
        spot-like candidates, the one nearest it is taken -- the same for
        every frame, so that frames cannot alternate between two objects."""
        sp = self.cfg.spot
        src, top, _bits = self._spot_source(gray)
        # the saturated method thresholds the 8-bit frame (the threshold's
        # meaning); the unsaturated one works on the full-depth frame if any
        r = (V.find_spot_for_calibration(gray, sp, "saturated", calib_xy=before)
             if mode == "saturated" else
             V.find_spot_for_calibration(src, sp, "unsaturated", max_value=top,
                                         calib_xy=before))
        if not r.ok:
            whys.append(r.why)
            return
        xs.append(r.x); ys.append(r.y)
        nan = float("nan")
        mom = V.spot_second_moment(src, (r.x, r.y), sp, max_value=top)
        rel = V.spot_relative_area(src, (r.x, r.y), sp, max_value=top)
        enc = V.spot_encircled(src, (r.x, r.y), sp, max_value=top, moments=mom)
        gau = V.spot_gauss_fit(src, (r.x, r.y), sp, max_value=top, moments=mom)
        areas.append(float(r.area) if mode == "saturated" else (rel.area if rel.ok else 0.0))
        sizes.append({"d4": mom.d4sigma if mom.ok else nan, "rel": rel.area if rel.ok else nan,
                      "d86": enc.d_px if enc.ok else nan, "g2": gau.sigma2 if gau.ok else nan})
        sats.append(bool(mom.saturated or rel.saturated))

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
        sp.ref_d4sigma_px = sp.ref_rel_area = 0.0      # nothing measured
        sp.ref_d86_px = sp.ref_gauss_sigma2 = 0.0
        self._emit("info", f"spot position entered by hand: ({x:.2f}, {y:.2f}) px")
        return {"x": x, "y": y, "area": 0.0, "d4sigma_px": 0.0, "rel_area": 0.0,
                "jitter_px": 0.0, "frames": 0}

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
        self._temporal_deep = deque(maxlen=max(1, self.cfg.camera.running_avg_frames))
        self._apply_exposure_if_changed()

    def _apply_exposure_if_changed(self) -> None:
        """Write camera.exposure_us to the camera ONLY when the user changed it.

        A set_config carries the whole group, usually with the exposure we
        adopted at start; that must not count as a request. Only a value that
        differs from what the camera last reported is written -- that is the
        user asking for a new exposure. Before start() nothing is written.
        """
        known = getattr(self, "_exposure_known", None)
        if known is None:            # not started, or the camera has no exposure
            return
        try:
            want = float(self.cfg.camera.exposure_us or 0.0)
        except (TypeError, ValueError):
            return
        if want <= 0 or abs(want - known) <= 1e-6 * max(1.0, abs(known)):
            return
        try:
            self.set_camera_feature("ExposureTime", want)
        except Exception as exc:
            self._emit("warn", f"could not apply exposure {want:g} us: {exc}")
            self.cfg.camera.exposure_us = known     # cfg keeps telling the truth

    def _emit(self, level: str, msg: str) -> None:
        try:
            self._on_event(level, msg)
        except Exception:
            pass


def status_to_dict(s: CameraStatus) -> dict:
    return asdict(s)
