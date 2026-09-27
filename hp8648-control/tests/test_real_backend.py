"""The real GPIB backend (visa_8648.py) against a FAKE pyvisa -- offline.

What these tests pin down is Lukas's start-up rule (2026-09-27): connecting
READS the generator and changes nothing. The fake instrument answers the
queries from a state dict and records every write; the only write allowed at
open() is *CLS (it clears the status/error registers, nothing at the RF
output). Shutdown still switches RF off -- that is not part of the rule.
"""

import sys
import time
import types

import pytest

from hp8648.config import Config
from hp8648.source import SignalSource

#: writes open()/start() may send: *CLS only clears status + error queue
ALLOWED_AT_START = {"*CLS"}


class FakeInstrument:
    """Answers the SCPI queries the backend uses, from `state`."""

    def __init__(self, state):
        self.state = state
        self.writes = []
        self.timeout = None
        self.write_termination = self.read_termination = None
        self.closed = False

    def write(self, cmd):
        self.writes.append(cmd)
        st = self.state
        head, _, arg = cmd.partition(" ")
        if head == "OUTP:STAT":
            st["OUTP:STAT?"] = "1" if arg == "ON" else "0"
        elif head == "POW:AMPL":
            v = float(arg.split()[0])
            st["POW:AMPL?"] = f"{v:+.1f}"
        elif head == "FREQ:CW":
            st["FREQ:CW?"] = f"{float(arg.split()[0]) * 1e6:.0f}"

    def query(self, cmd):
        if cmd == "SYST:ERR?":
            return '+0,"No error"'
        return str(self.state[cmd]) + "\n"

    def control_ren(self, mode):
        pass

    def close(self):
        self.closed = True


def _busy_state(**extra):
    """A generator someone left running: RF ON, 2.2 GHz, +3 dBm, AM on."""
    st = {
        "*IDN?": "HEWLETT-PACKARD,8648D,3847U00000,B.04.01",
        "OUTP:STAT?": "1",
        "FREQ:CW?": "2200000000",
        "POW:AMPL?": "+3.0",
        "STAT:QUES:POW:COND?": "0",
        "AM:STAT?": "1", "FM:STAT?": "0", "PM:STAT?": "0",
        "POW:REF:STAT?": "0", "FREQ:REF:STAT?": "0", "POW:ATT:AUTO?": "1",
    }
    st.update(extra)
    return st


@pytest.fixture
def fake_visa(monkeypatch):
    """Install a fake `pyvisa` module; returns a holder for the instrument."""
    holder = {}

    class RM:
        def open_resource(self, name):
            holder["inst"] = FakeInstrument(holder["state"])
            return holder["inst"]

        def close(self):
            pass

    mod = types.SimpleNamespace(ResourceManager=RM)
    monkeypatch.setitem(sys.modules, "pyvisa", mod)
    return holder


def test_open_sends_no_state_changing_write(fake_visa):
    from hp8648.backends.visa_8648 import Visa8648
    fake_visa["state"] = _busy_state()
    b = Visa8648("GPIB0::19::INSTR")
    b.open()
    inst = fake_visa["inst"]
    assert set(inst.writes) <= ALLOWED_AT_START, inst.writes
    # nothing we could have switched moved
    assert inst.state["OUTP:STAT?"] == "1" and inst.state["AM:STAT?"] == "1"
    assert b.startup_notes() == []


def test_start_adopts_the_real_instrument_state(fake_visa):
    """The whole start path (backend + brain + a few worker cycles): the
    status shows what the box was doing, and only *CLS was written."""
    from hp8648.backends.visa_8648 import Visa8648
    fake_visa["state"] = _busy_state()
    cfg = Config()
    cfg.hardware.poll_s = 0.02
    src = SignalSource(Visa8648("GPIB0::19::INSTR"), cfg)
    src.start()
    try:
        time.sleep(0.4)          # > MOD_CHECK_EVERY cycles, so modulation was re-read
        inst = fake_visa["inst"]
        assert set(inst.writes) <= ALLOWED_AT_START, inst.writes
        s = src.status()
        assert (s.rf_on, s.frequency_Hz, s.power_dBm) == (True, 2.2e9, 3.0)
        assert (s.rf_set, s.frequency_set_Hz, s.power_set_dBm) == (True, 2.2e9, 3.0)
        assert s.modulation == {"am": True, "fm": False, "pm": False}
        assert s.idn.startswith("HEWLETT-PACKARD,8648D")
    finally:
        src.shutdown()
    # shutdown is unchanged: RF off on the way out
    assert fake_visa["inst"].writes[-1] == "OUTP:STAT OFF"


def test_power_reference_mode_is_read_and_converted_not_switched_off(fake_visa):
    """A unit meter-style setting is converted in software, never changed:
    with POW:REF:STAT ON the box talks dB relative to POW:REF."""
    from hp8648.backends.visa_8648 import Visa8648
    fake_visa["state"] = _busy_state(**{"POW:REF:STAT?": "1", "POW:REF?": "-10.0",
                                        "POW:AMPL?": "+5.0"})
    b = Visa8648()
    b.open()
    inst = fake_visa["inst"]
    assert set(inst.writes) <= ALLOWED_AT_START
    assert b.read_power() == pytest.approx(-5.0)          # +5 dB re -10 dBm
    assert any("reference mode is ON" in n for n in b.startup_notes())
    b.set_power(-7.0)                                      # an explicit command
    assert inst.writes[-1] == "POW:AMPL 3.0 DB"


def test_attenuator_hold_is_reported_not_changed(fake_visa):
    from hp8648.backends.visa_8648 import Visa8648
    fake_visa["state"] = _busy_state(**{"POW:ATT:AUTO?": "0"})
    b = Visa8648()
    b.open()
    assert set(fake_visa["inst"].writes) <= ALLOWED_AT_START
    assert any("attenuator HOLD" in n for n in b.startup_notes())
