"""The real backend's command strings, against a FAKE serial port.

No hardware and no pyserial needed: a stand-in `serial` module is put into
sys.modules, so `import serial` inside open() finds it. This pins down what we
believe the SynthHD wants (API guide v1.0b) -- if the real unit disagrees,
change the backend AND this test together, deliberately.
"""

import sys
import types

import pytest

from windfreak.backends.synthhd import SerialSynthHD


class FakePort:
    """Records every write; answers queries from a small table."""

    def __init__(self, port=None, timeout=None):
        self.port, self.timeout = port, timeout
        self.writes = []
        self._pending = b""
        self.closed = False
        self.replies = {"+": "SynthHD PRO", "v0": "3.25", "v1": "2.06", "z": "31.5"}

    def reset_input_buffer(self):
        self._pending = b""

    def write(self, data: bytes):
        text = data.decode("ascii")
        self.writes.append(text)
        # a query is the LAST command in the packet ("C0f?" -> "f?")
        for key, reply in (("f?", "1000.00000000"), ("p", "1"), ("V", "1")):
            if text.endswith(key):
                self._pending = (reply + "\n").encode()
        if text in self.replies:
            self._pending = (self.replies[text] + "\n").encode()

    def readline(self):
        out, self._pending = self._pending, b""
        return out

    def close(self):
        self.closed = True


@pytest.fixture
def fake_serial(monkeypatch):
    ports = []

    def factory(port=None, timeout=None):
        p = FakePort(port, timeout)
        ports.append(p)
        return p

    mod = types.SimpleNamespace(Serial=factory)
    monkeypatch.setitem(sys.modules, "serial", mod)
    return ports


def test_open_silences_both_outputs_first_and_builds_an_id(fake_serial):
    b = SerialSynthHD("COM7")
    b.open()
    port = fake_serial[0]
    assert port.port == "COM7"
    # the first two packets switch both channels OFF, before anything else
    assert port.writes[0] == "C0h0r0" and port.writes[1] == "C1h0r0"
    assert "SynthHD PRO" in b.idn() and "3.25" in b.idn()
    assert not any("\n" in w or "\r" in w for w in port.writes), "no terminators"
    b.close()
    assert port.writes[-2:] == ["C0h0r0", "C1h0r0"] and port.closed


def test_quiet_mode_also_powers_the_pll_down(fake_serial):
    b = SerialSynthHD("COM7", pll_off_when_rf_off=True)
    b.open()
    assert fake_serial[0].writes[0] == "C0h0r0E0"


def test_command_formats(fake_serial):
    b = SerialSynthHD("COM7")
    b.open()
    port = fake_serial[0]
    port.writes.clear()
    b.set_output(1, True)
    b.set_frequency(0, 2.5e9)
    b.set_power(1, -12.5)
    b.set_reference("external", 10.0)
    b.set_reference("internal_10MHz", 10.0)
    assert port.writes == ["C1E1r1h1", "C0f2500.00000000", "C1W-12.500",
                           "*10.000", "x0", "x2"]
    assert b.read_frequency(0) == pytest.approx(1e9)
    assert b.read_locked(1) is True and b.read_leveled(0) is True
    assert b.read_temperature() == 31.5


def test_phase_is_sent_as_steps_from_where_we_are(fake_serial):
    """The SynthHD phase command ADDS to the current phase, so going 0 -> 90
    -> 30 must send +90 and then +300 (= -60)."""
    b = SerialSynthHD("COM7", phase_command="relative")
    b.open()
    port = fake_serial[0]
    port.writes.clear()
    b.set_phase(0, 90.0)
    b.set_phase(0, 30.0)
    b.set_phase(0, 30.0)                  # no change -> nothing sent
    b.set_phase(1, 45.0)                  # the other channel keeps its own zero
    assert port.writes == ["C0~90.000", "C0~300.000", "C1~45.000"]


def test_absolute_phase_variant(fake_serial):
    b = SerialSynthHD("COM7", phase_command="absolute")
    b.open()
    port = fake_serial[0]
    port.writes.clear()
    b.set_phase(0, 90.0); b.set_phase(0, 30.0)
    assert port.writes == ["C0~90.000", "C0~30.000"]


def test_a_missing_reply_raises_instead_of_hanging(fake_serial):
    b = SerialSynthHD("COM7")
    b.open()
    fake_serial[0].replies.pop("z")
    with pytest.raises(TimeoutError):
        b.read_temperature()


def test_late_reply_does_not_shift_every_later_answer(fake_serial):
    """A reply that arrives after its query timed out must not be read as the
    answer to the NEXT query (which would shift every reply by one)."""
    b = SerialSynthHD("COM7")
    b.open()
    port = fake_serial[0]
    port._pending = b"1\n"             # a stale lock-flag reply is waiting
    assert b.read_temperature() == 31.5
    assert b.read_locked(0) is True


def test_query_timeout_raises_and_clears(fake_serial):
    b = SerialSynthHD("COM7")
    b.open()
    port = fake_serial[0]
    del port.replies["z"]
    orig = port.write

    def write_partial(data):
        orig(data)
        if data == b"z":
            port._pending = b"31"          # a partial line, no newline
    port.write = write_partial
    with pytest.raises(TimeoutError):
        b.read_temperature()
    assert port._pending == b""
