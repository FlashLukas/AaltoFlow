"""Bugs found in the deep cleaning of 2026-09-28, each pinned by a test.

Every test here FAILED on the code before the fix (checked one by one), so
each one is the proof that the bug was real, and the guard that it stays fixed.
Network tests use scratch ports 5791..5796 so they never collide with a
service Lukas has running.
"""

import math
import time

import pytest

from clMag.acquisition import AcquisitionThread
from clMag.config import Config
from clMag.sim_system import build_sim_system

NAN = float("nan")


def _wait(pred, timeout_s=5.0, poll_s=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if pred():
            return True
        time.sleep(poll_s)
    return False


def _started(**pre):
    cfg = Config()
    ctrl, kepco, probe, acq, cal = build_sim_system(cfg, **pre)
    events = []
    ctrl._on_event = lambda lvl, msg: events.append((lvl, msg))
    ctrl.start()
    return cfg, ctrl, kepco, probe, events


# --------------------------------------------------------------------------
# 1. A NaN setpoint slipped through the +-3 A clamp and the ramp ran away.
#    `nan > lim` and `nan < -lim` are both False, so _clamp_current passed NaN
#    on; the ramper's copysign(increment, nan) is +increment, and `done` is
#    never true, so the commanded current climbed 0.05 A every tick for ever.

@pytest.mark.parametrize("verb", ["set_current", "set_field"])
def test_nan_setpoint_is_refused_and_current_stays_in_limits(verb):
    cfg, ctrl, kepco, probe, events = _started()
    try:
        with pytest.raises(ValueError):
            getattr(ctrl, verb)(NAN)
        time.sleep(1.0)       # 100 ticks: the old ramp would be at ~5 A by now
        cur = ctrl.status().current_A
        assert math.isfinite(cur) and abs(cur) <= cfg.limits.current_max_A
    finally:
        ctrl.shutdown()


def test_nan_demag_and_aux_are_refused():
    # demag(NaN) built its step list in `while True: ... if mag <= 1e-6: break`
    # -- never true for NaN, so the CONTROL THREAD looped for ever appending to
    # a list (hung loop + unbounded memory). Proven by reading, not by running:
    # running the old code would eat the test machine's memory.
    # aux_set_ao(NaN): max(lo, min(hi, nan)) == hi, i.e. NaN drove the BNC to +10 V.
    cfg, ctrl, kepco, probe, events = _started()
    try:
        with pytest.raises(ValueError):
            ctrl.demag(NAN)
        with pytest.raises(ValueError):
            ctrl.aux_set_ao("Dev1/ao0", NAN)
        assert ctrl.status().aux["ao"]["Dev1/ao0"] is None, "NaN reached the AO"
        ctrl.set_current(0.2)
        assert _wait(lambda: abs(ctrl.status().current_A - 0.2) < 1e-9), \
            "control loop no longer processes commands"
    finally:
        ctrl.shutdown()


# --------------------------------------------------------------------------
# 2. An exception on the control thread killed it silently: the magnet stayed
#    at whatever current it had, and every later command was accepted
#    ("ok": true) and never executed. calibrate(n_per_leg=1) was one way in
#    (ZeroDivisionError in the sweep grid), a failed supply write another.

def test_calibrate_with_too_few_points_is_refused_and_loop_survives():
    cfg, ctrl, kepco, probe, events = _started()
    try:
        with pytest.raises(ValueError):
            ctrl.calibrate(n_per_leg=1, dwell_s=0.0)
        ctrl.set_current(0.3)
        assert _wait(lambda: abs(ctrl.status().current_A - 0.3) < 1e-9), \
            "control thread died on calibrate(n_per_leg=1)"
    finally:
        ctrl.shutdown()


def test_control_loop_survives_a_failing_supply_write():
    cfg, ctrl, kepco, probe, events = _started()
    real_set = kepco.set_current
    fails = {"n": 1}

    def flaky(amps):
        if fails["n"] > 0:
            fails["n"] -= 1
            raise IOError("GPIB timeout (simulated)")
        return real_set(amps)

    kepco.set_current = flaky
    try:
        ctrl.set_current(0.5)
        # the loop reports the fault and stops where it is ...
        assert _wait(lambda: any(l == "error" and "GPIB timeout" in m
                                 for l, m in events)), "fault not reported"
        assert ctrl._thread.is_alive(), "control thread died"
        # ... and the next command still works
        ctrl.set_current(0.4)
        assert _wait(lambda: abs(ctrl.status().current_A - 0.4) < 1e-9)
    finally:
        ctrl.shutdown()


# --------------------------------------------------------------------------
# 3. One failed DAQ read killed the acquisition thread. Nothing noticed: the
#    status kept publishing the last field as "measured" for ever, and a STABLE
#    magnet stayed "stable" on a frozen number.

class _FlakyProbe:
    def __init__(self):
        self.calls = 0

    def read_voltage(self, samples, rate_Hz):
        self.calls += 1
        time.sleep(0.005)
        if self.calls == 3:
            raise IOError("DAQmx read failed (simulated)")
        return 2.510 + 0.001 * self.calls      # a field that keeps changing


def test_acquisition_survives_a_failed_read():
    cfg = Config()
    acq = AcquisitionThread(_FlakyProbe(), cfg.hall, cfg.acquisition)
    acq.start()
    try:
        assert _wait(lambda: acq._probe.calls >= 10, timeout_s=3.0), \
            "acquisition thread died on one failed read"
        assert acq.errors >= 1 and "DAQmx" in acq.last_error
    finally:
        acq.stop()


# --------------------------------------------------------------------------
# 4. Re-sending the field the magnet is already STABLE at made it re-seek:
#    jump 2 mT BELOW (the undershoot) and climb back. A client polling
#    `setpoint == target and field_stable` (ClMagClient.wait_stable, and
#    scan-core's adopt_then_flag) saw the OLD frame -- same setpoint, stable --
#    and read its detector while the magnet was being pulled away (gotcha #2
#    with a twist: the stale frame and the new command carry the same number).

def test_same_setpoint_while_stable_is_a_no_op():
    cfg, ctrl, kepco, probe, events = _started()
    tol = cfg.limits.field_tolerance_mT
    try:
        ctrl.set_field(40.0)
        assert _wait(lambda: ctrl.status().field_stable, timeout_s=15.0)
        ctrl.set_field(40.0)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.6:
            st = ctrl.status()
            assert st.state == "STABLE", f"re-seek started: state {st.state}"
            assert abs(st.measured_field_mT - 40.0) <= tol + 0.05, \
                f"field left the target: {st.measured_field_mT:.3f} mT"
            time.sleep(0.01)
    finally:
        ctrl.shutdown()


# --------------------------------------------------------------------------
# 5. ClMagClient.wait_idle() returned AT ONCE when the service was idle before
#    the command: the cached frame still said IDLE because the control thread
#    had not dequeued the demag yet. A script doing demag(); wait_idle();
#    measure() measured during the demag.

def test_wait_idle_is_not_fooled_by_the_idle_frame_before_the_command():
    pytest.importorskip("zmq")
    from clMag.net.client import ClMagClient
    from clMag.net.service import ClMagService

    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    svc = ClMagService(ctrl, host="127.0.0.1", cmd_port=5791, pub_port=5792)
    svc.start()
    client = None
    try:
        client = ClMagClient(host="127.0.0.1", cmd_port=5791, pub_port=5792)
        client.start()
        assert _wait(lambda: client.status().state == "IDLE")
        client.demag(0.5)
        st = client.wait_idle(timeout_s=40.0)
        assert st.state == "IDLE"
        # really finished: the current is at zero and stays in IDLE
        time.sleep(0.3)
        st = client.status()
        assert st.state == "IDLE", f"wait_idle returned mid-demag ({st.state})"
        assert abs(st.current_A) < 1e-9
    finally:
        if client:
            client.shutdown()
        svc.stop()


def test_service_refuses_non_finite_numbers():
    pytest.importorskip("zmq")
    from clMag.net.client import ClMagClient
    from clMag.net.service import ClMagService

    cfg = Config()
    ctrl, *_ = build_sim_system(cfg)
    svc = ClMagService(ctrl, host="127.0.0.1", cmd_port=5793, pub_port=5794)
    svc.start()
    client = None
    try:
        client = ClMagClient(host="127.0.0.1", cmd_port=5793, pub_port=5794)
        client.start()
        for msg in ({"cmd": "set_current", "current_A": NAN},
                    {"cmd": "set_field", "field_mT": NAN},
                    {"cmd": "demag", "amplitude_A": NAN},
                    {"cmd": "calibrate", "n_per_leg": 1},
                    {"cmd": "aux_set_ao", "channel": "Dev1/ao0", "volts": NAN}):
            r = client._cmd(msg)
            assert r["ok"] is False, f"{msg} was accepted"
        assert client._cmd({"cmd": "info"})["ok"] is True
    finally:
        if client:
            client.shutdown()
        svc.stop()


# --------------------------------------------------------------------------
# 6. A field outside the calibration's range (which describe advertises as the
#    field's min/max) was accepted: the jump went to the full 3 A, the seek
#    could never arrive, and the loop stayed in SEEK for ever -- emitting a red
#    "current over limit -> clamped" event on EVERY 10 ms tick (100 lines/s),
#    because the seek cap (target estimate + 0.08 A) lay beyond the 3 A limit.
#    The same flood hits an IN-range target near the top once the magnet gives
#    a little less field than when it was calibrated (drift, temperature):
#    then the top of the range is simply not reachable any more.

def test_field_outside_calibration_is_refused():
    cfg, ctrl, kepco, probe, events = _started()
    try:
        lo, hi = ctrl.calibration.range_mT
        ctrl.set_field(hi + 5.0)
        time.sleep(0.5)
        st = ctrl.status()
        assert st.setpoint_field_mT is None and st.state == "IDLE", \
            f"out-of-range target adopted: {st.state} {st.setpoint_field_mT}"
        assert any(l == "error" and "outside" in m for l, m in events)
    finally:
        ctrl.shutdown()


def test_seek_near_the_top_of_the_range_does_not_flood_the_log():
    cfg, ctrl, kepco, probe, events = _started()
    try:
        lo, hi = ctrl.calibration.range_mT
        probe._B_sat *= 0.99          # the magnet now gives 1 % less field
        ctrl.set_field(hi - 0.05)
        time.sleep(4.0)
        errors = [m for l, m in events if l == "error"]
        assert len(errors) <= 2, f"{len(errors)} error events, e.g. {errors[:2]}"
        assert abs(ctrl.status().current_A) <= cfg.limits.current_max_A
    finally:
        ctrl.shutdown()


# --------------------------------------------------------------------------
# 7. describe told scan-core that a `current` set had settled when
#    state in {IDLE, HOLD}. Before the control thread dequeues the command the
#    state is still the OLD IDLE, so a current sweep read its detector one
#    point behind -- gotcha #2 in describe. The ramped current itself is in
#    status (current_A snaps exactly onto the target when the ramp ends), so
#    the honest rule is "the service echoes my current".

def _settled(settle: dict, target, st: dict) -> bool:
    """The settle rules as scan-core evaluates them (scan_core/instrument.py)."""
    pol = settle["policy"]
    if pol == "state_in":
        return st.get(settle["key"]) in set(settle["states"])
    if pol == "echoes":
        v = st.get(settle["key"])
        return v is not None and abs(float(v) - float(target)) <= settle.get("tol", 1e-6)
    raise AssertionError(f"unexpected policy {pol}")


def test_describe_current_settle_is_not_fooled_by_the_frame_before_the_command():
    from clMag.net.describe import build_manifest
    from clMag.net.protocol import status_to_dict

    cfg = Config()
    ctrl, kepco, probe, acq, cal = build_sim_system(cfg)
    probe.emulate_timing = False
    acq.start()                      # the control thread is NOT started:
    try:                             # the command stays queued, as in the race
        settle = next(p for p in build_manifest(ctrl)["parameters"]
                      if p["id"] == "current")["settle"]
        ctrl.set_current(1.0)
        stale = status_to_dict(ctrl.status())
        assert stale["state"] == "IDLE" and stale["current_A"] == 0.0
        assert not _settled(settle, 1.0, stale), \
            "settle rule says 'done' before the command was even taken up"
    finally:
        acq.stop()
