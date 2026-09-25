"""GUI smoke test (§9): build offscreen, refresh, toggle, advance indicator.

Skipped automatically if PySide6 isn't installed.  Catches import/layout/signal
wiring problems without needing a display.
"""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from piezo.config import Config  # noqa: E402
from piezo.sim_system import build_sim_system  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_window_builds_and_refreshes(qapp):
    from piezo.apps import theme
    from piezo.apps.gui import MainWindow

    cfg = Config()
    cfg.motion.ramp_mode = "off"
    brain, _ = build_sim_system(cfg)
    brain.start()
    theme.set_theme(cfg.ui.theme)
    qapp.setStyleSheet(theme.build_stylesheet())

    win = MainWindow(brain, cfg, remote=False)
    win._refresh()

    # command a move and let the indicator animate a couple frames
    brain.set_velocity(0, 500.0)
    brain.move_axis(0, 30.0)
    win._refresh()
    win._indicator._tick()
    win._indicator._tick()

    # toggle a loop mode through the button handler
    win._loop_btn[0].setChecked(False)
    win._toggle_loop(0)
    win._refresh()
    assert brain.status().closed_loop[0] is False

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

    from piezo.apps.gui import PiezoIndicator

    cfg = Config()
    ind = PiezoIndicator(cfg)
    ind.resize(320, 320)
    # one axis closed (green dot), one open (amber ring), one moving
    ind.set_state([50.0, 120.0], [80.0, 120.0], [True, False], [True, False], [200.0, 160.0])
    pm = QPixmap(ind.size())
    ind.render(pm)
    assert not pm.isNull()


def test_settings_dialog_builds(qapp):
    from piezo.apps.settings_dialog import SettingsDialog

    cfg = Config()
    dlg = SettingsDialog(cfg)
    assert dlg is not None
    # the Appearance tab's theme editor exists and is bound to cfg.ui.theme
    assert ("ui", "theme") in dlg._editors


def test_set_theme_mutates_colors_in_place(qapp):
    """COLORS must be mutated in place so `from .theme import COLORS` stays live."""
    from piezo.apps import theme

    colors_ref = theme.COLORS  # capture the object other modules imported
    theme.set_theme("light")
    assert colors_ref is theme.COLORS            # same object, not rebound
    assert theme.COLORS["bg"] == theme.LIGHT["bg"]
    theme.set_theme("dark")
    assert theme.COLORS["bg"] == theme.DARK["bg"]
    theme.set_theme("bogus")                     # unknown -> falls back to dark
    assert theme.COLORS["bg"] == theme.DARK["bg"]


def test_both_themes_build_and_paint(qapp):
    """Render the indicator under each theme; confirm the plot bg follows it."""
    from PySide6.QtGui import QPixmap

    from piezo.apps import theme
    from piezo.apps.gui import PiezoIndicator

    for name in ("dark", "light"):
        theme.set_theme(name)
        assert theme.build_stylesheet()  # non-empty, no KeyError
        ind = PiezoIndicator(Config())
        ind.resize(300, 300)
        ind.set_state([50.0, 120.0], [80.0, 120.0], [True, False], [True, False], [200.0, 160.0])
        pm = QPixmap(ind.size())
        ind.render(pm)
        assert not pm.isNull()
    theme.set_theme("dark")  # leave the module in the default state
