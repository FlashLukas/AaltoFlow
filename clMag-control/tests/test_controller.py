"""Integration test: the whole controller drives the simulated magnet to a
field setpoint, flags stable, and holds it -- with real threads."""

import time

from clMag.config import Config
from clMag.calibration import FieldCalibration
from clMag.backends.sim import SimulatedKepco, SimulatedHallProbe
from clMag.acquisition import AcquisitionThread
from clMag.controller import Controller


def _build(cfg, kepco, probe):
    probe.emulate_timing = False
    I_max = cfg.limits.current_max_A
    up = [(-I_max) + 2 * I_max * i / 49 for i in range(50)]
    raw = []
    for I in up + list(reversed(up)):
        kepco.set_current(I)
        v = probe.read_voltage(cfg.acquisition.precise_samples, cfg.acquisition.precise_rate_Hz)
        raw.append((I, cfg.hall.volts_to_field(v)))
    return FieldCalibration.from_sweep(raw, hall=cfg.hall)


def _make_controller():
    cfg = Config()
    cfg.pid.Kc_A_per_mT = 0.01     # sim-tuned gains
    cfg.pid.Ti_s = 0.15
    kepco = SimulatedKepco(current_max_A=cfg.limits.current_max_A)
    probe = SimulatedHallProbe(kepco, hall=cfg.hall)
    kepco.open()
    cal = _build(cfg, kepco, probe)
    # keep timing off so the test runs fast; behaviour is identical
    probe.emulate_timing = False
    acq = AcquisitionThread(probe, cfg.hall, cfg.acquisition)
    ctrl = Controller(cfg, kepco, acq, calibration=cal)
    return cfg, ctrl, kepco


def _wait(ctrl, predicate, timeout_s):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if predicate(ctrl.status()):
            return True
        time.sleep(0.02)
    return False


def test_seek_reaches_and_flags_stable():
    cfg, ctrl, kepco = _make_controller()
    ctrl.start()
    try:
        ctrl.set_field(50.0)
        assert _wait(ctrl, lambda s: s.field_stable, timeout_s=10.0), "never became stable"
        s = ctrl.status()
        assert abs(s.measured_field_mT - 50.0) <= cfg.limits.field_tolerance_mT
    finally:
        ctrl.shutdown()
    # shutdown must leave the supply at zero
    assert abs(kepco.read_current()) < 1e-6


def test_seek_downward_then_field_holds():
    cfg, ctrl, kepco = _make_controller()
    ctrl.start()
    try:
        ctrl.set_field(-30.0)
        assert _wait(ctrl, lambda s: s.field_stable, timeout_s=10.0)
        assert abs(ctrl.status().measured_field_mT + 30.0) <= cfg.limits.field_tolerance_mT
    finally:
        ctrl.shutdown()


def test_clamp_reports_over_limit():
    cfg, ctrl, kepco = _make_controller()
    seen = []
    ctrl._on_event = lambda lvl, msg: seen.append((lvl, msg))
    ctrl.start()
    try:
        ctrl.set_current(99.0)          # way over the 3 A limit
        time.sleep(0.3)
        assert any(lvl == "error" for lvl, _ in seen), "over-limit not reported"
        assert abs(ctrl.status().current_A) <= cfg.limits.current_max_A + 1e-9
    finally:
        ctrl.shutdown()
