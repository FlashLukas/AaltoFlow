"""GUI smoke test (section 9): build offscreen, refresh, animate, paint.

Skipped automatically if PySide6 isn't installed. Catches import / layout /
signal-wiring problems without needing a display.
"""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ddr25.config import Config  # noqa: E402
from ddr25.sim_system import build_sim_system  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_window_builds_refreshes_and_drives(qapp):
    from ddr25.apps import theme
    from ddr25.apps.gui import MainWindow

    cfg = Config()
    cfg.hardware.sim_start_velocity = 720.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    qapp.setStyleSheet(theme.build_stylesheet())
    try:
        win = MainWindow(brain, cfg, remote=False)
        win._refresh()
        assert "NOT HOMED" in win._state_lbl.text()

        brain.home()
        t0 = time.monotonic()
        while brain.status().moving and time.monotonic() - t0 < 5:
            time.sleep(0.02)
        brain.set_velocity(20.0)
        win._target_in.setValue(90.0)
        win._target_in.editingFinished.emit()
        brain.move_to(90.0)
        win._refresh()
        win._dial._tick()
        win._dial._tick()

        brain.store_angle(0, "gui")
        win._reload_angles()
        assert win._table.item(0, 1).text() == "gui"

        brain._emit("warn", "hello")        # crosses the Bridge signal
        qapp.processEvents()
        assert "hello" in win._log.toPlainText()
        win.close()
    finally:
        brain.shutdown()


def test_dial_paints_in_every_state_and_both_themes(qapp):
    from PySide6.QtGui import QPixmap

    from ddr25.apps import theme
    from ddr25.apps.gui import RotaryDial

    for name in ("dark", "light"):
        theme.set_theme(name)
        dial = RotaryDial()
        dial.resize(320, 320)
        for args in ((123.4, 300.0, 176.6, True, True, False, "literal"),
                     (725.0, 10.0, -15.0, True, True, False, "shortest"),
                     (float("nan"), None, 0.0, False, False, False, "literal"),
                     (0.0, None, 0.0, True, False, True, "literal")):
            dial.set_state(*args)
            dial._tick()
            pm = QPixmap(dial.size())
            dial.render(pm)
            assert not pm.isNull()
    theme.set_theme("dark")


def test_settings_dialog_builds_and_writes_back(qapp):
    from PySide6.QtWidgets import QComboBox

    from ddr25.apps.settings_dialog import SettingsDialog

    cfg = Config()
    dlg = SettingsDialog(cfg)
    combo = dlg._editors[("motion", "wrap")]
    assert isinstance(combo, QComboBox)
    combo.setCurrentText("negative")
    dlg._accept()
    assert cfg.motion.wrap == "negative"
