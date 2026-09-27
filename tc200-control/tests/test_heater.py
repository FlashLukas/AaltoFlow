"""The Heater brain against the simulated TC200, on a FAKE clock: the block
takes minutes to heat, and the tests move time forward by hand instead of
waiting. Covers adopt-at-start, clamps, the toggle-safe enable, the sensor
interlock, alarms, reached/not-reached and the shutdown policy."""

import math

import pytest

from tc200.config import Config
from tc200.sim_system import build_sim_system


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _make(cfg=None, **sim):
    clock = Clock()
    cfg = cfg or Config()
    sim.setdefault("seed", 1)
    heater, box = build_sim_system(cfg, clock=clock, **sim)
    events = []
    heater._on_event = lambda lvl, msg: events.append((lvl, msg))
    return heater, box, clock, events


def _run(heater, clock, seconds, step=1.0):
    n = int(seconds / step)
    for _ in range(n):
        clock.t += step
        heater.poll_once()


# ---- start: adopt, command nothing -------------------------------------------

def test_start_adopts_everything_and_commands_nothing():
    heater, box, clock, events = _make(temperature_C=50.0, setpoint_C=55.0, enabled=True,
                                       p_gain=80, i_gain=3, d_gain=1, pmax_W=7.5,
                                       tmax_C=90.0)
    heater.start(poll=False)
    s = heater.status()
    assert s.connected and s.simulated
    assert s.setpoint_C == 55.0 and s.enabled is True
    assert (s.p_gain, s.i_gain, s.d_gain) == (80, 3, 1)
    assert s.pmax_W == 7.5 and s.tmax_C == 90.0
    # adopted into cfg too, so the Settings dialog shows the truth
    assert heater.cfg.device.p_gain == 80 and heater.cfg.device.tmax_C == 90.0
    # and the box was not touched
    assert box.enabled is True and box.tset == 55.0
    heater.cfg.hardware.disable_on_shutdown = False
    heater.shutdown()


def test_start_never_switches_a_cold_heater_on():
    heater, box, clock, _ = _make(enabled=False)
    heater.start(poll=False)
    assert box.enabled is False and heater.status().enabled is False
    heater.shutdown()


def test_push_on_start_pushes_the_stored_settings():
    cfg = Config()
    cfg.hardware.push_on_start = True
    cfg.device.p_gain, cfg.device.pmax_W, cfg.device.tmax_C = 60, 4.0, 80.0
    heater, box, clock, events = _make(cfg, p_gain=125, pmax_W=10.0, tmax_C=120.0)
    heater.start(poll=False)
    assert box.p == 60 and box.pmax == 4.0 and box.tmax == 80.0
    assert heater.status().tmax_C == 80.0
    assert box.enabled is False                  # pushing settings is not heating
    heater.shutdown()


def test_wrong_sensor_is_announced_at_start():
    heater, box, clock, events = _make(sensor="ptc1000")
    heater.start(poll=False)
    assert heater.status().sensor_ok is False
    assert any(lvl == "warn" and "ptc1000" in m for lvl, m in events)
    heater.shutdown()


# ---- setpoint --------------------------------------------------------------------

def test_setpoint_clamps_and_warns():
    heater, box, clock, events = _make(tmax_C=120.0)
    heater.start(poll=False)
    heater.set_temperature(5.0)
    assert heater.status().setpoint_C == 20.0
    heater.set_temperature(500.0)
    # ceiling = min(limit 100, TMAX 120 - margin 5)
    assert heater.status().setpoint_C == 100.0 and box.tset == 100.0
    assert sum(1 for lvl, m in events if lvl == "warn" and "clamped" in m) == 2
    with pytest.raises(ValueError):
        heater.set_temperature(float("nan"))
    heater.shutdown()


def test_ceiling_follows_tmax():
    heater, box, clock, events = _make(tmax_C=120.0, setpoint_C=60.0)
    heater.start(poll=False)
    assert heater.temperature_max() == 100.0
    heater.set_tmax(50.0)
    assert heater.temperature_max() == 45.0
    assert heater.status().temperature_max_C == 45.0
    # the box dragged its setpoint down with TMAX, and the brain adopted it
    assert box.tset == 50.0 and heater.status().setpoint_C == 50.0
    heater.set_temperature(80.0)
    assert heater.status().setpoint_C == 45.0
    heater.shutdown()


def test_setpoint_on_a_disabled_heater_warns():
    heater, box, clock, events = _make(enabled=False)
    heater.start(poll=False)
    heater.set_temperature(40.0)
    assert any(lvl == "warn" and "OFF" in m for lvl, m in events)
    heater.shutdown()


def test_front_panel_setpoint_change_is_adopted():
    heater, box, clock, events = _make(setpoint_C=30.0)
    heater.start(poll=False)
    box.tset = 42.0                                   # someone at the front panel
    _run(heater, clock, 1)
    assert heater.status().setpoint_C == 42.0
    assert any("adopted" in m for _, m in events)
    heater.shutdown()


def test_commanded_setpoint_is_kept_unrounded():
    """scan-core's adopt check compares the status with what it SENT; the box
    keeps one decimal, which must not count as a front-panel change."""
    heater, box, clock, _ = _make()
    heater.start(poll=False)
    heater.set_temperature(33.04)
    _run(heater, clock, 2)
    assert box.tset == 33.0
    assert heater.status().setpoint_C == 33.04
    heater.shutdown()


# ---- reached -----------------------------------------------------------------------

def test_reached_needs_heater_on_band_and_hold_time():
    cfg = Config()
    cfg.temperature.stable_time_s = 30.0
    heater, box, clock, _ = _make(cfg, temperature_C=22.0, setpoint_C=22.0)
    heater.start(poll=False)
    heater.set_temperature(40.0)
    _run(heater, clock, 600)
    assert heater.status().temperature_stable is False       # heater off: never
    heater.set_enabled(True)
    assert heater.status().temperature_stable is False
    _run(heater, clock, 10)
    assert heater.status().temperature_stable is False       # still heating
    _run(heater, clock, 600)
    s = heater.status()
    assert s.temperature_stable is True
    assert abs(s.temperature_C - 40.0) <= cfg.temperature.tolerance_C
    # a new setpoint clears the flag in the SAME step that stores it
    heater.set_temperature(41.0)
    assert heater.status().temperature_stable is False
    assert heater.status().setpoint_C == 41.0
    heater.shutdown()


def test_hold_time_is_honoured():
    cfg = Config()
    cfg.temperature.stable_time_s = 30.0
    heater, box, clock, _ = _make(cfg, temperature_C=40.0, setpoint_C=40.0, enabled=True,
                                  ambient_C=22.0)
    heater.start(poll=False)
    _run(heater, clock, 600)                  # settle the sim's integral first
    heater.set_temperature(40.05)             # inside the band from the start
    _run(heater, clock, 20)
    assert heater.status().temperature_stable is False
    _run(heater, clock, 15)
    assert heater.status().temperature_stable is True
    heater.shutdown()


def test_a_command_arriving_during_a_poll_does_not_count_that_poll():
    """gotcha #2 / #17 in brain form: readings taken BEFORE a new setpoint must
    not count towards it. Reproduced by issuing the command from inside the
    poll's own hardware read (the hardware lock is re-entrant, so this lands
    exactly between 'poll started' and 'poll stored its readings')."""
    cfg = Config()
    cfg.temperature.stable_time_s = 0.0      # without the guard ONE poll would flag it
    heater, box, clock, _ = _make(cfg, temperature_C=40.0, setpoint_C=40.0, enabled=True)
    heater.start(poll=False)
    _run(heater, clock, 600)                 # settle the sim's integral first
    assert heater.status().temperature_stable is True
    real_read = box.read_temperature
    fired = []

    def read_and_command():
        t = real_read()
        if not fired:
            fired.append(True)
            heater.set_temperature(40.1)     # inside the band: only the guard stops it
        return t

    box.read_temperature = read_and_command
    clock.t += 1.0
    heater.poll_once()
    assert fired and heater.status().setpoint_C == 40.1
    assert heater.status().temperature_stable is False
    box.read_temperature = real_read
    _run(heater, clock, 1)                   # the NEXT poll is after the command
    assert heater.status().temperature_stable is True
    heater.shutdown()


# ---- the output switch --------------------------------------------------------------

def test_enable_is_idempotent_although_ens_toggles():
    heater, box, clock, _ = _make(enabled=False)
    heater.start(poll=False)
    heater.set_enabled(True)
    heater.set_enabled(True)                  # a blind `ens` would switch it OFF here
    assert box.enabled is True
    heater.set_enabled(False)
    heater.set_enabled(False)
    assert box.enabled is False
    heater.shutdown()


def test_enable_refused_with_wrong_sensor_or_alarm():
    heater, box, clock, _ = _make(sensor="th10k")
    heater.start(poll=False)
    with pytest.raises(RuntimeError, match="sensor"):
        heater.set_enabled(True)
    assert box.enabled is False
    box.sensor = "ptc100"
    box.sensor_alarm = True
    with pytest.raises(RuntimeError, match="alarm"):
        heater.set_enabled(True)
    box.sensor_alarm = False
    box.cycle_mode = True
    with pytest.raises(RuntimeError, match="CYCLE"):
        heater.set_enabled(True)
    box.cycle_mode = False
    heater.set_enabled(True)
    assert box.enabled is True
    heater.shutdown()


def test_sensor_alarm_switches_off_and_is_reported():
    heater, box, clock, events = _make(enabled=True, setpoint_C=40.0)
    heater.start(poll=False)
    box.sensor_alarm = True
    _run(heater, clock, 1)
    s = heater.status()
    assert s.sensor_alarm is True and s.enabled is False and not s.temperature_stable
    assert any(lvl == "error" and "SENSOR" in m for lvl, m in events)
    heater.shutdown()


def test_tmax_trip_is_reported():
    heater, box, clock, events = _make(temperature_C=22.0, setpoint_C=22.0, enabled=True,
                                       tmax_C=40.0, pmax_W=18.0)
    heater.start(poll=False)
    box.tset = 60.0                               # set above TMAX behind our back
    _run(heater, clock, 300)
    s = heater.status()
    assert s.tmax_alarm is True
    assert any(lvl == "error" and "TMAX" in m for lvl, m in events)
    assert box.T < 45.0                           # the relay held it near TMAX
    heater.shutdown()


def test_set_sensor_refused_while_heating():
    heater, box, clock, _ = _make(enabled=True)
    heater.start(poll=False)
    with pytest.raises(RuntimeError):
        heater.set_sensor("ptc1000")
    heater.set_enabled(False)
    heater.set_sensor("ptc1000")
    assert box.sensor == "ptc1000" and heater.status().sensor_ok is False
    with pytest.raises(ValueError):
        heater.set_sensor("k-type")
    heater.shutdown()


# ---- stored settings ------------------------------------------------------------------

def test_gain_pmax_tmax_clamps():
    heater, box, clock, events = _make()
    heater.start(poll=False)
    heater.set_p_gain(0)
    heater.set_i_gain(999)
    heater.set_d_gain(-3)
    assert (box.p, box.i, box.d) == (1, 250, 0)
    heater.set_pmax(99.0)
    assert box.pmax == heater.cfg.limits.pmax_max_W
    heater.cfg.limits.pmax_max_W = 5.0
    heater.set_pmax(12.0)
    assert box.pmax == 5.0
    heater.set_tmax(1000.0)
    assert box.tmax == 205.0
    assert sum(1 for lvl, m in events if lvl == "warn" and "clamped" in m) >= 5
    heater.shutdown()


def test_apply_config_pushes_only_what_changed():
    heater, box, clock, events = _make(p_gain=125, pmax_W=10.0)
    heater.start(poll=False)
    calls = []
    orig = box.set_pmax
    box.set_pmax = lambda w: (calls.append(w), orig(w))
    heater.apply_config()                    # nothing changed -> nothing sent
    assert calls == []
    heater.cfg.device.pmax_W = 6.0
    heater.cfg.device.p_gain = 100
    heater.apply_config()
    assert calls == [6.0] and box.p == 100
    heater.shutdown()


def test_apply_config_never_touches_setpoint_or_output():
    heater, box, clock, events = _make(setpoint_C=80.0, enabled=True)
    heater.start(poll=False)
    heater.cfg.limits.temperature_max_C = 50.0
    heater.apply_config()
    assert box.tset == 80.0 and box.enabled is True
    assert any("outside the new limits" in m for _, m in events)
    heater.cfg.hardware.disable_on_shutdown = False
    heater.shutdown()


def test_front_panel_gain_change_is_adopted():
    heater, box, clock, events = _make()
    heater.start(poll=False)
    box.p = 42
    _run(heater, clock, 10)                   # past settings_poll_s
    assert heater.status().p_gain == 42 and heater.cfg.device.p_gain == 42
    heater.shutdown()


# ---- link loss and shutdown -------------------------------------------------------------

def test_read_failure_shows_as_hw_error_and_recovers():
    heater, box, clock, events = _make()
    heater.start(poll=False)
    orig = box.read_temperature

    def broken():
        raise OSError("port gone")
    box.read_temperature = broken
    _run(heater, clock, 1)
    assert "port gone" in heater.status().hw_error
    box.read_temperature = orig
    _run(heater, clock, 1)
    assert heater.status().hw_error == ""
    heater.shutdown()


def test_shutdown_switches_heater_off_by_default():
    heater, box, clock, events = _make(enabled=True)
    heater.start(poll=False)
    heater.shutdown()
    assert box.enabled is False
    assert any("switched OFF" in m for _, m in events)
    heater.shutdown()                        # twice is harmless


def test_shutdown_can_leave_it_heating():
    cfg = Config()
    cfg.hardware.disable_on_shutdown = False
    heater, box, clock, _ = _make(cfg, enabled=True)
    heater.start(poll=False)
    heater.shutdown()
    assert box.enabled is True


def test_commands_refused_when_not_connected():
    heater, box, clock, _ = _make()
    with pytest.raises(RuntimeError):
        heater.set_temperature(30.0)
    s = heater.status()
    assert s.connected is False and math.isnan(s.temperature_C)


def test_poll_thread_runs_and_stops():
    import time
    cfg = Config()
    cfg.hardware.poll_s = 0.05
    heater, box = build_sim_system(cfg)
    heater.start()
    time.sleep(0.4)
    assert heater.status().readings >= 3
    heater.shutdown()
    n = heater.status().readings
    time.sleep(0.2)
    assert heater.status().readings == n


def test_sim_box_disables_when_its_sensor_is_changed_while_heating():
    """Manual 5.6.3: changing the sensor while enabled disables the heater.
    The brain refuses first; this pins the sim to the box's own behaviour, so a
    brain that ever skipped its check would be caught as an output that went OFF."""
    heater, box, clock, events = _make(enabled=True)
    box.set_sensor("ptc1000")
    assert box.enabled is False and box.sensor == "ptc1000"
