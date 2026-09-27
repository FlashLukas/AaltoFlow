"""The brain against the simulated BOP: clamps, the ramp, the safety rules,
the acquisition and the thread rules. Stepped by hand on a fake clock (see
conftest), except where the real worker thread is the point of the test."""

import time

import pytest

from kepco.config import Config
from kepco.sim_system import build_sim_system

from conftest import run_for


def _mains(spy):
    """Every value written to the main channel in current mode, in order."""
    return [c[1] for c in spy.calls if c[0] == "program_current"]


# ---- lifecycle / safety -------------------------------------------------------

def test_start_leaves_output_off_in_the_configured_mode(rig):
    supply, spy, clock, _ = rig
    s = supply.status()
    assert s.connected and s.output is False and s.output_request is False
    assert spy.inner.output_on is False
    assert spy.inner.mode == "current"
    # the output was explicitly switched off BEFORE anything else was programmed
    names = [c[0] for c in spy.calls]
    assert names.index("set_output") < names.index("set_mode")


def test_output_on_programs_zero_before_switching_on(rig):
    supply, spy, clock, _ = rig
    supply.set_current(1.0)
    spy.calls.clear()
    supply.set_output(True)
    supply.step()
    names = [c[0] for c in spy.calls]
    i_on = names.index("set_output")
    # OUTP ON restores the saved programmed value (manual B.20): it must be 0
    assert ("program_current", 0.0) in spy.calls[:i_on]


def test_ramp_never_steps_faster_than_the_rate(rig):
    supply, spy, clock, _ = rig
    supply.cfg.ramp.rate_A_per_s = 0.5
    supply.set_current(2.0)
    supply.set_output(True)
    supply.step()
    run_for(supply, clock, 1.0, dt=0.05)
    s = supply.status()
    assert s.ramping is True
    assert s.programmed == pytest.approx(0.5, abs=1e-9)   # 0.5 A/s for 1 s
    run_for(supply, clock, 4.0, dt=0.05)
    s = supply.status()
    assert s.programmed == 2.0 and s.ramping is False
    vals = [0.0] + _mains(spy)
    steps = [abs(b - a) for a, b in zip(vals, vals[1:])]
    assert max(steps) <= 0.5 * 0.05 + 1e-9


def test_output_off_ramps_to_zero_before_outp_off(rig):
    supply, spy, clock, _ = rig
    supply.cfg.ramp.rate_A_per_s = 1.0
    supply.set_current(1.0)
    supply.set_output(True)
    supply.step()
    run_for(supply, clock, 2.0)
    assert supply.status().programmed == 1.0
    spy.calls.clear()
    supply.set_output(False)
    supply.step()
    assert supply.status().output is True          # still on: ramping down
    assert supply.status().ramping is True
    run_for(supply, clock, 2.0)
    assert supply.status().output is False
    names = [c[0] for c in spy.calls]
    i_off = names.index("set_output")
    before = [c[1] for c in spy.calls[:i_off] if c[0] == "program_current"]
    assert before[-1] == 0.0                       # at zero BEFORE OUTP OFF
    assert len(before) >= 15                       # walked, not stepped
    assert spy.calls[i_off] == ("set_output", False)


def test_shutdown_ramps_down_within_the_budget():
    cfg = Config()
    cfg.ramp.rate_A_per_s = 0.1                    # 20 s from 2 A at this rate...
    cfg.safety.shutdown_ramp_s = 1.0               # ...but shutdown must take ~1 s
    supply, sim = build_sim_system(cfg, seed=0)
    supply.start(poll=False)
    supply.set_current(2.0)
    supply.cfg.ramp.enabled = False                # get to 2 A quickly for the test
    supply.set_output(True)
    supply.step()
    assert supply.status().programmed == 2.0
    supply.cfg.ramp.enabled = True
    t0 = time.monotonic()
    supply.shutdown()
    took = time.monotonic() - t0
    assert sim.output_on is False
    assert took < 2.5
    assert supply.status().connected is False


def test_output_off_now_skips_the_ramp(rig):
    supply, spy, clock, events = rig
    supply.set_current(1.0)
    supply.set_output(True)
    supply.step()
    run_for(supply, clock, 1.0)
    assert supply.status().programmed > 0
    supply.output_off_now()
    supply.step()
    assert supply.status().output is False
    assert spy.inner.output_on is False
    assert any(lvl == "warn" and "NOW" in m for lvl, m in events)


def test_watchdog_ramps_down_when_no_client_speaks(rig):
    supply, spy, clock, events = rig
    supply.cfg.safety.watchdog_s = 1.0
    supply.set_current(0.2)
    supply.set_output(True)
    supply.touch()
    supply.step()
    run_for(supply, clock, 0.8)
    supply.touch()                                  # a ping in time
    run_for(supply, clock, 0.8)
    assert supply.status().output_request is True
    run_for(supply, clock, 3.0)                     # silence
    assert supply.status().output is False
    assert any("no client" in m for _, m in events)


# ---- clamps and refusals -------------------------------------------------------

def test_setpoints_clamp_and_warn(rig):
    supply, spy, clock, events = rig
    supply.set_current(99.0)
    assert supply.cfg.output.current_A == supply.cfg.limits.current_max_A
    supply.set_current(-99.0)
    assert supply.cfg.output.current_A == supply.cfg.limits.current_min_A
    supply.set_voltage_limit(-50.0)                 # limit is an absolute value
    assert supply.cfg.output.voltage_limit_V == 20.0
    supply.step()
    s = supply.status()
    assert s.current_set_A == -10.0 and s.voltage_limit_V == 20.0
    assert sum("clamped" in m for lvl, m in events if lvl == "warn") >= 3


def test_limits_can_be_narrowed_and_reclamp(rig):
    supply, spy, clock, _ = rig
    supply.set_current(5.0)
    supply.cfg.limits.current_max_A = 2.0
    supply.apply_config()
    supply.step()
    assert supply.status().current_set_A == 2.0


def test_wrong_mode_setters_are_refused(rig):
    supply, *_ = rig
    with pytest.raises(ValueError, match="current mode"):
        supply.set_voltage(1.0)
    supply.set_mode("voltage")
    with pytest.raises(ValueError, match="voltage mode"):
        supply.set_current(1.0)


def test_mode_change_refused_while_output_is_on(rig):
    supply, spy, clock, _ = rig
    supply.set_output(True)
    supply.step()
    with pytest.raises(ValueError, match="output off"):
        supply.set_mode("voltage")
    supply.set_output(False)
    run_for(supply, clock, 0.5)                     # includes the hold at zero
    supply.set_mode("voltage")
    supply.step()
    assert spy.inner.mode == "voltage"
    assert supply.status().mode == "voltage"


def test_non_finite_values_are_refused(rig):
    supply, *_ = rig
    with pytest.raises(ValueError):
        supply.set_current(float("nan"))


# ---- threads -------------------------------------------------------------------

def test_status_never_touches_hardware(rig):
    supply, spy, clock, _ = rig
    n = len(spy.calls)
    for _ in range(50):
        supply.status()
    assert len(spy.calls) == n


def test_setters_do_not_touch_hardware_or_the_snapshot(rig):
    """Gotcha #1: setters change attributes; only the worker acts and rebuilds."""
    supply, spy, clock, _ = rig
    n = len(spy.calls)
    before = supply.status()
    supply.set_current(1.5)
    supply.set_voltage_limit(3.0)
    assert len(spy.calls) == n
    assert supply.status().current_set_A == before.current_set_A   # not yet adopted
    supply.step()
    assert supply.status().current_set_A == 1.5


# ---- the simulated physics -----------------------------------------------------

def test_compliance_limits_a_coil_current_step(rig):
    """With the ramp off, a 2 A step into R=2, L=0.1 hits the 3 V compliance:
    the current slews at (Vlim - IR)/L and settles at Vlim/R = 1.5 A."""
    supply, spy, clock, _ = rig
    supply.cfg.ramp.enabled = False
    supply.set_voltage_limit(3.0)
    supply.set_current(2.0)
    supply.set_output(True)
    supply.step()
    clock.advance(0.25); supply.step()          # past one measurement period
    s = supply.status()
    assert s.at_limit is True
    run_for(supply, clock, 1.0)
    assert spy.inner.true_current == pytest.approx(1.5, abs=0.01)


def test_voltage_mode_settles_at_v_over_r(rig):
    supply, spy, clock, _ = rig
    supply.set_mode("voltage")
    supply.set_current_limit(5.0)
    supply.set_voltage(4.0)
    supply.cfg.ramp.rate_V_per_s = 20.0
    supply.set_output(True)
    supply.step()
    run_for(supply, clock, 1.5)
    assert spy.inner.true_current == pytest.approx(4.0 / 2.0, abs=0.01)
    assert supply.status().at_limit is False


# ---- acquisition ----------------------------------------------------------------

def test_acquire_counts_only_readings_after_the_settle_time(rig):
    supply, spy, clock, _ = rig
    supply.cfg.acquisition.settle_s = 0.35
    supply.cfg.acquisition.readings = 3
    supply.cfg.hardware.poll_hz = 5.0
    n = supply.acquire()
    supply.step()
    s = supply.status()
    assert s.acq_id == n and s.acquiring is True
    run_for(supply, clock, 0.3)                 # still inside the settle time
    assert supply.status().acquiring is True
    run_for(supply, clock, 1.0)
    s = supply.status()
    assert s.acquiring is False and s.sample["acq_id"] == n
    assert s.sample["n"] == 3
    assert "current_std_A" in s.sample


def test_acquire_with_the_worker_thread_measures_the_setpoint():
    cfg = Config()
    cfg.ramp.rate_A_per_s = 5.0
    supply, sim = build_sim_system(cfg, seed=3)
    supply.start()
    try:
        supply.set_current(1.2)
        supply.set_output(True)
        t_end = time.monotonic() + 5
        while time.monotonic() < t_end:
            s = supply.status()
            if s.output and not s.ramping and s.current_set_A == 1.2:
                break
            time.sleep(0.02)
        time.sleep(0.2)
        n = supply.acquire()
        t_end = time.monotonic() + 5
        while time.monotonic() < t_end:
            s = supply.status()
            if s.acq_id == n and not s.acquiring:
                break
            time.sleep(0.02)
        assert s.sample["acq_id"] == n
        assert s.sample["current_A"] == pytest.approx(1.2, abs=0.01)
        assert s.sample["voltage_V"] == pytest.approx(1.2 * cfg.sim.load_R_ohm, abs=0.02)
    finally:
        supply.shutdown()
    assert sim.output_on is False


def test_ramp_step_rate_is_capped_at_the_bit4886_limit():
    # manual sec. 4.1.1.3: 25 ms is the fastest ramp step the card takes
    cfg = Config()
    cfg.ramp.step_hz = 100.0
    supply, _ = build_sim_system(cfg, seed=0)
    assert supply.cfg.ramp.step_hz == 40.0


def test_no_frame_says_settled_before_the_ramp_has_arrived(rig):
    """Walk every snapshot a scan-core wait could see (gotcha #2): whenever the
    new setpoint is adopted AND ramping is False, the output must already BE
    at the setpoint. A frame from before the command must not qualify."""
    supply, spy, clock, _ = rig
    supply.set_current(0.5)
    supply.set_output(True)
    run_for(supply, clock, 2.0)
    assert supply.status().programmed == 0.5 and not supply.status().ramping
    supply.set_current(1.5)                      # the scan's next point
    seen_settled = False
    for _ in range(80):                          # 4 s of worker steps
        s = supply.status()
        settled = (s.current_set_A == 1.5) and not s.ramping
        if settled:
            assert s.programmed == 1.5
            seen_settled = True
        clock.advance(0.05)
        supply.step()
    assert seen_settled
