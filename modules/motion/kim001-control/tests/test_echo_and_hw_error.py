"""Lukas's three decisions of 2026-09-28, one block of tests each.

(kim-control's decisions, kept on the single focus axis Z of a KIM001.)

1. TARGET ECHO. A scan used to settle a kim position on `moving` alone
   (flag_only). Commands are fire-and-forget, so the status frame from BEFORE
   the move still says "not moving" -- and the scan took the point before the
   stage had left (gotcha #2). Now status carries `target_um` (what the last
   move command asked for) and describe says adopt_then_flag: wait until the
   echo equals the requested position, THEN believe `moving`.
2. LOUD HARDWARE ERRORS. A failed read published position 0, moving False,
   connected True: a dead link looked like a stage resting at the origin.
3. SETTINGS OK re-applied the Fast/Slow and Large/Small presets, so an
   adopted 112 V / 500 steps/s became 125 V / 300 even if only the theme was
   changed.

Every test failed on the code before these changes. Network tests use ports
19452/19453 (not the service's 5637/5638, nor the other test files' ports).
"""

from __future__ import annotations

import os
import time

import pytest

from kim001.backends.sim import SimKim
from kim001.config import Config
from kim001.kim001 import Kim

CMD, PUB = 19452, 19453

# A controller state the .ini would never produce (as in test_adopt_on_start).
FOUND = {"position": 183, "rate": 500, "accel": 1000, "voltage": 112}
Z = 2


def _brain(backend_cls=SimKim, state=None, **kw):
    cfg = Config()                                 # plain 20 nm per step
    backend = backend_cls(cfg, state=state or FOUND, **kw)
    brain = Kim(backend, cfg)
    events: list = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    return brain, backend, events


# --------------------------------------------------------------------------- #
# 1. target echo
# --------------------------------------------------------------------------- #
class _PeekDuringMove(SimKim):
    """A backend that lets a "publisher" build a status frame at the worst
    moments of a move: just BEFORE the controller has the command (it is on
    the USB wire) and just AFTER it was accepted.

    `frames` collects (moment, target_um, moving) of the moved axis. The
    invariant a scan relies on: a frame that shows the NEW target must show
    the post-command `moving` (True for a real move)."""

    def __init__(self, cfg, state=None):
        super().__init__(cfg, state=state)
        self.brain = None
        self.frames: list = []
        self.calls: list = []

    def _peek(self, moment, axis):
        if self.brain is not None:
            st = self.brain.status()
            self.frames.append((moment, st.target_um[axis], st.moving[axis]))

    def move_to(self, axis, position_steps):
        self._peek("before", axis)              # command not yet at the controller
        self.calls.append(("move_to", axis))
        super().move_to(axis, position_steps)
        self._peek("after", axis)               # accepted, echo maybe not stored yet

    def is_moving(self, axis):
        self.calls.append(("is_moving", axis))
        return super().is_moving(axis)


def _no_stale_pair(frames, new_target):
    """No frame may pair the NEW target with "not moving" while the move is
    real -- that pair is exactly what made a scan settle too early."""
    return [f for f in frames if f[1] == pytest.approx(new_target) and not f[2]]


@pytest.mark.parametrize("verb", ["move_to_um", "move_to_step", "move_steps",
                                  "move_relative_um"])
def test_new_target_is_never_published_with_a_pre_command_moving(verb):
    brain, backend, _ = _brain(_PeekDuringMove)
    backend.brain = brain
    z0 = brain.status().position_um[Z]
    if verb == "move_to_um":
        brain.move_to_um(Z, 12.3456789)
        new = 12.3456789
    elif verb == "move_to_step":
        brain.move_to_step(Z, 900)
        new = 900 * brain.um_per_step(Z)
    elif verb == "move_steps":
        brain.move_steps(Z, 400)
        new = (183 + 400) * brain.um_per_step(Z)
    else:
        brain.move_relative_um(Z, 8.0)
        new = z0 + 8.0
    before = [f for f in backend.frames if f[0] == "before"]
    assert before and all(f[1] != pytest.approx(new) for f in before), \
        "the echo was published before the controller had the move"
    assert _no_stale_pair(backend.frames, new) == []
    # and once the setter returned, the echo IS there, with the axis moving
    st = brain.status()
    assert st.target_um[Z] == pytest.approx(new)
    assert st.moving[Z] is True
    brain.shutdown()


def test_status_reads_the_echo_before_asking_the_hardware_about_moving():
    """The other half of the ordering rule: inside status() the echo target is
    read first, `moving` second. Checked by clearing the echo from inside the
    first is_moving() call: a status() that read the echo AFTER moving would
    publish the cleared value."""
    brain, backend, _ = _brain()
    brain.move_to_um(Z, 3.0)
    orig = backend.is_moving
    seen = {"n": 0}

    def is_moving(axis):
        if seen["n"] == 0:
            brain._target_um[Z] = -999.0      # "a new command landed meanwhile"
        seen["n"] += 1
        return orig(axis)

    backend.is_moving = is_moving
    st = brain.status()
    assert st.target_um[Z] == pytest.approx(3.0)     # the value read BEFORE moving
    brain.shutdown()


def test_echo_at_start_is_the_adopted_position():
    brain, _, _ = _brain()
    st = brain.status()
    assert st.target_um == pytest.approx(st.position_um)
    assert st.target_um == pytest.approx([0.0, 0.0, 183 * brain.um_per_step(Z)])
    brain.shutdown()


def test_echo_after_goto_zero_counter_and_stop():
    brain, backend, _ = _brain()
    k = brain.um_per_step(Z)
    # goto: the slot's target (X / Y of a slot are ignored: not fitted)
    brain.positions.store(3, 31, 667, 500, name="s")
    brain.goto_position(3)
    assert brain.status().target_um == pytest.approx([0.0, 0.0, 500 * k])
    # datum: the coordinates changed under the target -> "here" = 0
    brain.stop_all()
    brain.zero_counter(Z)
    assert brain.status().target_um[Z] == 0.0
    # STOP: the target becomes where the axis stopped, so a scan waiting for
    # the old target keeps waiting instead of settling short of it
    brain.move_to_step(Z, 100_000)
    time.sleep(0.05)
    brain.stop(Z)
    st = brain.status()
    assert st.target_um[Z] == pytest.approx(st.position_um[Z])
    assert st.target_um[Z] != pytest.approx(100_000 * k)
    brain.shutdown()


def test_clamped_absolute_move_echoes_where_the_stage_really_goes():
    brain, _, _ = _brain()
    brain.set_leash(enabled=True, leash_z=1000)
    brain.move_to_um(Z, 100.0)                    # 5000 steps, leash 1000
    assert brain.status().target_um[Z] == pytest.approx(1000 * brain.um_per_step(Z))
    brain.shutdown()


def test_describe_settles_positions_on_the_echo():
    from kim001.net.describe import build_manifest
    brain, _, _ = _brain()
    m = {p["id"]: p for p in build_manifest(brain)["parameters"]}
    for i, ax in ((2, "z"),):
        settle = dict(m[f"position_{ax}"]["settle"])
        tol = settle.pop("tol")
        assert settle == {
            "policy": "adopt_then_flag", "setpoint_key": "target_um",
            "flag_key": "moving", "invert": True, "index": i}
        # half a step: a stop echoed on the step grid still matches, while a
        # clamped target (many steps away) does not
        step = brain.status().um_per_step[i]
        assert tol == pytest.approx(0.5 * step)
        assert m[f"position_{ax}"]["stream"] == {"group": "position", "channel": ax}
    assert m["hw_error"]["read_path"] == ["hw_error"]
    brain.shutdown()


def test_echo_and_hw_error_travel_to_the_client():
    pytest.importorskip("zmq")
    from kim001.net.client import Kim001Client
    from kim001.net.service import Kim001Service

    cfg = Config()
    brain = Kim(SimKim(cfg, state=FOUND), cfg)
    svc = Kim001Service(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=30)
    svc.start()
    cli = Kim001Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    try:
        cli.move_to_um("Z", 21.5)
        payload = svc.status_payload()
        assert payload["target_um"][Z] == pytest.approx(21.5)
        assert payload["hw_error"] == ""
        t_end = time.monotonic() + 5
        while time.monotonic() < t_end:
            st = cli.status()
            if st.target_um and st.target_um[Z] == pytest.approx(21.5):
                break
            time.sleep(0.02)
        assert st.target_um[Z] == pytest.approx(21.5)
        assert st.hw_error == ""
    finally:
        cli.close()
        svc.stop()
        time.sleep(0.1)


# --------------------------------------------------------------------------- #
# 2. loud hardware errors
# --------------------------------------------------------------------------- #
class _Unplugged(SimKim):
    """A sim whose reads fail while `dead` is set -- the USB cable pulled."""

    def __init__(self, cfg, state=None):
        super().__init__(cfg, state=state)
        self.dead = False

    def read_position(self, axis):
        if self.dead:
            raise RuntimeError("device not responding")
        return super().read_position(axis)


def test_failed_read_is_loud_and_keeps_the_last_good_position():
    brain, backend, events = _brain(_Unplugged)
    good = brain.status()
    assert good.hw_error == "" and good.connected
    backend.dead = True
    frames = [brain.status() for _ in range(5)]
    st = frames[-1]
    assert "device not responding" in st.hw_error
    assert st.connected is False
    assert st.position_steps == [0, 0, 183]             # last good, not zeros
    assert st.position_um == pytest.approx(good.position_um)
    assert st.moving == [False, False, True]            # Z: "at rest" cannot be confirmed
    assert st.voltage == [0.0, 0.0, 112.0]
    errors = [m for lvl, m in events if lvl == "error"]
    assert len(errors) == 1, errors                     # once per episode, not per frame
    # the link comes back: error cleared, one info line
    backend.dead = False
    st = brain.status()
    assert st.hw_error == "" and st.connected
    assert any(lvl == "info" and "recovered" in m for lvl, m in events)
    # a second episode is reported again
    backend.dead = True
    brain.status()
    brain.status()
    assert len([m for lvl, m in events if lvl == "error"]) == 2
    backend.dead = False
    brain.shutdown()


def test_link_lost_before_the_first_status_frame_still_shows_the_real_position():
    """start() takes one good reading, so even a link that dies before anyone
    asked for status freezes on the real position, not on zeros."""
    brain, backend, _ = _brain(_Unplugged)
    backend.dead = True
    st = brain.status()
    assert st.hw_error and st.position_steps == [0, 0, 183]
    backend.dead = False
    brain.shutdown()


# --------------------------------------------------------------------------- #
# 3. Settings OK leaves the adopted drive state alone
# --------------------------------------------------------------------------- #
@pytest.fixture
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_apply_config_with_nothing_changed_writes_nothing():
    """apply_config pushes only what differs from the controller: Settings OK
    after changing, say, the theme is not a reason to write the drive."""
    brain, backend, _ = _brain()
    backend.writes.clear()
    brain.apply_config()
    assert backend.writes == []
    brain.shutdown()


def test_settings_ok_does_not_reapply_the_presets(qapp, monkeypatch):
    from kim001.apps import gui as gui_mod

    class _OkDialog:                     # Settings opened; only the theme changed
        def __init__(self, cfg, parent=None):
            self.cfg = cfg

        def exec(self):
            self.cfg.ui.theme = "light"
            return True

    monkeypatch.setattr(gui_mod, "SettingsDialog", _OkDialog)
    brain, backend, _ = _brain()
    win = gui_mod.MainWindow(brain, brain.cfg, remote=False)
    try:
        backend.writes.clear()
        win._open_settings()
        st = brain.status()
        assert st.voltage[Z] == 112.0
        assert st.step_rate[Z] == 500.0
        assert st.acceleration[Z] == 1000.0
        assert backend.writes == []
    finally:
        win.close()
        brain.shutdown()


def test_gui_shows_hw_error_in_red(qapp):
    from kim001.apps import gui as gui_mod
    from kim001.apps import theme

    brain, backend, _ = _brain(_Unplugged)
    win = gui_mod.MainWindow(brain, brain.cfg, remote=False)
    try:
        win._refresh()
        assert win._hw_error_lbl.isHidden()
        backend.dead = True
        win._refresh()
        assert not win._hw_error_lbl.isHidden()
        assert "device not responding" in win._hw_error_lbl.text()
        assert theme.COLORS["danger"].lower() in win._hw_error_lbl.styleSheet().lower()
        backend.dead = False
        win._refresh()
        assert win._hw_error_lbl.isHidden()
    finally:
        win.close()
        brain.shutdown()
