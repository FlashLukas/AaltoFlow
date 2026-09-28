"""Lukas's answers of 2026-09-28 to the open questions of the deep cleaning.

1. The long-term stabilizer INTEGRATES: a slow drift is removed completely,
   without re-introducing the hysteresis limit cycle (gotcha #11).
2. field_stable DROPS when a STABLE field drifts out of tolerance (time
   filtered, so one noisy reading does not flicker it) and comes back.
3. A calibration ends with the current ramped to ZERO, and IDLE is reached
   only after that ramp.
4. An internal control-loop error HOLDS the current and is visible in status.
Plus the suite convention: a failed hardware read shows as `hw_error`.

Each test FAILED on the code before the change. The sim runs with real DAQ
timing (100 ms per precise reading), as the service does.
"""

import time

import pytest

from clMag.config import Config
from clMag.net.protocol import status_to_dict
from clMag.sim_system import build_sim_system


def _wait(pred, timeout_s=10.0, poll_s=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if pred():
            return True
        time.sleep(poll_s)
    return False


def _started():
    cfg = Config()
    ctrl, kepco, probe, acq, cal = build_sim_system(cfg)
    events = []
    ctrl._on_event = lambda lvl, msg: events.append((lvl, msg))
    ctrl.start()
    return cfg, ctrl, kepco, probe, events


def _record_writes(kepco):
    """Wrap the supply's set_current to log every value the loop commands."""
    writes = []
    real = kepco.set_current

    def rec(amps):
        writes.append((time.monotonic(), amps))
        return real(amps)

    kepco.set_current = rec
    return writes


def _reversals(values, last_dir=0):
    """How often the commanded current changed DIRECTION -- each reversal
    flips the magnet onto the other hysteresis branch. `last_dir` is the
    direction before the first value (+1: the field was approached from
    below, so the first move down already counts)."""
    n, prev = 0, None
    for v in values:
        if prev is not None and abs(v - prev) > 1e-12:
            d = 1 if v > prev else -1
            if last_dir and d != last_dir:
                n += 1
            last_dir = d
        prev = v
    return n


def _mean_field(ctrl, seconds=1.0):
    vals = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        vals.append(ctrl.status().measured_field_mT)
        time.sleep(0.02)
    return sum(vals) / len(vals)


def _stable_at(ctrl, field):
    ctrl.set_field(field)
    assert _wait(lambda: ctrl.status().field_stable, 15.0), "never became stable"


# --------------------------------------------------------------------------
# 1. The integrating stabilizer.

@pytest.mark.parametrize("drift_mT", [+0.5, -0.5])
def test_stabilizer_removes_a_slow_drift_without_a_limit_cycle(drift_mT):
    """50 mT approached from below (the upward branch). A drift of +0.5 mT
    (field too HIGH: the correction has to go AGAINST the approach direction,
    the dangerous case for hysteresis) or -0.5 mT (with it). The old
    proportional trim (0.001 A/mT) moved the current by 0.5 mA -- 0.02 mT --
    and left the field ~0.5 mT off, five times the tolerance."""
    cfg, ctrl, kepco, probe, events = _started()
    try:
        _stable_at(ctrl, 50.0)
        writes = _record_writes(kepco)
        probe.set_drift(0.1, drift_mT)                 # 5 s of drift
        time.sleep(5.0 + 6.0)                          # drift + time to converge
        err = _mean_field(ctrl, 1.0) - 50.0
        assert abs(err) <= cfg.limits.field_tolerance_mT, \
            f"stabilizer left {err:+.3f} mT of the drift"
        # No limit cycle: once converged the current is left alone ...
        t_late = time.monotonic() - 3.0
        late = [a for t, a in writes if t >= t_late]
        assert max(late) - min(late) < 1e-9, "stabilizer still dithering"
        # ... and direction reversals are few and bounded. Against the branch
        # each correction costs exactly two (a back-step and the return that
        # puts the magnet back on the approach branch); with it, none.
        rev = _reversals([a for _, a in writes], last_dir=+1)
        if drift_mT < 0:
            assert rev == 0, f"{rev} reversals for a drift WITH the branch"
        else:
            assert rev <= 20, f"{rev} reversals: looks like a limit cycle"
        # the corrections are published
        st = ctrl.status()
        assert st.stabilizer is True
        assert abs(st.stabilizer_trim_A) > 1e-4
        assert ctrl.status().field_stable
    finally:
        ctrl.shutdown()


def test_small_error_against_the_branch_does_not_limit_cycle():
    """The case the hard freeze (gotcha #11) was about: the field sits
    0.14 mT too HIGH on the upward branch. Lowering the current flips the iron
    onto the downward branch (the sim's 2h = 0.16 mT), so a plain integrator
    overshoots, corrects back up, flips again... Checked: with backlash_A = 0
    this cycled for ever (7 reversals in 10 s, still moving, field_stable
    False). With the back-step-and-return it ends on the upward branch after
    one correction: two reversals, then nothing moves."""
    cfg, ctrl, kepco, probe, events = _started()
    try:
        _stable_at(ctrl, 50.0)
        writes = _record_writes(kepco)
        probe.set_drift(1000.0, 0.14)                  # a step, not a ramp
        time.sleep(8.0)
        t_late = time.monotonic() - 4.0
        late = [a for t, a in writes if t >= t_late]
        assert late and max(late) - min(late) < 1e-9, "still cycling"
        rev = _reversals([a for _, a in writes], last_dir=+1)
        assert rev <= 2, f"{rev} reversals"
        assert abs(_mean_field(ctrl, 1.0) - 50.0) <= cfg.limits.field_tolerance_mT
        assert ctrl.status().field_stable
    finally:
        ctrl.shutdown()


def test_stabilizer_respects_its_total_authority():
    """A drift far larger than the stabilizer may correct: the trim stops at
    max_trim_A (anti-windup: it does not keep accumulating), with a warning."""
    cfg, ctrl, kepco, probe, events = _started()
    cfg.stabilizer.max_trim_A = 0.02
    try:
        _stable_at(ctrl, 50.0)
        probe.set_drift(1000.0, -5.0)                  # a 5 mT step
        assert _wait(lambda: any("authority" in m for _, m in events), 10.0)
        time.sleep(2.0)
        assert abs(ctrl.status().stabilizer_trim_A) <= 0.02 + 1e-12
    finally:
        ctrl.shutdown()


# --------------------------------------------------------------------------
# 2. field_stable drops when the field drifts out of tolerance.

def test_field_stable_drops_on_drift_and_comes_back():
    cfg, ctrl, kepco, probe, events = _started()
    ctrl.stabilizer_enabled = False          # watch the flag, not the fix
    try:
        _stable_at(ctrl, 50.0)
        probe.set_drift(1000.0, 0.5)         # step 5x the tolerance
        assert _wait(lambda: not ctrl.status().field_stable, 3.0), \
            "field_stable stayed True with the field 0.5 mT off"
        assert ctrl.status().state == "STABLE"   # still regulating, just not in tol
        probe.set_drift(0.0, 0.0)
        assert _wait(lambda: ctrl.status().field_stable, 3.0), \
            "field_stable did not come back when the field returned"
    finally:
        ctrl.shutdown()


def test_one_noisy_reading_does_not_flicker_field_stable():
    cfg, ctrl, kepco, probe, events = _started()
    ctrl.stabilizer_enabled = False
    try:
        _stable_at(ctrl, 50.0)
        real = probe.read_voltage
        spikes = {"n": 1}

        def spiky(samples, rate):
            v = real(samples, rate)
            if spikes["n"] > 0:
                spikes["n"] -= 1
                v += cfg.hall.field_to_volts(1.0) - cfg.hall.field_to_volts(0.0)
            return v

        probe.read_voltage = spiky
        t0 = time.monotonic()
        flags = []
        while time.monotonic() - t0 < 1.0:
            flags.append(ctrl.status().field_stable)
            time.sleep(0.005)
        assert spikes["n"] == 0
        assert all(flags), "one 1 mT spike made field_stable flicker"
    finally:
        ctrl.shutdown()


# --------------------------------------------------------------------------
# 3. A calibration ends at zero current; IDLE only after that ramp.

def test_calibration_ends_at_zero_and_idle_only_after_the_ramp():
    cfg, ctrl, kepco, probe, events = _started()
    try:
        seq = ctrl.calibrate(n_per_leg=3, dwell_s=0.05)
        first_idle = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20.0:
            st = ctrl.status()
            if st.cmd_done >= seq and st.state == "IDLE":
                first_idle = st
                break
            time.sleep(0.002)
        assert first_idle is not None, "calibration never finished"
        assert abs(first_idle.current_A) < 1e-9, \
            f"IDLE reached at {first_idle.current_A:+.3f} A, not at zero"
        assert abs(kepco.read_current()) < 1e-9
        assert ctrl.calibration is not None and len(ctrl.calibration.currents_A) >= 3
    finally:
        ctrl.shutdown()


# --------------------------------------------------------------------------
# 4. A control-loop error holds the current and is visible in status.

def test_loop_error_holds_the_current_and_shows_in_status():
    cfg, ctrl, kepco, probe, events = _started()
    try:
        ctrl.set_current(0.5)
        assert _wait(lambda: abs(kepco.read_current() - 0.5) < 1e-9)
        real = kepco.set_current
        fails = {"n": 1}

        def flaky(amps):
            if fails["n"] > 0 and amps > 0.5 + 1e-9:
                fails["n"] -= 1
                raise IOError("GPIB timeout (simulated)")
            return real(amps)

        kepco.set_current = flaky
        ctrl.set_current(1.0)
        assert _wait(lambda: fails["n"] == 0)
        assert _wait(lambda: ctrl.status().state == "IDLE")
        time.sleep(0.3)
        # HELD at the last value that reached the supply -- not one ramp
        # step further (the ramp had already advanced when the write failed)
        assert abs(kepco.read_current() - 0.5) < 1e-9
        assert abs(ctrl.status().current_A - 0.5) < 1e-9
        st = ctrl.status()
        assert "GPIB timeout" in st.loop_error
        assert st.hw_error == ""          # the supply answers again
        d = status_to_dict(st)
        assert "GPIB timeout" in d["loop_error"] and d["hw_error"] == ""
        # the next command clears the sticky message
        ctrl.set_current(0.2)
        assert _wait(lambda: ctrl.status().loop_error == "")
    finally:
        ctrl.shutdown()


def test_supply_that_keeps_failing_shows_hw_error():
    cfg, ctrl, kepco, probe, events = _started()
    try:
        ctrl.set_current(0.3)
        assert _wait(lambda: abs(kepco.read_current() - 0.3) < 1e-9)
        real = kepco.set_current
        broken = {"on": True}

        def dead(amps):
            if broken["on"]:
                raise IOError("VI_ERROR_TMO (simulated)")
            return real(amps)

        kepco.set_current = dead
        assert _wait(lambda: "VI_ERROR_TMO" in ctrl.status().hw_error, 3.0)
        broken["on"] = False
        assert _wait(lambda: ctrl.status().hw_error == "", 3.0)
    finally:
        ctrl.shutdown()


# --------------------------------------------------------------------------
# hw_error: a failed Hall read must not look healthy.

def test_failed_hall_read_shows_hw_error_and_keeps_last_good_field():
    cfg, ctrl, kepco, probe, events = _started()
    try:
        time.sleep(0.4)
        real = probe.read_voltage
        broken = {"on": True}

        def dead(samples, rate):
            time.sleep(0.01)
            if broken["on"]:
                raise IOError("DAQmx -200279 (simulated)")
            return real(samples, rate)

        probe.read_voltage = dead
        assert _wait(lambda: "DAQmx" in ctrl.status().hw_error, 3.0)
        assert "DAQmx" in status_to_dict(ctrl.status())["hw_error"]
        broken["on"] = False
        assert _wait(lambda: ctrl.status().hw_error == "", 3.0)
    finally:
        ctrl.shutdown()
