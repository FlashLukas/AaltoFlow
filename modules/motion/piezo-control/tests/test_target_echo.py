"""Target echo + loud hardware errors (Lukas's decisions, 2026-09-28).

1. TARGET ECHO.  The position settle used to be `flag_only(moving)`.  Commands
   are fire-and-forget, so the first frame a scan sees after `move_axis` can be
   one built BEFORE the move -- `moving` still False -- and the scan "arrives"
   at once, at the old place.  The status now carries `target_um` (the target
   as requested by the last move of each axis) and the position settles with
   `adopt_then_flag(target_um, moving)`.  That is only safe if a frame with the
   new target can never carry a `moving` from before the move; proven below by
   sampling status from another thread while moves run.

2. LOUD HARDWARE ERRORS.  A failed position read used to publish NaN and, for
   a closed-loop axis, `moving = False` (NaN compares as "there").  Now
   `hw_error` says so, the last good position is kept, a closed-loop axis
   reports moving (unknown is not at rest), and one error event is sent per
   failure episode.

Offline, simulator only, no ports.
"""

from __future__ import annotations

import threading
import time

import pytest

from piezo.backends.sim import SimPiezo
from piezo.config import Config
from piezo.net import protocol as P
from piezo.net.client import _status_from_dict
from piezo.net.describe import build_manifest
from piezo.piezo import SETTLE_TOL, Piezo


def _brain(cfg=None, backend_cls=SimPiezo):
    cfg = cfg or Config()
    backend = backend_cls(cfg)
    brain = Piezo(backend, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    return brain, backend, events


def _wait(pred, timeout):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- #
# describe + what the echo holds
# --------------------------------------------------------------------------- #
def test_position_settles_on_the_echoed_target():
    brain, _, _ = _brain()
    brain.start()
    params = {p["id"]: p for p in build_manifest(brain)["parameters"]}
    for i, ax in enumerate("xy"):
        assert params[f"position_{ax}"]["settle"] == {
            "policy": "adopt_then_flag", "setpoint_key": "target_um",
            "flag_key": "moving", "invert": True, "index": i}
    brain.shutdown()


def test_echo_adopts_the_setpoint_at_start():
    brain, sim, _ = _brain()
    sim.preset(0, position=42.5)
    sim.preset(1, position=17.25)
    brain.start()
    assert brain.status().target_um == [42.5, 17.25]
    brain.shutdown()


@pytest.mark.parametrize("mode", ["software", "hardware", "off"])
def test_echo_is_the_requested_target_unrounded(mode):
    cfg = Config()
    cfg.motion.ramp_mode = mode
    brain, _, _ = _brain(cfg)
    brain.start()
    brain.move_axis(1, 12.3456789)
    assert brain.status().target_um[1] == 12.3456789
    brain.move_relative(0, 2.5)                      # rel origin 0 -> 2.5 um
    assert brain.status().target_um[0] == 2.5
    brain.shutdown()


def test_stop_replaces_the_echo_with_the_held_position():
    cfg = Config()
    cfg.motion.ramp_mode = "software"
    cfg.motion.vel_x = 5.0
    brain, sim, _ = _brain(cfg)
    brain.start()
    brain.move_axis(0, 100.0)
    time.sleep(0.1)
    brain.stop(0)
    st = brain.status()
    assert st.moving[0] is False
    assert st.target_um[0] == pytest.approx(sim.read_setpoint(0))
    assert st.target_um[0] < 100.0
    brain.shutdown()


# --------------------------------------------------------------------------- #
# the ordering rule
# --------------------------------------------------------------------------- #
def _sample_during(brain, action, seconds):
    """Call status() as fast as possible from another thread while ``action``
    runs in this one; return every frame seen (with the setpoint the backend
    held when the frame was finished)."""
    frames = []
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            st = brain.status()
            frames.append((st, list(brain.backend._setpoint)))

    t = threading.Thread(target=sampler, daemon=True)
    t.start()
    action()
    time.sleep(seconds)
    stop.set()
    t.join(2.0)
    return frames


@pytest.mark.parametrize("mode,closed", [("software", True), ("software", False),
                                         ("hardware", True), ("hardware", False)])
def test_no_frame_pairs_the_new_target_with_a_premove_moving(mode, closed):
    cfg = Config()
    cfg.motion.ramp_mode = mode
    cfg.motion.vel_x = 200.0                       # 30 um in 0.15 s
    brain, sim, _ = _brain(cfg)
    sim.preset(0, position=10.0, closed=closed)
    if mode == "hardware":
        sim.preset(0, slew=200.0)
    brain.start()
    target = 40.0
    frames = _sample_during(brain, lambda: brain.move_axis(0, target), 0.4)
    assert frames
    seen_new = False
    for st, setpoint in frames:
        if st.target_um[0] != target:
            continue
        seen_new = True
        if st.moving[0]:
            continue
        # "arrived": the drive must really be at the target ...
        assert setpoint[0] == pytest.approx(target), (st, setpoint)
        # ... and in closed loop the sensor too
        if closed:
            assert abs(st.position[0] - target) <= SETTLE_TOL, st
    assert seen_new
    assert not frames[-1][0].moving[0]              # and it did settle
    brain.shutdown()


def test_software_ramp_is_moving_until_the_last_step_is_written():
    """Lukas: moving must stay True until the ramp REACHES the target -- a scan
    measuring at the first moving=False must not catch the drive mid-ramp."""
    cfg = Config()
    cfg.motion.ramp_mode = "software"
    cfg.motion.vel_x = 100.0
    brain, sim, _ = _brain(cfg)
    brain.start()
    frames = _sample_during(brain, lambda: brain.move_axis(0, 20.0), 0.35)
    for st, setpoint in frames:
        if st.target_um[0] == 20.0 and not st.moving[0]:
            assert setpoint[0] == 20.0
    brain.shutdown()


def test_the_setter_stores_the_echo_only_after_the_hardware_write():
    """A frame built while set_setpoint is on the wire must not show the new
    target yet.  (status() blocks on the brain's lock meanwhile, so it cannot
    finish before the setter; if it does, it must describe the OLD target.)"""
    seen = {}

    class Probe(SimPiezo):
        def set_setpoint(self, axis, position):
            if "frame" not in seen and position == 33.0:
                out = []
                th = threading.Thread(target=lambda: out.append(brain.status()),
                                      daemon=True)
                th.start()
                th.join(0.1)
                seen["frame"] = out
                seen["thread"] = th
            super().set_setpoint(axis, position)

    cfg = Config()
    cfg.motion.ramp_mode = "off"
    brain, _, _ = _brain(cfg, Probe)
    brain.start()
    brain.move_axis(0, 33.0)
    early = seen["frame"]
    if early:                                         # finished during the write
        assert early[0].target_um[0] != 33.0
    seen["thread"].join(2.0)
    assert brain.status().target_um[0] == 33.0
    brain.shutdown()


# --------------------------------------------------------------------------- #
# loud hardware errors
# --------------------------------------------------------------------------- #
class FlakySim(SimPiezo):
    fail = False

    def read_position(self, axis):
        if self.fail:
            raise OSError("COM3: no reply")
        return super().read_position(axis)


def test_a_failed_read_is_loud_and_keeps_the_last_good_position():
    cfg = Config()
    cfg.motion.ramp_mode = "off"
    brain, sim, events = _brain(cfg, FlakySim)
    sim.preset(0, position=30.0, closed=True)
    sim.preset(1, position=60.0, closed=True)
    brain.start()
    good = brain.status()
    assert good.hw_error == ""
    assert good.moving == [False, False]

    sim.fail = True
    events.clear()
    for _ in range(4):
        st = brain.status()
        assert "COM3: no reply" in st.hw_error
        assert st.position == good.position           # last good, not NaN
        assert st.moving == [True, True]              # CL: unknown != arrived
    assert len([m for lvl, m in events if lvl == "error"]) == 1

    sim.fail = False
    st = brain.status()
    assert st.hw_error == "" and st.moving == [False, False]
    assert any(lvl == "info" and "recovered" in m for lvl, m in events)
    sim.fail = True
    brain.status()
    assert len([m for lvl, m in events if lvl == "error"]) == 2
    brain.shutdown()


def test_the_wire_carries_target_and_hw_error():
    brain, sim, _ = _brain(Config(), FlakySim)
    brain.start()
    brain.move_axis(1, 5.5)
    sim.fail = True
    d = P.status_to_dict(brain.status())
    assert d["target_um"][1] == 5.5 and d["hw_error"]
    rs = _status_from_dict(d)
    assert rs.target_um[1] == 5.5 and rs.hw_error == d["hw_error"]
    assert _status_from_dict({}).hw_error == ""        # older service
    brain.shutdown()


def test_gui_shows_hw_error_in_red():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from piezo.apps import theme
    from piezo.apps.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    cfg = Config()
    cfg.motion.ramp_mode = "off"
    brain, sim, _ = _brain(cfg, FlakySim)
    brain.start()
    win = MainWindow(brain, cfg, remote=False)
    win._refresh()
    assert win._hw_err_lbl.isHidden()
    sim.fail = True
    win._refresh()
    assert not win._hw_err_lbl.isHidden()
    assert "COM3: no reply" in win._hw_err_lbl.text()
    assert theme.COLORS["danger"].lower() in win._hw_err_lbl.styleSheet().lower()
    sim.fail = False
    win._refresh()
    assert win._hw_err_lbl.isHidden()
    win.close()
    brain.shutdown()
    del app
