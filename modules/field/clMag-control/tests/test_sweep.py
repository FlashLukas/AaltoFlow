"""The field SWEEP (ramp_field): a continuous, paced field ramp for fly scans.

What has to hold, and why:
  * the field FOLLOWS a moving setpoint (closed loop: feed-forward through the
    calibration + PI on the measured field), at the asked pace;
  * the current never steps back against the sweep (gotcha #11: a dithering
    current flips the iron's hysteresis branch);
  * at the end the usual endgame settles it: field_stable at the target;
  * the sweep is numbered (status ramp_id) so a client can tell "my sweep is
    over" from a stale frame;
  * ramp_stop ends it where it is; a new set_field takes over;
  * the stream records every reading with its time (the fly scan bins by it);
  * a target outside the calibration is refused in the caller's thread.
"""

import time

import pytest

from test_controller import _make_controller, _wait


def _run_until(ctrl, pred, timeout_s):
    return _wait(ctrl, pred, timeout_s)


def test_sweep_follows_the_setpoint_and_settles():
    cfg, ctrl, kepco = _make_controller()
    ctrl.start()
    try:
        ctrl.set_field(-10.0)
        assert _wait(ctrl, lambda s: s.field_stable, 10.0)
        ctrl.stream_start()
        currents = []
        t0 = time.monotonic()
        rid = ctrl.ramp_field(20.0, 15.0)          # 30 mT at 15 mT/s = 2 s
        assert rid == 1
        assert _wait(ctrl, lambda s: s.ramp_id == rid and s.ramping, 2.0)
        errs = []
        while ctrl.status().ramping:
            s = ctrl.status()
            currents.append(s.current_A)
            if s.setpoint_field_mT is not None:
                errs.append(abs(s.setpoint_field_mT - s.measured_field_mT))
            time.sleep(0.01)
        dur = time.monotonic() - t0
        assert 1.6 < dur < 3.5, dur                # the pace was kept
        # the current only went up (never back against the sweep)
        assert all(b >= a - 1e-12 for a, b in zip(currents, currents[1:]))
        # the field tracked the moving setpoint closely (sim: no coil lag)
        assert max(errs[len(errs) // 4:]) < 1.0
        assert _wait(ctrl, lambda s: s.field_stable, 10.0), "did not settle at the end"
        s = ctrl.status()
        assert s.setpoint_field_mT == 20.0 and s.ramp_id == rid and not s.ramping
        assert abs(s.measured_field_mT - 20.0) <= cfg.limits.field_tolerance_mT
        chunk = ctrl.stream_stop()
        f = [v for v in chunk["values"]["field"] if v is not None]
        assert len(chunk["t"]) > 50
        assert min(f) < -8 and max(f) > 18         # the whole sweep is recorded
        assert set(chunk["values"]) == {"field", "setpoint", "current"}
        assert chunk["t"] == sorted(chunk["t"])
    finally:
        ctrl.shutdown()
    assert abs(kepco.read_current()) < 1e-6        # shutdown unchanged: to zero


def test_sweep_down_and_stop_mid_way():
    cfg, ctrl, kepco = _make_controller()
    ctrl.start()
    try:
        ctrl.set_field(30.0)
        assert _wait(ctrl, lambda s: s.field_stable, 10.0)
        rid = ctrl.ramp_field(-30.0, 10.0)
        assert _wait(ctrl, lambda s: s.ramp_id == rid and s.ramping, 2.0)
        time.sleep(1.0)
        ctrl.ramp_stop()
        assert _wait(ctrl, lambda s: not s.ramping, 2.0)
        s = ctrl.status()
        assert s.state == "IDLE" and not s.field_stable
        held = s.current_A
        here = s.measured_field_mT
        assert 5.0 < here < 28.0                   # stopped on the way, not at the end
        time.sleep(0.3)
        assert ctrl.status().current_A == held     # current held where it was
    finally:
        ctrl.shutdown()


def test_a_new_set_field_takes_over_a_sweep():
    cfg, ctrl, kepco = _make_controller()
    ctrl.start()
    try:
        ctrl.ramp_field(40.0, 5.0)
        time.sleep(0.4)
        ctrl.set_field(0.0)
        assert _wait(ctrl, lambda s: s.field_stable and s.setpoint_field_mT == 0.0, 10.0)
        assert not ctrl.status().ramping
    finally:
        ctrl.shutdown()


def test_refused_outside_the_calibration_and_rate_clamped():
    cfg, ctrl, kepco = _make_controller()
    seen = []
    ctrl._on_event = lambda lvl, msg: seen.append((lvl, msg))
    lo, hi = ctrl.calibration.range_mT
    with pytest.raises(ValueError):
        ctrl.ramp_field(hi + 10.0, 1.0)
    with pytest.raises(ValueError):
        ctrl.ramp_field(float("nan"), 1.0)
    ctrl.start()
    try:
        ctrl.ramp_field(5.0, 1e6)                  # absurd pace: clamped, warned
        assert any("clamped" in m for _, m in seen)
        assert _wait(ctrl, lambda s: s.ramp_rate_mT_per_s == cfg.limits.sweep_rate_max_mT_per_s, 2.0)
    finally:
        ctrl.shutdown()


def test_describe_offers_the_ramp_and_the_stream():
    from clMag.net.describe import build_manifest
    cfg, ctrl, kepco = _make_controller()
    m = build_manifest(ctrl)
    field = next(p for p in m["parameters"] if p["id"] == "field")
    r = field["ramp"]
    assert r["start"] == {"verb": "ramp_field",
                          "args": {"to": "field_mT", "rate": "rate_mT_per_s"}}
    assert r["stop"]["verb"] == "ramp_stop"
    assert r["readback"]["measured"] is True
    assert r["readback"]["stream"] == {"group": "field", "channel": "field"}
    assert r["rate"]["unit"] == "mT/s"
    assert r["rate"]["min"] <= r["rate"]["default"] <= r["rate"]["max"]
    meas = next(p for p in m["parameters"] if p["id"] == "measured_field")
    assert meas["stream"] == {"group": "field", "channel": "field"}
    # no calibration -> no sweep offered (it would be refused)
    ctrl.calibration = None
    field = next(p for p in build_manifest(ctrl)["parameters"] if p["id"] == "field")
    assert "ramp" not in field
