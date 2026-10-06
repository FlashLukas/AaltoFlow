"""Generator behaviour against the simulated TG: set/read, clamping, refusals,
lifecycle, and the clean-stop rule."""

import pytest

from shsg.backends.sim import SimulatedTG44A
from shsg.config import Config, Signal
from shsg.generator import Generator, Refused
from shsg.sim_system import build_sim_system


@pytest.fixture
def gen():
    cfg = Config()
    g, backend = build_sim_system(cfg)
    events = []
    g._on_event = lambda lvl, msg: events.append((lvl, msg))
    g.events = events            # stash for assertions
    g.sim = backend
    g.start()
    yield g
    g.shutdown()


def test_start_leaves_cw_off_by_default(gen):
    s = gen.status()
    assert s.connected is True
    assert s.rf_on is False          # the simulated TG starts with its output off
    assert s.tg_ready and not s.tg_busy and s.hw_error == ""


def test_set_and_read_back(gen):
    gen.set_frequency(2.0e9)
    gen.set_power(-17.0)
    gen.set_rf(True)
    s = gen.status()
    assert (s.frequency_Hz, s.power_dBm, s.rf_on) == (2.0e9, -17.0, True)


def test_each_setter_sends_only_its_own_part(gen):
    """Missing = keep (the owner's tg_cw contract): a frequency change must not
    re-send the level, and never switches the output."""
    gen.set_frequency(2.0e9)
    gen.set_power(-17.0)
    gen.set_rf(True)
    assert gen.sim.commands == [
        {"on": None, "freq_hz": 2.0e9, "level_dbm": None},
        {"on": None, "freq_hz": None, "level_dbm": -17.0},
        {"on": True, "freq_hz": None, "level_dbm": None}]


def test_level_clamped_high_and_low(gen):
    gen.set_power(1000.0)
    assert gen.status().power_dBm == gen.cfg.limits.power_max_dBm
    assert any("clamped" in m for lvl, m in gen.events if lvl == "warn")
    gen.set_power(-1000.0)
    assert gen.status().power_dBm == gen.cfg.limits.power_min_dBm


def test_frequency_clamped(gen):
    gen.set_frequency(1e15)
    assert gen.status().frequency_Hz == gen.cfg.limits.freq_max_Hz
    gen.set_frequency(0.0)
    assert gen.status().frequency_Hz == gen.cfg.limits.freq_min_Hz


def test_default_limits_are_the_tg44a_range():
    lim = Config().limits
    assert (lim.freq_min_Hz, lim.freq_max_Hz) == (10.0, 4.4e9)
    assert (lim.power_min_dBm, lim.power_max_dBm) == (-30.0, -10.0)


def test_in_range_values_not_clamped(gen):
    gen.set_power(-20.0)
    gen.set_frequency(1.0e9)
    s = gen.status()
    assert (s.power_dBm, s.frequency_Hz) == (-20.0, 1.0e9)
    assert not any(lvl == "warn" for lvl, _ in gen.events)


def test_refused_while_a_sweep_holds_the_tg(gen):
    gen.sim.simulate_sweep(True)
    assert gen.status().tg_busy and not gen.status().tg_ready
    with pytest.raises(Refused, match="sweep"):
        gen.set_frequency(2e9)
    with pytest.raises(Refused):
        gen.set_rf(True)
    assert gen.sim.commands == []                 # nothing reached the TG
    assert gen.status().frequency_Hz == Config().signal.frequency_Hz
    assert any(lvl == "error" for lvl, _ in gen.events)
    gen.sim.simulate_sweep(False)
    gen.set_frequency(2e9)
    assert gen.status().frequency_Hz == 2e9


def test_refused_without_a_tg():
    g = Generator(SimulatedTG44A(attached=False), Config())
    g.start()
    assert "no tracking generator" in g.status().hw_error
    with pytest.raises(Refused, match="no tracking generator"):
        g.set_rf(True)
    g.shutdown()


def test_widened_limits_meet_the_hardware_range(gen):
    """Limits widened past the TG: we clamp to the (wrong) limit, the TG
    (like the signalhound service) refuses, and the value is not taken."""
    gen.cfg.limits.power_max_dBm = 0.0
    with pytest.raises(ValueError, match="outside the TG range"):
        gen.set_power(-5.0)
    assert gen.status().power_dBm == Config().signal.power_dBm


def test_command_before_start_is_refused():
    g, _ = build_sim_system(Config())
    with pytest.raises(Refused, match="not connected"):
        g.set_rf(True)


def test_clean_shutdown_switches_cw_off():
    g, backend = build_sim_system(Config())
    g.start()
    g.set_rf(True)
    g.shutdown()
    assert backend.read_state()["rf_on"] is False
    assert g.status().connected is False


def test_shutdown_leaves_cw_on_when_configured():
    cfg = Config()
    cfg.hardware.off_on_shutdown = False
    g, backend = build_sim_system(cfg)
    g.start()
    g.set_rf(True)
    g.shutdown()
    assert backend.read_state()["rf_on"] is True


def test_shutdown_keep_outputs_sends_no_command():
    """A restart (shutdown{keep_outputs}) parks nothing even with
    off_on_shutdown on (the default) -- and still disconnects."""
    g, backend = build_sim_system(Config())
    g.start()
    g.set_rf(True)
    n = len(backend.commands)
    g.shutdown(keep_outputs=True)
    assert backend.commands[n:] == []
    assert backend.read_state()["rf_on"] is True
    assert g.status().connected is False


def test_shutdown_switches_off_an_unknown_state():
    """Unknown may mean emitting: off is the safe direction on a clean stop."""
    g, backend = build_sim_system(Config(signal=Signal(rf_on=True)))
    g.start()
    backend.simulate_unknown()
    g.shutdown()
    assert backend.commands[-1] == {"on": False, "freq_hz": None, "level_dbm": None}


def test_shutdown_twice_is_safe():
    g, _ = build_sim_system(Config())
    g.start()
    g.shutdown()
    g.shutdown()


def test_apply_config_reclamps_to_new_limits(gen):
    gen.set_power(-12.0)
    gen.cfg.limits.power_max_dBm = -15.0
    gen.apply_config()
    assert gen.status().power_dBm == -15.0


def test_apply_config_while_busy_warns_but_keeps_the_config(gen):
    gen.sim.simulate_sweep(True)
    gen.cfg.signal.power_dBm = -25.0
    gen.apply_config()                              # must not raise
    assert gen.cfg.signal.power_dBm == -25.0
    assert any(lvl == "warn" and "not applied" in m for lvl, m in gen.events)


def test_off_is_a_park(gen):
    """The TG44A cannot be silenced: rf off = parked, and status says where."""
    gen.set_rf(True)
    assert gen.status().parked is False
    gen.set_rf(False)
    s = gen.status()
    assert s.rf_on is False and s.parked is True
    assert (s.park_Hz, s.park_dBm) == (10_000.0, -30.0)
