"""Adopt-on-start (Lukas's rule, 2026-09-27): at start the service READS the
generator and changes nothing on it.

Two layers are tested:
  * the Generator against a simulated box that is already doing something
    (RF ON, 2.345 GHz, -12.5 dBm, 45 deg -- none of it the config default), and
    against a backend whose setters raise, so ANY write at start fails the test;
  * the real VISA backend against a fake `pyvisa` that records every write,
    so open() is proved to send nothing but *CLS (which only clears the error
    queue).
"""

from __future__ import annotations

import sys
import types

import pytest

from smb.backends.sim import SimulatedSMB100A
from smb.config import Config, Signal
from smb.generator import Generator

# A state that differs from Config().signal in EVERY field, so adoption is
# visible and cannot pass by coincidence.
PRE = Signal(frequency_Hz=2.345e9, power_dBm=-12.5, phase_deg=45.0, rf_on=True)


class NoWriteBackend(SimulatedSMB100A):
    """A simulated box that fails the test on any state-changing call."""

    def _refuse(self, *a, **k):
        raise AssertionError("state-changing write during start()")

    set_output = set_power = set_frequency = set_phase = _refuse


def _gen(backend, cfg=None):
    g = Generator(backend, cfg or Config())
    g.events = []
    g._on_event = lambda lvl, msg: g.events.append((lvl, msg))
    return g


def test_status_after_start_reflects_instrument_state():
    backend = SimulatedSMB100A(startup=PRE)
    g = _gen(backend)
    g.start()
    s = g.status()
    assert s.rf_on is True                    # RF was NOT switched off
    assert s.frequency_Hz == PRE.frequency_Hz
    assert s.power_dBm == PRE.power_dBm
    assert s.phase_deg == PRE.phase_deg
    # the brain's desired values are the adopted ones too (what a later
    # apply_config / status-while-offline would use)
    assert (g._rf_on, g._freq, g._power, g._phase) == (
        True, PRE.frequency_Hz, PRE.power_dBm, PRE.phase_deg)
    assert any("adopted" in m for _, m in g.events)
    g.shutdown()


def test_start_issues_no_writes():
    g = _gen(NoWriteBackend(startup=PRE))
    g.start()                                  # raises if anything is written
    assert g.status().rf_on is True


def test_out_of_limit_state_is_left_alone_and_warned():
    cfg = Config()
    cfg.limits.power_max_dBm = -20.0           # the box sits at -12.5 dBm
    g = _gen(NoWriteBackend(startup=PRE), cfg)
    g.start()
    assert g.status().power_dBm == PRE.power_dBm
    assert any(lvl == "warn" and "outside" in m for lvl, m in g.events)


def test_unrelated_set_config_writes_nothing():
    """A theme change (or pressing OK in Settings without edits) must not
    re-send the signal."""
    g = _gen(NoWriteBackend(startup=PRE))
    g.start()
    g.cfg.ui.theme = "light"
    g.apply_config()                           # NoWriteBackend would raise


def test_changed_default_is_applied_but_rf_is_not_switched():
    backend = SimulatedSMB100A(startup=PRE)
    g = _gen(backend)
    g.start()
    g.cfg.signal.power_dBm = -3.0              # the user edits one default
    g.cfg.signal.rf_on = False                 # ...and the legacy RF flag
    g.apply_config()
    assert backend.read_power() == -3.0        # explicit change -> applied
    assert backend.read_frequency() == PRE.frequency_Hz   # untouched
    assert backend.read_output() is True       # RF only via set_rf


# ---- real backend against a fake pyvisa ------------------------------------

class _FakeInst:
    def __init__(self, angle_unit="DEG"):
        self.writes: list[str] = []
        self.queries: list[str] = []
        self.timeout = None
        self.write_termination = self.read_termination = None
        self._answers = {"UNIT:ANGL?": angle_unit, "OUTP:STAT?": "1",
                         "POW?": "-12.5", "FREQ?": "2345000000",
                         "PHAS?": "0.785398163" if angle_unit == "RAD" else "45",
                         "*IDN?": "Rohde&Schwarz,SMB100A,FAKE,1.0"}

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        self.queries.append(cmd)
        return self._answers[cmd] + "\n"

    def close(self):
        pass


@pytest.fixture
def fake_pyvisa(monkeypatch):
    holder = {}

    class RM:
        def open_resource(self, name):
            return holder["inst"]

        def close(self):
            pass

    mod = types.ModuleType("pyvisa")
    mod.ResourceManager = RM
    monkeypatch.setitem(sys.modules, "pyvisa", mod)
    return holder


@pytest.mark.parametrize("unit", ["DEG", "RAD"])
def test_visa_open_only_clears_errors_and_reads(fake_pyvisa, unit):
    from smb.backends.visa_scpi import VisaSMB100A
    inst = fake_pyvisa["inst"] = _FakeInst(angle_unit=unit)
    backend = VisaSMB100A("FAKE::INSTR", settle_s=0)
    g = _gen(backend)
    g.start()
    # *CLS empties the error queue only; everything else must be a query.
    assert inst.writes == ["*CLS"], inst.writes
    assert "UNIT:ANGL?" in inst.queries
    s = g.status()
    assert s.rf_on is True
    assert s.frequency_Hz == 2.345e9
    assert s.power_dBm == -12.5
    assert s.phase_deg == pytest.approx(45.0, abs=1e-6)   # RAD converted in software
    g.shutdown()                               # shutdown behaviour unchanged:
    assert inst.writes[-1] == "OUTP:STAT OFF"  # RF off on the way out
