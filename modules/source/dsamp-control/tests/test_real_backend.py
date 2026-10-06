"""The real serial backend against a FAKE port (no pyserial, no hardware).

We cannot test the real amplifier offline, but we can test what we send and how
we read replies: the command strings from the vendor's command list, the reply
parsers for the plausible formats (bare numbers, numbers with units), and that
close() switches the stage off. The fake answers like a device might -- whether
it really does is the # VERIFY list in dsi_serial.py.
"""

import sys
import types

import pytest

from dsamp.backends import dsi_serial
from dsamp.backends.dsi_serial import DsiSerialAmp, parse_number, parse_state


class FakeSerial:
    """Records writes; answers queries from a table."""

    def __init__(self, *a, **kw):
        self.written = []
        self.replies = {"*IDN?": "DS Instruments,GB6000L,FAKE,1.0",
                        "GAIN?": "12.5", "OUTP:STAT?": "ON",
                        "*TEMP?": "31C", "*SYSVOLTS?": "5.07V", "SYST:ERR?": "0,No error"}
        self._pending = b""
        self.closed = False

    def write(self, data):
        line = data.decode("ascii").strip()
        self.written.append(line)
        if line in self.replies:
            self._pending = (self.replies[line] + "\n").encode("ascii")

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._pending = b""

    def readline(self):
        out, self._pending = self._pending, b""
        return out

    def close(self):
        self.closed = True


@pytest.fixture
def fake_serial(monkeypatch):
    mod = types.ModuleType("serial")
    made = []

    def Serial(*a, **kw):
        s = FakeSerial()
        made.append(s)
        return s
    mod.Serial = Serial
    monkeypatch.setitem(sys.modules, "serial", mod)
    monkeypatch.setattr(dsi_serial.time, "sleep", lambda s: None)
    return made


def test_parsers_accept_plausible_reply_formats():
    assert parse_number("20C") == 20.0
    assert parse_number("5.17V") == 5.17
    assert parse_number("+12.5") == 12.5
    assert parse_number("GAIN 7") == 7.0
    assert parse_state("ON") is True and parse_state("1") is True
    assert parse_state("off") is False and parse_state("0") is False
    with pytest.raises(ValueError):
        parse_number("hello")
    with pytest.raises(ValueError):
        parse_state("maybe")


def test_open_sends_only_queries_and_reads_idn(fake_serial):
    """Lukas's rule: open() changes nothing on the amplifier -- every line it
    sends is a query (ends in '?')."""
    b = DsiSerialAmp("COM99")
    b.open()
    ser = fake_serial[0]
    assert ser.written and all(w.endswith("?") for w in ser.written), ser.written
    assert "OUTP:STAT OFF" not in ser.written
    assert b.idn().startswith("DS Instruments")


def test_commands_and_readbacks(fake_serial):
    b = DsiSerialAmp("COM99")
    b.open()
    ser = fake_serial[0]
    b.set_gain(10.5)
    b.set_output(True)
    assert ser.written[-2:] == ["GAIN 10.5", "OUTP:STAT ON"]
    # a whole-dB gain goes out exactly like the manual's example, "GAIN 10"
    b.set_gain(10.0)
    assert ser.written[-1] == "GAIN 10"
    assert b.read_gain() == 12.5
    assert b.read_output() is True
    assert b.read_temperature() == 31.0
    assert b.read_supply() == 5.07
    assert b.check_errors() == []


def test_supply_in_millivolts_is_converted(fake_serial):
    b = DsiSerialAmp("COM99")
    b.open()
    fake_serial[0].replies["*SYSVOLTS?"] = "5070"
    assert b.read_supply() == pytest.approx(5.07)


def test_close_switches_off_and_gives_the_buttons_back(fake_serial):
    b = DsiSerialAmp("COM99", buttons_on_exit=True)
    b.open()
    ser = fake_serial[0]
    b.close()
    assert ser.written[-2:] == ["OUTP:STAT OFF", "*BUTTONS ON"]
    assert ser.closed
    b.close()                          # twice is harmless


def test_close_keeping_outputs_leaves_the_stage(fake_serial):
    # shutdown{keep_outputs}: a restart sends no OUTP (the buttons still go back)
    b = DsiSerialAmp("COM99", buttons_on_exit=True)
    b.open()
    ser = fake_serial[0]
    n = len(ser.written)
    b.close(output_off=False)
    assert ser.written[n:] == ["*BUTTONS ON"] and ser.closed


def test_brain_on_the_real_backend(fake_serial):
    """The brain runs unchanged on the serial backend."""
    from dsamp.amplifier import Amplifier
    from dsamp.config import Config
    amp = Amplifier(DsiSerialAmp("COM99"), Config())
    amp.start()
    try:
        # the whole start-up, brain included, sent nothing but queries
        assert all(w.endswith("?") for w in fake_serial[0].written), fake_serial[0].written
        s = amp.status()
        # adopted: the fake reports ON at 12.5 dB (above the 10 dB ceiling)
        assert s.connected and s.amp_on is True
        assert s.gain_dB == 12.5 and s.gain_set_dB == 12.5
        assert s.temperature_C == 31.0
    finally:
        amp.shutdown()
    assert fake_serial[0].written[-2:] == ["OUTP:STAT OFF", "*BUTTONS ON"]
