"""GUI smoke test, offscreen; skipped when the gui extra is not installed."""

import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mag2d.backends.sim import FakeClock
from mag2d.config import Config
from mag2d.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _rig():
    cfg = Config()
    clock = FakeClock()
    ctrl, sim = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=2)
    ctrl.start(run_thread=False)
    return cfg, clock, ctrl, sim


def _run(ctrl, clock, win, seconds):
    for _ in range(int(seconds * 50)):
        clock.advance(0.02)
        ctrl.tick()
    win._refresh()


def test_window_builds_drives_and_paints(app):
    from mag2d.apps.gui import MainWindow
    cfg, clock, ctrl, sim = _rig()
    win = MainWindow(ctrl, cfg)
    win.resize(1320, 900)
    _run(ctrl, clock, win, 0.5)
    assert win.state_badge.text() == "STABLE"

    win.field_spin.setValue(100.0); win.angle_spin.setValue(60.0)
    win._go_polar()
    _run(ctrl, clock, win, 6.0)
    assert ctrl.status().setpoint_angle_deg == 60.0
    assert win.mag_value.text() not in ("", "--")
    assert "field stable" in win.stable_dot.text()
    win.dial.grab()                                  # runs paintEvent
    win.dial._tick()

    win.bx_spin.setValue(-30.0); win.by_spin.setValue(10.0)
    win._go_vector()
    assert ctrl.status().setpoint_bx_mT == -30.0

    win._toggle_output()                             # energized -> ramp down
    _run(ctrl, clock, win, 6.0)
    assert ctrl.status().state == "OFF" and win.output_btn.text() == "Energize"
    win.grab()
    win.timer.stop()


def test_fault_is_shown_and_refusals_go_to_the_log(app):
    from mag2d.apps.gui import MainWindow
    cfg, clock, ctrl, sim = _rig()
    win = MainWindow(ctrl, cfg)
    sim.p.water_ok = False
    _run(ctrl, clock, win, 0.2)
    assert "water" in win.fault_label.text()
    assert "NO WATER" in win.water_lamp.text()
    win._go_polar()                                  # refused: must not raise
    assert "refused" in win.log.toPlainText()
    win.timer.stop()


def test_bypass_checkbox_asks_first(app, monkeypatch):
    from PySide6 import QtWidgets
    from mag2d.apps.gui import MainWindow
    cfg, clock, ctrl, sim = _rig()
    win = MainWindow(ctrl, cfg)

    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *a, **k: QtWidgets.QMessageBox.No)
    win.bypass_chk.click()
    assert not cfg.interlock.water_bypass and not win.bypass_chk.isChecked()

    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *a, **k: QtWidgets.QMessageBox.Yes)
    win.bypass_chk.click()
    assert cfg.interlock.water_bypass
    win.timer.stop()


def test_settings_dialog_parses_and_applies(app):
    from mag2d.apps.settings_dialog import SettingsDialog
    cfg, clock, ctrl, sim = _rig()
    applied = []
    dlg = SettingsDialog(ctrl, cfg, lambda: applied.append(True))
    dlg.w[("control", "kp_V_per_mT")][0].setText("5e-3")
    dlg.w[("interlock", "temp_monitor")][0].setChecked(True)
    dlg._apply_and_close()
    assert cfg.control.kp_V_per_mT == 5e-3 and cfg.interlock.temp_monitor is True
    assert applied == [True]

    dlg = SettingsDialog(ctrl, cfg, lambda: None)
    dlg.w[("control", "kp_V_per_mT")][0].setText("fast")
    dlg._apply_and_close()
    assert cfg.control.kp_V_per_mT == 5e-3             # nothing half-applied
