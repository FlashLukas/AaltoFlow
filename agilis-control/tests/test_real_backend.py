"""The real AG-UC2 driver against a FAKE serial port (offline).

Checks the exact ASCII that goes out, the TE error check after every set
command, reply parsing, and the safe close (stop both axes, back to local).
"""

import sys
import types

import pytest

from agilis.backends.ag_uc2 import AgUC2, _last_int
from agilis.config import Config


class FakeSerial:
    """Answers like the manual says an AG-UC2 does (formats are # VERIFY)."""

    def __init__(self, **kw):
        self.kw = kw
        self.sent = []
        self._out = []
        self.te = 0
        self.tp = {1: 1234, 2: -56}
        self.closed = False

    def write(self, data: bytes):
        line = data.decode("ascii")
        assert line.endswith("\r\n")
        cmd = line[:-2]
        self.sent.append(cmd)
        if cmd == "VE":
            self._out.append("AG-UC2 v2.2.1")
        elif cmd == "TE":
            self._out.append(f"TE{self.te}")
        elif cmd.endswith("TP"):
            ax = int(cmd[0])
            self._out.append(f"{ax}TP{self.tp[ax]}")
        elif cmd.endswith("TS"):
            self._out.append(f"{cmd[0]}TS0")
        elif cmd == "PH":
            self._out.append("PH2")
        elif "SU" in cmd and cmd.endswith("?"):
            self._out.append(f"{cmd[0]}SU{cmd[3]}23")

    def readline(self):
        return (self._out.pop(0) + "\r\n").encode("ascii") if self._out else b""

    def reset_input_buffer(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture()
def fake_serial(monkeypatch):
    made = []
    mod = types.ModuleType("serial")
    mod.EIGHTBITS, mod.PARITY_NONE, mod.STOPBITS_ONE = 8, "N", 1

    def Serial(**kw):
        s = FakeSerial(**kw)
        made.append(s)
        return s
    mod.Serial = Serial
    monkeypatch.setitem(sys.modules, "serial", mod)
    return made


def _open(fake_serial, **hw):
    cfg = Config()
    cfg.hardware.port = "COM9"
    for k, v in hw.items():
        setattr(cfg.hardware, k, v)
    dev = AgUC2(cfg)
    dev.open()
    return dev, fake_serial[-1]


def test_open_sets_link_and_remote_mode(fake_serial):
    dev, ser = _open(fake_serial)
    assert ser.kw["baudrate"] == 921600 and ser.kw["port"] == "COM9"
    assert ser.sent[:3] == ["VE", "MR", "TE"]
    assert "CC" not in "".join(ser.sent)                 # AG-UC2 has no CC
    assert "AG-UC2" in dev.idn()


def test_uc8_channel_is_selected(fake_serial):
    _dev, ser = _open(fake_serial, channel=2)
    assert "CC2" in ser.sent


def test_empty_port_is_refused(fake_serial):
    with pytest.raises(RuntimeError, match="port"):
        AgUC2(Config()).open()


def test_command_strings(fake_serial):
    dev, ser = _open(fake_serial)
    ser.sent.clear()
    dev.move_by(1, -250)
    dev.jog(2, 3)
    dev.stop(2)
    dev.zero_counter(1)
    dev.set_amplitude(2, +1, 30)
    dev.set_amplitude(2, -1, 12)
    sets = [c for c in ser.sent if c != "TE"]
    assert sets == ["1PR-250", "2JA3", "2ST", "1ZP", "2SU30", "2SU-12"]
    assert ser.sent.count("TE") == 6                     # every set is checked


def test_queries_parse(fake_serial):
    dev, _ser = _open(fake_serial)
    assert dev.read_position(1) == 1234
    assert dev.read_position(2) == -56
    assert dev.axis_state(1) == 0
    assert dev.limit_status() == 2
    assert dev.read_amplitude(1, -1) == 23


def test_te_error_raises_with_its_meaning(fake_serial):
    dev, ser = _open(fake_serial)
    ser.te = -6
    with pytest.raises(RuntimeError, match="not allowed in current state"):
        dev.move_by(1, 10)


def test_silence_is_a_timeout(fake_serial):
    dev, ser = _open(fake_serial)
    ser.write = lambda data: ser.sent.append(data)        # nothing answers
    with pytest.raises(TimeoutError):
        dev.read_position(1)


def test_close_stops_both_axes_and_returns_to_local(fake_serial):
    dev, ser = _open(fake_serial)
    ser.sent.clear()
    dev.close()
    assert [c for c in ser.sent if c != "TE"] == ["1ST", "2ST", "ML"]
    assert ser.closed


def test_last_int():
    assert _last_int("1TP-42", "TP") == -42
    assert _last_int("TE-6", "TE") == -6
    with pytest.raises(RuntimeError):
        _last_int("??", "x")


def test_refused_remote_mode_frees_the_com_port(fake_serial, monkeypatch):
    """MR is refused (-6) while an axis still moves; open() must then close
    the port instead of holding it for the life of the process."""
    monkeypatch.setattr(FakeSerial, "__init__", _init_te(-6))
    cfg = Config()
    cfg.hardware.port = "COM9"
    dev = AgUC2(cfg)
    with pytest.raises(RuntimeError, match="-6"):
        dev.open()
    assert fake_serial[-1].closed and dev._ser is None


def _init_te(code):
    orig = FakeSerial.__init__

    def init(self, **kw):
        orig(self, **kw)
        self.te = code
    return init
