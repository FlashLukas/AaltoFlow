"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise it is skipped, so the headless core suite is unaffected).

It builds the window against the simulator offscreen, refreshes it, and toggles
RF — enough to catch import errors, layout crashes, and signal-wiring mistakes
without a display."""

import os

import pytest

pytest.importorskip("PySide6")           # skip cleanly if the gui extra isn't installed
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from smb.config import Config
from smb.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


def test_window_builds_and_refreshes(app):
    from smb.apps.gui import MainWindow
    cfg = Config()
    gen, _ = build_sim_system(cfg)
    win = MainWindow(gen, cfg)
    win.resize(1080, 660)

    win._refresh()
    assert win.freq_value.text() != ""

    # exercise the interactions that could throw
    win._change_freq_unit("GHz")
    win._change_freq_unit("MHz")
    win.freq_spin.setValue(1500.0); win._set_frequency()
    win.power_spin.setValue(-8.0); win._set_power()
    win.phase_spin.setValue(45.0); win._set_phase()
    win._toggle_rf()
    win._refresh()
    assert gen.status().rf_on is True

    # antenna animation frame advance must not throw
    win.antenna._tick()
    win.close()


def test_settings_dialog_builds(app):
    from smb.apps.settings_dialog import SettingsDialog
    cfg = Config()
    gen, _ = build_sim_system(cfg)
    applied = []
    dlg = SettingsDialog(gen, cfg, lambda: applied.append(True))
    # change the power ceiling in the widget, then apply -> generator re-clamps
    dlg.w[("limits", "power_max_dBm")].setValue(5.0)
    dlg._apply_and_close()
    assert cfg.limits.power_max_dBm == 5.0
    assert applied == [True]
