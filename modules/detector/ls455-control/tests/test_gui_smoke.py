"""GUI smoke test, offscreen; skipped when the gui extra is not installed."""

import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ls455.config import Config
from ls455.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_split_mT_picks_a_readable_unit():
    from ls455.apps.gui import split_mT
    assert split_mT(42.0123) == ("42.012", "mT")
    assert split_mT(0.00321) == ("3.2100", "µT")
    assert split_mT(1234.5)[1] == "T"
    assert split_mT(float("nan")) == ("--", "mT")


def test_window_builds_and_refreshes(app):
    from ls455.apps.gui import MainWindow
    cfg = Config()
    cfg.acquisition.settle_time_constants = 0.0
    meter, _ = build_sim_system(cfg, realtime=False)
    win = MainWindow(meter, cfg)
    for _ in range(3):
        meter.poll_once()
        win._refresh()
    assert win.field_value.text() not in ("", "—")
    assert win.range_combo.count() == 5              # the HSE probe's ranges

    win.auto_chk.click()                             # user click -> auto off
    assert meter.status().auto_range is False
    win.range_combo.setCurrentIndex(0); win._set_range()
    assert meter.status().range_mT == 0.35
    win.mode_combo.activated.emit(1)                 # user picks RMS
    assert meter.status().mode == "rms"
    win._refresh()
    assert win.band_combo.isVisibleTo(win) and not win.digits_combo.isVisibleTo(win)

    win._acquire()
    for _ in range(cfg.acquisition.readings + 1):
        meter.poll_once()
    win._refresh()
    assert "#1" in win.sample_label.text()
    win.indicator._tick()
    win.indicator.grab()                             # runs paintEvent
    win.indicator.set_state(-12.0, 35.0, "overload")
    win.indicator.grab()                             # negative + overload path
    win.close()


def test_settings_dialog_parses_and_applies(app):
    from ls455.apps.settings_dialog import SettingsDialog
    cfg = Config()
    meter, _ = build_sim_system(cfg, realtime=False)
    applied = []
    dlg = SettingsDialog(meter, cfg, lambda: applied.append(True))
    dlg.w[("acquisition", "readings")][0].setText("7")
    dlg.w[("limits", "range_min_mT")][0].setText("3.5e-3")
    dlg._apply_and_close()
    assert cfg.acquisition.readings == 7 and cfg.limits.range_min_mT == 3.5e-3
    assert applied == [True]

    dlg = SettingsDialog(meter, cfg, lambda: None)
    dlg.w[("acquisition", "readings")][0].setText("seven")
    dlg._apply_and_close()
    assert cfg.acquisition.readings == 7          # nothing half-applied


def test_settings_fixed_choices_are_drop_downs(app):
    """A string with a fixed set of values (the theme, ...) is a drop-down, not
    free text; the selection lands in cfg, and a value the list does not know
    (a hand-edited .ini) is kept and marked, never silently replaced."""
    from PySide6 import QtWidgets
    from ls455.apps.settings_dialog import SettingsDialog
    cfg = Config()
    ctrl, _ = build_sim_system(cfg, realtime=False)
    dlg = SettingsDialog(ctrl, cfg, lambda: None)
    theme, _ = dlg.w[("ui", "theme")]
    assert isinstance(theme, QtWidgets.QComboBox)
    assert [theme.itemText(i) for i in range(theme.count())] == ["dark", "light"]
    theme.setCurrentIndex(theme.findData("light"))
    box, _ = dlg.w[("meter", "rms_band")]
    assert isinstance(box, QtWidgets.QComboBox)
    assert [box.itemText(i) for i in range(box.count())] == ['wide', 'narrow']
    box.setCurrentIndex(box.findData("narrow"))
    dlg._apply_and_close()
    assert cfg.ui.theme == "light"
    assert cfg.meter.rms_band == "narrow"
    cfg.ui.theme = "sepia"
    dlg._refresh_widgets_from_cfg()
    assert theme.currentData() == "sepia" and "not a known value" in theme.currentText()
    assert dlg._pull_into_cfg() and cfg.ui.theme == "sepia"
