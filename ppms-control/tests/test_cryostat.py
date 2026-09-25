"""The Cryostat brain on the simulated DynaCool, with a FAKE clock.

A fake clock makes the ramps and hold times deterministic: the test moves time
forward and calls `poll_once()` itself, so nothing here sleeps or races."""

import math

import pytest

from ppms.config import Config
from ppms.cryostat import Cryostat
from ppms.backends.sim import SimulatedDynaCool


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make(field_mT=0.0, temperature_K=300.0, **cfg_changes):
    clock = Clock()
    cfg = Config()
    for k, v in cfg_changes.items():
        group, name = k.split("__")
        setattr(getattr(cfg, group), name, v)
    sim = SimulatedDynaCool(field_mT=field_mT, temperature_K=temperature_K,
                            clock=clock, noise=False)
    cryo = Cryostat(sim, cfg, clock=clock)
    events = []
    cryo._on_event = lambda lvl, msg: events.append((lvl, msg))
    cryo.start(poll=False)
    return cryo, sim, clock, events


def run(cryo, clock, seconds, step=0.25):
    """Advance the fake clock, polling like the poll thread would."""
    n = int(round(seconds / step))
    for _ in range(n):
        clock.advance(step)
        cryo.poll_once()


class RecordingSim(SimulatedDynaCool):
    """Remembers every setpoint command, to prove what was (not) sent."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.commands = []

    def set_field(self, *a):
        self.commands.append(("field",) + a)
        super().set_field(*a)

    def set_temperature(self, *a):
        self.commands.append(("temperature",) + a)
        super().set_temperature(*a)


def test_start_adopts_what_multivu_is_doing_and_commands_nothing():
    clock = Clock()
    sim = RecordingSim(field_mT=5000.0, temperature_K=2.0, clock=clock, noise=False)
    cryo = Cryostat(sim, Config(), clock=clock)
    cryo.start(poll=False)
    s = cryo.status()
    assert s.setpoint_field_mT == 5000.0 and s.setpoint_temperature_K == 2.0
    assert sim.commands == [], "starting the service must not move the cryostat"
    cryo.shutdown()
    assert sim.commands == [], "stopping the service must not move the cryostat"


def test_field_reached_needs_band_holding_and_hold_time():
    cryo, sim, clock, _ = make(field__stable_time_s=3.0)
    cryo.set_field(44.0)                      # 2 s at 22 mT/s
    run(cryo, clock, 1.0)
    s = cryo.status()
    assert s.field_status == "Ramping" and not s.field_stable
    run(cryo, clock, 1.5)                     # arrived at ~2 s, holding now
    s = cryo.status()
    assert s.field_status == "Holding (driven)"
    assert abs(s.measured_field_mT - 44.0) < 1e-9
    assert not s.field_stable, "reached before the hold time"
    run(cryo, clock, 3.0)
    assert cryo.status().field_stable


def test_a_new_setpoint_never_shows_next_to_the_old_reached_flag():
    """The adopt-then-flag trap: the first status after a set must carry the NEW
    setpoint AND field_stable False -- never the new setpoint with the old True."""
    cryo, sim, clock, _ = make(field__stable_time_s=0.0)
    cryo.set_field(10.0)
    run(cryo, clock, 2.0)
    assert cryo.status().field_stable
    cryo.set_field(20.0)                      # no poll in between
    s = cryo.status()
    assert s.setpoint_field_mT == 20.0
    assert s.field_stable is False


def test_readings_taken_before_a_command_do_not_count_for_it():
    """A poll whose reads straddle a new command must not score the new point."""
    cryo, sim, clock, _ = make(field__stable_time_s=0.0)
    cryo.set_field(10.0)
    run(cryo, clock, 2.0)
    # simulate: the poll read the hardware, THEN a command arrived, THEN it stores
    real_read = sim.read_chamber

    def read_chamber_then_command():
        cryo._field_gen += 1              # what set_field does inside its lock
        cryo._field_sp = 10.0             # same value: readings would look perfect
        return real_read()

    sim.read_chamber = read_chamber_then_command
    cryo._field_band.reset()
    cryo.poll_once()
    assert cryo.status().field_stable is False


def test_temperature_reached_needs_multivu_stable():
    cryo, sim, clock, _ = make(temperature__stable_time_s=1.0)
    sim.NEAR_S = 2.0
    cryo.set_temperature(299.0)               # 1 K at 20 K/min: 3 s
    run(cryo, clock, 3.5)
    s = cryo.status()
    assert s.temperature_status == "Near"
    assert abs(s.temperature_K - 299.0) < 1e-9
    assert not s.temperature_stable, "Near is not Stable"
    run(cryo, clock, 3.0)
    s = cryo.status()
    assert s.temperature_status == "Stable" and s.temperature_stable


def test_clamps_are_announced():
    cryo, sim, clock, events = make()
    cryo.set_field(-1e9)
    assert cryo.status().setpoint_field_mT == -cryo.cfg.limits.field_max_mT
    cryo.set_temperature(0.1)
    assert cryo.status().setpoint_temperature_K == cryo.cfg.limits.temperature_min_K
    cryo.set_field_rate(1e6)
    assert cryo.cfg.field.rate_mT_per_s == cryo.cfg.limits.field_rate_max_mT_per_s
    assert sum(1 for lvl, m in events if lvl == "warn" and "clamped" in m) == 3


def test_rate_and_approach_are_sent_with_the_next_setpoint():
    clock = Clock()
    sim = RecordingSim(clock=clock, noise=False)
    cryo = Cryostat(sim, Config(), clock=clock)
    cryo.start(poll=False)
    cryo.set_field_rate(5.0)
    cryo.set_field_approach("oscillate")
    assert sim.commands == []                 # settings alone move nothing
    cryo.set_field(100.0)
    assert sim.commands == [("field", 100.0, 5.0, "oscillate")]
    cryo.set_temperature_rate(2.0)
    cryo.set_temperature_approach("no_overshoot")
    cryo.set_temperature(10.0)
    assert sim.commands[-1] == ("temperature", 10.0, 2.0, "no_overshoot")


def test_bad_values_are_refused():
    cryo, *_ = make()
    with pytest.raises(ValueError):
        cryo.set_field_approach("persistent")   # the DynaCool is driven-only
    with pytest.raises(ValueError):
        cryo.set_temperature_approach("linear")
    with pytest.raises(ValueError):
        cryo.set_field(float("nan"))


def test_a_read_failure_is_reported_and_nothing_counts_as_reached():
    cryo, sim, clock, events = make(field__stable_time_s=0.0)
    cryo.set_field(10.0)
    run(cryo, clock, 2.0)
    assert cryo.status().field_stable

    def broken():
        raise OSError("COM link lost")

    sim.read_field = broken
    run(cryo, clock, 0.5)
    s = cryo.status()
    assert "COM link lost" in s.hw_error
    assert not s.field_stable and not s.temperature_stable
    assert sum(1 for lvl, _ in events if lvl == "error") == 1, "error spammed every poll"
    del sim.read_field                        # back to the class method
    run(cryo, clock, 0.5)
    assert cryo.status().hw_error == ""


def test_apply_config_never_commands():
    clock = Clock()
    sim = RecordingSim(field_mT=8000.0, clock=clock, noise=False)
    cryo = Cryostat(sim, Config(), clock=clock)
    events = []
    cryo._on_event = lambda lvl, msg: events.append((lvl, msg))
    cryo.start(poll=False)
    cryo.cfg.limits.field_max_mT = 5000.0     # the magnet sits outside the new envelope
    cryo.cfg.field.rate_mT_per_s = 999.0
    cryo.apply_config()
    assert sim.commands == []
    assert cryo.cfg.field.rate_mT_per_s == cryo.cfg.limits.field_rate_max_mT_per_s
    assert any("outside the new limit" in m for _, m in events)


def test_status_before_a_reading_has_no_numbers():
    s = Cryostat(SimulatedDynaCool(), Config()).status()
    assert not s.connected and math.isnan(s.measured_field_mT)


def test_commands_refused_when_not_connected():
    cryo = Cryostat(SimulatedDynaCool(), Config())
    with pytest.raises(RuntimeError):
        cryo.set_field(1.0)
