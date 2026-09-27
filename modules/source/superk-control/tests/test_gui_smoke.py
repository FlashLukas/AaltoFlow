"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise it is skipped, so the headless core suite is unaffected).

It builds the window against the simulator offscreen, refreshes it and drives
the controls -- enough to catch import errors, layout crashes and signal-wiring
mistakes without a display."""

import os
import time

import pytest

pytest.importorskip("PySide6")           # skip cleanly if the gui extra isn't installed
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from superk.config import Config
from superk.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def test_window_builds_and_drives_the_laser(app):
    from superk.apps.gui import MainWindow
    cfg = Config()
    cfg.hardware.sim_warmup_s = 0.0
    cfg.hardware.poll_hz = 20.0
    laser, backend = build_sim_system(cfg)
    win = MainWindow(laser, cfg)
    try:
        win._refresh()
        assert win.power_value.text() not in ("", "-")
        assert win.filter_combo.count() == 3

        win.power_spin.setValue(30.0); win._set_power()
        win.wl1_spin.setValue(720.0); win.amp1_spin.setValue(60.0); win._set_line(1)
        win.line_wl[2].setValue(610.0); win.line_amp[2].setValue(50.0); win._set_line(2)
        win._toggle_rf()
        win._emission_on(confirm=False)     # the dialog is skipped in tests only
        _pump(app, 0.4)
        win._refresh()
        s = laser.status()
        assert s.emission_on and s.rf_on
        assert s.wavelength_nm[0] == 720.0 and s.amplitude_pct[1] == 50.0
        assert "ON" in win.state_badge.text()

        # a refused command lands in the log, it does not raise
        backend.open_interlock()
        _pump(app, 0.2)
        win._emission_on(confirm=False)
        assert "refused" in win.log.toPlainText()

        # switching crystal from the combo moves the spin box ranges
        win._set_filter(2)                   # IR
        _pump(app, 0.2)
        win._refresh()
        assert win.wl1_spin.minimum() == 1100.0
        win.spectrum._tick()
        win.spectrum.repaint()
    finally:
        win.close()
    assert backend.read_emission() is False  # closing a LOCAL gui switches off


def test_settings_dialog_builds_and_applies(app):
    from superk.apps.settings_dialog import SettingsDialog
    cfg = Config()
    laser, _ = build_sim_system(cfg)
    laser.start()
    try:
        applied = []
        dlg = SettingsDialog(laser, cfg, lambda: applied.append(True))
        dlg.w[("limits", "power_max_pct")].setValue(20.0)
        dlg.w[("hardware", "autodetect")].setChecked(False)
        dlg._apply_and_close()
        assert cfg.limits.power_max_pct == 20.0
        assert cfg.hardware.autodetect is False
        assert applied == [True]
    finally:
        laser.shutdown()
