"""GUI smoke test (section 9): build offscreen, refresh, toggle, advance indicator.

Skipped automatically if PySide6 isn't installed.  Catches import/layout/signal
wiring problems without needing a display. The panel shows ONLY the focus axis
(Z); X / Y are not fitted on a KIM001 and must not appear.
"""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from kim001.config import Config  # noqa: E402
from kim001.sim_system import build_sim_system  # noqa: E402

Z = 2


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _window(cfg=None):
    from kim001.apps.gui import MainWindow

    cfg = cfg or Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    return MainWindow(brain, cfg, remote=False), brain, cfg


def test_only_the_focus_axis_is_on_the_panel(qapp):
    """Hidden, not greyed: no X / Y rows, readouts, step-size boxes, leash box
    or camera px calibration card."""
    from PySide6.QtWidgets import QLabel

    win, brain, _ = _window()
    try:
        assert list(win._target) == [Z]
        assert list(win._big) == [Z]
        assert list(win._cal_boxes) == [Z]
        assert len(win._jog_buttons) == 1
        assert not hasattr(win, "_leash_xy")
        assert not hasattr(win, "_cal_table")            # kim's X/Y camera card
        texts = " ".join(lbl.text() for lbl in win.findChildren(QLabel))
        assert "CAMERA CALIBRATION" not in texts
        assert "XY" not in texts
        assert win._table.columnCount() == 3             # #, name, Z
    finally:
        win.close()
        brain.shutdown()


def test_step_size_card_types_values_in_and_saves(qapp):
    """Type forward/backward µm per step for Z, Apply: the brain converts with them."""
    win, brain, cfg = _window()
    try:
        fwd, bwd = win._cal_boxes[Z]
        assert fwd.isEnabled() and bwd.isEnabled()
        fwd.setValue(0.0161)
        bwd.setValue(0.0203)
        win._apply_step_sizes()
        assert cfg.calibration.um_per_step_z == pytest.approx(0.0161)
        assert cfg.calibration.um_per_step_z_bwd == pytest.approx(0.0203)
        assert brain.um_per_step(Z, -1) == pytest.approx(0.0203)
        win._refresh()
        assert "16.1 nm up / 20.3 nm down" in win._cal_hint.text()
        # back to one number ("same")
        bwd.setValue(0.0)
        win._apply_step_sizes()
        assert brain.um_per_step(Z, -1) == pytest.approx(0.0161)
    finally:
        win.close()
        brain.shutdown()


def test_window_builds_and_refreshes(qapp):
    from kim001.apps import theme

    qapp.setStyleSheet(theme.build_stylesheet())
    win, brain, cfg = _window()
    win._refresh()

    # command a move (steps) and let the indicator animate a couple frames
    brain.set_step_rate(Z, 2000)
    brain.move_to_step(Z, 3000)
    win._refresh()
    win._indicator._tick()
    win._indicator._tick()

    # exercise both move languages through the GUI action path
    win._unit.setCurrentText("µm")
    win._rel_mode.setChecked(True)
    win._target[Z].setValue(5.0)
    win._move(Z)                     # relative um move
    win._unit.setCurrentText("steps")
    win._rel_mode.setChecked(False)
    win._target[Z].setValue(200.0)
    win._move(Z)                     # absolute step move
    win._jog(Z, +1)                  # jog

    # motion-preset toggles: Fast/Slow and Large/Small steps
    win._speed_btn.setChecked(True)    # -> fast
    assert abs(brain.status().step_rate[Z] - cfg.motion.fast_rate) < 1e-6
    assert brain.status().speed_fast is True
    assert win._speed_btn.text() == "Movement: Fast"
    win._speed_btn.setChecked(False)   # -> slow
    assert abs(brain.status().step_rate[Z] - cfg.motion.slow_rate) < 1e-6
    win._steps_btn.setChecked(True)    # -> large steps = max voltage
    assert abs(brain.status().voltage[Z] - cfg.limits.max_voltage) < 1e-6
    win._steps_btn.setChecked(False)   # -> small steps = min voltage
    assert abs(brain.status().voltage[Z] - cfg.limits.min_voltage) < 1e-6
    assert win._steps_btn.text() == "Steps: Small"

    # arm the leash from the home-screen card and confirm the indicator rescales
    win._unit.setCurrentText("steps")   # leash box now in steps
    win._leash_on.setChecked(True)
    win._leash_z.setValue(500)
    win._apply_leash()
    win._refresh()
    assert brain.status().leash is True
    assert brain.status().limit_hi[Z] == 500
    assert win._indicator._half() == 500

    # switching the unit to µm re-expresses the same range via the calibration
    win._unit.setCurrentText("µm")      # 500 steps * (Z µm/step)
    assert win._leash_z_lbl.text() == "Z ± µm"
    expected_um = 500 * brain.status().um_per_step[Z]
    assert abs(win._leash_z.value() - expected_um) < 1e-6

    # store a position and confirm the table reloads
    brain.store_position(0, "gui")
    win._reload_positions()
    assert win._table.rowCount() == len(brain.get_positions())

    # exercise an event into the log (crosses the Bridge signal)
    brain._emit("warn", "hello")

    win.close()
    brain.shutdown()


def test_indicator_paints_to_pixmap(qapp):
    """Actually paint the indicator to a pixmap -> catches paintEvent errors,
    at rest and moving, with and without the leash."""
    from PySide6.QtGui import QPixmap

    from kim001.apps.gui import FocusIndicator, InertiaIndicator

    assert InertiaIndicator is FocusIndicator
    ind = FocusIndicator(Config())
    ind.resize(500, 240)
    for moving, leash in ((False, False), (True, True)):
        ind.set_state([0, 0, 2000], [False, False, moving], [0, 0, -5000], [0, 0, 5000], leash)
        ind._tick()
        pm = QPixmap(ind.size())
        ind.render(pm)
        assert not pm.isNull()
    assert ind._frac() == pytest.approx(0.7)


def test_jog_box_follows_unit(qapp):
    win, brain, _ = _window()
    try:
        win._unit.setCurrentText("steps")
        assert win._jog_lbl.text() == "jog step (steps)"
        win._jog_step.setValue(100)          # 100 steps
        win._unit.setCurrentText("µm")       # 100 * 0.02 µm/step = 2 µm
        assert win._jog_lbl.text() == "jog step (µm)"
        assert abs(win._jog_step.value() - 2.0) < 1e-6
        # a µm jog converts back to steps for the move
        brain.set_step_rate(Z, 2000)
        win._jog_step.setValue(5.0)          # 5 µm -> 250 steps
        win._jog(Z, +1)
        t0 = time.monotonic()
        while brain.status().moving[Z] and time.monotonic() - t0 < 4.0:
            time.sleep(0.01)
        assert brain.status().position_steps[Z] == 250
    finally:
        win.close()
        brain.shutdown()


def test_settings_dialog_builds(qapp):
    from kim001.apps.settings_dialog import SettingsDialog

    cfg = Config()
    dlg = SettingsDialog(cfg)
    assert dlg is not None


def test_render_entry_point_exists():
    """tools/render_panels.py has no hand-made target for kim001 and falls back
    to <package>.apps.gui.main(theme=...)."""
    import inspect

    from kim001.apps import gui

    assert "theme" in inspect.signature(gui.main).parameters
