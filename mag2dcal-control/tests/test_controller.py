"""The brain on the simulated magnet, on SIMULATED TIME.

Every test gives the same FakeClock to the simulator and the controller and calls
tick() in a loop, so a 5-second settle runs in milliseconds and is repeatable
(seeded noise). No thread, no sleep, no flakiness.

The calibration machinery is in test_calibration.py and the freeze argument in
test_freeze.py; this file is the state machine, the setpoint model, the
interlocks and the stabilizer.
"""

import math

import pytest

from mag2dcal.backends.sim import FakeClock
from mag2dcal.config import Config
from mag2dcal.controller import Refused, WaterInterlockError
from mag2dcal.sim_system import build_sim_system


@pytest.fixture
def rig(tmp_path):
    cfg = Config()
    # Never touch the project's real Calibrations folder from a test, and start
    # UNCALIBRATED so these tests exercise the straight-line fallback.
    cfg.calibration.directory = str(tmp_path)
    cfg.calibration.load_newest_on_start = False
    clock = FakeClock()
    ctrl, sim = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=3)
    events = []
    ctrl._on_event = lambda level, msg: events.append((level, msg))
    return cfg, clock, ctrl, sim, events


def run(ctrl, clock, seconds):
    dt = 1.0 / ctrl.cfg.control.loop_hz
    for _ in range(int(round(seconds / dt))):
        clock.advance(dt)
        ctrl.tick()


def run_until_stable(ctrl, clock, limit_s=30.0):
    """Seconds until field_stable, or None."""
    dt = 1.0 / ctrl.cfg.control.loop_hz
    t0 = clock()
    while clock() - t0 < limit_s:
        clock.advance(dt)
        ctrl.tick()
        if ctrl.status().field_stable:
            return clock() - t0
    return None


def started(rig):
    """Start and let the magnet settle at 0 mT.

    3 s, not mag2d's 1 s: even a zero setpoint is approached the same way as any
    other -- undershoot by field_step_mT, then trim back up at the trim rate --
    so arriving takes about two seconds. That is the price of always knowing
    which hysteresis branch the field is on.
    """
    cfg, clock, ctrl, sim, events = rig
    ctrl.start(run_thread=False)
    run(ctrl, clock, 3.0)
    return rig


# ---------------------------------------------------------------- settling

def test_start_energizes_at_zero_and_is_stable(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    s = ctrl.status()
    assert s.energized and sim.enable
    assert s.state == "STABLE" and s.field_stable
    assert s.setpoint_field_mT == 0.0 and abs(s.measured_magnitude_mT) < 1.0
    assert not s.calibrated
    assert any(lvl == "warn" and "no calibration" in m for lvl, m in events)


def test_settles_to_a_polar_target_and_freezes(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(150.0, 45.0)
    t = run_until_stable(ctrl, clock)
    assert t is not None and t < 10.0, f"150 mT @ 45 deg took {t} s"
    tol = cfg.control.tolerance_mT
    bx, by = sim.true_field()
    assert abs(bx - 150 * math.cos(math.radians(45))) <= tol
    assert abs(by - 150 * math.sin(math.radians(45))) <= tol
    s = ctrl.status()
    assert abs(s.measured_angle_deg - 45.0) < 0.5
    assert abs(s.measured_field_mT - 150.0) < 1.0
    # ... and the drive has stopped moving, which is this module's whole point.
    # The stabilizer is switched off first: it is allowed to nudge a frozen
    # output, and here we are asking what the FAST loop does.
    assert s.frozen
    cfg.stabilizer.enabled = False
    ctrl.set_stabilizer(False)
    before = list(sim.ao)
    run(ctrl, clock, 2.0)
    assert sim.ao == before


def test_the_state_goes_seek_then_hold_then_stable(rig):
    """HOLD is the visible moment between "stopped pushing" and "confirmed"."""
    cfg, clock, ctrl, sim, events = started(rig)
    cfg.control.stable_time_s = 1.0         # long enough to catch HOLD
    ctrl.set_field(40.0, 0.0)
    seen = []
    dt = 1.0 / cfg.control.loop_hz
    for _ in range(int(30.0 / dt)):
        clock.advance(dt)
        ctrl.tick()
        st = ctrl.status().state
        if not seen or seen[-1] != st:
            seen.append(st)
        if ctrl.status().field_stable:
            break
    assert seen[0] == "SEEK" and seen[-1] == "STABLE"
    assert "HOLD" in seen, seen


def test_settles_to_a_cartesian_target(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_vector(-60.0, 80.0)
    assert run_until_stable(ctrl, clock) is not None
    s = ctrl.status()
    assert s.setpoint_field_mT == 100.0            # hypot(-60, 80)
    assert abs(s.setpoint_angle_deg - math.degrees(math.atan2(80, -60))) < 1e-9
    bx, by = sim.true_field()
    assert abs(bx + 60.0) <= cfg.control.tolerance_mT
    assert abs(by - 80.0) <= cfg.control.tolerance_mT


def test_a_small_step_still_arrives_from_the_chosen_side(rig):
    """A 1 mT step first backs off by field_step_mT and comes back. That looks
    wasteful and is deliberate: it is the only way to know which hysteresis
    branch the field ends up on."""
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(20.0, 30.0)
    assert run_until_stable(ctrl, clock) is not None
    before = sim.true_field()[0]
    ctrl.set_field(21.0)
    # it goes DOWN first (the undershoot), then up onto the target
    run(ctrl, clock, 0.4)
    assert sim.true_field()[0] < before
    t = run_until_stable(ctrl, clock)
    assert t is not None and t < 6.0, f"a 1 mT step took {t} s"


def test_angle_only_change_keeps_the_magnitude(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(80.0, 0.0)
    assert run_until_stable(ctrl, clock) is not None
    ctrl.set_angle(90.0)
    s = ctrl.status()
    assert s.setpoint_field_mT == 80.0 and s.setpoint_angle_deg == 90.0
    assert run_until_stable(ctrl, clock) is not None
    s = ctrl.status()
    assert abs(s.measured_magnitude_mT - 80.0) < 1.0
    assert abs(s.measured_angle_deg - 90.0) < 0.5


def test_set_field_without_angle_keeps_the_angle(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(50.0, 30.0)
    ctrl.set_field(-40.0)                          # signed magnitude, angle kept
    s = ctrl.status()
    assert s.setpoint_angle_deg == 30.0 and s.setpoint_field_mT == -40.0
    assert s.setpoint_bx_mT < 0 and s.setpoint_by_mT < 0


def test_set_bx_and_set_by_keep_the_other_component(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_vector(30.0, 40.0)
    ctrl.set_bx(-12.5)
    s = ctrl.status()
    assert s.setpoint_bx_mT == -12.5 and s.setpoint_by_mT == 40.0
    ctrl.set_by(7.25)
    s = ctrl.status()
    assert s.setpoint_bx_mT == -12.5 and s.setpoint_by_mT == 7.25
    assert abs(s.setpoint_field_mT - math.hypot(-12.5, 7.25)) < 1e-12


# ---------------------------------------------------------------- setpoint model

def test_setpoints_are_echoed_exactly_as_commanded(rig):
    """scan-core waits for |echo - sent| <= 1e-6; any tidying-up would hang a scan."""
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(12.345678901234, 33.333333333333)
    s = ctrl.status()
    assert s.setpoint_field_mT == 12.345678901234
    assert s.setpoint_angle_deg == 33.333333333333
    ctrl.set_angle(359.999999)                      # NOT normalised to -0.000001
    assert ctrl.status().setpoint_angle_deg == 359.999999
    ctrl.set_bx(7.123456789)
    assert ctrl.status().setpoint_bx_mT == 7.123456789
    ctrl.set_by(-0.1)
    assert ctrl.status().setpoint_by_mT == -0.1


def test_a_new_setpoint_clears_stable_in_the_same_snapshot(rig):
    """No status may show the NEW setpoint with the OLD point's field_stable."""
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(30.0, 0.0)
    assert run_until_stable(ctrl, clock) is not None
    ctrl.set_field(30.2)                            # inside tolerance of the old field
    s = ctrl.status()
    assert s.setpoint_field_mT == 30.2 and not s.field_stable
    assert run_until_stable(ctrl, clock) is not None


def test_clamps_emit_warn_events(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    events.clear()
    ctrl.set_field(500.0, 1000.0)
    s = ctrl.status()
    assert s.setpoint_field_mT == cfg.limits.field_max_mT
    assert s.setpoint_angle_deg == cfg.limits.angle_max_deg
    warns = [m for lvl, m in events if lvl == "warn"]
    assert any("field" in m for m in warns) and any("angle" in m for m in warns)

    events.clear()
    ctrl.set_vector(300.0, 400.0)                   # |B| 500 -> 180, direction kept
    s = ctrl.status()
    assert abs(math.hypot(s.setpoint_bx_mT, s.setpoint_by_mT) - 180.0) < 1e-9
    assert abs(s.setpoint_by_mT / s.setpoint_bx_mT - 4 / 3) < 1e-12
    assert any(lvl == "warn" for lvl, _ in events)

    with pytest.raises(ValueError):
        ctrl.set_field(float("nan"))


# ---------------------------------------------------------------- stabilizer

def _drift_trial(tmp_path, enabled, drift=-0.02, hold_s=40.0):
    cfg = Config()
    cfg.calibration.load_newest_on_start = False
    cfg.calibration.directory = str(tmp_path)
    cfg.stabilizer.enabled = enabled
    clock = FakeClock()
    ctrl, sim = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=2)
    ctrl.start(run_thread=False)
    ctrl.set_field(40.0, 0.0)
    assert run_until_stable(ctrl, clock) is not None
    cfg.sim.drift_mT_per_s = drift               # now the field starts to sag
    worst = 0.0
    dt = 1.0 / cfg.control.loop_hz
    for _ in range(int(hold_s / dt)):
        clock.advance(dt)
        ctrl.tick()
        worst = max(worst, abs(ctrl.status().setpoint_bx_mT - sim.true_field()[0]))
    return worst, ctrl.status()


def test_the_stabilizer_removes_a_slow_drift(tmp_path):
    """The freeze deliberately stops correcting, so something has to answer a
    drift -- slowly, and only once it is worth answering."""
    with_it, st_on = _drift_trial(tmp_path, True)
    without, st_off = _drift_trial(tmp_path, False)
    assert with_it < without, (with_it, without)
    assert st_on.stabilizer and not st_off.stabilizer
    # it holds the field near the deadband instead of letting it run to the
    # edge of the tolerance band
    assert with_it < 0.8 * without


def test_the_stabilizer_does_nothing_inside_its_deadband(tmp_path):
    """A correction that is not needed is just another direction flip."""
    cfg = Config()
    cfg.calibration.load_newest_on_start = False
    clock = FakeClock()
    ctrl, sim = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=2)
    ctrl.start(run_thread=False)
    ctrl.set_field(40.0, 0.0)
    assert run_until_stable(ctrl, clock) is not None
    assert abs(ctrl.status().error_mT) < cfg.stabilizer.deadband_mT
    before = list(sim.ao)
    run(ctrl, clock, 20.0)                       # twenty stabilizer periods
    assert sim.ao == before


def test_set_stabilizer_is_reported_in_status(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    assert ctrl.status().stabilizer is True
    ctrl.set_stabilizer(False)
    assert ctrl.status().stabilizer is False and cfg.stabilizer.enabled is False
    ctrl.set_stabilizer(True)
    assert ctrl.status().stabilizer is True


# ---------------------------------------------------------------- output

def test_output_off_ramps_down_then_releases_enable(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(100.0, 0.0)
    assert run_until_stable(ctrl, clock) is not None
    ctrl.set_output(False)
    s = ctrl.status()
    assert s.state == "RAMP_DOWN" and s.energized and not s.field_stable
    run(ctrl, clock, 8.0)
    s = ctrl.status()
    assert s.state == "OFF" and not s.energized and not sim.enable
    assert s.output_V == [0.0, 0.0]
    assert _max_step(sim, cfg) <= cfg.control.slew_V_per_s / cfg.control.loop_hz + 1e-9

    ctrl.set_output(True)
    assert run_until_stable(ctrl, clock) is not None     # back at 100 mT
    assert abs(ctrl.status().measured_magnitude_mT - 100.0) < 1.0


def _max_step(sim, cfg):
    log = sim.ao_log
    return max(max(abs(b[1] - a[1]), abs(b[2] - a[2])) for a, b in zip(log, log[1:]))


def test_not_stable_while_off(rig):
    cfg, clock, ctrl, sim, events = rig
    cfg.control.energize_on_start = False
    ctrl.start(run_thread=False)
    run(ctrl, clock, 2.0)
    s = ctrl.status()
    assert s.state == "OFF" and not s.energized and not s.field_stable


# ---------------------------------------------------------------- interlocks

def test_water_off_at_start_refuses_unless_bypassed(rig):
    cfg, clock, ctrl, sim, events = rig
    cfg.sim.water_ok = False
    with pytest.raises(WaterInterlockError):
        ctrl.start(run_thread=False)
    assert not sim.is_open and not sim.enable       # closed again, nothing energized

    cfg.interlock.water_bypass = True
    ctrl.start(run_thread=False)
    run(ctrl, clock, 3.0)
    s = ctrl.status()
    assert s.state == "STABLE" and not s.water_ok and s.water_bypass


def test_water_lost_while_running_faults_ramps_down_and_refuses(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(150.0, 45.0)
    assert run_until_stable(ctrl, clock) is not None

    sim.p.water_ok = False
    run(ctrl, clock, 0.1)
    s = ctrl.status()
    assert s.state == "FAULT" and "water" in s.fault and not s.field_stable
    assert s.setpoint_field_mT == 0.0               # clearing later cannot jump back
    assert any(lvl == "error" and "FAULT" in m for lvl, m in events)

    run(ctrl, clock, 8.0)
    s = ctrl.status()
    assert s.output_V == [0.0, 0.0] and not s.energized and not sim.enable
    assert _max_step(sim, cfg) <= cfg.control.slew_V_per_s / cfg.control.loop_hz + 1e-9

    for call in (lambda: ctrl.set_field(10.0), lambda: ctrl.set_angle(10.0),
                 lambda: ctrl.set_vector(1.0, 1.0), lambda: ctrl.set_bx(1.0),
                 lambda: ctrl.set_by(1.0), lambda: ctrl.set_output(True),
                 lambda: ctrl.calibrate()):
        with pytest.raises(Refused):
            call()
    with pytest.raises(Refused):
        ctrl.clear_fault()                          # water still off

    sim.p.water_ok = True
    run(ctrl, clock, 0.1)
    assert ctrl.status().state == "FAULT"           # latched until cleared
    ctrl.clear_fault()
    assert ctrl.status().state == "OFF" and ctrl.status().fault == ""
    ctrl.set_output(True)
    ctrl.set_field(20.0)
    assert run_until_stable(ctrl, clock) is not None


def test_water_bypass_prevents_the_fault(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_water_bypass(True)
    sim.p.water_ok = False
    run(ctrl, clock, 2.0)
    s = ctrl.status()
    assert s.state == "STABLE" and not s.water_ok and s.water_bypass
    assert any(lvl == "warn" and "BYPASS" in m for lvl, m in events)


def test_temperature_monitor(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    s = ctrl.status()
    assert all(abs(t - cfg.sim.ambient_C) < 1.0 for t in s.temp_C)

    cfg.interlock.max_temp_C = 30.0
    sim._temp = [35.0, 25.0]                        # coil 1 hot
    run(ctrl, clock, 0.2)
    assert ctrl.status().state != "FAULT"           # monitor is off by default

    cfg.interlock.temp_monitor = True
    run(ctrl, clock, 0.1)
    s = ctrl.status()
    assert s.state == "FAULT" and "temperature 1" in s.fault and s.temp_monitor

    with pytest.raises(Refused):
        ctrl.clear_fault()
    sim._temp = [25.0, 25.0]
    sim.p.ambient_C = 25.0
    run(ctrl, clock, 0.1)
    ctrl.clear_fault()


def test_hardware_read_failure_while_energized_faults(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(50.0, 0.0)
    run(ctrl, clock, 1.0)

    def broken():
        raise OSError("DAQ unplugged")
    real = sim.read_hall
    sim.read_hall = broken
    run(ctrl, clock, 0.1)
    s = ctrl.status()
    assert s.state == "FAULT" and "DAQ unplugged" in s.fault and "DAQ unplugged" in s.hw_error
    sim.read_hall = real
    run(ctrl, clock, 4.0)
    ctrl.clear_fault()
    assert ctrl.status().state == "OFF"


# ---------------------------------------------------------------- shutdown

def test_shutdown_ramps_to_zero_and_disables(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(150.0, 45.0)
    assert run_until_stable(ctrl, clock) is not None
    assert max(abs(v) for v in sim.ao) > 5.0
    t0 = clock()
    ctrl.shutdown()
    assert sim.ao == [0.0, 0.0] and not sim.enable and not sim.is_open
    # it RAMPED: took about |V| / slew, and never stepped faster than the slew
    assert clock() - t0 > 2.0
    assert _max_step(sim, cfg) <= cfg.control.slew_V_per_s / cfg.control.loop_hz + 1e-9
    ctrl.shutdown()                                  # idempotent


def test_shutdown_during_a_calibration_still_parks_the_coils(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.calibrate(n_per_leg=21, dwell_s=0.5, v_max=5.0)
    run(ctrl, clock, 3.0)
    assert ctrl.status().state == "CALIBRATE" and abs(sim.ao[0]) > 1.0
    ctrl.shutdown()
    assert sim.ao == [0.0, 0.0] and not sim.enable and not sim.is_open


def test_status_never_touches_the_hardware(rig):
    cfg, clock, ctrl, sim, events = started(rig)

    def boom(*a):
        raise AssertionError("status() called the backend")
    sim.read_hall = sim.read_temps = sim.read_water = sim.write_ao = boom
    ctrl.status()
