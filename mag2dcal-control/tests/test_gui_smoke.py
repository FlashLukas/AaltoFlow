"""GUI smoke test, offscreen; skipped when the gui extra is not installed."""

import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mag2dcal.backends.sim import FakeClock
from mag2dcal.calibration import AxisCalibration, Calibration
from mag2dcal.config import Config
from mag2dcal.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _rig(tmp_path=None):
    cfg = Config()
    cfg.calibration.load_newest_on_start = False
    if tmp_path is not None:
        cfg.calibration.directory = str(tmp_path)
    clock = FakeClock()
    ctrl, sim = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=2)
    ctrl.start(run_thread=False)
    return cfg, clock, ctrl, sim


def _run(ctrl, clock, win, seconds):
    for _ in range(int(seconds * 50)):
        clock.advance(0.02)
        ctrl.tick()
    win._refresh()


def _toy():
    up = [(-5.0 + 2.5 * i, 20.0 * (-5.0 + 2.5 * i) + 0.4) for i in range(5)]
    axis = AxisCalibration(up=up, down=[(V, B - 0.8) for V, B in up])
    return Calibration(axes=[axis, axis], note="test curve")


def test_window_builds_drives_and_paints(app):
    from mag2dcal.apps.gui import MainWindow
    cfg, clock, ctrl, sim = _rig()
    win = MainWindow(ctrl, cfg)
    win.resize(1320, 980)
    _run(ctrl, clock, win, 3.0)
    assert win.state_badge.text() == "STABLE"
    assert "no calibration" in win.cal_label.text()

    win.field_spin.setValue(100.0); win.angle_spin.setValue(60.0)
    win._go_polar()
    _run(ctrl, clock, win, 10.0)
    assert ctrl.status().setpoint_angle_deg == 60.0
    assert win.mag_value.text() not in ("", "--")
    assert "field stable" in win.stable_dot.text()
    win.dial.grab()                                  # runs paintEvent
    win.dial._tick()

    win.bx_spin.setValue(-30.0); win.by_spin.setValue(10.0)
    win._go_vector()
    assert ctrl.status().setpoint_bx_mT == -30.0

    win._toggle_output()                             # energized -> ramp down
    _run(ctrl, clock, win, 8.0)
    assert ctrl.status().state == "OFF" and win.output_btn.text() == "Energize"
    win.grab()
    win.timer.stop()


def test_the_calibration_card_follows_the_loaded_curve(app):
    from mag2dcal.apps.gui import MainWindow
    cfg, clock, ctrl, sim = _rig()
    win = MainWindow(ctrl, cfg)
    _run(ctrl, clock, win, 0.2)
    assert "no calibration" in win.cal_label.text()
    assert win.field_spin.maximum() == pytest.approx(cfg.limits.field_max_mT)

    ctrl.set_calibration(_toy())
    _run(ctrl, clock, win, 0.2)
    assert "points" in win.cal_label.text()
    # the spin boxes followed the calibration's narrower envelope
    assert win.field_spin.maximum() == pytest.approx(99.6)
    win.timer.stop()


def test_the_calibration_progress_bar_appears_during_a_sweep(app):
    from mag2dcal.apps.gui import MainWindow
    cfg, clock, ctrl, sim = _rig()
    win = MainWindow(ctrl, cfg)
    _run(ctrl, clock, win, 0.2)
    # isHidden(), not isVisible(): the window itself is never shown offscreen,
    # so every widget in it reports isVisible() False whatever we asked for.
    assert win.cal_bar.isHidden()
    ctrl.calibrate(n_per_leg=5, dwell_s=0.1, v_max=2.0)
    _run(ctrl, clock, win, 1.0)
    assert win.state_badge.text() == "CALIBRATE"
    assert not win.cal_bar.isHidden() and not win.cal_run_btn.isEnabled()
    ctrl.zero()                                      # abort
    _run(ctrl, clock, win, 1.0)
    assert win.cal_bar.isHidden() and win.cal_run_btn.isEnabled()
    win.timer.stop()


def test_the_calibration_viewer_plots_both_legs(app):
    from mag2dcal.apps.calibration_viewer import CalibrationViewer, can_view
    assert not can_view(None) and not can_view(Calibration())
    cal = _toy()
    assert can_view(cal)
    dlg = CalibrationViewer(cal)
    dlg.resize(760, 560)
    dlg.grab()                                       # paints without raising
    dlg.close()


def test_the_stabilizer_checkbox_round_trips(app):
    from mag2dcal.apps.gui import MainWindow
    cfg, clock, ctrl, sim = _rig()
    win = MainWindow(ctrl, cfg)
    _run(ctrl, clock, win, 0.2)
    assert win.stab_chk.isChecked()
    win.stab_chk.click()
    _run(ctrl, clock, win, 0.2)
    assert not ctrl.stabilizer_enabled and not win.stab_chk.isChecked()
    win.timer.stop()


def test_fault_is_shown_and_refusals_go_to_the_log(app):
    from mag2dcal.apps.gui import MainWindow
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
    from mag2dcal.apps.gui import MainWindow
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
    from mag2dcal.apps.settings_dialog import SettingsDialog
    cfg, clock, ctrl, sim = _rig()
    applied = []
    dlg = SettingsDialog(ctrl, cfg, lambda: applied.append(True))
    dlg.w[("control", "kp_V_per_mT")][0].setText("5e-3")
    dlg.w[("interlock", "temp_monitor")][0].setChecked(True)
    dlg.w[("control", "freeze_enabled")][0].setChecked(False)
    dlg.w[("calibration", "n_per_leg")][0].setText("31")
    dlg._apply_and_close()
    assert cfg.control.kp_V_per_mT == 5e-3 and cfg.interlock.temp_monitor is True
    assert cfg.control.freeze_enabled is False
    assert cfg.calibration.n_per_leg == 31 and isinstance(cfg.calibration.n_per_leg, int)
    assert applied == [True]

    dlg = SettingsDialog(ctrl, cfg, lambda: None)
    dlg.w[("control", "kp_V_per_mT")][0].setText("fast")
    dlg._apply_and_close()
    assert cfg.control.kp_V_per_mT == 5e-3             # nothing half-applied
