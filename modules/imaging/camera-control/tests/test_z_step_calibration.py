"""Z STEP CALIBRATION by the camera (Lukas, 2026-09-28).

Why: the kim Z (PIA25, slip-stick) steps UP much smaller than DOWN -- during
the D4sigma rig test the counter at focus climbed from -1.5 to +344 um while
the image stayed in focus. The counter is then a poor ruler, and every routine
that goes back to a Z by the counter (the sweep's park, a failed run's return)
lands somewhere else.

How: sigma^2 (the D4sigma metric) is EXACTLY a parabola in the true Z. Walk Z
up through focus in equal COUNTER steps and fit a parabola in counter units:
its curvature is c * s_up^2 (s = true distance per counter unit). Walk down
through focus the same way: c * s_down^2. The ratio of the two curvatures is
(s_up / s_down)^2 -- the step-size RATIO, whatever c is. The absolute scale is
not in it; the geometric mean of the two is kept at the stage's current step.

Tested against SimSlipStickZ (true distance 1.0 x the commanded one up, 0.7 x
down, or another ratio) and the coherent spot. Assertions on the TRUE Z.
"""

import math
import time

import numpy as np
import pytest

from camera.backends.sim import SimCamera, SimSlipStickZ, SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config

ZF = 30.0


def _wait(cond, timeout=120.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


def _rig(up=1.0, down=0.7, slip=True, calibrate=True):
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
    if slip:
        z = SimSlipStickZ(z0=ZF, z_focus=ZF, vmin=cfg.limits.z_min_v, vmax=cfg.limits.z_max_v,
                          up_gain=up, down_gain=down)
    else:
        z = SimZFocus(z0=ZF, z_focus=ZF, vmin=cfg.limits.z_min_v, vmax=cfg.limits.z_max_v)
    cam = SimCamera(xy, z, pixel_size_x_um=cfg.image.pixel_size_x_um,
                    pixel_size_y_um=cfg.image.pixel_size_y_um, spot_model="coherent",
                    noise=1.5)
    brain = Camera(cam, xy, z, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    if calibrate:
        brain.calibrate_spot(10)        # in focus
    return brain, z, events


def _true(z):
    return z.true_z() if hasattr(z, "true_z") else z.read_z()


def _run_zcal(brain):
    n = brain.calibrate_z_steps()
    assert _wait(lambda: brain.status().zcal_id == n and not brain.status().zcal_running)
    return brain.status()


@pytest.mark.parametrize("down", [0.7, 0.5])
def test_the_step_ratio_is_recovered_from_the_two_curvatures(down):
    brain, z, events = _rig(down=down)
    try:
        s = _run_zcal(brain)
        assert s.zcal_state == "OK", s.zcal_state
        want = 1.0 / down                                  # s_up / s_down
        assert s.zcal_ratio == pytest.approx(want, rel=0.03), s.zcal_ratio
        assert s.zcal_r2_up > 0.99 and s.zcal_r2_down > 0.99
        # written to the Z stage, geometric mean kept at its step (nominal 1.0)
        up, dn = z.step_sizes()
        assert up / dn == pytest.approx(s.zcal_ratio, rel=1e-9)
        assert math.sqrt(up * dn) == pytest.approx(1.0, rel=1e-9)
        assert (s.zcal_up_um, s.zcal_down_um) == pytest.approx((up, dn))
        # reported in an event with the numbers
        assert any("Z step calibration" in m and "ratio" in m for _l, m in events)
        # and Z was left near focus, by the image
        assert abs(_true(z) - ZF) < 0.6, _true(z) - ZF
    finally:
        brain.shutdown()


def test_after_the_calibration_the_sweep_parks_in_focus_on_the_slip_stick_z():
    """The sweep measures every level climbing, then drives BACK to the best Z
    by the counter. On an asymmetric Z that lands off focus (the old reason for
    one_way); once the camera knows the two step sizes, "back" is right."""
    errs = {}
    for calibrated in (False, True):
        brain, z, events = _rig()
        try:
            if calibrated:
                assert _run_zcal(brain).zcal_state == "OK"
            af = brain.cfg.autofocus
            af.mechanism, af.routine = "spot_d4sigma", "sweep"
            af.drive_amplitude_v, af.steps = 16.0, 33
            z.set_z(z.read_z() - 3.0)                       # a little below focus
            brain.autofocus()
            assert _wait(lambda: not brain.status().af_running)
            assert brain.status().af_error == "OK"
            errs[calibrated] = _true(z) - ZF
        finally:
            brain.shutdown()
    assert abs(errs[True]) < 0.5, errs
    assert abs(errs[False]) > 1.0, errs                   # why it is needed


def test_it_refuses_when_the_minimum_is_not_bracketed():
    brain, z, events = _rig()
    try:
        af = brain.cfg.autofocus
        af.zcal_start_offset_v = 0.5
        af.zcal_max_travel_v = 5.0
        z.set_z(z.read_z() + 10.0)                          # well ABOVE focus
        before = z.read_z()
        sizes = z.step_sizes()
        s = _run_zcal(brain)
        assert s.zcal_state.startswith("failed"), s.zcal_state
        assert "bracket" in s.zcal_state
        assert z.step_sizes() == sizes                      # nothing written
        assert abs(z.read_z() - before) < 1e-6              # Z back (by the counter)
        assert math.isnan(s.zcal_ratio)
    finally:
        brain.shutdown()


def test_it_refuses_a_poor_fit():
    brain, z, events = _rig()
    try:
        rng = np.random.default_rng(3)
        orig = brain._zcal_metric

        def noisy(gray):
            m = orig(gray)
            return m * float(rng.uniform(0.6, 1.6)) if np.isfinite(m) else m
        brain._zcal_metric = noisy
        sizes = z.step_sizes()
        s = _run_zcal(brain)
        assert s.zcal_state.startswith("failed") and "R^2" in s.zcal_state, s.zcal_state
        assert z.step_sizes() == sizes
    finally:
        brain.shutdown()


def test_it_needs_a_calibrated_spot_and_an_open_loop_counting_z():
    brain, z, events = _rig(calibrate=False)
    try:
        s = _run_zcal(brain)
        assert s.zcal_state.startswith("failed") and "spot" in s.zcal_state
    finally:
        brain.shutdown()
    brain, z, events = _rig(slip=False)
    try:
        s = _run_zcal(brain)
        assert s.zcal_state.startswith("failed") and "step counter" in s.zcal_state
    finally:
        brain.shutdown()


def test_kill_stops_it_and_writes_nothing():
    brain, z, events = _rig()
    try:
        sizes = z.step_sizes()
        n = brain.calibrate_z_steps()
        assert _wait(lambda: brain.status().zcal_running, 10)
        time.sleep(0.3)
        brain.kill_af()
        assert _wait(lambda: brain.status().zcal_id == n and not brain.status().zcal_running)
        assert brain.status().zcal_state == "killed"
        assert z.step_sizes() == sizes
    finally:
        brain.shutdown()


def test_describe_verb_client_and_config():
    pytest.importorskip("zmq")
    from camera.net.client import CameraClient
    from camera.net.describe import build_manifest
    from camera.net.protocol import apply_config_dict, config_to_dict
    from camera.net.service import CameraService

    brain, z, events = _rig()
    try:
        m = {p["id"]: p for p in build_manifest(brain)["parameters"]}
        act = m["calibrate_z_steps"]
        assert act["kind"] == "action"
        assert act["wait"]["target_key"] == "zcal_id"
        assert act["wait"]["check"] == {"key": "zcal_state", "equals": "OK"}
        assert m["zcal_ratio"]["kind"] == "indicator"
        svc = CameraService(brain, host="127.0.0.1", cmd_port=15731, pub_port=15732,
                            status_hz=20)
        svc.start()
        cli = CameraClient("127.0.0.1", 15731, 15732, timeout_ms=3000)
        cli.start()
        try:
            n = cli.calibrate_z_steps()
            assert n == brain.status().zcal_id
            assert _wait(lambda: cli.status().zcal_id == n and not cli.status().zcal_running)
            assert cli.status().zcal_state == "OK"
        finally:
            cli.close()
            svc.stop()
    finally:
        brain.shutdown()
    cfg = Config()
    cfg.autofocus.zcal_step_v, cfg.autofocus.zcal_min_r2 = 0.3, 0.9
    wire = Config()
    apply_config_dict(wire, config_to_dict(cfg))
    assert (wire.autofocus.zcal_step_v, wire.autofocus.zcal_min_r2) == (0.3, 0.9)


def test_the_autofocus_tab_runs_it_and_shows_the_result():
    import os
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from camera.apps.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    brain, z, events = _rig()
    try:
        win = MainWindow(brain, brain.cfg, remote=False)
        assert win.b_zcal.text() == "Calibrate Z steps"
        win.b_zcal.click()
        assert _wait(lambda: brain.status().zcal_id == 1 and not brain.status().zcal_running)
        win._refresh()
        assert "step up / down" in win.lab_zcal.text()
        win._update_zcal_plot()
        assert "counter" in win.af_plot._xlabel
        assert not win.grab().isNull()
        win.close()
    finally:
        brain.shutdown()
        app.processEvents()
