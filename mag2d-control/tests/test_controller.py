"""The brain on the simulated magnet, on SIMULATED TIME.

Every test gives the same FakeClock to the simulator and the controller and calls
tick() in a loop, so a 5-second settle runs in milliseconds and is repeatable
(seeded noise). No thread, no sleep, no flakiness.
"""

import math

import pytest

from mag2d.backends.sim import FakeClock
from mag2d.config import Config
from mag2d.controller import Refused, WaterInterlockError
from mag2d.sim_system import build_sim_system


@pytest.fixture
def rig():
    cfg = Config()
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


def run_until_stable(ctrl, clock, limit_s=20.0):
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
    cfg, clock, ctrl, sim, events = rig
    ctrl.start(run_thread=False)
    run(ctrl, clock, 1.0)
    return rig


# ---------------------------------------------------------------- settling

def test_start_energizes_at_zero_and_is_stable(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    s = ctrl.status()
    assert s.energized and sim.enable
    assert s.state == "STABLE" and s.field_stable
    assert s.setpoint_field_mT == 0.0 and abs(s.measured_magnitude_mT) < 1.0


def test_settles_to_a_polar_target_within_a_few_seconds(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(150.0, 45.0)
    t = run_until_stable(ctrl, clock)
    assert t is not None and t < 5.0, f"150 mT @ 45 deg took {t} s"
    tol = cfg.control.tolerance_mT
    bx, by = sim.true_field()
    assert abs(bx - 150 * math.cos(math.radians(45))) <= tol
    assert abs(by - 150 * math.sin(math.radians(45))) <= tol
    s = ctrl.status()
    assert abs(s.measured_angle_deg - 45.0) < 0.5
    assert abs(s.measured_field_mT - 150.0) < 1.0


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


def test_small_step_is_fast(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(20.0, 30.0)
    assert run_until_stable(ctrl, clock) is not None
    run(ctrl, clock, 0.5)
    ctrl.set_field(21.0)
    t = run_until_stable(ctrl, clock)
    assert t is not None and t < 0.8, f"a 1 mT step took {t} s"


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


# ---------------------------------------------------------------- output

def test_output_off_ramps_down_then_releases_enable(rig):
    cfg, clock, ctrl, sim, events = started(rig)
    ctrl.set_field(100.0, 0.0)
    assert run_until_stable(ctrl, clock) is not None
    ctrl.set_output(False)
    s = ctrl.status()
    assert s.state == "RAMP_DOWN" and s.energized and not s.field_stable
    run(ctrl, clock, 5.0)
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
    run(ctrl, clock, 1.0)
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

    run(ctrl, clock, 5.0)
    s = ctrl.status()
    assert s.output_V == [0.0, 0.0] and not s.energized and not sim.enable
    assert _max_step(sim, cfg) <= cfg.control.slew_V_per_s / cfg.control.loop_hz + 1e-9

    for call in (lambda: ctrl.set_field(10.0), lambda: ctrl.set_angle(10.0),
                 lambda: ctrl.set_vector(1.0, 1.0), lambda: ctrl.set_bx(1.0),
                 lambda: ctrl.set_by(1.0), lambda: ctrl.set_output(True)):
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
    run(ctrl, clock, 1.0)
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


def test_status_never_touches_the_hardware(rig):
    cfg, clock, ctrl, sim, events = started(rig)

    def boom(*a):
        raise AssertionError("status() called the backend")
    sim.read_hall = sim.read_temps = sim.read_water = sim.write_ao = boom
    ctrl.status()
