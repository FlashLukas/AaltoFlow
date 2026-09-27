"""The real backend against a FAKE serial port that answers the way the TC200
does according to InstrumentKit's transcripts: it echoes the command, then the
answer, then a '> ' prompt. No pyserial and no hardware needed.

These pin down the PARSING and the command strings; whether the real firmware
answers exactly like this is on the VERIFY list."""

import pytest

from tc200.backends.serial_tc200 import (SerialTC200, all_ints, first_number, parse_sensor,
                                        parse_stat, strip_reply)
from tc200.config import Config
from tc200.heater import Heater


class FakeTC200Port:
    """Just enough of a pyserial Serial: write(), read_until(), buffers."""

    def __init__(self):
        self.sent = []
        self._out = b""
        self.state = {"tact": 24.3, "tset": 30.0, "enabled": False, "sensor": "PTC100",
                      "pid": (125, 5, 0), "pmax": 10.0, "tmax": 120.0}

    def reset_input_buffer(self):
        self._out = b""

    def close(self):
        pass

    def write(self, data: bytes):
        cmd = data.decode("ascii").rstrip("\r")
        self.sent.append(cmd)
        st = self.state
        if cmd == "":
            ans = "Command error CMD_NOT_DEFINED"
        elif cmd == "*idn?":
            ans = "THORLABS TC200 VERSION 2.0"
        elif cmd == "tact?":
            ans = f"{st['tact']:.1f} C"
        elif cmd == "tset?":
            ans = f"{st['tset']:.1f} C"
        elif cmd.startswith("tset="):
            st["tset"] = float(cmd[5:]); ans = ""
        elif cmd == "stat?":
            ans = f"{0x10 | 0x04 | (1 if st['enabled'] else 0):X}"   # C units, PTC100
        elif cmd == "ens":
            st["enabled"] = not st["enabled"]; ans = ""
        elif cmd == "sns?":
            ans = f"Sensor = {st['sensor']}, Beta = 3970"
        elif cmd.startswith("sns="):
            st["sensor"] = cmd[4:].upper(); ans = ""
        elif cmd == "pid?":
            ans = " ".join(str(v) for v in st["pid"])
        elif cmd.startswith("pgain="):
            p, i, d = st["pid"]; st["pid"] = (int(cmd[6:]), i, d); ans = ""
        elif cmd == "pmax?":
            ans = f"{st['pmax']:.1f} Watts"
        elif cmd.startswith("pmax="):
            st["pmax"] = float(cmd[5:]); ans = ""
        elif cmd == "tmax?":
            ans = f"{st['tmax']:.1f} C"
        elif cmd.startswith("tmax="):
            st["tmax"] = float(cmd[5:]); ans = ""
        else:
            ans = "Command error CMD_NOT_DEFINED"
        body = f"{cmd}\r" + (f"{ans}\r" if ans else "") + "> "
        self._out += body.encode("ascii")

    def read_until(self, terminator=b"\n"):
        i = self._out.find(terminator)
        if i < 0:
            data, self._out = self._out, b""       # "timeout": whatever there is
            return data
        data, self._out = self._out[:i + 1], self._out[i + 1:]
        # the fake's "> " leaves a trailing space; the real read consumes it next time
        if self._out.startswith(b" "):
            self._out = self._out[1:]
        return data


def _backend():
    port = FakeTC200Port()
    cfg = Config()
    be = SerialTC200(cfg, serial_factory=lambda p, b, t: port)
    be.open()
    return be, port, cfg


# ---- pure parsers ---------------------------------------------------------------

def test_strip_reply_removes_echo_and_prompt():
    assert strip_reply("tact?", "tact?\r24.3 C\r> ") == "24.3 C"
    assert strip_reply("stat?", "stat?\r54\r> ") == "54"
    assert strip_reply("ens", "ens\r> ") == ""
    assert strip_reply("tact?", "24.3 C\r> ") == "24.3 C"          # no echo


def test_number_parsers():
    assert first_number("Tset = 54.3 C") == 54.3
    assert first_number("15.9 Watts") == 15.9
    assert all_ints("126 0 0") == [126, 0, 0]
    with pytest.raises(RuntimeError):
        first_number("no number here")


def test_stat_bits_in_both_bases():
    # bit 0 reads the same in base 10 and 16 (InstrumentKit's 54 / 55)
    assert parse_stat("54", 16).enabled is False
    assert parse_stat("55", 16).enabled is True
    assert parse_stat("55", 10).enabled is True
    s = parse_stat("56", 16)                         # 0x56 = 0101 0110
    assert s.cycle_mode is True and s.sensor_alarm is True
    assert parse_stat("15 TMAX ERROR", 16).tmax_alarm is True
    assert parse_stat("15", 16).tmax_alarm is False


def test_stat_alarm_text_is_never_read_as_the_number():
    # The "A" of TMAX and the "E" of ERROR are hex digits: whichever side of
    # the number the alarm text lands on, the byte must still be 0x15.
    for text in ("TMAX ERROR 15", "TMAX ERROR\n15", "15\nTMAX ERROR", "0x15 TMAX ERROR"):
        s = parse_stat(text, 16)
        assert s.raw == 0x15, text
        assert s.enabled is True and s.cycle_mode is False and s.tmax_alarm is True
    with pytest.raises(RuntimeError):
        parse_stat("TMAX ERROR", 16)


def test_sensor_parser_prefers_the_longer_name():
    assert parse_sensor("Sensor = PTC1000, Beta = 3970") == "ptc1000"
    assert parse_sensor("Sensor = PTC100, Beta = 3970") == "ptc100"
    assert parse_sensor("TH10K") == "th10k"
    with pytest.raises(RuntimeError):
        parse_sensor("K type")


# ---- the backend over the fake port ------------------------------------------------

def test_reads_and_writes():
    be, port, cfg = _backend()
    assert be.idn().startswith("THORLABS")
    assert be.read_temperature() == 24.3
    assert be.read_setpoint() == 30.0
    be.set_setpoint(41.26)
    assert "tset=41.3" in port.sent                  # one decimal, like the box
    assert be.read_sensor() == "ptc100"
    assert be.read_pid() == (125, 5, 0)
    be.set_p_gain(90)
    assert be.read_pid()[0] == 90
    assert be.read_pmax() == 10.0 and be.read_tmax() == 120.0
    be.set_pmax(4.25); be.set_tmax(80)
    assert "pmax=4.2" in port.sent or "pmax=4.3" in port.sent
    assert "tmax=80.0" in port.sent
    # every command lower case (manual 6.3.2)
    assert all(c == c.lower() for c in port.sent if c)
    be.close()


def test_toggle_via_the_brain_is_safe():
    be, port, cfg = _backend()
    assert be.read_status().enabled is False
    be.toggle_enable()
    assert be.read_status().enabled is True
    be.close()


def test_command_error_raises():
    be, port, cfg = _backend()
    with pytest.raises(RuntimeError, match="refused"):
        be._query("bogus?")
    be.close()


def test_no_prompt_is_a_timeout_error():
    be, port, cfg = _backend()
    port.write = lambda data: None                   # the unit went silent
    with pytest.raises(RuntimeError, match="no reply"):
        be.read_temperature()


def test_brain_runs_on_the_real_backend_code_path():
    port = FakeTC200Port()
    cfg = Config()
    heater = Heater(SerialTC200(cfg, serial_factory=lambda p, b, t: port), cfg)
    heater.start(poll=False)
    s = heater.status()
    assert s.connected and not s.simulated
    assert s.sensor == "ptc100" and s.sensor_ok
    assert s.temperature_C == 24.3 and s.setpoint_C == 30.0
    heater.set_enabled(True)
    heater.set_enabled(True)                         # idempotent: one `ens` only
    assert port.sent.count("ens") == 1 and port.state["enabled"] is True
    heater.shutdown()                                # disable_on_shutdown
    assert port.state["enabled"] is False


def test_start_on_the_real_code_path_sends_only_queries():
    """The exact bytes at start: a bare CR (framing flush) and queries ending in
    '?'. Nothing with '=' and no `ens` -- the box is left heating at 55 C with
    its own gains, and the status shows exactly that."""
    port = FakeTC200Port()
    port.state.update({"tact": 54.8, "tset": 55.0, "enabled": True, "pid": (70, 4, 1),
                       "pmax": 6.0, "tmax": 90.0})
    cfg = Config()
    cfg.device.p_gain, cfg.device.pmax_W = 125, 18.0     # stored values that differ
    heater = Heater(SerialTC200(cfg, serial_factory=lambda p, b, t: port), cfg)
    heater.start(poll=False)
    heater.poll_once()
    assert port.sent, "nothing was sent at all?"
    for cmd in port.sent:
        assert cmd == "" or cmd.endswith("?"), f"start sent a non-query: {cmd!r}"
    s = heater.status()
    assert s.enabled is True and s.setpoint_C == 55.0 and s.temperature_C == 54.8
    assert (s.p_gain, s.i_gain, s.d_gain, s.pmax_W, s.tmax_C) == (70, 4, 1, 6.0, 90.0)
    assert port.state["enabled"] is True and port.state["pid"] == (70, 4, 1)
    heater.shutdown()


def test_missing_pyserial_is_a_clear_error(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "serial":
            raise ImportError("no serial")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    be = SerialTC200(Config())
    with pytest.raises(RuntimeError, match="extra real"):
        be.open()
