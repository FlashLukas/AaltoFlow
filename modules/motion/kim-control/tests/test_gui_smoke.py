"""GUI smoke test (§9): build offscreen, refresh, toggle, advance indicator.

Skipped automatically if PySide6 isn't installed.  Catches import/layout/signal
wiring problems without needing a display.
"""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from kim.config import Config  # noqa: E402
from kim.sim_system import build_sim_system  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_step_size_card_types_values_in_and_yields_to_the_camera(qapp):
    """The no-camera path in the GUI: type forward/backward µm per step, Apply.
    When a camera calibration is in charge the boxes are disabled instead."""
    from kim.apps.gui import MainWindow
    from kim import pxcal

    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    win = MainWindow(brain, cfg, remote=False)
    try:
        fwd, bwd = win._cal_boxes[1]            # Y
        assert fwd.isEnabled()
        fwd.setValue(0.0189)
        bwd.setValue(0.0319)
        win._apply_step_sizes()
        assert cfg.calibration.um_per_step_y == pytest.approx(0.0189)
        assert cfg.calibration.um_per_step_y_bwd == pytest.approx(0.0319)
        assert brain.um_per_step(1, -1) == pytest.approx(0.0319)

        # a camera table appears -> X and Y are its business now
        brain._pxcal = pxcal.PxCalibration(
            table={"85": {"X+": [0.5, 0.0], "X-": [0.4, 0.0],
                          "Y+": [0.0, 0.3], "Y-": [0.0, 0.2]}},
            pixel_size_um=0.05)
        win._refresh()
        assert not win._cal_boxes[1][0].isEnabled()
        assert win._cal_boxes[2][0].isEnabled()        # Z is never camera-driven
        assert "CAMERA" in win._cal_manual_hint.text()
    finally:
        win.close()
        brain.shutdown()


def test_window_builds_and_refreshes(qapp):
    from kim.apps import theme
    from kim.apps.gui import MainWindow

    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    qapp.setStyleSheet(theme.build_stylesheet())

    win = MainWindow(brain, cfg, remote=False)
    win._refresh()

    # command a move (steps) and let the indicator animate a couple frames
    brain.set_step_rate(0, 2000)
    brain.move_to_step(0, 3000)
    win._refresh()
    win._indicator._tick()
    win._indicator._tick()

    # exercise both move languages through the GUI action path
    win._unit.setCurrentText("µm")
    win._rel_mode.setChecked(True)
    win._target[0].setValue(5.0)
    win._move(0)                     # relative um move
    win._unit.setCurrentText("steps")
    win._rel_mode.setChecked(False)
    win._target[1].setValue(200.0)
    win._move(1)                     # absolute step move
    win._jog(0, +1)                  # jog

    # motion-preset toggles: Fast/Slow and Large/Small steps
    # (default config is slow+small, so these each cause a real state change)
    win._speed_btn.setChecked(True)    # -> fast
    assert abs(brain.status().step_rate[0] - cfg.motion.fast_rate) < 1e-6
    assert brain.status().speed_fast is True
    assert win._speed_btn.text() == "Movement: Fast"
    win._speed_btn.setChecked(False)   # -> slow
    assert abs(brain.status().step_rate[0] - cfg.motion.slow_rate) < 1e-6
    win._steps_btn.setChecked(True)    # -> large steps = max voltage
    assert abs(brain.status().voltage[0] - cfg.limits.max_voltage) < 1e-6
    win._steps_btn.setChecked(False)   # -> small steps = min voltage
    assert abs(brain.status().voltage[0] - cfg.limits.min_voltage) < 1e-6
    assert win._steps_btn.text() == "Steps: Small"

    # arm the leash from the home-screen card and confirm the indicator rescales
    win._unit.setCurrentText("steps")   # leash boxes now in steps
    win._leash_on.setChecked(True)
    win._leash_xy.setValue(1000)
    win._leash_z.setValue(500)
    win._apply_leash()
    win._refresh()
    assert brain.status().leash is True
    assert win._indicator._half(0) == 1000

    # switching the unit to µm re-expresses the same range via the calibration
    win._unit.setCurrentText("µm")      # 1000 steps * (X µm/step)
    assert win._leash_xy_lbl.text() == "XY ± µm"
    expected_um = 1000 * brain.status().um_per_step[0]
    assert abs(win._leash_xy.value() - expected_um) < 1e-6

    # store a position and confirm the table reloads
    brain.store_position(0, "gui")
    win._reload_positions()
    assert win._table.rowCount() == len(brain.get_positions())

    # exercise an event into the log (crosses the Bridge signal)
    brain._emit("warn", "hello")

    win.close()
    brain.shutdown()


def test_indicator_paints_to_pixmap(qapp):
    """Actually paint the indicator to a pixmap -> catches paintEvent errors."""
    from PySide6.QtGui import QPixmap

    from kim.apps.gui import InertiaIndicator

    cfg = Config()
    ind = InertiaIndicator(cfg)
    ind.resize(300, 240)
    ind.set_state([5000, 10000, 2000], [False, True, False])
    pm = QPixmap(ind.size())
    ind.render(pm)
    assert not pm.isNull()


def test_jog_box_follows_unit(qapp):
    from kim.apps.gui import MainWindow

    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    win = MainWindow(brain, cfg, remote=False)
    win._unit.setCurrentText("steps")
    assert win._jog_lbl.text() == "jog step (steps)"
    win._jog_step.setValue(100)          # 100 steps
    win._unit.setCurrentText("µm")       # 100 * 0.02 µm/step = 2 µm
    assert win._jog_lbl.text() == "jog step (µm)"
    assert abs(win._jog_step.value() - 2.0) < 1e-6
    # a µm jog converts back to steps for the move
    import time
    brain.set_step_rate(0, 2000)
    win._jog_step.setValue(5.0)          # 5 µm -> 250 steps
    win._jog(0, +1)
    t0 = time.monotonic()
    while brain.status().moving[0] and time.monotonic() - t0 < 4.0:
        time.sleep(0.01)
    assert brain.status().position_steps[0] == 250
    win.close()
    brain.shutdown()


def test_settings_dialog_builds(qapp):
    from kim.apps.settings_dialog import SettingsDialog

    cfg = Config()
    dlg = SettingsDialog(cfg)
    assert dlg is not None
