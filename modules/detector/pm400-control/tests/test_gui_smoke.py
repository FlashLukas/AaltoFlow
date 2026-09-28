"""GUI smoke test, offscreen; skipped when the gui extra is not installed."""

import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pm400.config import Config
from pm400.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_split_value_picks_a_readable_unit():
    from pm400.apps.gui import split_value
    assert split_value(3.3136e-6, "W") == ("3.3136", "µW")
    assert split_value(0.0174, "W")[1] == "mW"
    assert split_value(2e-4, "J")[1] == "µJ"
    assert split_value(float("nan"), "J") == ("--", "J")


def test_window_builds_refreshes_and_follows_the_head(app):
    from pm400.apps.gui import MainWindow
    cfg = Config()
    meter, _ = build_sim_system(cfg, realtime=False)
    win = MainWindow(meter, cfg)
    for _ in range(3):
        meter.poll_once()
        win._refresh()
    assert win.value_label.text() not in ("", "--")

    win.auto_chk.click()                         # user click -> auto off
    assert meter.status().auto_range is False
    win._acquire()
    for _ in range(cfg.acquisition.readings):
        meter.poll_once()
    win._refresh()
    assert "#1" in win.sample_label.text()

    for head in ("thermal", "pyro", "none"):
        cfg.sim.head = head
        meter.check_head()
        meter.poll_once()
        win._refresh()
        win.indicator._tick()
        win.indicator.grab()                     # runs paintEvent for every head kind
    assert not win.zero_btn.isEnabled()          # no head: nothing to zero
    assert win.value_title.text() == "POWER"
    cfg.sim.head = "pyro"
    meter.check_head()
    meter.poll_once()
    win._refresh()
    assert win.value_title.text() == "ENERGY PER PULSE"
    assert win.unit_label.text().endswith("J")
    win.close()


def test_settings_dialog_parses_and_applies(app):
    from pm400.apps.settings_dialog import SettingsDialog
    cfg = Config()
    meter, _ = build_sim_system(cfg, realtime=False)
    applied = []
    dlg = SettingsDialog(meter, cfg, lambda: applied.append(True))
    assert ("sim", "head") in dlg.w
    dlg.w[("acquisition", "readings")][0].setText("7")
    dlg.w[("limits", "range_min_W")][0].setText("5e-8")
    dlg._apply_and_close()
    assert cfg.acquisition.readings == 7 and cfg.limits.range_min_W == 5e-8
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
    from pm400.apps.settings_dialog import SettingsDialog
    cfg = Config()
    ctrl, _ = build_sim_system(cfg, realtime=False)
    dlg = SettingsDialog(ctrl, cfg, lambda: None)
    theme, _ = dlg.w[("ui", "theme")]
    assert isinstance(theme, QtWidgets.QComboBox)
    assert [theme.itemText(i) for i in range(theme.count())] == ["dark", "light"]
    theme.setCurrentIndex(theme.findData("light"))
    box, _ = dlg.w[("sim", "head")]
    assert isinstance(box, QtWidgets.QComboBox)
    assert [box.itemText(i) for i in range(box.count())] == ['photodiode', 'thermal', 'pyro', 'none']
    box.setCurrentIndex(box.findData("thermal"))
    dlg._apply_and_close()
    assert cfg.ui.theme == "light"
    assert cfg.sim.head == "thermal"
    cfg.ui.theme = "sepia"
    dlg._refresh_widgets_from_cfg()
    assert theme.currentData() == "sepia" and "not a known value" in theme.currentText()
    assert dlg._pull_into_cfg() and cfg.ui.theme == "sepia"
