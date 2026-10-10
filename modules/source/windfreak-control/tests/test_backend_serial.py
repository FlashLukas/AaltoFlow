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


#: What the fake SynthHD answers, per query. Deliberately NOT the config
#: defaults: channel A radiating at 2.45 GHz / -5 dBm, B off (muted, amplifier
#: off) at 3.2 GHz with its PLL down, external 10 MHz reference.
STATE_REPLIES = {
    "C0f?": "2450.00000000", "C0W?": "-5.000", "C0h?": "1", "C0r?": "1", "C0E?": "1",
    "C1f?": "3200.00000000", "C1W?": "-12.000", "C1h?": "0", "C1r?": "0", "C1E?": "0",
    "x?": "0", "*?": "10.000",
}

#: Bytes allowed during open() + read_state(): pure queries only. A "C<n>"
#: channel select in front of a query picks which channel it addresses and
#: changes no output, so it is allowed as a PREFIX of a query.
_QUERY_TAILS = ("?", "p", "V", "z", "+", "v0", "v1")


def is_query(packet: str) -> bool:
    body = packet[2:] if packet[:1] == "C" and packet[1:2] in "01" else packet
    return body in ("+", "v0", "v1", "z", "p", "V") or (len(body) == 2 and body[1] == "?")


class FakePort:
    """Records every write; answers queries from a small table."""

    def __init__(self, port=None, timeout=None):
        self.port, self.timeout = port, timeout
        self.writes = []
        self._pending = b""
        self.closed = False
        self.replies = {"+": "SynthHD PRO", "v0": "3.25", "v1": "2.06", "z": "31.5",
                        **STATE_REPLIES}

    def reset_input_buffer(self):
        self._pending = b""

    def write(self, data: bytes):
        text = data.decode("ascii")
        self.writes.append(text)
        # a query is the LAST command in the packet ("C0f?" -> "f?")
        for key, reply in (("f?", "1000.00000000"), ("p", "1"), ("V", "1")):
            if text.endswith(key) and text not in self.replies:
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


def test_open_and_read_state_send_queries_only(fake_serial):
    """The read-only start rule, byte by byte: open() and read_state() may
    send nothing but queries -- no RF off, no channel spacing, no reference."""
    b = SerialSynthHD("COM7", pll_off_when_rf_off=True)
    b.open()
    b.read_state()
    port = fake_serial[0]
    assert port.port == "COM7"
    assert port.writes, "it did ask something"
    bad = [w for w in port.writes if not is_query(w)]
    assert bad == [], f"state-changing writes at start: {bad}"
    assert "SynthHD PRO" in b.idn() and "3.25" in b.idn()
    assert not any("\n" in w or "\r" in w for w in port.writes), "no terminators"
    # shutdown is NOT covered by the rule: both outputs off on the way out
    b.close()
    assert port.writes[-2:] == ["C0h0r0E0", "C1h0r0E0"] and port.closed


def test_close_keeping_outputs_sends_nothing(fake_serial):
    """shutdown{keep_outputs}: a restart closes the port, writes nothing."""
    b = SerialSynthHD("COM7", pll_off_when_rf_off=True)
    b.open()
    port = fake_serial[0]
    n = len(port.writes)
    b.close(rf_off=False)
    assert port.writes[n:] == [] and port.closed


def test_read_state_parses_what_the_instrument_holds(fake_serial):
    b = SerialSynthHD("COM7")
    b.open()
    st = b.read_state()
    a, bb = st["channels"]
    assert a == {"rf_on": True, "rf_partial": False, "pll_on": True,
                 "frequency_Hz": pytest.approx(2.45e9), "power_dBm": -5.0,
                 "phase_deg": 0.0}
    assert bb["rf_on"] is False and bb["pll_on"] is False
    assert bb["frequency_Hz"] == pytest.approx(3.2e9) and bb["power_dBm"] == -12.0
    assert st["reference"] == "external" and st["ext_MHz"] == 10.0
    assert st["unread"] == []


def test_an_unanswered_state_query_is_reported_not_fatal(fake_serial):
    """Every "?" form is still # VERIFY on the v2 firmware: one that gets no
    answer must not stop the service -- and an unreadable mute/amplifier
    state is reported as possibly radiating (the safe side)."""
    b = SerialSynthHD("COM7")
    b.open()
    port = fake_serial[0]
    del port.replies["C1W?"]
    del port.replies["C1h?"]
    st = b.read_state()
    assert st["channels"][1]["power_dBm"] is None
    assert st["channels"][1]["rf_on"] is True and st["channels"][1]["rf_partial"] is True
    assert "b.power_dBm" in st["unread"] and "b.rf_on" in st["unread"]


def test_half_on_output_is_flagged(fake_serial):
    b = SerialSynthHD("COM7")
    b.open()
    fake_serial[0].replies["C0h?"] = "0"          # muted, amplifier still on
    a = b.read_state()["channels"][0]
    assert a["rf_on"] is False and a["rf_partial"] is True


def test_quiet_mode_also_powers_the_pll_down(fake_serial):
    b = SerialSynthHD("COM7", pll_off_when_rf_off=True)
    b.open()
    port = fake_serial[0]
    port.writes.clear()
    b.set_output(0, False)
    assert port.writes == ["C0h0r0E0"]


def test_channel_spacing_command(fake_serial):
    b = SerialSynthHD("COM7")
    b.open()
    port = fake_serial[0]
    port.writes.clear()
    b.set_channel_spacing(50.0)
    assert port.writes == ["i50.0"]


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
    assert b.read_frequency(0) == pytest.approx(2.45e9)
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


def test_many_small_phase_steps_do_not_drift(fake_serial):
    """A phase SWEEP sends hundreds of small relative steps, each rounded to
    0.001 deg on the wire. The backend must count what it SENT, or the
    rounding errors add up and its idea of the phase drifts away from what
    the instrument has accumulated."""
    b = SerialSynthHD("COM7", phase_command="relative")
    b.open()
    port = fake_serial[0]
    port.writes.clear()
    for k in range(1, 401):
        b.set_phase(0, k * 0.1234567)
    sent = sum(float(w.split("~")[1]) for w in port.writes)
    assert b._phase_sent[0] == pytest.approx(sent % 360.0, abs=1e-9)
    # and the instrument ends up where it was asked, to the wire's resolution
    assert abs(sent - 400 * 0.1234567) <= 0.0005


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
