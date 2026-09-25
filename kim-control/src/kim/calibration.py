"""Measure the XY px/step table from CAMERA FEEDBACK (runs inside the kim brain).

Started by :meth:`Kim.start_px_calibration` on its own thread, so the service
keeps answering (the camera service is itself a client of kim, and it keeps
polling us while we poll it). The camera is reached over ZeroMQ with the raw
wire contract only -- ``status``, ``get_config``, ``get_frame`` -- and no
``camera`` import, the same decoupling the camera uses toward kim.

------------------------------------------------------------------------------
The procedure, and the reason for each step
------------------------------------------------------------------------------
For every voltage, axis (X, Y), repeat, and direction (+, -):

1. PRELOAD: one move in the run's direction, grown until the image visibly
   shifts. Slip-stick actuators behave differently for the first steps after a
   reversal, so those steps are thrown away -- and the preload's shift gives a
   first px/step estimate to size the run.
2. RUN: reference frame, then ``increments`` equal moves totalling about
   ``target_frac`` of the smaller frame dimension (~240 px on 1936x1096), each
   followed by locating the reference's centre patch in the new frame
   (normalised template matching; it survives shifts of hundreds of pixels,
   unlike phase correlation, which silently lost lock on this rig).
3. FIT: least-squares slope of image shift vs ACTUAL signed steps moved (read
   back from the controller, so a leash clamp cannot fake the numbers), through
   the origin, as a 2-vector column. The residual flags non-linearity.
4. RETURN HOME, closed loop on the camera: + then - is NOT a round trip on an
   asymmetric axis (Y left ~35 um per 800 steps), so after each axis the stage
   is driven back until the very first frame's patch is where it started.
   Without this, five voltages of repeats would walk the sample off the field.

Finally: restore the voltages, return home, VALIDATE with a few commanded image
moves at the original voltage (commanded vs measured), and save.

Safety: refuses to start while the camera's stabiliser, autofocus or continuous
focus is on; other motion commands are refused while it runs (see Kim); STOP
aborts it; every move goes through the brain's clamps and leash.
"""

from __future__ import annotations

import base64
import threading
import time
from datetime import datetime

import numpy as np
import zmq

from . import pxcal


class CalibrationAborted(Exception):
    pass


class CameraLink:
    """Raw REQ client for the camera service (status / get_config / get_frame)."""

    def __init__(self, host: str = "127.0.0.1", cmd_port: int = 5563, timeout_ms: int = 8000):
        self.host, self.cmd_port, self.timeout_ms = host, cmd_port, timeout_ms
        self._ctx = zmq.Context.instance()
        self._req = None

    def _make(self):
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        self._req.connect(f"tcp://{self.host}:{self.cmd_port}")

    def rpc(self, **req) -> dict:
        if self._req is None:
            self._make()
        try:
            self._req.send_json(req)
            reply = self._req.recv_json()
        except zmq.Again:
            self._req.close(0)
            self._req = None
            raise TimeoutError(f"camera service at {self.host}:{self.cmd_port} did not answer "
                               f"{req.get('cmd')!r}")
        if not reply.get("ok", False):
            raise RuntimeError(f"camera {req.get('cmd')}: {reply.get('error')}")
        return reply

    def status(self) -> dict:
        return self.rpc(cmd="status")["status"]

    def config(self) -> dict:
        return self.rpc(cmd="get_config")["config"]

    def frame(self) -> np.ndarray:
        import cv2  # lazy: only the calibration needs OpenCV
        png = base64.b64decode(self.rpc(cmd="get_frame")["png_b64"])
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise RuntimeError("camera returned no frame yet")
        return img

    def close(self):
        if self._req is not None:
            self._req.close(0)
            self._req = None


def image_context(cam_status: dict, cam_config: dict, frame_shape) -> dict:
    """What the px/step table is only valid for. The camera passes the same dict
    with every move_image_px, built by the same rules (see camera's remote_kim)."""
    img = (cam_config or {}).get("image", {})
    return {
        "objective": cam_status.get("objective_name", ""),
        "rotation_deg": float(img.get("rotation_deg", 0.0)),
        "symmetry": str(img.get("symmetry", "none")),
        "clip": [bool(img.get("clip_enabled", False))] + [
            int(img.get(k, 0)) for k in ("clip_left", "clip_top", "clip_right", "clip_bottom")],
        "frame": [int(frame_shape[0]), int(frame_shape[1])],
    }


def choose_patch(gray: np.ndarray, patch: int, margin: float = 0.0,
                 avoid: list | None = None) -> tuple[int, int]:
    """Top-left (x0, y0) of the best square ``patch`` to track in ``gray``.

    Best = most texture (sum of gradient magnitude), among windows that
      * keep their CENTRE at least ``margin`` px from every frame edge, so the
        feature stays in view while the stage moves it by up to ``margin``;
      * do not overlap any ``avoid`` disc (cx, cy, r) -- the LASER SPOT: it is
        fixed in the image, so a patch containing it keeps "matching" at zero
        shift and biases every px/step low (the lab rig at 63x, 2026-09-14, had
        the spot in the middle of the old fixed centre patch).
    Falls back to no margin, then to the frame centre, if nothing qualifies.
    """
    import cv2
    h, w = gray.shape[:2]
    patch = int(patch)
    g = gray.astype(np.float32)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    # sum over every patch x patch window, indexed by its TOP-LEFT corner
    integral = cv2.integral(cv2.magnitude(gx, gy))
    score = (integral[patch:, patch:] - integral[:-patch, patch:]
             - integral[patch:, :-patch] + integral[:-patch, :-patch])   # (h-p+1, w-p+1)
    ys, xs = np.mgrid[0:score.shape[0], 0:score.shape[1]]
    half = patch / 2.0
    clear = np.ones_like(score, bool)
    for cx, cy, r in (avoid or []):
        # distance from the disc centre to the nearest point of each window
        dx = np.maximum(np.maximum(xs - cx, cx - (xs + patch)), 0)
        dy = np.maximum(np.maximum(ys - cy, cy - (ys + patch)), 0)
        clear &= (dx * dx + dy * dy) > r * r
    # relax the margin gradually: a patch hugging the frame edge would carry its
    # feature out of view during the run
    for frac in (1.0, 0.85, 0.7, 0.55, 0.4, 0.0):
        m = float(margin) * frac
        ok = clear & (xs + half >= m) & (ys + half >= m) \
            & (xs + half <= w - m) & (ys + half <= h - m)
        if ok.any():
            idx = np.argmax(np.where(ok, score, -1.0))
            return int(xs.flat[idx]), int(ys.flat[idx])
    return w // 2 - patch // 2, h // 2 - patch // 2


def template_patch(cam_status: dict, frame_shape, avoid: list,
                   min_side: int = 24) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """The camera's user-drawn TEMPLATE as a patch, if it can be used.

    Returns ((x0, y0), (w, h)) when a template is loaded, tracking is on and it
    matches in the current frame; None when there is no usable template (the
    caller then picks a patch automatically). Raises if the template covers an
    ``avoid`` disc -- a template around the laser spot would track the laser.
    """
    s = cam_status or {}
    if not (s.get("pattern_loaded") and s.get("tracking_on") and s.get("match_found")):
        return None
    tw, th = int(s.get("template_w", 0)), int(s.get("template_h", 0))
    if tw < min_side or th < min_side:
        return None
    h, w = frame_shape[:2]
    x0 = int(round(float(s["template_x"]) - tw / 2.0))
    y0 = int(round(float(s["template_y"]) - th / 2.0))
    x0, y0 = max(0, min(w - tw, x0)), max(0, min(h - th, y0))
    for cx, cy, r in avoid:
        nx, ny = min(max(cx, x0), x0 + tw), min(max(cy, y0), y0 + th)
        if (nx - cx) ** 2 + (ny - cy) ** 2 <= r * r:
            raise RuntimeError("the camera template contains the laser spot, which does not "
                               "move with the sample -- draw it around a sample feature away "
                               "from the spot, or clear it to let the calibration choose")
    return (x0, y0), (tw, th)


def locate(ref: np.ndarray, cur: np.ndarray, patch,
           origin: tuple[int, int] | None = None) -> tuple[float, float, float]:
    """Shift (dx, dy) in px of the image content from ``ref`` to ``cur``, and the
    match score (TM_CCOEFF_NORMED peak, 1.0 = identical). The tracked patch is
    ``ref[y0:y0+ph, x0:x0+pw]``: ``patch`` is a side (square) or (pw, ph), and
    ``origin`` = (x0, y0), default centred in the frame."""
    import cv2
    h, w = ref.shape
    pw, ph = (patch, patch) if np.isscalar(patch) else (int(patch[0]), int(patch[1]))
    if origin is None:
        y0, x0 = h // 2 - ph // 2, w // 2 - pw // 2
    else:
        x0, y0 = int(origin[0]), int(origin[1])
    res = cv2.matchTemplate(cur, ref[y0:y0 + ph, x0:x0 + pw], cv2.TM_CCOEFF_NORMED)
    _, score, _, (mx, my) = cv2.minMaxLoc(res)

    def sub(vm, v0, vp):             # parabola through the peak -> sub-pixel
        d = vm - 2.0 * v0 + vp
        return 0.0 if d == 0 else 0.5 * (vm - vp) / d

    fx = mx + (sub(res[my, mx - 1], res[my, mx], res[my, mx + 1]) if 0 < mx < res.shape[1] - 1 else 0.0)
    fy = my + (sub(res[my - 1, mx], res[my, mx], res[my + 1, mx]) if 0 < my < res.shape[0] - 1 else 0.0)
    return float(fx - x0), float(fy - y0), float(score)


class PxCalibrator:
    """One calibration run. Construct, then ``run()`` (blocking) on a thread."""

    def __init__(self, brain, camera: CameraLink, voltages=(85, 95, 105, 115, 125),
                 repeats: int = 2, increments: int = 4, target_frac: float = 0.22,
                 settle_s: float = 0.7, min_score: float = 0.8, home_tol_px: float = 3.0,
                 move_timeout_s: float = 60.0, progress=lambda msg: None):
        self.brain = brain
        self.camera = camera
        self.voltages = [float(v) for v in voltages]
        self.repeats = max(1, int(repeats))
        self.increments = max(2, int(increments))
        self.target_frac = target_frac
        self.settle_s = settle_s
        self.min_score = min_score
        self.home_tol_px = home_tol_px
        self.move_timeout_s = move_timeout_s
        self.progress = progress
        self._abort = threading.Event()
        self._home = None
        self._patch = 360
        self._origin = None        # top-left of the tracked patch (choose_patch)

    # -- control ----------------------------------------------------------------
    def abort(self) -> None:
        self._abort.set()

    def _check_abort(self):
        if self._abort.is_set():
            raise CalibrationAborted("calibration aborted")

    # -- primitives ---------------------------------------------------------------
    def _frame(self) -> np.ndarray:
        time.sleep(self.settle_s)     # let the camera deliver post-move frames
        return self.camera.frame()

    def _move(self, axis: int, delta: int) -> int:
        """Relative move; blocks until arrived. Returns the ACTUAL steps moved."""
        self._check_abort()
        be = self.brain.backend
        before = int(be.read_position(axis))
        target = self.brain._calibration_move(axis, before + int(delta))
        t_end = time.monotonic() + self.move_timeout_s
        while True:
            self._check_abort()
            pos = int(be.read_position(axis))
            if not be.is_moving(axis) and pos == target:
                return pos - before
            if time.monotonic() > t_end:
                be.stop(axis)
                raise TimeoutError(f"axis {'XY'[axis]} did not reach {target} (at {pos})")
            time.sleep(0.03)

    def _locate(self, ref, cur, what: str):
        dx, dy, score = locate(ref, cur, self._patch, self._origin)
        if score < self.min_score:
            raise RuntimeError(f"lost the image while {what} (match score {score:.2f} < "
                               f"{self.min_score}); is the camera focused on a textured area?")
        return dx, dy

    def _room_px(self, direction) -> float:
        """How far the tracked patch can move along ``direction`` (a px vector)
        before any part of it leaves the frame."""
        h, w = self._home.shape[:2]
        pw, ph = (self._patch, self._patch) if np.isscalar(self._patch) else self._patch
        x0, y0 = self._origin if self._origin is not None else (w // 2 - pw // 2, h // 2 - ph // 2)
        ux, uy = float(direction[0]), float(direction[1])
        n = float(np.hypot(ux, uy))
        if n < 1e-9:
            return float(self.target_px)
        ux, uy = ux / n, uy / n
        limits = []
        if ux > 1e-3:
            limits.append((w - (x0 + pw)) / ux)
        elif ux < -1e-3:
            limits.append(x0 / -ux)
        if uy > 1e-3:
            limits.append((h - (y0 + ph)) / uy)
        elif uy < -1e-3:
            limits.append(y0 / -uy)
        return float(max(0.0, min(limits))) if limits else float(self.target_px)

    # -- measurement ----------------------------------------------------------------
    def _run_direction(self, axis: int, sign: int) -> np.ndarray:
        """Preload, then measure one direction's column (px per signed step)."""
        name = "XY"[axis] + ("+" if sign > 0 else "-")
        # 1. preload, grown until the image shifts visibly -> first estimate
        probe, est = 20, None
        ref = self._frame()
        while est is None:
            moved = self._move(axis, sign * probe)
            dx, dy = self._locate(ref, self._frame(), f"preloading {name}")
            shift = float(np.hypot(dx, dy))
            if shift >= 8.0 or probe >= 800:
                est = max(shift, 1e-3) / max(abs(moved), 1)
            else:
                ref = self.camera.frame()
                probe *= 2
        # 2. the run. Its length is capped by the ROOM the tracked patch has in
        # the direction the image moves: on the lab rig at 63x (2026-09-14) a run
        # sized only by the preload estimate overshot -- the first slip-stick
        # steps after a reversal are small, so the estimate was low -- pushed the
        # patch past the frame edge, and the match was lost (score 0.57).
        room = self._room_px((dx, dy))
        run_px = min(self.target_px, 0.85 * room)
        total = int(np.clip(run_px / est, 2 * self.increments, 20000))
        inc = max(1, total // self.increments)
        ref = self._frame()
        steps, shifts, cum = [0.0], [(0.0, 0.0)], 0
        for k in range(self.increments):
            cum += self._move(axis, sign * inc)
            dx, dy = self._locate(ref, self._frame(), f"measuring {name} ({k + 1}/{self.increments})")
            steps.append(float(cum))
            shifts.append((dx, dy))
            # ...and by what is MEASURED: one more increment of this size would
            # leave the frame, so stop with the points we have
            per_inc = float(np.hypot(dx, dy)) / (k + 1)
            if k + 1 < self.increments and np.hypot(dx, dy) + per_inc > 0.95 * room:
                self.progress(f"{name}: run stopped after {k + 1} increments "
                              f"({np.hypot(dx, dy):.0f} px of {room:.0f} px room)")
                break
        s = np.array(steps)
        d = np.array(shifts)
        # 3. slope through the origin, per image axis
        col = np.array([np.dot(s, d[:, 0]), np.dot(s, d[:, 1])]) / np.dot(s, s)
        resid = float(np.max(np.hypot(*(d - np.outer(s, col)).T)))
        px_um = getattr(self, "_px_um", 0.0)
        size = f"{np.hypot(*col):.4f} px/step"
        if px_um:
            size = f"{np.hypot(*col) * px_um * 1000:.1f} nm/step ({size})"
        self.progress(f"{name}: {size}, image dir "
                      f"{np.degrees(np.arctan2(col[1], col[0])):+.1f} deg, fit residual {resid:.1f} px")
        return col

    def _return_home(self, cols: dict | None, axis: int | None = None) -> float:
        """Drive back until the first frame's patch is home again (closed loop)."""
        for _ in range(8):
            dx, dy = self._locate(self._home, self._frame(), "returning home")
            err = float(np.hypot(dx, dy))
            if err <= self.home_tol_px:
                return err
            want = np.array([-dx, -dy])          # undo the content shift
            if axis is not None:
                # only this axis is known yet: project onto its column
                c_plus, c_minus = cols[f"{'XY'[axis]}+"], cols[f"{'XY'[axis]}-"]
                c = c_plus if np.dot(want, c_plus) >= 0 else c_minus
                s = float(np.dot(want, c) / np.dot(c, c))
                if round(s) == 0:
                    return err
                self._move(axis, int(round(s)))
            else:
                sx, sy = pxcal.solve_steps(want, cols)
                if round(sx) == 0 and round(sy) == 0:
                    return err
                if round(sx):
                    self._move(0, int(round(sx)))
                if round(sy):
                    self._move(1, int(round(sy)))
        return err

    # -- the whole procedure ----------------------------------------------------
    def run(self) -> pxcal.PxCalibration:
        cam = self.camera.status()
        # remembered so the progress lines can report micrometres, not only the
        # image pixels the fit is done in
        self._px_um = float(cam.get("pixel_size_x", 0.0) or 0.0)
        busy = [k for k in ("stabilize_on", "af_running", "continuous_focus_on") if cam.get(k)]
        if busy:
            raise RuntimeError(f"switch off on the camera first: {', '.join(busy)}")
        cfg = self.brain.cfg
        orig_v = [float(cfg.motion.voltage_x), float(cfg.motion.voltage_y)]

        self._home = self.camera.frame()
        h, w = self._home.shape
        self._patch = int(min(360, h // 3, w // 3))
        self.target_px = self.target_frac * min(h, w)
        cam_cfg = self.camera.config()
        context = image_context(cam, cam_cfg, self._home.shape)
        # Track a textured patch that stays clear of the (image-fixed) laser spot.
        avoid = []
        spot = (cam_cfg or {}).get("spot", {})
        if spot.get("ref_set"):
            r_spot = np.sqrt(max(float(spot.get("ref_area", 0.0)), 0.0) / np.pi)
            avoid.append((float(spot["ref_x"]), float(spot["ref_y"]),
                          max(40.0, 3.0 * r_spot + 10.0)))     # spot plus its rings
        drawn = template_patch(cam, self._home.shape, avoid)
        if drawn is not None:
            # The user's template: keep runs short enough that it stays in view.
            self._origin, self._patch = drawn
            (x0, y0), (pw, ph) = drawn
            room = min(x0, y0, w - (x0 + pw), h - (y0 + ph))
            self.target_px = float(max(40.0, min(self.target_px, room - 10)))
            source = f"the camera TEMPLATE ({pw}x{ph} px at ({x0}, {y0}))"
        else:
            self._origin = choose_patch(self._home, self._patch,
                                        margin=self.target_px + self._patch / 2, avoid=avoid)
            source = (f"an automatically chosen {self._patch} px patch at {self._origin}"
                      + (f", clear of the laser spot at ({avoid[0][0]:.0f}, {avoid[0][1]:.0f})"
                         if avoid else ""))
        self.progress(f"camera {w}x{h}, objective '{context['objective']}', tracking {source}, "
                      f"runs of ~{self.target_px:.0f} px")

        table, spread = {}, {}
        be = self.brain.backend
        start_steps = [int(be.read_position(0)), int(be.read_position(1))]
        try:
            for vi, v in enumerate(self.voltages):
                self.brain._calibration_voltage(0, v)
                self.brain._calibration_voltage(1, v)
                per_dir = {d: [] for d in pxcal.DIRS}
                for axis in (0, 1):
                    for rep in range(self.repeats):
                        self.progress(f"[{vi + 1}/{len(self.voltages)}] {v:g} V, axis "
                                      f"{'XY'[axis]}, repeat {rep + 1}/{self.repeats}")
                        for sign in (+1, -1):
                            per_dir[f"{'XY'[axis]}{'+' if sign > 0 else '-'}"].append(
                                self._run_direction(axis, sign))
                        partial = {d: np.mean(c, axis=0) for d, c in per_dir.items() if c}
                        err = self._return_home(partial, axis=axis)
                        self.progress(f"home within {err:.1f} px")
                key = f"{v:g}"
                table[key] = {d: np.mean(c, axis=0).tolist() for d, c in per_dir.items()}
                spread[key] = {d: np.std(c, axis=0).tolist() for d, c in per_dir.items()}
        except CalibrationAborted:
            raise                          # STOP means stop: do not move again
        except Exception:
            # A failure left the stage wherever the run was. Go back to the start
            # step counts -- open loop, so only roughly on an asymmetric axis, but
            # the sample does not stay walked off (lab rig 2026-09-14: X was left
            # 270 steps out).
            for axis in (0, 1):
                try:
                    self.brain._calibration_move(axis, start_steps[axis])
                except Exception:
                    pass
            self.progress(f"failed: stage sent back to its start counts {start_steps}")
            raise
        finally:
            for axis in (0, 1):
                try:
                    self.brain._calibration_voltage(axis, orig_v[axis])
                except Exception:
                    pass

        cal = pxcal.PxCalibration(
            table=table, spread=spread, context=context,
            pixel_size_um=float(cam.get("pixel_size_x", 0.0)),
            step_rate=float(cfg.motion.rate_x),
            created=datetime.now().isoformat(timespec="seconds"))

        # home at the ORIGINAL voltages, then validate commanded image moves there
        cols = pxcal.columns_at(cal, *orig_v)
        self._return_home(cols)
        cal.validation = self._validate(cols)
        self._return_home(cols)
        return cal

    def _validate(self, cols) -> list:
        t = 0.5 * self.target_px
        out = []
        for want in ((t, 0.0), (-t, 0.0), (0.0, t), (0.0, -t), (0.7 * t, 0.7 * t), (-0.7 * t, -0.7 * t)):
            self._check_abort()
            ref = self._frame()
            sx, sy = pxcal.solve_steps(want, cols)
            if round(sx):
                self._move(0, int(round(sx)))
            if round(sy):
                self._move(1, int(round(sy)))
            dx, dy = self._locate(ref, self._frame(), "validating")
            err = float(np.hypot(dx - want[0], dy - want[1]))
            out.append({"commanded_px": list(want), "measured_px": [dx, dy], "error_px": err,
                        "error_pct": 100.0 * err / float(np.hypot(*want))})
            self.progress(f"check: asked ({want[0]:+.0f}, {want[1]:+.0f}) px, got "
                          f"({dx:+.1f}, {dy:+.1f}) px -> error {err:.1f} px")
        return out
