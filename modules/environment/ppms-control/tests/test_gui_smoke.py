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

from ppms.config import Config
from ppms.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


def test_window_builds_and_refreshes(app):
    from ppms.apps.gui import MainWindow
    cfg = Config()
    cryo, _ = build_sim_system(cfg, field_mT=250.0, temperature_K=10.0)
    cryo.start()
    try:
        win = MainWindow(cryo, cfg)
        win.resize(1180, 740)
        win._refresh()
        # the setpoint boxes start at what the cryostat was ALREADY set to
        assert win.field_spin.value() == 250.0
        assert win.temp_spin.value() == 10.0
        assert win.field_value.text() != "—"

        win.field_spin.setValue(-40.0); win.frate_spin.setValue(4.0)
        win.fapproach.setCurrentText("no_overshoot")
        win._set_field()
        s = cryo.status()
        assert s.setpoint_field_mT == -40.0 and s.field_approach == "no_overshoot"
        assert cfg.field.rate_mT_per_s == 4.0
        win.temp_spin.setValue(20.0); win._set_temperature()
        assert cryo.status().setpoint_temperature_K == 20.0
        win._zero_field()
        assert cryo.status().setpoint_field_mT == 0.0
        win._refresh()
        win.indicator._tick()                 # animation frame must not throw
        win.indicator.repaint()
        win.close()
    finally:
        cryo.shutdown()


def test_settings_dialog_builds_and_applies(app):
    from ppms.apps.settings_dialog import SettingsDialog
    cfg = Config()
    cryo, _ = build_sim_system(cfg)
    applied = []
    dlg = SettingsDialog(cryo, cfg, lambda: applied.append(True))
    dlg.w[("limits", "field_max_mT")].setValue(5000.0)
    dlg.w[("field", "approach")].setCurrentText("oscillate")
    dlg._apply_and_close()
    assert cfg.limits.field_max_mT == 5000.0
    assert cfg.field.approach == "oscillate"
    assert applied == [True]
