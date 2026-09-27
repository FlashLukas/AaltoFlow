"""GUI smoke test: build offscreen, refresh, press buttons, paint the indicator.

Skipped automatically if PySide6 isn't installed.  Catches import/layout/signal
wiring problems without needing a display.
"""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from elliptec.config import Config  # noqa: E402
from elliptec.sim_system import build_sim_system  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.parametrize("theme_name", ["dark", "light"])
def test_window_builds_and_refreshes(qapp, theme_name):
    from elliptec.apps import theme
    from elliptec.apps.gui import MainWindow

    theme.set_theme(theme_name)
    try:
        cfg = Config()
        cfg.axes.addresses = "0,1"
        cfg.sim.max_speed_deg_s = 900.0
        brain, _ = build_sim_system(cfg)
        brain.start()
        qapp.setStyleSheet(theme.build_stylesheet())

        win = MainWindow(brain, cfg, remote=False)
        assert len(win._cards) == 2
        win._refresh()

        card = win._cards[1]
        card.target.setValue(77.0)
        brain.move_abs(1, 77.0)
        win._refresh()
        card.indicator._tick()
        t0 = time.monotonic()
        while brain.status().moving[1] and time.monotonic() - t0 < 3:
            time.sleep(0.02)
        win._refresh()
        assert card.big.text() == "77.000"

        brain._emit("warn", "hello")         # crosses the Bridge signal
        win._do(lambda: brain.move_abs(9, 1))  # an error lands in the log, no raise
        win.close()
        brain.shutdown()
    finally:
        theme.set_theme("dark")


def test_indicator_paints_every_state(qapp):
    from PySide6.QtGui import QPixmap

    from elliptec.apps.gui import MountIndicator

    ind = MountIndicator()
    ind.resize(240, 240)
    for state in ((None, None, 0.0, False, False, ""),
                  (30.0, 120.0, 10.0, True, False, ""),
                  (300.0, 300.0, 0.0, False, True, "mechanical timeout")):
        ind.set_state(*state)
        ind._tick()
        pm = QPixmap(ind.size())
        ind.render(pm)
        assert not pm.isNull()


def test_settings_dialog_builds(qapp):
    from elliptec.apps.settings_dialog import SettingsDialog

    dlg = SettingsDialog(Config())
    dlg._accept()                             # writes the editors back unchanged
