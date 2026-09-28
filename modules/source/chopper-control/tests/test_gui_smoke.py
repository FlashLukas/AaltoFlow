"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise it is skipped, so the headless core suite is unaffected).

It builds the window against the simulator offscreen, refreshes it, and drives
the controls -- enough to catch import errors, layout crashes and signal-wiring
mistakes without a display."""

import os

import pytest

pytest.importorskip("PySide6")           # skip cleanly if the gui extra isn't installed
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from chopper.config import Config
from chopper.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_window_builds_and_drives_the_brain(app):
    from chopper.apps.gui import MainWindow
    cfg = Config()
    ch, _ = build_sim_system(cfg)
    win = MainWindow(ch, cfg)
    win.resize(1180, 760)
    win._refresh()
    assert win.set_value.text() not in ("", "--")
    assert win.blade_combo.count() >= 2

    win.freq_spin.setValue(420.0); win._set_frequency()
    assert ch.status().setpoint_frequency_Hz == 420.0
    win.phase_spin.setValue(45.0); win._set_phase()
    assert ch.status().phase_deg == 45.0

    # blade change while running: refused, logged, window survives
    win.blade_combo.setCurrentText("MC1F60"); win._apply_modes()
    assert ch.status().blade == "MC1F10HP"
    assert "standby" in win.log.toPlainText()

    win._toggle_run()                       # -> standby
    assert ch.status().enabled is False
    win._refresh()
    win.blade_combo.setCurrentText("MC1F60"); win._apply_modes()
    assert ch.status().blade == "MC1F60"
    win._refresh()
    assert win.freq_spin.minimum() == 120.0

    # the wheel animation must not throw, running or not
    win.wheel._tick()
    win._toggle_run()
    win._refresh(); win.wheel._tick()
    win.close()


def test_settings_dialog_applies(app):
    from chopper.apps.settings_dialog import SettingsDialog
    cfg = Config()
    ch, _ = build_sim_system(cfg)
    ch.start(poll=False)
    applied = []
    dlg = SettingsDialog(ch, cfg, lambda: applied.append(True))
    dlg.w[("limits", "freq_max_Hz")].setValue(500.0)
    dlg.w[("hardware", "stop_on_exit")].setChecked(True)
    dlg._apply_and_close()
    assert cfg.limits.freq_max_Hz == 500.0
    assert cfg.hardware.stop_on_exit is True
    assert applied == [True]
    ch.shutdown()
