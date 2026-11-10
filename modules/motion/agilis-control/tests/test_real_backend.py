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
    """Answers like an AG-UC2 -- including how it releases replies, as
    MEASURED on the Blue Box (2026-11-10):
      * at most ONE reply line goes out per incoming write; replies wait in
        a queue (`held`) in order;
      * two lines in one write are both executed, but only one line goes out;
      * `delay_next` = the next reply is produced too late for its write's
        release (as while an axis steps), so it stays held -> one behind;
        `delay_after_move` sets it at every PR.
    """

    def __init__(self, **kw):
        self.kw = kw
        self.sent = []                 # every command line, in order
        self.writes = 0                # USB writes (a write can hold 2 lines)
        self.held: list[str] = []      # replies waiting in the controller
        self._out: list[str] = []      # released, readable
        self.te = 0
        self.tp = {1: 1234, 2: -56}
        self.closed = False
        self.timeout = kw.get("timeout")
        self.silent_ma = False
        self.delay_next = False
        self.delay_after_move = False
        self.dtr = self.rts = None
        self.write_times: list[float] = []

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
            if self.delay_after_move:
                self.delay_next = True
        return None                                             # set commands: silent

    def write(self, data: bytes):
        text = data.decode("ascii")
        assert text.endswith("\r\n")
        self.writes += 1
        self.write_times.append(time.monotonic())
        late = []
        for cmd in text[:-2].split("\r\n"):
            self.sent.append(cmd)
            reply = self._answer(cmd)
            if reply is None:
                continue
            if self.delay_next:                                 # ready too late
                self.delay_next = False
                late.append(reply)
            else:
                self.held.append(reply)
        if self.held:                                           # ONE line out per write
            self._out.append(self.held.pop(0))
        self.held.extend(late)

    def flush(self):
        pass

    def readline(self):
        return (self._out.pop(0) + "\r\n").encode("ascii") if self._out else b""

    def reset_input_buffer(self):
        self._out.clear()

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
    cfg.hardware.timeout_s = 0.3           # the fake answers at once; keep timeouts short
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
    assert ser.sent == ["VE"]                            # in step: no release query
    assert ser.dtr is True and ser.rts is True           # as Newport's library opens it
    dev.enable_remote()
    assert ser.sent == ["VE", "MR", "TE"]
    assert ser.writes == len(ser.sent)                   # ONE command per write
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
    ser.writes = 0
    dev.move_by(1, -250)
    dev.jog(2, 3)
    dev.stop(2)
    dev.zero_counter(1)
    dev.set_amplitude(2, +1, 30)
    dev.set_amplitude(2, -1, 12)
    sets = [c for c in ser.sent if c != "TE"]
    assert sets == ["1PR-250", "2JA3", "2ST", "1ZP", "2SU30", "2SU-12"]
    assert ser.sent.count("TE") == 6                     # every set is checked
    assert ser.writes == len(ser.sent)                   # ONE command per write


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
    assert [c for c in ser.sent if c != "TE"] == ["1ST", "2ST", "ML"]
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
    assert [c for c in ser.sent if c != "TE"] == ["VE", "1TS", "2TS", "MR"]


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
    assert ser.sent == ["1MV-3", "TE"]
    ser.sent.clear()
    assert dev.measure_position(2) == 523
    assert ser.sent == ["2MA"]                # no TE: the reply IS the answer
    assert ser.timeout == dev.cfg.hardware.timeout_s   # the long timeout was put back
    assert dev.move_absolute(1, 250) == 250
    assert ser.sent[-1] == "1PA250"


def test_silent_ma_asks_te_why(fake_serial):
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    ser.silent_ma = True
    ser.te = -6
    dev.cfg.hardware.limit_op_timeout_s = 0.2     # a refused MA answers nothing: do not wait 150 s
    with pytest.raises(RuntimeError, match="not allowed in current state"):
        dev.measure_position(1)


def _init_te(code):
    orig = FakeSerial.__init__

    def init(self, **kw):
        orig(self, **kw)
        self.te = code
    return init


# --------------------------------------------------------------------------- #
# 2026-11-10, Blue Box rig: the AG-UC2 lets out at most ONE reply line per
# incoming write. A late reply (while stepping) or two lines in one write leave
# a line held, and every answer is one behind. The fake above does exactly that.
# --------------------------------------------------------------------------- #
def _controller_one_behind(ser):
    """Put the fake in the rig's broken state: one stale line held."""
    ser.held.append("AG-UC2 v2.2.1 ")


def test_one_command_per_write_and_no_release_query_in_step(fake_serial):
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    ser.sent.clear()
    ser.writes = 0
    assert dev.read_position(1) == 1234
    dev.move_by(2, 10)
    assert dev.axis_state(2) == 0
    assert ser.sent == ["1TP", "2PR10", "TE", "2TS"]       # no VE: nothing was held
    assert ser.writes == 4 and ser.held == [] and dev.late_lines == 0


def test_one_behind_still_pairs_each_question_with_its_answer(fake_serial):
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    _controller_one_behind(ser)
    for _ in range(3):                                      # repeatedly: nothing drifts
        assert dev.read_position(1) == 1234
        assert dev.read_position(2) == -56
        assert dev.axis_state(2) == 0
        assert dev.limit_status() == 1
        assert dev.read_amplitude(1, +1) == 23
    assert len(ser.held) <= 1, "the backlog must never GROW"
    assert dev.late_lines > 0


def test_a_move_that_puts_it_one_behind_is_survived(fake_serial):
    """The rig's failure: move_steps X 100 -> 'did not answer (timeout)', then
    stop -> "refused '1ST': error 1" (the held PH1 read as TE)."""
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    ser.delay_after_move = True
    dev.move_by(1, 100)                                     # its TE comes out late
    assert dev.read_position(1) == 1334
    assert dev.limit_status() == 1
    dev.stop(1)                                             # TE0 -- no error
    dev.stop(2)
    for _ in range(5):
        dev.move_by(2, 10)
        assert dev.axis_state(2) == 0
    assert dev.read_position(2) == -6
    assert len(ser.held) <= 2, "bounded, not growing with every exchange"


def test_te_errors_are_still_seen_when_one_behind(fake_serial):
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    _controller_one_behind(ser)
    dev.read_position(1)
    ser.te = -6
    with pytest.raises(RuntimeError, match="not allowed in current state"):
        dev.move_by(1, 10)


def test_never_two_commands_in_one_write(fake_serial):
    """Two lines in one write = one answer out, one held: the backlog grows.
    The driver never writes two."""
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    _controller_one_behind(ser)
    for _ in range(10):
        dev.read_position(1)
        dev.move_by(1, 1)
    assert ser.writes == len(ser.sent)


def test_hopelessly_out_of_step_says_power_cycle(fake_serial):
    """More lines held than the release attempts can get through: a clear
    message, not a bare timeout."""
    from agilis.backends.ag_uc2 import AgUC2OutOfStep
    dev, ser = _open(fake_serial)
    ser.held.extend(["X1"] * 10)
    with pytest.raises(AgUC2OutOfStep, match="power-cycle"):
        dev.read_position(1)


def test_ma_answer_held_back_is_released(fake_serial):
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    ser.delay_next = True                                   # the MA answer comes late
    dev.cfg.hardware.limit_op_timeout_s = 0.2
    assert dev.measure_position(2) == 523


def test_brain_start_and_polls_work_when_one_behind(fake_serial):
    """The whole brain on a controller ALREADY one behind (the rig after the
    failure, before its power-cycle)."""
    from agilis.agilis import AgilisStage
    cfg = Config()
    cfg.hardware.port = "COM9"
    cfg.hardware.timeout_s = 0.3
    dev = AgUC2(cfg)
    real_serial = sys.modules["serial"].Serial

    def behind(**kw):
        s = real_serial(**kw)
        _controller_one_behind(s)
        return s
    sys.modules["serial"].Serial = behind
    brain = AgilisStage(dev, cfg)
    brain.start()
    try:
        time.sleep(0.5)
        st = brain.status()
        assert st.position_steps == [1234, -56]
        assert st.amplitude_fwd == [23, 23]
        assert not st.hw_error
    finally:
        brain.shutdown()


def test_defaults_follow_newports_pace():
    cfg = Config()
    assert cfg.hardware.poll_hz == 5.0 and cfg.hardware.timeout_s >= 2.0


def test_writes_are_never_closer_than_the_minimum_gap(fake_serial):
    """What keeps two commands out of ONE USB packet on the real link (the
    driver merged them otherwise, and the backlog grew): a gap between writes.
    This morning's first fix wrote the release query right behind each
    command -- it fails here."""
    from agilis.backends.ag_uc2 import MIN_GAP_S
    dev, ser = _open(fake_serial)
    dev.enable_remote()
    _controller_one_behind(ser)
    ser.write_times.clear()
    for _ in range(5):
        dev.read_position(1)
        dev.move_by(1, 1)
    gaps = [b - a for a, b in zip(ser.write_times, ser.write_times[1:])]
    assert min(gaps) >= MIN_GAP_S * 0.95, min(gaps)
