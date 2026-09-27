"""GUI smoke test (section 9): build offscreen, refresh, animate, paint.

Skipped automatically if PySide6 isn't installed. Catches import/layout/signal
wiring problems without needing a display.
"""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from helpers import make_brain  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_window_builds_and_refreshes(qapp):
    from smaract.apps import theme
    from smaract.apps.gui import MainWindow

    brain, _ = make_brain()
    qapp.setStyleSheet(theme.build_stylesheet())
    win = MainWindow(brain, brain.cfg, remote=False)
    win._refresh()
    assert win._big.text() != "--"

    win._jog_step.setValue(0.2)
    win._jog(+1)                                  # a relative step works unreferenced
    win._refresh()
    win._indicator._tick()
    win._indicator._tick()

    win._goto.setValue(10.0)
    win._move()                                   # refused: logged, not raised
    assert "NOT referenced" in win._log.toPlainText()

    win._reload_positions()
    assert win._table.rowCount() == len(brain.get_positions())
    brain._emit("warn", "hello")                  # crosses the Bridge signal
    win.close()
    brain.shutdown()


@pytest.mark.parametrize("theme_name", ["dark", "light"])
@pytest.mark.parametrize("referenced,moving", [(False, False), (True, True), (True, False)])
def test_indicator_paints_to_pixmap(qapp, theme_name, referenced, moving):
    """Actually paint the indicator -> catches paintEvent errors in every state."""
    from PySide6.QtGui import QPixmap

    from smaract.apps import theme
    from smaract.apps.gui import RailIndicator

    theme.set_theme(theme_name)
    try:
        ind = RailIndicator()
        ind.resize(520, 160)
        ind.set_state(12.5, 20.0, -115.0, 115.0, moving, referenced, False, 3.0)
        ind._tick()
        pm = QPixmap(ind.size())
        ind.render(pm)
        assert not pm.isNull()
        ind.set_state(float("nan"), float("nan"), -115.0, 115.0, False, False, True, 0.0)
        ind.render(pm)
    finally:
        theme.set_theme("dark")


def test_settings_dialog_builds_with_every_group(qapp):
    from smaract.apps.settings_dialog import _GROUPS, SettingsDialog
    from smaract.config import Config

    cfg = Config()
    dlg = SettingsDialog(cfg)
    groups = {g for g, _ in dlg._editors}
    assert set(_GROUPS) <= groups and "ui" in groups
    dlg._accept()                                 # writes back without error
    assert cfg.hardware.nm_per_count == Config().hardware.nm_per_count
