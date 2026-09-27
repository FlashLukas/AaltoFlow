"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise it is skipped, so the headless core suite is unaffected).

It builds the window against the simulator offscreen, refreshes it, and presses
the buttons -- enough to catch import errors, layout crashes and signal-wiring
mistakes without a display."""

import os

import pytest

pytest.importorskip("PySide6")           # skip cleanly if the gui extra isn't installed
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tc200.config import Config
from tc200.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


def test_window_builds_and_refreshes(app):
    from tc200.apps.gui import MainWindow
    cfg = Config()
    cfg.hardware.poll_s = 0.05
    heater, sim = build_sim_system(cfg, temperature_C=35.0, setpoint_C=42.0, p_gain=90)
    heater.start()
    try:
        win = MainWindow(heater, cfg)
        win.resize(1180, 760)
        win._refresh()
        # the boxes start at what the TC200 was ALREADY set to
        assert win.temp_spin.value() == 42.0
        assert win.p_spin.value() == 90
        assert win.temp_value.text() != "—"

        win.temp_spin.setValue(50.0)
        win._set_temperature()
        assert heater.status().setpoint_C == 50.0
        win.on_btn.click()
        assert sim.enabled is True
        win.off_btn.click()
        assert sim.enabled is False

        win.p_spin.setValue(60); win.pmax_spin.setValue(5.0)
        win._apply_controller()
        assert sim.p == 60 and sim.pmax == 5.0

        sim.sensor = "ptc1000"                   # wrong sensor: refused, logged
        heater.poll_once()
        win.on_btn.click()
        assert sim.enabled is False
        win._refresh()
        assert "WRONG SENSOR" in win.alarm_label.text()

        win.indicator.set_state(35.0, 42.0, 20.0, 120.0, True, False, False)
        win.indicator._tick()                    # animation frame must not throw
        win.indicator.repaint()
        win.indicator.set_state(None, None, None, None, False, False, True)
        win.indicator.repaint()
        win.close()
    finally:
        heater.shutdown()


def test_settings_dialog_builds_and_applies(app):
    from tc200.apps.settings_dialog import SettingsDialog
    cfg = Config()
    heater, sim = build_sim_system(cfg)
    heater.start(poll=False)
    try:
        applied = []
        dlg = SettingsDialog(heater, cfg, lambda: applied.append(True))
        dlg.w[("limits", "temperature_max_C")].setValue(80.0)
        dlg.w[("device", "d_gain")].setValue(3)
        dlg.w[("hardware", "stat_base")].setCurrentText("10")
        dlg.w[("hardware", "disable_on_shutdown")].setChecked(False)
        dlg._apply_and_close()
        assert cfg.limits.temperature_max_C == 80.0
        assert cfg.device.d_gain == 3 and sim.d == 3          # pushed to the box
        assert cfg.hardware.stat_base == 10
        assert cfg.hardware.disable_on_shutdown is False
        assert applied == [True]
    finally:
        heater.shutdown()
