"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise it is skipped, so the headless core suite is unaffected).

It builds the window against the simulator offscreen, drives the controls and
paints the V-I plane in both source functions -- enough to catch import errors,
layout crashes and signal-wiring mistakes without a display."""

import os
import time

import pytest

pytest.importorskip("PySide6")           # skip cleanly if the gui extra isn't installed
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from k2450.config import Config
from k2450.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def test_window_builds_drives_and_paints(app):
    from k2450.apps.gui import MainWindow
    cfg = Config()
    cfg.measure.nplc = 0.1
    smu, sim = build_sim_system(cfg, seed=0)
    win = MainWindow(smu, cfg)
    try:
        win.resize(1280, 860)
        win.show()
        win._refresh()
        assert win.v_value.text() == "--"          # output off: no reading

        win.limit_spin.setValue(10.0)              # 10 mA
        win._set_limit()
        win.level_spin.setValue(2.0)
        win._set_level()
        win._toggle_output()
        assert sim.get_output() is True
        _pump(app, 0.4)
        win._refresh()
        assert win.i_value.text() != "--"
        assert win.i_unit.text() == "mA"            # ~2 mA through 1 kohm
        win._acquire()
        _pump(app, 0.6)
        win._refresh()
        assert win.sample_label.text().startswith("#")
        win.iv.grab()                               # paintEvent must not throw

        # range combos + NPLC + 4-wire
        win.mrange_combo.setCurrentIndex(3)
        win._set_measure_range()
        win.wire_check.setChecked(True)
        win._set_four_wire(True)
        win.nplc_spin.setValue(0.5)
        win._set_nplc()
        assert smu.status().four_wire is True

        # switch to sourcing current: output goes off, widgets re-label
        win.fn_combo.setCurrentText("current")
        win._set_function()
        win._refresh()
        assert sim.get_output() is False
        assert win.level_label.text() == "Current"
        assert "mA" in win.level_spin.suffix()
        win.iv.grab()
    finally:
        win.close()
    assert sim.get_output() is False                # closing the local GUI = output off


def test_refused_command_is_logged_not_raised(app):
    from k2450.apps.gui import MainWindow
    cfg = Config()
    smu, _ = build_sim_system(cfg, seed=0)
    win = MainWindow(smu, cfg)
    try:
        win._acquire()                              # output off -> refused
        assert "OFF" in win.log.toPlainText()
    finally:
        win.close()


def test_demo_pose_runs(app):
    from k2450.apps.gui import MainWindow
    cfg = Config()
    smu, sim = build_sim_system(cfg, seed=0)
    win = MainWindow(smu, cfg)
    try:
        win.start_demo()
        _pump(app, 0.5)
        assert sim.get_output() is True
        assert cfg.sim.load == "diode"
    finally:
        win.close()


def test_settings_dialog_builds_and_applies(app):
    from k2450.apps.settings_dialog import SettingsDialog
    cfg = Config()
    smu, _ = build_sim_system(cfg, realtime=False)
    smu.start(poll=False)
    try:
        smu.set_voltage(20.0)
        applied = []
        dlg = SettingsDialog(smu, cfg, lambda: applied.append(True))
        dlg.w[("limits", "voltage_max_V")].setValue(5.0)
        dlg._apply_and_close()
        assert cfg.limits.voltage_max_V == 5.0
        assert smu.status().source_voltage_set_V == 5.0     # re-clamped
        assert applied == [True]
    finally:
        smu.shutdown()
