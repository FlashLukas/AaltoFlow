"""Generator behaviour against the simulated backend: set/read, clamping,
lifecycle, and that events fire on a clamp."""

import pytest

from smb.config import Config
from smb.sim_system import build_sim_system


@pytest.fixture
def gen():
    cfg = Config()
    g, _ = build_sim_system(cfg)
    events = []
    g._on_event = lambda lvl, msg: events.append((lvl, msg))
    g.events = events            # stash for assertions
    g.start()
    yield g
    g.shutdown()


def test_start_leaves_rf_off_by_default(gen):
    s = gen.status()
    assert s.connected is True
    assert s.rf_on is False          # default Signal.rf_on is False


def test_set_and_read_back(gen):
    gen.set_frequency(2.0e9)
    gen.set_power(-7.0)
    gen.set_phase(90.0)
    gen.set_rf(True)
    s = gen.status()
    assert s.frequency_Hz == 2.0e9
    assert s.power_dBm == -7.0
    assert s.phase_deg == 90.0
    assert s.rf_on is True


def test_power_clamped_high(gen):
    gen.set_power(1000.0)
    assert gen.status().power_dBm == gen.cfg.limits.power_max_dBm
    assert any("clamped" in m for lvl, m in gen.events if lvl == "warn")


def test_power_clamped_low(gen):
    gen.set_power(-1000.0)
    assert gen.status().power_dBm == gen.cfg.limits.power_min_dBm


def test_frequency_clamped(gen):
    gen.set_frequency(1e15)
    assert gen.status().frequency_Hz == gen.cfg.limits.freq_max_Hz
    gen.set_frequency(0.0)
    assert gen.status().frequency_Hz == gen.cfg.limits.freq_min_Hz


def test_phase_clamped(gen):
    gen.set_phase(10_000.0)
    assert gen.status().phase_deg == gen.cfg.limits.phase_max_deg


def test_in_range_values_not_clamped(gen):
    gen.set_power(-20.0)
    gen.set_frequency(1.0e9)
    gen.set_phase(30.0)
    s = gen.status()
    assert (s.power_dBm, s.frequency_Hz, s.phase_deg) == (-20.0, 1.0e9, 30.0)


def test_shutdown_turns_rf_off():
    cfg = Config()
    g, backend = build_sim_system(cfg)
    g.start()
    g.set_rf(True)
    assert backend.read_output() is True
    g.shutdown()
    assert backend.read_output() is False
    assert g.status().connected is False


def test_apply_config_reclamps_to_new_limits(gen):
    gen.set_power(15.0)              # in range under the default max (18)
    assert gen.status().power_dBm == 15.0
    gen.cfg.limits.power_max_dBm = 10.0
    gen.apply_config()
    assert gen.status().power_dBm == 10.0
