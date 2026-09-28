"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise it is skipped, so the headless core suite is unaffected).

It builds the window against the simulator offscreen, refreshes it, and toggles
RF — enough to catch import errors, layout crashes, and signal-wiring mistakes
without a display."""

import os

import pytest

pytest.importorskip("PySide6")           # skip cleanly if the gui extra isn't installed
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from shsg.config import Config
from shsg.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


def test_window_builds_and_refreshes(app):
    from shsg.apps.gui import MainWindow
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
    win.power_spin.setValue(-18.0); win._set_power()
    win._toggle_rf()
    win._refresh()
    assert gen.status().rf_on is True
    assert gen.status().power_dBm == -18.0

    # antenna animation frame advance must not throw
    win.antenna._tick()
    win.close()


def test_settings_dialog_builds(app):
    from shsg.apps.settings_dialog import SettingsDialog
    cfg = Config()
    gen, _ = build_sim_system(cfg)
    applied = []
    dlg = SettingsDialog(gen, cfg, lambda: applied.append(True))
    # change the power ceiling in the widget, then apply -> generator re-clamps
    dlg.w[("limits", "power_max_dBm")].setValue(-15.0)
    dlg.w[("hardware", "off_on_shutdown")].setChecked(False)
    dlg._apply_and_close()
    assert cfg.limits.power_max_dBm == -15.0
    assert cfg.hardware.off_on_shutdown is False
    assert applied == [True]


def test_busy_unknown_and_hw_error_are_shown_and_a_refusal_does_not_throw(app):
    from shsg.apps.gui import MainWindow
    cfg = Config()
    gen, sim = build_sim_system(cfg)
    win = MainWindow(gen, cfg)
    try:
        sim.simulate_sweep(True)
        win._refresh()
        assert not win.busy_label.isHidden()
        win._set_frequency()               # refused -> logged, must not raise
        sim.simulate_sweep(False)
        sim.simulate_unknown()
        win._refresh()
        assert not win.unknown_label.isHidden()
        assert win.state_badge.text() == "CW ?"
        sim.set_attached(False)
        win._refresh()
        assert not win.err_label.isHidden()
        assert "no tracking generator" in win.err_label.text()
    finally:
        win.close()


def test_run_app_pins_a_decimal_point(app, monkeypatch):
    """Number boxes must not follow a comma-decimal Windows locale (gotcha #18)."""
    from PySide6 import QtCore, QtWidgets
    from shsg.apps import gui
    monkeypatch.setattr(QtWidgets.QApplication, "exec", lambda self: 0)
    cfg = Config()
    gen, _ = build_sim_system(cfg)
    assert gui.run_app(gen, cfg) == 0
    assert QtCore.QLocale().toString(1000.5, "f", 1) == "1000.5"
    for w in QtWidgets.QApplication.topLevelWidgets():
        w.close()


def test_parked_is_said_plainly(app):
    from shsg.apps.gui import MainWindow, _park_text
    assert _park_text(10_000.0, -30.0) == \
        "RF off  -  TG parked at 10 kHz, -30 dBm (the TG44A cannot be silenced)"
    cfg = Config()
    gen, _ = build_sim_system(cfg)
    win = MainWindow(gen, cfg)
    try:
        win._refresh()                     # the sim starts parked (rf_on False)
        assert not win.park_label.isHidden()
        assert "10 kHz" in win.park_label.text()
        assert win.state_badge.text() == "PARKED"
        win._toggle_rf()
        win._refresh()
        assert win.park_label.isHidden()
    finally:
        win.close()
