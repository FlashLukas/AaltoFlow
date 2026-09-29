"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise skipped). Builds the window against the simulator
offscreen, refreshes it, sets an output, clicks a digital output, and applies
the Settings dialog -- enough to catch import errors, layout crashes and
signal-wiring mistakes without a display."""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from usb6001.sim_system import build_sim_system, demo_config


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_window_builds_and_refreshes(app):
    from usb6001.apps.gui import MainWindow
    cfg = demo_config()
    daq, sim = build_sim_system(cfg)
    win = MainWindow(daq, cfg)
    win._refresh()
    assert "unknown" in win.ao_now[0].text()           # adopt: AO unknown at start
    assert sim.writes == []

    win.ao_spin[0].setValue(1.5)
    win._set_ao(0)
    win._refresh()
    assert "1.5000" in win.ao_now[0].text()

    # the p0.4 row is an output checkbox; a user click drives the line
    d, box = win.dio_rows[4]
    assert d == "out"
    box.click()
    assert daq.status().dio[4] is True
    win._refresh()                                      # refresh must not re-send
    n = len(sim.writes)
    win._refresh()
    assert len(sim.writes) == n
    # an input row is a lamp, not a checkbox
    assert win.dio_rows[0][0] == "in"

    win._read_now()
    assert win.sample_label.text().startswith("#")
    win.indicator.repaint()
    win.close()


def test_settings_dialog_applies_in_place(app):
    from usb6001.apps.settings_dialog import SettingsDialog
    cfg = demo_config()
    daq, _ = build_sim_system(cfg)
    daq.start(poll=False)
    applied = []
    dlg = SettingsDialog(daq, cfg, lambda: applied.append(True))
    dlg.w[("dio", 0, "direction")].setCurrentText("out")
    dlg.w[("ai", 2, "unit")].setText("K")
    dlg.w[("ao", 1, "max_V")].setValue(2.0)
    dlg._apply_and_close()
    assert cfg.dio.lines[0].direction == "out"
    assert cfg.ai.channels[2].unit == "K"
    assert cfg.ao.channels[1].max_V == 2.0
    assert applied == [True]
    assert daq.status().restart_pending is True
    daq.shutdown()
