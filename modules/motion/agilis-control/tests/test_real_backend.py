"""The real AG-UC2 driver against a FAKE serial port (offline).

Checks the exact ASCII that goes out, the TE error check after every set
command, reply parsing, and the safe close (stop both axes, back to local).
"""

import re
import sys
import time
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
        self.timeout = kw.get("timeout")
        self.silent_ma = False
        # THE LATE STATE seen on the Blue Box rig (2026-11-10): every reply is
        # held until the NEXT command arrives. `late_after_move` enters it at
        # the first PR, as the real controller did.
        self.late = False
        self.late_after_move = False
        self._held: list[str] = []

    def _answer(self, cmd: str):
        if cmd == "VE":
            return "AG-UC2 v2.2.1 "                             # the rig's text, trailing blank
        if cmd == "TE":
            return f"TE{self.te}"
        if cmd.endswith("TP"):
            ax = int(cmd[0])
            return f"{ax}TP{self.tp[ax]}"
        if cmd.endswith("TS"):
            return f"{cmd[0]}TS0"
        if cmd == "PH":
            return "PH1"                                        # X on its switch (the rig)
        if "SU" in cmd and cmd.endswith("?"):
            return f"{cmd[0]}SU{cmd[3]}23"
        if cmd.endswith("MA") and not self.silent_ma:
            return f"{cmd[0]}MA523"                             # VERIFY format
        if "PA" in cmd:
            return cmd                                          # "xxPAnn at the end"
        if "PR" in cmd:
            ax = int(cmd[0])
            self.tp[ax] += int(cmd[3:])
            if self.late_after_move:
                self.late = True
        return None                                             # set commands: silent

    def write(self, data: bytes):
        line = data.decode("ascii")
        assert line.endswith("\r\n")
        cmd = line[:-2]
        self.sent.append(cmd)
        if self.late:
            self._out.extend(self._held)                        # released by THIS command
            self._held = []
        reply = self._answer(cmd)
        if reply is not None:
            (self._held if self.late else self._out).append(reply)

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


def test_open_only_queries_and_remote_mode_is_separate(fake_serial):
    """open() must change nothing: VE only. MR is its own step, which the
    brain takes once the axes are at rest."""
    dev, ser = _open(fake_serial)
    assert ser.kw["baudrate"] == 921600 and ser.kw["port"] == "COM9"
    assert ser.sent == ["VE", "VE"]                      # the query + its pusher
    dev.enable_remote()
    assert ser.sent == ["VE", "VE", "MR", "TE", "VE"]
    assert "CC" not in "".join(ser.sent)                 # AG-UC2 has no CC
    assert "AG-UC2" in dev.idn()


def test_uc8_channel_is_selected(fake_serial):
    dev, ser = _open(fake_serial, channel=2)
    dev.enable_remote()
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
    sets = [c for c in ser.sent if c not in ("TE", "VE")]
    assert sets == ["1PR-250", "2JA3", "2ST", "1ZP", "2SU30", "2SU-12"]
    assert ser.sent.count("TE") == 6                     # every set is checked
    assert ser.sent.count("VE") == 6                     # ...and pushed


def test_queries_parse(fake_serial):
    dev, _ser = _open(fake_serial)
    assert dev.read_position(1) == 1234
    assert dev.read_position(2) == -56
    assert dev.axis_state(1) == 0
    assert dev.limit_status() == 1
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


def test_close_without_remote_leaves_the_controller_alone(fake_serial):
    dev, ser = _open(fake_serial)
    ser.sent.clear()
    dev.close()
    assert ser.sent == [] and ser.closed


def test_close_stops_both_axes_and_returns_to_local(fake_serial):
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    ser.sent.clear()
    dev.close()
    assert [c for c in ser.sent if c not in ("TE", "VE")] == ["1ST", "2ST", "ML"]
    assert ser.closed


def test_last_int():
    assert _last_int("1TP-42", "TP") == -42
    assert _last_int("TE-6", "TE") == -6
    with pytest.raises(RuntimeError):
        _last_int("??", "x")


def test_refused_remote_mode_frees_the_com_port(fake_serial, monkeypatch):
    """MR is refused (-6) while an axis still moves; the brain then closes
    the driver, which frees the port and -- since it never took remote mode --
    sends nothing (no ST, no ML)."""
    from agilis.agilis import AgilisStage
    monkeypatch.setattr(FakeSerial, "__init__", _init_te(-6))
    cfg = Config()
    cfg.hardware.port = "COM9"
    dev = AgUC2(cfg)
    brain = AgilisStage(dev, cfg)
    with pytest.raises(RuntimeError, match="-6"):
        brain.start()
    ser = fake_serial[-1]
    assert ser.closed and dev._ser is None
    assert [c for c in ser.sent if c not in ("TE", "VE")] == ["1TS", "2TS", "MR"]


#: Everything the start-up may send: queries, TE, and MR (needed to read at all).
_READ_ONLY = re.compile(r"^(VE|TE|PH|MR|[12]TS|[12]TP|[12]SU[+-]\?)$")


def test_brain_start_on_the_real_driver_writes_nothing_but_mr(fake_serial):
    """THE rule (2026-09-27): start reads the controller and changes nothing.
    A fake AG-UC2 records every line; after start + a few polls, every line
    sent must be a query, TE, or the one MR."""
    from agilis.agilis import AgilisStage
    cfg = Config()
    cfg.hardware.port = "COM9"
    cfg.motion.amp_fwd_x = 40                  # an .ini value that must NOT be sent
    dev = AgUC2(cfg)
    brain = AgilisStage(dev, cfg)
    brain.start()
    try:
        time.sleep(0.2)                        # let the poll thread run too
        ser = fake_serial[-1]
        bad = [c for c in ser.sent if not _READ_ONLY.match(c)]
        assert bad == [], bad
        assert ser.sent.count("MR") == 1
        st = brain.status()
        assert st.position_steps == [1234, -56]            # adopted counters
        assert st.amplitude_fwd == [23, 23] and st.amplitude_bwd == [23, 23]
        assert cfg.motion.amp_fwd_x == 23                  # the .ini 40 was replaced
        assert brain.startup_writes and brain.startup_writes[0].startswith("MR")
    finally:
        brain.shutdown()


def test_ma_pa_mv_strings_and_the_long_wait(fake_serial):
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    ser.sent.clear()
    dev.move_to_limit(1, -3)
    assert ser.sent == ["1MV-3", "TE", "VE"]
    ser.sent.clear()
    assert dev.measure_position(2) == 523
    assert ser.sent == ["2MA"]                # no TE: the reply IS the answer
    assert ser.timeout == 0.5                  # the long timeout was put back
    assert dev.move_absolute(1, 250) == 250
    assert ser.sent[-1] == "1PA250"


def test_silent_ma_asks_te_why(fake_serial):
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    ser.silent_ma = True
    ser.te = -6
    with pytest.raises(RuntimeError, match="not allowed in current state"):
        dev.measure_position(1)


def _init_te(code):
    orig = FakeSerial.__init__

    def init(self, **kw):
        orig(self, **kw)
        self.te = code
    return init


# --------------------------------------------------------------------------- #
# 2026-11-10, Blue Box rig: after a move the AG-UC2 answered ONE COMMAND LATE
# (every reply held until the next command, until a power-cycle). Each test
# below failed on the old driver.
# --------------------------------------------------------------------------- #
def test_late_replies_still_pair_each_question_with_its_answer(fake_serial):
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    ser.late = True
    assert dev.read_position(1) == 1234
    assert dev.read_position(2) == -56
    assert dev.axis_state(2) == 0
    assert dev.limit_status() == 1
    assert dev.read_amplitude(1, +1) == 23
    assert dev.read_position(1) == 1234        # again: nothing drifts by one


def test_a_move_then_late_replies_no_timeout_and_no_false_error(fake_serial):
    """The rig's failure: move_steps X 100 -> 'did not answer (timeout)', then
    stop -> "refused '1ST': error 1" (the shifted PH1 read as TE)."""
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    ser.late_after_move = True
    dev.move_by(1, 100)                        # the move itself enters the late state
    assert ser.late
    assert dev.read_position(1) == 1334
    assert dev.limit_status() == 1
    dev.stop(1)                                # TE0 -- no error
    dev.stop(2)
    assert dev.read_position(2) == -56
    assert dev.late_lines > 0                  # pusher / late lines were dropped


def test_te_errors_are_still_seen_in_the_late_state(fake_serial):
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    ser.late = True
    dev.read_position(1)
    ser.te = -6
    with pytest.raises(RuntimeError, match="not allowed in current state"):
        dev.move_by(1, 10)


def test_ma_answer_held_back_is_released_by_a_pusher(fake_serial):
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    ser.late = True
    assert dev.measure_position(2) == 523


def test_brain_start_and_polls_work_on_a_late_controller(fake_serial):
    """The whole brain on a controller that is ALREADY in the late state (the
    rig after the failure, before its power-cycle)."""
    from agilis.agilis import AgilisStage
    cfg = Config()
    cfg.hardware.port = "COM9"
    dev = AgUC2(cfg)
    real_serial = sys.modules["serial"].Serial

    def late_serial(**kw):
        s = real_serial(**kw)
        s.late = True
        return s
    sys.modules["serial"].Serial = late_serial
    brain = AgilisStage(dev, cfg)
    brain.start()
    try:
        time.sleep(0.3)
        st = brain.status()
        assert st.position_steps == [1234, -56]
        assert st.amplitude_fwd == [23, 23]
        assert not st.hw_error
    finally:
        brain.shutdown()
