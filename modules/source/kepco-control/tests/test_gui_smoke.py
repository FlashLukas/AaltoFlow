"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise it is skipped).

It builds the window against the simulator offscreen, drives the controls that
could throw, and paints the quadrant indicator."""

import os
import time

import pytest

pytest.importorskip("PySide6")           # skip cleanly if the gui extra isn't installed
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from kepco.config import Config
from kepco.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(app, seconds):
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        app.processEvents()
        time.sleep(0.01)


def test_window_builds_drives_and_paints(app):
    from kepco.apps.gui import MainWindow
    cfg = Config()
    cfg.ramp.rate_A_per_s = 5.0
    supply, sim = build_sim_system(cfg, seed=0)
    win = MainWindow(supply, cfg)
    try:
        win.resize(1280, 860)
        win._refresh()
        assert win.set_title.text().startswith("CURRENT")

        win.set_spin.setValue(1.5); win._apply_setpoint()
        win.lim_spin.setValue(6.0); win._apply_limit()
        win.rate_spin.setValue(2.0); win._apply_rate()
        win._toggle_output()
        _pump(app, 1.5)
        win._refresh()
        s = supply.status()
        assert s.output and s.current_set_A == 1.5 and s.voltage_limit_V == 6.0
        assert win.i_value.text() not in ("", "-")
        win.quad.grab()                            # paintEvent must not throw
        win.quad._tick()

        # a mode change with the output on is refused and logged, not raised
        win.btn_volt.click()
        assert supply.status().mode == "current"

        win._toggle_output()                       # ramp down + off
        _pump(app, 1.5)
        win.btn_volt.click()
        _pump(app, 0.2)
        win._refresh()
        assert win.set_title.text().startswith("VOLTAGE")
        win.quad.grab()
    finally:
        win.close()
    assert sim.output_on is False


def test_settings_dialog_builds_every_group(app):
    from kepco.apps.settings_dialog import SettingsDialog
    cfg = Config()
    supply, _ = build_sim_system(cfg)
    applied = []
    dlg = SettingsDialog(supply, cfg, lambda: applied.append(True))
    for group in Config._GROUPS:
        assert any(g == group for g, _ in dlg.w), group
    dlg.w[("limits", "current_max_A")].setValue(5.0)
    dlg.w[("ramp", "enabled")].setChecked(False)
    dlg._apply_and_close()
    assert cfg.limits.current_max_A == 5.0
    assert cfg.ramp.enabled is False
    assert applied == [True]
