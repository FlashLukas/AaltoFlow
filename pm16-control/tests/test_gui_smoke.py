"""GUI smoke test, offscreen; skipped when the gui extra is not installed."""

import math
import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pm16.config import Config
from pm16.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_split_W_picks_a_readable_unit():
    from pm16.apps.gui import split_W
    assert split_W(3.3136e-6) == ("3.3136", "µW")
    assert split_W(0.0174)[1] == "mW"
    assert split_W(float("nan")) == ("--", "W")


def test_window_builds_and_refreshes(app):
    from pm16.apps.gui import MainWindow
    cfg = Config()
    meter, _ = build_sim_system(cfg, realtime=False)
    win = MainWindow(meter, cfg)
    for _ in range(3):
        meter.poll_once()
        win._refresh()
    assert win.power_value.text() not in ("", "—")

    win.wl_spin.setValue(800.0); meter.set_wavelength(win.wl_spin.value())
    win.auto_chk.click()                         # user click -> auto off
    assert meter.status().auto_range is False
    win._acquire()
    for _ in range(cfg.acquisition.readings):
        meter.poll_once()
    win._refresh()
    assert "#1" in win.sample_label.text()
    win.indicator._tick()
    win.indicator.grab()                         # runs paintEvent
    win.close()


def test_settings_dialog_parses_and_applies(app):
    from pm16.apps.settings_dialog import SettingsDialog
    cfg = Config()
    meter, _ = build_sim_system(cfg, realtime=False)
    applied = []
    dlg = SettingsDialog(meter, cfg, lambda: applied.append(True))
    dlg.w[("acquisition", "readings")][0].setText("7")
    dlg.w[("limits", "range_min_W")][0].setText("5e-8")
    dlg._apply_and_close()
    assert cfg.acquisition.readings == 7 and cfg.limits.range_min_W == 5e-8
    assert applied == [True]

    dlg = SettingsDialog(meter, cfg, lambda: None)
    dlg.w[("acquisition", "readings")][0].setText("seven")
    dlg._apply_and_close()
    assert cfg.acquisition.readings == 7          # nothing half-applied
