"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise it is skipped, so the headless core suite is unaffected).

It builds the window against the simulator offscreen, refreshes it, drives
every control and paints the spectrum indicator -- enough to catch import
errors, layout crashes and signal-wiring mistakes without a display."""

import os
import time

import pytest

pytest.importorskip("PySide6")           # skip cleanly if the gui extra isn't installed
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dssg.config import Config
from dssg.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_window_builds_and_refreshes(app):
    from dssg.apps.gui import MainWindow
    cfg = Config()
    synth, _ = build_sim_system(cfg)
    win = MainWindow(synth, cfg)             # starts the brain
    try:
        win.resize(1080, 700)
        win._refresh()
        assert win.freq_value.text() not in ("", "—")
        # entry fields start at what the unit holds (a spin box's default
        # range once clamped 1000 MHz to 99.99 here)
        # ...and that is the ADOPTED state of the (simulated) box, not the preset
        assert win.freq_spin.value() == pytest.approx(cfg.sim.state_frequency_Hz / 1e6)
        assert win.power_spin.value() == pytest.approx(cfg.sim.state_power_dBm)
        assert win.ref_combo.currentText() == cfg.sim.state_reference
        # the spin range follows the UNIT's range (12 GHz), not cfg's 13 GHz
        win._change_freq_unit("GHz")
        assert win.freq_spin.maximum() == pytest.approx(12.0)
        win._change_freq_unit("MHz")
        win.freq_spin.setValue(1500.0); win._set_frequency()
        win.power_spin.setValue(-8.0); win._set_power()
        win.phase_spin.setValue(45.0); win._set_phase()
        win.ref_combo.setCurrentText("internal"); win._set_reference()
        win._toggle_rf()
        time.sleep(0.4)                      # let the poll thread read it back
        win._refresh()
        assert synth.status().rf_on is True
        assert synth.status().frequency_Hz == 1.5e9
        win.spectrum._tick()
        win.spectrum.grab()                  # runs paintEvent with RF on
    finally:
        win.close()
    assert synth.status().connected is False  # closing the window shuts down


def test_refused_command_is_logged_not_raised(app):
    from dssg.apps.gui import MainWindow
    cfg = Config()
    cfg.sim.has_phase = False
    synth, _ = build_sim_system(cfg)
    win = MainWindow(synth, cfg)
    try:
        win._set_phase()                     # the unit has no phase: must not raise
        assert "phase" in win.log.toPlainText()
        win._refresh()
        assert win.phase_value.text() == "n/a"
        win.spectrum.grab()
    finally:
        win.close()


def test_settings_dialog_builds(app):
    from dssg.apps.settings_dialog import SettingsDialog
    cfg = Config()
    synth, _ = build_sim_system(cfg)
    applied = []
    dlg = SettingsDialog(synth, cfg, lambda: applied.append(True))
    dlg.w[("limits", "power_max_dBm")].setValue(1.0)
    dlg.w[("hardware", "mute_buzzer")].setChecked(False)
    dlg._apply_and_close()
    assert cfg.limits.power_max_dBm == 1.0
    assert cfg.hardware.mute_buzzer is False
    assert applied == [True]
