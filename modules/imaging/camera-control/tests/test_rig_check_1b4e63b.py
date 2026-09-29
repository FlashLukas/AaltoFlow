"""Rig check of 1b4e63b (lab PC, real KIM Z, 63x, 2026-09-29): three bugs.

1. The Z step calibration ended OFF focus (+184 counter steps, D4sigma 40
   against ~29.8 at focus) and still said OK. Reproduced in the simulator with
   a rig-like ratio (up steps SMALLER than down, ~0.75) whose up step grows
   after the walks (the rig's run-to-run variation is ~8 %): the counter
   prediction overshoots focus, and the old park could only walk further UP
   from there -- it never came back, and reported OK. Now the park walks up
   by the image, backs off below focus when it passed it, and when it still
   cannot get within the tolerance it SAYS so ("Z left off focus ... run an
   autofocus") instead of OK.
2. At the short autofocus exposure the fixed threshold does not see the spot
   (peak ~200 only at focus): the main view read "spot not seen: nothing above
   the threshold" during every autofocus. While the AF exposure is on, the
   threshold check is PAUSED and says so; the threshold-free sizes keep going.
3. One one_way autofocus ended with af_error "RuntimeError" and no text. The
   state now carries the exception's MESSAGE (truncated), for every failure
   path of autofocus and the Z step calibration, and an unexpected exception's
   traceback is printed once to the service console (stderr, ASCII).
"""

import math
import os
import time

import numpy as np
import pytest

import camera.backends.sim as S
from camera.backends.sim import SimCamera, SimSlipStickZ, SimXYStage
from camera.camera import Camera
from camera.config import Config

ZF = 30.0


def _wait(cond, timeout=180.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


def _rig(up=0.75, down=1.0, peak=None, calibrate=True):
    """The zcal rig of test_z_step_calibration, but RIG-LIKE: up steps smaller
    than down (the rig's width ratio 0.74-0.80)."""
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.hardware.z_step_time_ms = 0.0
    cfg.spot.lookup_region_px = 150
    cfg.spot.thr_lower = 150
    af = cfg.autofocus
    af.averages_per_level = 2
    af.zcal_step_v = 0.5
    af.zcal_start_offset_v = 6.0
    af.zcal_averages = 2
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = SimSlipStickZ(z0=ZF, z_focus=ZF, vmin=cfg.limits.z_min_v, vmax=cfg.limits.z_max_v,
                      up_gain=up, down_gain=down)
    kw = {} if peak is None else {"coherent_peak": peak}
    cam = SimCamera(xy, z, pixel_size_x_um=cfg.image.pixel_size_x_um,
                    pixel_size_y_um=cfg.image.pixel_size_y_um, spot_model="coherent",
                    noise=1.5, **kw)
    brain = Camera(cam, xy, z, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    if calibrate:
        brain.calibrate_spot(10)        # in focus
    return brain, cam, z, events


def _run_zcal(brain):
    n = brain.calibrate_z_steps()
    assert _wait(lambda: brain.status().zcal_id == n and not brain.status().zcal_running)
    return brain.status()


class _UpStepChanges:
    """After the walks (= when the calibration writes the step sizes) the up
    step becomes ``factor`` x what the walks measured -- the actuator's
    run-to-run variation, exaggerated so the prediction clearly misses."""

    def __init__(self, factor=None, stuck=False):
        self.factor, self.stuck = factor, stuck

    def __enter__(self):
        self.orig_set = SimSlipStickZ.set_step_sizes
        self.orig_move = SimSlipStickZ.move_counter
        factor, stuck = self.factor, self.stuck

        def sss(z, up, down):
            self.orig_set(z, up, down)
            if factor is not None:
                z.up_gain *= factor
            z._stuck = stuck

        def mc(z, steps):
            if getattr(z, "_stuck", False):
                return                            # the stage no longer moves
            self.orig_move(z, steps)
        SimSlipStickZ.set_step_sizes = sss
        SimSlipStickZ.move_counter = mc
        return self

    def __exit__(self, *exc):
        SimSlipStickZ.set_step_sizes = self.orig_set
        SimSlipStickZ.move_counter = self.orig_move


# --------------------------------------------------------------------------- #
# 1. the Z step calibration's park
# --------------------------------------------------------------------------- #
def test_the_sim_reproduces_the_rig_ratio_and_parks_when_nothing_changes():
    brain, cam, z, events = _rig()
    try:
        s = _run_zcal(brain)
        assert s.zcal_state == "OK", s.zcal_state
        assert s.zcal_ratio == pytest.approx(0.75, rel=0.03)
        assert abs(z.true_z() - ZF) < 0.6, z.true_z() - ZF
    finally:
        brain.shutdown()


@pytest.mark.parametrize("factor", [1.3, 1.6])
def test_an_overshooting_prediction_is_walked_back_by_the_image(factor):
    """The counter prediction lands ABOVE focus (the up step grew). The old
    park only walked on up, stopped when it got worse, left Z there
    (true error +2.4 at x1.3) and said OK. Now it backs off below focus and
    walks up until the image is within the tolerance."""
    with _UpStepChanges(factor):
        brain, cam, z, events = _rig()
        try:
            s = _run_zcal(brain)
            assert s.zcal_state == "OK", s.zcal_state
            assert abs(z.true_z() - ZF) < 0.8, z.true_z() - ZF
            # by the IMAGE: sigma^2 now within the tolerance of the minimum
            m = brain._zcal_metric(brain.latest_frame())
            m_ref = brain.get_zcal_curve()["m_ref"]
            assert m <= 1.10 * m_ref, (m, m_ref)          # one noisy frame: a little slack
            msg = [m_ for _l, m_ in events if "ratio up/down" in m_][-1]
            assert "by the image" in msg, msg
            assert "off focus" not in msg
        finally:
            brain.shutdown()


def test_a_park_that_cannot_reach_focus_says_so_instead_of_ok():
    """The stage stops moving after the walks: the image never gets back
    within the tolerance. The ratio is written (it was measured), but the
    state is NOT a silent OK: it says Z is off focus, by how much in D4sigma,
    and what to do."""
    with _UpStepChanges(stuck=True):
        brain, cam, z, events = _rig()
        try:
            s = _run_zcal(brain)
            assert s.zcal_state != "OK"
            assert s.zcal_state.startswith("Z left off focus"), s.zcal_state
            assert "D4sigma" in s.zcal_state and "run an autofocus" in s.zcal_state
            assert math.isfinite(s.zcal_ratio)            # the measurement itself stands
            warn = [m for lvl, m in events if lvl == "warn" and "off focus" in m]
            assert warn and "D4sigma" in warn[-1] and "run an autofocus" in warn[-1], events[-4:]
        finally:
            brain.shutdown()


def test_the_gui_shows_the_ratio_and_the_off_focus_warning():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from camera.apps.gui import MainWindow
    from camera.camera import CameraStatus
    app = QApplication.instance() or QApplication([])
    brain, cam, z, events = _rig(calibrate=False)
    try:
        win = MainWindow(brain, brain.cfg)
        s = CameraStatus()
        s.zcal_id, s.zcal_state = 1, ("Z left off focus (D4sigma 40.0 vs 29.8 px at focus) "
                                      "-- run an autofocus; steps written")
        s.zcal_ratio, s.zcal_ratio_err, s.zcal_up_um, s.zcal_down_um = 0.74, 0.01, 0.017, 0.023
        win._refresh_zcal(s)
        t = win.lab_zcal.text()
        assert "0.740" in t and "off focus" in t and "run an autofocus" in t
        win.close()
        app.processEvents()
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 2. the threshold check during the autofocus exposure
# --------------------------------------------------------------------------- #
def test_at_the_af_exposure_the_threshold_check_is_paused_not_not_seen():
    brain, cam, z, events = _rig(calibrate=False)
    try:
        sp = brain.cfg.spot
        sp.ref_set, sp.ref_x, sp.ref_y = True, 320.0, 240.0
        sp.thr_lower = 255                     # nothing reaches it: "nothing above"
        frame = brain.latest_frame()
        st = brain.status()
        brain._measure_spot(frame, st)
        assert not st.spot_found and st.spot_found_why_short   # the normal "why"
        assert "paused" not in st.spot_found_why_short
        brain._af_expo_active = True           # the autofocus exposure is on
        st = brain.status()
        brain._measure_spot(frame, st)
        assert st.spot_found_why_short == "threshold check paused (autofocus exposure)"
        assert "nothing above" not in st.spot_found_why
        assert "autofocus exposure" in st.spot_found_why
        assert math.isfinite(st.spot_sigma2_px2)       # the sizes keep running
        brain._af_expo_active = False
        brain._expo_hold_frames = 2            # frames still in flight after the restore
        st = brain.status()
        brain._measure_spot(frame, st)
        assert st.spot_found_why_short == "threshold check paused (autofocus exposure)"
    finally:
        brain._af_expo_active = False
        brain.shutdown()


def test_the_image_label_and_live_card_do_not_say_not_seen_while_paused():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from camera.apps.camera_view import CameraView
    from camera.apps.spot_tab import SpotTab
    from camera.camera import CameraStatus
    app = QApplication.instance() or QApplication([])
    s = CameraStatus()
    s.spot_found, s.spot_calibrated, s.spot_x, s.spot_y = False, True, 200.0, 200.0
    s.af_exposure_active = True
    s.spot_found_why_short = "threshold check paused (autofocus exposure)"
    s.spot_found_why = "threshold check paused: the frame is at the autofocus exposure"
    cfg = Config()
    cfg.spot.ref_set, cfg.spot.ref_x, cfg.spot.ref_y = True, 200.0, 200.0
    view = CameraView()
    view.resize(600, 600)
    view.set_frame(np.zeros((400, 400), np.uint8))
    texts = []
    real = view._label
    view._label = lambda p, at, text, colour, avoid=(): (texts.append(text),
                                                         real(p, at, text, colour, avoid))[1]
    view.set_show_spot_info(True)
    view.set_overlay(s, cfg)
    view.grab()
    assert "threshold check paused (autofocus exposure)" in texts, texts
    assert not any("not seen" in t for t in texts), texts
    tab = SpotTab(None, cfg, lambda lvl, msg: None, lambda: None)
    tab.update_status(s)
    assert "NOT seen" not in tab.lab_live.text()
    assert "paused" in tab.lab_live.text()
    view.close()
    tab.close()
    app.processEvents()


def test_during_an_autofocus_at_a_short_exposure_the_label_never_says_not_seen():
    """End to end: a dim AF exposure (the spot's peak below the fixed threshold
    off focus) -- every status frame during the run with the AF exposure on
    says "paused", none says "nothing above the threshold"."""
    brain, cam, z, events = _rig(up=1.0, down=1.0, peak=700.0)
    try:
        brain.cfg.spot.thr_lower = 250                   # the AF frames' peak is ~210 at best
        brain.cfg.autofocus.exposure_us = 1500.0         # 0.3 x the working light
        brain.cfg.autofocus.mechanism = "spot_d4sigma"
        brain.cfg.autofocus.routine = "sweep"
        brain.cfg.autofocus.drive_amplitude_v = 8.0
        brain.cfg.autofocus.steps = 9
        seen = []
        brain.autofocus()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 60:
            s = brain.status()
            if s.af_exposure_active and not s.spot_found:
                seen.append(s.spot_found_why_short)
            if not s.af_running and s.af_id:
                break
            time.sleep(0.005)
        assert seen, "no frame at the AF exposure without a threshold detection"
        assert all(w == "threshold check paused (autofocus exposure)" for w in seen), set(seen)
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 3. failure messages
# --------------------------------------------------------------------------- #
def test_af_error_carries_the_message_and_the_traceback_goes_to_stderr(capfd):
    brain, cam, z, events = _rig()
    try:
        brain.cfg.autofocus.routine = "one_way"

        def boom(go, live):
            raise RuntimeError("kim did not answer move_to_step within 20 s")
        brain._af_one_way = boom
        brain.autofocus()
        assert _wait(lambda: not brain.status().af_running and brain.status().af_id == 1)
        s = brain.status()
        assert s.af_error == "RuntimeError: kim did not answer move_to_step within 20 s"
        err = [m for lvl, m in events if lvl == "error"]
        assert err and "RuntimeError: kim did not answer" in err[-1], err
        out = capfd.readouterr().err
        assert "Traceback" in out and "boom" in out
        assert out.count("Traceback") == 1                 # once, not per handler
        out.encode("ascii")                                # ASCII (gotcha #14)
    finally:
        brain.shutdown()


def test_an_empty_or_long_message_is_handled():
    from camera.camera import failure_text
    assert failure_text(RuntimeError()) == "RuntimeError"
    t = failure_text(ValueError("x" * 1000))
    assert t.startswith("ValueError: xxx") and t.endswith("...") and len(t) <= 200
    assert failure_text(KeyError("k")) == "KeyError: 'k'"
    # a message with a line break stays on one line
    assert "\n" not in failure_text(RuntimeError("a\nb"))


def test_a_crashed_autofocus_says_what_crashed(capfd):
    brain, cam, z, events = _rig()
    try:
        def crash(req):
            raise KeyError("missing_setting")
        brain._run_autofocus = crash
        brain.autofocus()
        assert _wait(lambda: not brain.status().af_running and brain.status().af_id == 1)
        assert brain.status().af_error == "crashed: KeyError: 'missing_setting'"
        out = capfd.readouterr().err
        assert out.count("Traceback") == 1
    finally:
        brain.shutdown()


def test_a_zcal_failure_carries_the_message(capfd):
    brain, cam, z, events = _rig()
    try:
        orig = z.move_counter
        calls = {"n": 0}

        def mc(steps):
            calls["n"] += 1
            if calls["n"] == 3:
                raise ValueError("counter target 1e9 outside the leash")
            orig(steps)
        z.move_counter = mc
        s = _run_zcal(brain)
        assert s.zcal_state == "failed: ValueError: counter target 1e9 outside the leash", \
            s.zcal_state
        err = [m for lvl, m in events if lvl == "error"]
        assert err and "ValueError: counter target 1e9" in err[-1]
        assert capfd.readouterr().err.count("Traceback") == 1
    finally:
        brain.shutdown()
