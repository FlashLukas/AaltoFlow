"""Adopt-on-start (Lukas's rule, 2026-09-27): at start the service READS the
tracking generator and changes nothing on it.

Tested against a simulated TG that is already doing something (CW ON,
2.345 GHz, -12.5 dBm -- none of it the config default), and against a backend
whose setter raises, so ANY write at start fails the test. The same rule for
the real backend (through a fake signalhound service) is in test_remote_sa.py.
"""

from __future__ import annotations

from shsg.backends.sim import SimulatedTG44A
from shsg.config import Config, Signal
from shsg.generator import Generator

# A state that differs from Config().signal in EVERY field, so adoption is
# visible and cannot pass by coincidence.
PRE = Signal(frequency_Hz=2.345e9, power_dBm=-12.5, rf_on=True)


class NoWriteBackend(SimulatedTG44A):
    """A simulated TG that fails the test on any state-changing call."""

    def set_cw(self, *a, **k):
        raise AssertionError("state-changing write during start()")


def _gen(backend, cfg=None):
    g = Generator(backend, cfg or Config())
    g.events = []
    g._on_event = lambda lvl, msg: g.events.append((lvl, msg))
    return g


def test_status_after_start_reflects_the_tg_state():
    g = _gen(SimulatedTG44A(startup=PRE))
    g.start()
    s = g.status()
    assert s.rf_on is True                    # the output was NOT switched off
    assert s.frequency_Hz == PRE.frequency_Hz
    assert s.power_dBm == PRE.power_dBm
    assert (g._rf_on, g._freq, g._power) == (True, PRE.frequency_Hz, PRE.power_dBm)
    assert any("adopted" in m for _, m in g.events)
    g.cfg.hardware.off_on_shutdown = False
    g.shutdown()


def test_start_issues_no_writes():
    g = _gen(NoWriteBackend(startup=PRE))
    g.start()                                  # raises if anything is written
    assert g.status().rf_on is True


def test_out_of_limit_state_is_left_alone_and_warned():
    cfg = Config()
    cfg.limits.power_max_dBm = -20.0           # the TG sits at -12.5 dBm
    g = _gen(NoWriteBackend(startup=PRE), cfg)
    g.start()
    assert g.status().power_dBm == PRE.power_dBm
    assert any(lvl == "warn" and "outside" in m for lvl, m in g.events)


def test_unknown_state_adopts_nothing():
    backend = NoWriteBackend(startup=PRE)
    backend.simulate_unknown()
    g = _gen(backend)
    g.start()
    assert (g._freq, g._power) == (Config().signal.frequency_Hz, Config().signal.power_dBm)
    assert g.status().tg_unknown and not g.status().tg_ready
    assert any(lvl == "warn" and "unknown" in m for lvl, m in g.events)


def test_unrelated_set_config_writes_nothing():
    """A theme change (or Apply in Settings without edits) must not re-send."""
    g = _gen(NoWriteBackend(startup=PRE))
    g.start()
    g.cfg.ui.theme = "light"
    g.apply_config()                           # NoWriteBackend would raise -> warn
    assert not any(lvl == "warn" and "not applied" in m for lvl, m in g.events)


def test_changed_default_is_applied_but_cw_is_not_switched():
    backend = SimulatedTG44A(startup=PRE)
    g = _gen(backend)
    g.start()
    g.cfg.signal.power_dBm = -22.0             # the user edits one default
    g.cfg.signal.rf_on = False                 # ...and the simulator-only flag
    g.apply_config()
    st = backend.read_state()
    assert st["power_dBm"] == -22.0            # explicit change -> applied
    assert st["frequency_Hz"] == PRE.frequency_Hz   # untouched
    assert st["rf_on"] is True                 # the output only via set_rf
    g.cfg.hardware.off_on_shutdown = False
    g.shutdown()
