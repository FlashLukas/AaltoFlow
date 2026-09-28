"""Regression tests from the deep cleaning of 2026-09-28.

Each test here FAILED on the code before that date and proves one bug; the
docstring says what went wrong on the stage.  All offline (simulator), network
tests on non-default ports 15792/15793.

A reminder of the sim's open-loop model, which several tests lean on: in OL the
read-out is ``true * (1 + g) + c`` with g = +1.5 % / -1.2 % and c = +0.20 /
-0.15 um for X / Y.  So at a held setpoint of 100 um the read-out is 101.70 (X)
and 98.65 (Y) -- the read-out is NOT where the drive is.
"""

import os
import threading
import time

import pytest

from piezo.config import Config
from piezo.sim_system import build_sim_system


def _wait(pred, timeout):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _open_loop_at(cfg, x=100.0, y=100.0):
    """A started brain with both axes in open loop, at rest at (x, y)."""
    brain, backend = build_sim_system(cfg)
    backend.preset(0, position=x, closed=False)
    backend.preset(1, position=y, closed=False)
    brain.start()
    return brain, backend


# --------------------------------------------------------------------------- #
# 1. "moving" in open loop never went False (hardware / off ramp mode)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["off", "hardware"])
def test_open_loop_move_settles(mode):
    """OL + hardware/off: `moving` compared the read-out with the target, but
    in OL the read-out carries the hysteresis error (here 1.5 %), so the axis
    reported moving FOREVER -- and scan-core, which waits on moving (describe
    settle policy flag_only/moving/invert), would wait until its timeout at
    every point of an open-loop scan."""
    cfg = Config()
    cfg.motion.ramp_mode = mode
    cfg.motion.vel_x = cfg.motion.vel_y = 200.0
    brain, backend = _open_loop_at(cfg, 50.0, 50.0)
    if mode == "hardware":
        brain.set_velocity(0, 200.0)         # 20 um at 200 um/s = 0.1 s
    brain.move_axis(0, 70.0)
    if mode == "hardware":
        assert brain.status().moving[0]      # honest while it slews
    assert _wait(lambda: not brain.status().moving[0], 1.5), brain.status()
    brain.shutdown()


# --------------------------------------------------------------------------- #
# 2. changing the velocity mid-ramp made the setpoint jump
# --------------------------------------------------------------------------- #
def test_velocity_change_mid_software_ramp_does_not_jump():
    """The ramp computed `from + v * (now - t0)` with the NEW v but the OLD
    anchor, so raising 10 -> 20 um/s after 1 s teleported the setpoint from
    ~10 to ~20 um in one tick (the kim gotcha #10, in this module)."""
    cfg = Config()
    cfg.motion.ramp_mode = "software"
    cfg.motion.ramp_hz = 100.0
    cfg.motion.vel_x = 10.0
    brain, backend = build_sim_system(cfg)
    brain.start()
    brain.move_axis(0, 100.0)
    time.sleep(1.0)
    before = backend.read_setpoint(0)          # ~10 um
    brain.set_velocity(0, 20.0)
    time.sleep(0.05)                           # a few ticks at 20 um/s = ~1 um
    after = backend.read_setpoint(0)
    assert after - before < 3.0, (before, after)
    brain.shutdown()


# --------------------------------------------------------------------------- #
# 3. the software ramp started from the READ-OUT, not from the drive
# --------------------------------------------------------------------------- #
def test_open_loop_software_ramp_starts_where_the_drive_is():
    """In OL the ramp was anchored at read_position(), which is off the held
    setpoint by the hysteresis error.  On Y (read-out 98.65 at setpoint 100)
    a move UP to 110 first stepped the drive DOWN by 1.35 um."""
    cfg = Config()
    cfg.motion.ramp_mode = "software"
    cfg.motion.ramp_hz = 100.0
    cfg.motion.vel_y = 10.0
    brain, backend = _open_loop_at(cfg)
    written = []
    real_set = backend.set_setpoint

    def spy(axis, value):
        if axis == 1:
            written.append(value)
        real_set(axis, value)

    backend.set_setpoint = spy
    brain.move_axis(1, 110.0)
    time.sleep(0.2)
    brain.shutdown()
    assert written, "the ramp wrote nothing"
    assert min(written) >= 100.0 - 1e-9, written[:5]      # never backwards
    assert written[0] - 100.0 < 0.5, written[:5]           # no jump either


# --------------------------------------------------------------------------- #
# 4. STOP moved an open-loop stage
# --------------------------------------------------------------------------- #
def test_stop_in_open_loop_does_not_move_the_drive():
    """stop() wrote the READ-OUT back as the setpoint.  In OL at rest at 100 um
    that is 101.70 on X: pressing STOP moved the stage by 1.7 um."""
    cfg = Config()
    cfg.motion.ramp_mode = "off"
    brain, backend = _open_loop_at(cfg)
    brain.stop(0)
    assert abs(backend.read_setpoint(0) - 100.0) < 1e-9, backend.read_setpoint(0)
    assert abs(brain.status().target[0] - 100.0) < 1e-9
    brain.shutdown()


def test_stop_mid_software_ramp_in_open_loop_freezes_the_drive():
    """Mid-ramp in OL, STOP must hold the drive where the ramp had put it,
    not jump it to the (biased) read-out."""
    cfg = Config()
    cfg.motion.ramp_mode = "software"
    cfg.motion.ramp_hz = 100.0
    cfg.motion.vel_x = 20.0
    brain, backend = _open_loop_at(cfg, 20.0, 20.0)
    brain.move_axis(0, 120.0)
    time.sleep(0.5)
    drive = backend.read_setpoint(0)            # ~30 um
    brain.stop(0)
    # at most one more ramp tick (0.2 um at 20 um/s, 100 Hz) may have landed
    assert abs(backend.read_setpoint(0) - drive) < 0.5, (drive, backend.read_setpoint(0))
    assert not brain.status().moving[0]
    brain.shutdown()


# --------------------------------------------------------------------------- #
# 5. a stale ramp write could land AFTER a direct move and strand the stage
# --------------------------------------------------------------------------- #
def test_ramp_write_cannot_overwrite_a_later_move():
    """The ramp thread computed its next setpoint under the lock but WROTE it
    after releasing the lock.  If a direct move (ramp mode off/hardware, or a
    stop) happened in between, the stale ramp value landed last: the stage
    stayed at the old ramp point while `target` showed the new one, and in CL
    `moving` stayed True forever.  Forced here by pausing the ramp thread
    inside its write."""
    cfg = Config()
    cfg.motion.ramp_mode = "software"
    cfg.motion.ramp_hz = 100.0
    cfg.motion.vel_x = 10.0
    brain, backend = build_sim_system(cfg)
    brain.start()
    brain.move_axis(0, 100.0)
    time.sleep(0.3)

    entered, release = threading.Event(), threading.Event()
    real_set = backend.set_setpoint
    armed = [True]

    def slow_set(axis, value):
        if armed[0] and threading.current_thread().name == "piezo-ramp":
            armed[0] = False
            entered.set()
            release.wait(2.0)
        real_set(axis, value)

    backend.set_setpoint = slow_set
    assert entered.wait(2.0)                  # the ramp thread is mid-write

    def direct_move():
        brain.cfg.motion.ramp_mode = "off"    # a direct (non-ramped) move
        brain.move_axis(0, 5.0)

    t = threading.Thread(target=direct_move)
    t.start()
    time.sleep(0.2)
    release.set()                             # let the stale write go
    t.join(3.0)
    time.sleep(0.1)
    assert abs(backend.read_setpoint(0) - 5.0) < 1e-9, backend.read_setpoint(0)
    assert _wait(lambda: not brain.status().moving[0], 1.0)
    brain.shutdown()


# --------------------------------------------------------------------------- #
# 6. GUI: jog in open loop stepped from the read-out
# --------------------------------------------------------------------------- #
def test_gui_jog_steps_from_the_target_not_the_readout():
    """The +/- jog added the step to the MEASURED position.  In OL on Y
    (read-out 98.65 at setpoint 100) a "+1 um" jog commanded 99.65: the stage
    moved 0.35 um BACKWARDS.  A jog is a step of the command."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from piezo.apps.gui import MainWindow

    QApplication.instance() or QApplication([])
    cfg = Config()
    cfg.motion.ramp_mode = "off"
    brain, backend = _open_loop_at(cfg)
    win = MainWindow(brain, cfg, remote=False)
    win._jog_step.setValue(1.0)
    win._jog(1, +1)
    assert abs(brain.status().target[1] - 101.0) < 1e-9, brain.status().target
    win.close()
    brain.shutdown()


# --------------------------------------------------------------------------- #
# 7. GUI (remote): Settings OK pushed a STALE loop mode back to the service
# --------------------------------------------------------------------------- #
def test_remote_settings_ok_does_not_revert_live_state(monkeypatch):
    """The remote GUI edits a LOCAL copy of the config, fetched once at launch.
    Toggling the loop mode (from this GUI or any other client) changes only the
    service's config, so pressing OK in Settings -- e.g. just to change the
    theme -- pushed the stale closed_loop_x=True back and set_config switched X
    back to closed loop (and re-clamped / moved it).  Same for a zero set by
    another client (gotcha #5)."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from piezo.apps import gui as gui_mod
    from piezo.net.client import PiezoClient
    from piezo.net.protocol import apply_config_dict
    from piezo.net.service import PiezoService

    QApplication.instance() or QApplication([])
    cfg = Config()
    cfg.motion.ramp_mode = "off"
    brain, _ = build_sim_system(cfg)
    svc = PiezoService(brain, host="127.0.0.1", cmd_port=15792, pub_port=15793, status_hz=20)
    svc.start()
    cli = PiezoClient(host="127.0.0.1", cmd_port=15792, pub_port=15793, timeout_ms=3000)
    cli.start()
    try:
        local = Config()
        apply_config_dict(local, cli.get_config())      # what run_gui.py does
        win = gui_mod.MainWindow(cli, local, remote=True)
        cli.set_closed_loop("X", False)                  # e.g. another client
        cli.move_axis("Y", 42.0)
        cli.set_zero("Y")                                # another client's zero
        # Settings dialog: the user just presses OK.
        monkeypatch.setattr(gui_mod.SettingsDialog, "exec", lambda self: True)
        win._open_settings()
        assert brain.status().closed_loop[0] is False
        assert abs(brain.cfg.relative.rel_y - 42.0) < 1e-6
        win.close()
    finally:
        cli.close()
        svc.stop()


# --------------------------------------------------------------------------- #
# 8. a bad ramp_hz killed the ramp thread for good
# --------------------------------------------------------------------------- #
def test_bad_ramp_hz_does_not_kill_the_ramp_thread():
    """set_config writes values INTO the config as they arrive (no type cast),
    and the ramp thread did float(cfg.motion.ramp_hz) unguarded on every loop.
    One bad value ("fast") raised inside the thread, the thread died silently,
    and from then on every software move stayed `moving` forever -- until the
    service was restarted, even after the value was corrected."""
    cfg = Config()
    cfg.motion.ramp_mode = "software"
    cfg.motion.vel_x = 500.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    cfg.motion.ramp_hz = "fast"           # what set_config would store
    time.sleep(0.1)
    cfg.motion.ramp_hz = 50.0             # corrected again
    brain.move_axis(0, 20.0)
    assert _wait(lambda: not brain.status().moving[0], 2.0), brain.status()
    assert abs(brain.status().target[0] - 20.0) < 1e-9
    brain.shutdown()
