"""Camera-frame calibration: the maths, and the whole procedure against a fake camera.

The fake camera renders a fixed random texture shifted by the TRUE image
displacement of a simulated stage whose columns are rotated 90 deg against the
image, asymmetric on Y, slightly crosstalking, and voltage-dependent -- the
shape of what the lab rig measured on 2026-09-13. The calibration must recover
those columns from pictures alone.
"""

from __future__ import annotations

import base64
import threading
import time

import numpy as np
import pytest
import zmq

cv2 = pytest.importorskip("cv2")

from kim import pxcal  # noqa: E402
from kim.backends.sim import SimKim  # noqa: E402
from kim.config import Config  # noqa: E402
from kim.kim import Kim  # noqa: E402


def true_columns(volts: float) -> dict:
    """px per signed step: X moves the image UP, Y moves it RIGHT (like the rig),
    Y 35 % asymmetric, slight crosstalk (the + columns are 90.19 deg apart),
    step size growing with voltage."""
    g = 1.0 + 0.01 * (volts - 85.0)          # +40 % from 85 to 125 V
    return {
        "X+": np.array([0.004, -0.30]) * g,
        "X-": np.array([0.006, -0.29]) * g,
        "Y+": np.array([0.48, 0.008]) * g,
        "Y-": np.array([0.36, 0.005]) * g,
    }


# --------------------------------------------------------------------------- #
# maths
# --------------------------------------------------------------------------- #
def test_solve_steps_inverts_rotated_asymmetric_columns():
    cols = true_columns(85)
    for want in [(100, 0), (-100, 0), (0, 80), (0, -80), (60, -40), (-60, 40), (-25, -25)]:
        sx, sy = pxcal.solve_steps(want, cols)
        got = (cols["X+" if sx >= 0 else "X-"] * sx + cols["Y+" if sy >= 0 else "Y-"] * sy)
        assert got == pytest.approx(want, abs=1e-6)


def test_columns_interpolate_between_voltages_and_hold_outside():
    cal = pxcal.PxCalibration(table={
        "85": {d: c.tolist() for d, c in true_columns(85).items()},
        "125": {d: c.tolist() for d, c in true_columns(125).items()},
    })
    mid = pxcal.columns_at(cal, 105, 105)
    for d in pxcal.DIRS:
        assert mid[d] == pytest.approx(true_columns(105)[d], rel=1e-9)
    low = pxcal.columns_at(cal, 60, 200)                  # clamped both ends
    assert low["X+"] == pytest.approx(true_columns(85)["X+"])
    assert low["Y+"] == pytest.approx(true_columns(125)["Y+"])


def test_geometry_reports_rotation_asymmetry_and_crosstalk():
    g = pxcal.axis_geometry(true_columns(85))
    assert g["X"]["image_dir_deg"] == pytest.approx(-89.2, abs=0.5)   # up
    assert g["Y"]["image_dir_deg"] == pytest.approx(1.0, abs=0.5)     # right
    assert g["Y"]["asymmetry"] == pytest.approx(0.48 / 0.36, rel=0.01)
    # atan2(-0.30, 0.004) = -89.24 deg, atan2(0.008, 0.48) = +0.95 deg -> 90.19 apart
    assert g["non_orthogonality_deg"] == pytest.approx(0.19, abs=0.01)


# --------------------------------------------------------------------------- #
# the procedure, end to end
# --------------------------------------------------------------------------- #
class TrueStage(SimKim):
    """SimKim with instant moves that also integrates the TRUE image shift."""

    def __init__(self, cfg, reversal_steps=0, reversal_gain=1.0):
        super().__init__(cfg)
        self.true_px = np.zeros(2)
        self.lock = threading.Lock()
        # slip-stick after a reversal: the first `reversal_steps` steps in the new
        # direction move only `reversal_gain` of a normal step (what the rig does)
        self.reversal_steps, self.reversal_gain = reversal_steps, reversal_gain
        self._last_dir = [0, 0]

    def move_to(self, axis, position_steps):
        with self.lock:
            delta = int(position_steps) - int(round(self._advance(axis)))
            if axis < 2 and delta:
                sgn = 1 if delta > 0 else -1
                d = "XY"[axis] + ("+" if delta > 0 else "-")
                eff = float(abs(delta))
                if self._last_dir[axis] and sgn != self._last_dir[axis] and self.reversal_steps:
                    slow = min(abs(delta), self.reversal_steps)
                    eff = slow * self.reversal_gain + (abs(delta) - slow)
                self._last_dir[axis] = sgn
                self.true_px = self.true_px + true_columns(self._volt[axis])[d] * sgn * eff
            super().move_to(axis, position_steps)
            self._pos[axis] = self._target[axis]          # arrive instantly
            self._moving[axis] = False


class FakeCamera:
    """Serves status / get_config / get_frame; the frame follows TrueStage."""

    def __init__(self, stage: TrueStage, size=(360, 480), laser=None):
        self.stage = stage
        self.h, self.w = size
        self.laser = laser        # (x, y, radius): a saturated spot FIXED in the image
        self.template = None      # (cx, cy, w, h): a user-drawn template, tracked + matched
        self.blank_after = None   # serve a featureless frame after this many frames
        self.frames_served = 0
        rng = np.random.default_rng(1)
        tex = cv2.GaussianBlur(rng.random((1600, 1600), np.float32), (0, 0), 2.5)
        self.tex = cv2.normalize(tex, None, 0, 255, cv2.NORM_MINMAX)
        self.x0, self.y0 = 800 - self.w // 2, 800 - self.h // 2
        self.stabilize_on = False
        ctx = zmq.Context.instance()
        self.sock = ctx.socket(zmq.REP)
        self.port = self.sock.bind_to_random_port("tcp://127.0.0.1")
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def frame(self):
        self.frames_served += 1
        if self.blank_after is not None and self.frames_served > self.blank_after:
            return np.full((self.h, self.w), 90, np.uint8)      # e.g. the sample drifted off
        dx, dy = self.stage.true_px
        m = np.float32([[1, 0, dx - self.x0], [0, 1, dy - self.y0]])
        img = cv2.warpAffine(self.tex, m, (self.w, self.h), flags=cv2.INTER_LINEAR)
        img = img.astype(np.uint8)
        if self.laser is not None:                # does NOT move with the stage
            x, y, r = self.laser
            for k in range(4, 0, -1):             # a few diffraction rings
                cv2.circle(img, (x, y), r + 6 * k, 200, 2)
            cv2.circle(img, (x, y), r, 255, -1)
        return img

    def _serve(self):
        poller = zmq.Poller()
        poller.register(self.sock, zmq.POLLIN)
        while not self._stop.is_set():
            if not dict(poller.poll(50)):
                continue
            req = self.sock.recv_json()
            cmd = req.get("cmd")
            if cmd == "status":
                rep = {"ok": True, "status": {
                    "stabilize_on": self.stabilize_on, "af_running": False,
                    "continuous_focus_on": False, "pixel_size_x": 0.413,
                    "objective_name": "20x test"}}
                if self.template is not None:
                    cx, cy, tw, th = self.template
                    rep["status"].update(pattern_loaded=True, tracking_on=True, match_found=True,
                                         template_x=float(cx), template_y=float(cy),
                                         template_w=tw, template_h=th)
            elif cmd == "get_config":
                spot = {"ref_set": False}
                if self.laser is not None:
                    x, y, r = self.laser
                    spot = {"ref_set": True, "ref_x": float(x), "ref_y": float(y),
                            "ref_area": float(np.pi * r * r)}
                rep = {"ok": True, "config": {"image": {"rotation_deg": 0.0, "symmetry": "none",
                                                        "clip_enabled": False},
                                              "spot": spot}}
            elif cmd == "get_frame":
                ok, buf = cv2.imencode(".png", self.frame())
                rep = {"ok": True, "png_b64": base64.b64encode(buf.tobytes()).decode()}
            else:
                rep = {"ok": False, "error": f"unknown {cmd}"}
            self.sock.send_json(rep)

    def close(self):
        self._stop.set()
        self.thread.join(timeout=1.0)
        self.sock.close(0)


@pytest.fixture
def rig(tmp_path):
    cfg = Config()
    cfg.calibration.px_file = str(tmp_path / "px.json")
    stage = TrueStage(cfg)
    brain = Kim(stage, cfg)
    brain.start()
    cam = FakeCamera(stage)
    yield brain, stage, cam
    brain.shutdown()
    cam.close()


def _wait_done(brain, timeout=120.0):
    t0 = time.monotonic()
    while brain.calibration_running():
        assert time.monotonic() - t0 < timeout, "calibration did not finish"
        time.sleep(0.05)


def test_calibration_recovers_true_columns_and_moves_the_image(rig):
    brain, stage, cam = rig
    assert brain.status().px_calibrated is False
    with pytest.raises(RuntimeError, match="no camera px/step calibration"):
        brain.move_image_px(10, 0)

    brain.start_px_calibration("127.0.0.1", cam.port, voltages=[85, 125], repeats=1,
                               settle_s=0.0)
    _wait_done(brain)
    st = brain.status()
    assert st.px_calibrated, st.calib_progress
    assert st.calib_progress.startswith("done"), st.calib_progress

    cal = pxcal.load(brain.px_file())
    for v in (85, 125):
        for d in pxcal.DIRS:
            measured = np.array(cal.table[f"{v:g}"][d])
            true = true_columns(v)[d]
            assert np.hypot(*(measured - true)) < 0.03 * np.hypot(*true), (v, d, measured, true)
    assert cal.context["objective"] == "20x test"
    assert max(c["error_px"] for c in cal.validation) < 3.0
    # it went home: the image content is back where it started
    assert np.hypot(*stage.true_px) < 4.0

    # and a commanded image move lands where asked, at an interpolated voltage
    brain.set_voltage(0, 105)
    brain.set_voltage(1, 105)
    before = stage.true_px.copy()
    brain.move_image_px(-60.0, 45.0, context=cal.context)
    assert stage.true_px - before == pytest.approx([-60.0, 45.0], abs=1.5)


def test_patch_avoids_the_laser_and_keeps_its_margin():
    from kim.calibration import choose_patch

    # a frame shaped like the lab camera's (wide), laser near the middle
    rng = np.random.default_rng(3)
    g = cv2.GaussianBlur(rng.random((600, 1000), np.float32), (0, 0), 2.5)
    g = cv2.normalize(g, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    cv2.circle(g, (500, 260), 20, 255, -1)        # the laser: the strongest "texture"
    x0, y0 = choose_patch(g, 200, margin=232, avoid=[(500, 260, 64)])
    # clear of the disc...
    nx, ny = min(max(500, x0), x0 + 200), min(max(260, y0), y0 + 200)
    assert np.hypot(nx - 500, ny - 260) > 64
    # ...and its centre at least the margin from every edge
    assert 232 <= x0 + 100 <= 1000 - 232 and 232 <= y0 + 100 <= 600 - 232
    # without the avoid disc it would sit on the laser
    x1, y1 = choose_patch(g, 200, margin=232)
    assert x1 <= 500 <= x1 + 200 and y1 <= 260 <= y1 + 200


def test_calibration_is_not_biased_by_a_fixed_laser_spot(tmp_path):
    """The laser stays put while the sample moves; tracking a patch that contains
    it would drag every measured shift towards zero."""
    cfg = Config()
    cfg.calibration.px_file = str(tmp_path / "px.json")
    stage = TrueStage(cfg)
    brain = Kim(stage, cfg)
    brain.start()
    cam = FakeCamera(stage, size=(600, 1000), laser=(500, 260, 18))
    try:
        brain.start_px_calibration("127.0.0.1", cam.port, voltages=[85], repeats=1, settle_s=0.0)
        _wait_done(brain)
        st = brain.status()
        assert st.calib_progress.startswith("done"), st.calib_progress
        cal = pxcal.load(brain.px_file())
        for d in pxcal.DIRS:
            measured, true = np.array(cal.table["85"][d]), true_columns(85)[d]
            assert np.hypot(*(measured - true)) < 0.03 * np.hypot(*true), (d, measured, true)
    finally:
        brain.shutdown()
        cam.close()


def _laser_rig(tmp_path, **stage_kw):
    cfg = Config()
    cfg.calibration.px_file = str(tmp_path / "px.json")
    stage = TrueStage(cfg, **stage_kw)
    brain = Kim(stage, cfg)
    brain.start()
    return brain, FakeCamera(stage, size=(600, 1000), laser=(500, 260, 18))


RS, RG = 400, 0.1
TPL = (150, 330, 120, 90)


def test_room_px_is_the_distance_to_the_frame_edge_along_the_motion():
    from types import SimpleNamespace

    from kim.calibration import PxCalibrator

    cal = PxCalibrator(brain=SimpleNamespace(), camera=None)
    cal._home = np.zeros((600, 1000), np.uint8)
    cal._patch, cal._origin, cal.target_px = (120, 90), (90, 285), 132.0
    assert cal._room_px((-5, 0)) == pytest.approx(90)            # left: x0
    assert cal._room_px((+5, 0)) == pytest.approx(1000 - 210)    # right: w - (x0 + pw)
    assert cal._room_px((0, -1)) == pytest.approx(285)           # up: y0
    assert cal._room_px((1, 1)) == pytest.approx(min(790, 225) * np.sqrt(2))
    assert cal._room_px((0, 0)) == pytest.approx(132.0)          # no direction: the target


def test_runs_stay_inside_the_frame_despite_small_post_reversal_steps(tmp_path):
    """The 63x failure: a template near the frame edge, and a low step estimate
    after a reversal made the run overshoot and carry the patch out of the frame."""
    brain, cam = _laser_rig(tmp_path, reversal_steps=RS, reversal_gain=RG)
    cam.template = TPL                          # close to the left frame edge: little room
    try:
        brain.start_px_calibration("127.0.0.1", cam.port, voltages=[85], repeats=2, settle_s=0.0)
        _wait_done(brain)
        assert brain.status().calib_progress.startswith("done"), brain.status().calib_progress
    finally:
        brain.shutdown()
        cam.close()


def test_a_failed_run_sends_the_stage_back_to_its_start(tmp_path):
    brain, cam = _laser_rig(tmp_path)
    stage = brain.backend
    start = [stage.read_position(0), stage.read_position(1)]
    cam.blank_after = 12                         # lose the image part-way through X
    try:
        brain.start_px_calibration("127.0.0.1", cam.port, voltages=[85], repeats=1, settle_s=0.0)
        _wait_done(brain)
        assert brain.status().calib_progress.startswith("failed"), brain.status().calib_progress
        assert [stage.read_position(0), stage.read_position(1)] == start
        assert brain.cfg.motion.voltage_x == 85.0
    finally:
        brain.shutdown()
        cam.close()


def test_calibration_tracks_the_user_drawn_template(tmp_path):
    brain, cam = _laser_rig(tmp_path)
    cam.template = (300, 330, 120, 90)          # a sample feature left of the laser
    msgs = []
    brain._on_event = lambda level, m: msgs.append(m)
    try:
        brain.start_px_calibration("127.0.0.1", cam.port, voltages=[85], repeats=1, settle_s=0.0)
        _wait_done(brain)
        assert brain.status().calib_progress.startswith("done"), brain.status().calib_progress
        assert any("camera TEMPLATE (120x90 px at (240, 285))" in m for m in msgs), msgs[:3]
        cal = pxcal.load(brain.px_file())
        for d in pxcal.DIRS:
            measured, true = np.array(cal.table["85"][d]), true_columns(85)[d]
            assert np.hypot(*(measured - true)) < 0.03 * np.hypot(*true), (d, measured, true)
    finally:
        brain.shutdown()
        cam.close()


def test_a_template_over_the_laser_is_refused(tmp_path):
    brain, cam = _laser_rig(tmp_path)
    cam.template = (500, 260, 100, 100)
    try:
        brain.start_px_calibration("127.0.0.1", cam.port, voltages=[85], repeats=1, settle_s=0.0)
        _wait_done(brain)
        assert "contains the laser spot" in brain.status().calib_progress
        assert not brain.status().px_calibrated
    finally:
        brain.shutdown()
        cam.close()


def test_mismatched_image_geometry_is_refused(rig):
    brain, stage, cam = rig
    brain.start_px_calibration("127.0.0.1", cam.port, voltages=[85], repeats=1, settle_s=0.0)
    _wait_done(brain)
    ctx = dict(pxcal.load(brain.px_file()).context, objective="50x")
    with pytest.raises(RuntimeError, match="geometry differs"):
        brain.move_image_px(10, 0, context=ctx)


def test_refuses_with_stabiliser_on_and_blocks_other_motion_while_running(rig):
    brain, stage, cam = rig
    cam.stabilize_on = True
    brain.start_px_calibration("127.0.0.1", cam.port, voltages=[85], repeats=1, settle_s=0.0)
    _wait_done(brain)
    assert "stabilize_on" in brain.status().calib_progress
    assert not brain.status().px_calibrated

    cam.stabilize_on = False
    brain.start_px_calibration("127.0.0.1", cam.port, voltages=[85, 95, 105], repeats=2,
                               settle_s=0.05)
    time.sleep(0.3)
    assert brain.calibration_running()
    with pytest.raises(RuntimeError, match="calibration is running"):
        brain.move_steps(0, 100)
    with pytest.raises(RuntimeError, match="calibration is running"):
        brain.set_voltage(1, 110)
    brain.stop_all()                                   # STOP aborts it
    _wait_done(brain, timeout=10.0)
    assert brain.status().calib_progress == "aborted"
    assert brain.cfg.motion.voltage_x == 85.0          # voltages restored
    brain.move_steps(0, 10)                            # motion allowed again


def test_bad_voltage_is_rejected_up_front(rig):
    brain, _, cam = rig
    with pytest.raises(ValueError, match="outside"):
        brain.start_px_calibration("127.0.0.1", cam.port, voltages=[85, 150])
