"""Target echo + loud hardware errors (Lukas's decisions, 2026-09-28).

1. TARGET ECHO.  A scan used to settle a position on `flag_only(moving)`.
   Commands are fire-and-forget, so the first status frame a scan sees after
   `move_axis` can be one built BEFORE the move -- `moving` still False -- and
   the scan then "arrives" at once, at the old place.  The status now carries
   `target_mm`, the target as requested by the last move of each axis, and the
   position settle is `adopt_then_flag(target_mm, moving)`: the scan first
   waits for ITS target to appear, and only then believes `moving`.

   That is only safe if a frame carrying the new target can never carry a
   `moving` read from before the move.  The ordering rule that guarantees it
   (setter: hardware move first, echo after; status: echo first, moving after)
   is proven here by injecting a move at EVERY backend call inside status().

2. LOUD HARDWARE ERRORS.  A failed read used to publish NaN positions and
   `moving = False` -- "at rest" -- as if healthy.  Now `hw_error` says what
   failed, the last good values are kept, and one error event is sent per
   failure episode.

Offline; the service test does not bind any port.
"""

from __future__ import annotations

import math

import pytest

from stage.backends.sim import SimStage
from stage.config import Config
from stage.net import protocol as P
from stage.net.client import _status_from_dict
from stage.net.describe import build_manifest
from stage.stage import Stage


def _brain(cfg=None, backend=None):
    cfg = cfg or Config()
    backend = backend or SimStage(cfg)
    brain = Stage(backend, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    return brain, backend, events


# --------------------------------------------------------------------------- #
# describe
# --------------------------------------------------------------------------- #
def test_position_settles_on_the_echoed_target():
    brain, _, _ = _brain()
    brain.start()
    params = {p["id"]: p for p in build_manifest(brain)["parameters"]}
    for i, ax in enumerate("xyz"):
        s = params[f"position_{ax}"]["settle"]
        assert s == {"policy": "adopt_then_flag", "setpoint_key": "target_mm",
                     "flag_key": "moving", "invert": True, "index": i}
    brain.shutdown()


# --------------------------------------------------------------------------- #
# what the echo holds
# --------------------------------------------------------------------------- #
def test_echo_adopts_the_position_at_start():
    brain, sim, _ = _brain()
    sim.preset(position=[7.5, 1.25, 3.0])
    brain.start()
    assert brain.status().target_mm == [7.5, 1.25, 3.0]
    brain.shutdown()


def test_echo_is_the_requested_target_unrounded():
    brain, _, _ = _brain()
    brain.start()
    brain.move_axis(0, 3.123456789)
    assert brain.status().target_mm[0] == 3.123456789
    brain.shutdown()


def test_echo_of_relative_and_logical_moves_is_the_device_target():
    # position_x is a DEVICE coordinate control, so the echo must be in device
    # coordinates whatever verb produced the move.
    brain, _, _ = _brain()
    brain.start()
    brain.cfg.relative.rel_x = 2.0
    brain.move_relative(0, 1.5)
    assert brain.status().target_mm[0] == pytest.approx(3.5)

    brain.set_matrix(0.0, 1.0, 1.0, 0.0)       # u -> device y, v -> device x
    brain.set_offset(2, 0.5)
    brain.move_logical(4.0, 6.0, 1.0)
    dev = brain.device_from_logical(4.0, 6.0, 1.0)
    assert brain.status().target_mm == pytest.approx(list(dev))
    brain.shutdown()


def test_echo_of_a_clamped_move_is_what_the_hardware_was_told():
    brain, _, _ = _brain()
    brain.start()
    hi = brain.cfg.limits.max_x
    brain.move_axis(0, hi + 10.0)
    assert brain.status().target_mm[0] == hi
    brain.shutdown()


def test_stop_replaces_the_echo_with_where_the_axis_stopped():
    # After STOP the axis is NOT at the requested target.  Keeping that target
    # in the echo would let a waiting scan "arrive" there once moving is False.
    brain, sim, _ = _brain()
    brain.start()
    brain.set_velocity(0, 0.5)
    brain.move_axis(0, 20.0)
    brain.stop(0)
    st = brain.status()
    assert st.moving[0] is False
    assert st.target_mm[0] == pytest.approx(st.position[0])
    assert st.target_mm[0] != 20.0
    brain.shutdown()


def test_home_clears_the_echo():
    brain, _, _ = _brain()
    brain.start()
    brain.move_axis(1, 4.0)
    brain.home(1)
    # None, not NaN: scan-core's adopt check does abs(float(sp) - target) > tol,
    # which is False for NaN -- a NaN echo would count as "adopted".
    assert brain.status().target_mm[1] is None
    brain.shutdown()


# --------------------------------------------------------------------------- #
# the ordering rule
# --------------------------------------------------------------------------- #
class InjectingSim(SimStage):
    """A sim that runs a MOVE COMMAND in the middle of a status() call.

    ``inject_at = k`` makes the k-th backend call made by status() first run
    ``brain.move_axis(0, target)`` -- exactly what the command thread can do
    between any two reads of the publisher thread.
    """

    def __init__(self, cfg):
        super().__init__(cfg)
        self.brain = None
        self.inject_at = None
        self.target = 12.0
        self._calls = 0
        self._busy = False

    def _maybe_inject(self):
        if self.inject_at is None or self._busy:
            return
        k = self._calls
        self._calls += 1
        if k == self.inject_at:
            self._busy = True
            try:
                self.brain.move_axis(0, self.target)
            finally:
                self._busy = False

    def read_position(self, axis):
        self._maybe_inject()
        return super().read_position(axis)

    def is_moving(self, axis):
        self._maybe_inject()
        return super().is_moving(axis)

    def is_homed(self, axis):
        self._maybe_inject()
        return super().is_homed(axis)

    def read_velocity(self, axis):
        self._maybe_inject()
        return super().read_velocity(axis)

    def read_acceleration(self, axis):
        self._maybe_inject()
        return super().read_acceleration(axis)


def test_a_frame_with_the_new_target_never_carries_a_stale_moving():
    """Inject the move at every possible point of status(); the forbidden frame
    is (echo == new target) AND (moving False) while the axis is not there."""
    cfg = Config()
    n_calls = None
    for k in range(40):
        sim = InjectingSim(cfg)
        brain, _, _ = _brain(cfg, sim)
        sim.brain = brain
        brain.start()
        brain.set_velocity(0, 0.1)    # slow: the move is still running afterwards
        sim.inject_at = k
        sim._calls = 0
        st = brain.status()
        if sim._calls <= k:          # ran out of calls: every point covered
            n_calls = sim._calls
            brain.shutdown()
            break
        if st.target_mm[0] == sim.target:
            assert st.moving[0] is True or math.isclose(st.position[0], sim.target), (
                f"injected at backend call {k}: frame says target "
                f"{st.target_mm[0]} but moving={st.moving[0]} at {st.position[0]}")
        brain.shutdown()
    assert n_calls is not None and n_calls >= 6   # the loop really covered status()


def test_the_setter_stores_the_echo_only_after_the_hardware_move():
    """A status frame built WHILE move_to is on the wire must still show the
    previous target (the hardware has not been told yet)."""
    seen = {}

    class SlowMove(SimStage):
        def move_to(self, axis, position):
            seen["during"] = brain.status().target_mm[axis]
            super().move_to(axis, position)

    cfg = Config()
    sim = SlowMove(cfg)
    brain, _, _ = _brain(cfg, sim)
    brain.start()
    brain.move_axis(0, 6.0)
    assert seen["during"] == 0.0
    assert brain.status().target_mm[0] == 6.0
    brain.shutdown()


# --------------------------------------------------------------------------- #
# loud hardware errors
# --------------------------------------------------------------------------- #
class FlakySim(SimStage):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.fail = False

    def read_position(self, axis):
        if self.fail:
            raise OSError("USB read timed out")
        return super().read_position(axis)


def test_a_failed_read_is_loud_and_keeps_the_last_good_values():
    cfg = Config()
    sim = FlakySim(cfg)
    brain, _, events = _brain(cfg, sim)
    sim.preset(position=[1.0, 2.0, 3.0], homed=[True, True, True])
    brain.start()
    good = brain.status()
    assert good.hw_error == ""

    sim.fail = True
    events.clear()
    bad = [brain.status() for _ in range(5)]
    for st in bad:
        assert "USB read timed out" in st.hw_error
        assert st.position == [1.0, 2.0, 3.0]            # last good, not NaN
        assert st.homed == [True, True, True]
        assert st.velocity == good.velocity              # not 0
        # unknown is not "at rest": a scan must not settle on a failed read
        assert st.moving == [True, True, True]
    errors = [m for lvl, m in events if lvl == "error"]
    assert len(errors) == 1, errors                      # one per episode

    sim.fail = False
    st = brain.status()
    assert st.hw_error == ""
    assert any(lvl == "info" and "recovered" in m for lvl, m in events)

    sim.fail = True                                      # a NEW episode
    brain.status()
    assert len([m for lvl, m in events if lvl == "error"]) == 2
    brain.shutdown()


def test_the_wire_carries_target_and_hw_error():
    cfg = Config()
    sim = FlakySim(cfg)
    brain, _, _ = _brain(cfg, sim)
    brain.start()
    brain.move_axis(2, 1.5)
    sim.fail = True
    d = P.status_to_dict(brain.status())
    assert d["target_mm"][2] == 1.5
    assert d["hw_error"]
    rs = _status_from_dict(d)
    assert rs.target_mm[2] == 1.5
    assert rs.hw_error == d["hw_error"]
    # an older service without the fields still parses
    old = _status_from_dict({"position": [0, 0, 0]})
    assert old.hw_error == ""
    brain.shutdown()


def test_gui_shows_hw_error_in_red():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from stage.apps import theme
    from stage.apps.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    cfg = Config()
    sim = FlakySim(cfg)
    brain, _, _ = _brain(cfg, sim)
    brain.start()
    win = MainWindow(brain, cfg, remote=False)
    win._refresh()
    assert win._hw_err_lbl.isHidden()
    sim.fail = True
    win._refresh()
    assert not win._hw_err_lbl.isHidden()
    assert "USB read timed out" in win._hw_err_lbl.text()
    assert theme.COLORS["danger"].lower() in win._hw_err_lbl.styleSheet().lower()
    sim.fail = False
    win._refresh()
    assert win._hw_err_lbl.isHidden()
    win.close()
    brain.shutdown()
    del app
