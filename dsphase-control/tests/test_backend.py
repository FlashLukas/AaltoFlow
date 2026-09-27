"""The real PS6000L backend against a FAKE serial port.

No hardware and no pyserial needed: a stand-in `serial` module is put into
sys.modules, so `import serial` inside open() gets it. This pins down the exact
command strings (from the V3 command list) and the reply parsing -- the part of
the real backend that can be checked offline.
"""

import sys
import types

import pytest

from dsphase.backends.ps6000l import PS6000L, parse_number, parse_on


class FakeSerial:
    """Answers like a PS6000L might. The reply FORMATS are guesses (# VERIFY in
    the backend); the parsers are tested against several of them below."""

    last = None

    def __init__(self, port, baud, timeout=None, write_timeout=None):
        FakeSerial.last = self
        self.port, self.baud = port, baud
        self.lines = []
        self._out = b""
        self.phase, self.att, self.on = 0.0, 0.0, True
        self.closed = False

    def write(self, data: bytes):
        line = data.decode("ascii").rstrip("\n")
        assert data.endswith(b"\n"), "terminator must be LINEFEED"
        self.lines.append(line)
        cmd = line.split(" ")
        if line == "*PING?":
            self._out = b"PONG!\n"
        elif line == "*IDN?":
            self._out = b"DS INSTRUMENTS,PS6000L,FAKE,1.5\n"
        elif line == "PHASE?":
            self._out = f"{self.phase:.1f}\n".encode()
        elif line == "ATT?":
            self._out = f"{self.att:.2f} dB\n".encode()
        elif line == "OUTP:STAT?":
            self._out = b"ON\n" if self.on else b"OFF\n"
        elif cmd[0] == "PHASE":
            self.phase = float(cmd[1])
        elif cmd[0] == "ATT":
            self.att = float(cmd[1])
        elif line.startswith("OUTP:STAT "):
            self.on = cmd[1] == "ON"
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._out = b""

    def readline(self):
        out, self._out = self._out, b""
        return out

    def close(self):
        self.closed = True


@pytest.fixture
def fake_serial(monkeypatch):
    mod = types.ModuleType("serial")
    mod.Serial = FakeSerial
    monkeypatch.setitem(sys.modules, "serial", mod)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return mod


def test_open_pings_identifies_and_switches_output_off(fake_serial):
    dev = PS6000L("COM5")
    dev.open()
    ser = FakeSerial.last
    assert ser.baud == 115200
    assert ser.lines[:3] == ["*PING?", "*IDN?", "OUTP:STAT OFF"]
    assert "PS6000L" in dev.idn()
    assert dev.read_output() is False


def test_command_strings(fake_serial):
    dev = PS6000L("COM5")
    dev.open()
    ser = FakeSerial.last
    dev.set_phase(-89.5)
    dev.set_attenuation(6.25)
    dev.set_output(True)
    assert ser.lines[-3:] == ["PHASE -89.5", "ATT 6.25", "OUTP:STAT ON"]
    assert dev.read_phase() == -89.5
    assert dev.read_attenuation() == 6.25
    assert dev.read_output() is True


def test_frequency_is_sent_only_with_a_template(fake_serial):
    dev = PS6000L("COM5")
    dev.open()
    n = len(FakeSerial.last.lines)
    dev.set_frequency(2400.0)
    assert len(FakeSerial.last.lines) == n            # nothing sent
    dev2 = PS6000L("COM5", freq_command="FREQ {mhz:.3f}MHZ")
    dev2.open()
    dev2.set_frequency(2400.0)
    assert FakeSerial.last.lines[-1] == "FREQ 2400.000MHZ"


def test_close_switches_output_off(fake_serial):
    dev = PS6000L("COM5")
    dev.open()
    dev.set_output(True)
    ser = FakeSerial.last
    dev.close()
    assert ser.lines[-1] == "OUTP:STAT OFF"
    assert ser.closed
    dev.close()                                        # twice is harmless


def test_no_pong_refuses_to_open(fake_serial):
    class Mute(FakeSerial):
        def readline(self):
            return b"garbage\n"
    fake_serial.Serial = Mute
    with pytest.raises(RuntimeError, match="PONG"):
        PS6000L("COM5").open()


def test_timeout_raises(fake_serial):
    dev = PS6000L("COM5")
    dev.open()
    FakeSerial.last.readline = lambda: b""
    with pytest.raises(TimeoutError):
        dev.read_phase()


@pytest.mark.parametrize("reply, want", [
    ("44.5", 44.5), ("-155", -155.0), ("44.5 DEG", 44.5), ("PHASE 44.5", 44.5),
    ("+1.5e1", 15.0),
])
def test_parse_number(reply, want):
    assert parse_number(reply) == want


def test_parse_number_rejects_text():
    with pytest.raises(ValueError):
        parse_number("ERR")


@pytest.mark.parametrize("reply, want", [
    ("ON", True), ("1", True), ("on\r", True), ("OFF", False), ("0", False), ("", False),
])
def test_parse_on(reply, want):
    assert parse_on(reply) is want


@pytest.mark.parametrize("value,text", [
    (90.0, "90"), (-30.0, "-30"), (44.5, "44.5"), (13.25, "13.25"),
    (5.625, "5.625"), (0.0, "0"), (-0.0, "0"), (180.0, "180"),
])
def test_numbers_are_sent_with_the_decimals_they_need(fake_serial, value, text):
    # A fixed '.1f' turned a 5.625 deg step into '5.6' -- a number the unit
    # would then read back, never matching what the brain expects.
    dev = PS6000L("COM5")
    dev.open()
    dev.set_phase(value)
    assert FakeSerial.last.lines[-1] == f"PHASE {text}"


def test_bad_frequency_template_gives_a_clear_error(fake_serial):
    dev = PS6000L("COM5", freq_command="FREQ {f}")
    dev.open()
    with pytest.raises(ValueError, match="freq_command"):
        dev.set_frequency(2400.0)


def test_freq_command_changed_live_reaches_the_real_backend(fake_serial):
    # The template lives in cfg.device and set_config can change it while the
    # service runs; the backend must not keep the copy it was built with.
    from dsphase.config import Config
    from dsphase.shifter import PhaseShifter
    cfg = Config()
    dev = PS6000L("COM5")                       # built with NO template
    brain = PhaseShifter(dev, cfg)
    brain.start()
    try:
        cfg.device.freq_command = "FREQ {mhz:.1f}MHZ"
        brain.set_frequency(1500.0)
        assert FakeSerial.last.lines[-1] == "FREQ 1500.0MHZ"
    finally:
        brain.shutdown()
