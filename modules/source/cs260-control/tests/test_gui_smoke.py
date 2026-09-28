"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise it is skipped, so the headless core suite is unaffected).

It builds the window against the simulator offscreen, refreshes it, and drives
every control -- enough to catch import errors, layout crashes and signal-wiring
mistakes without a display."""

import os
import time

import pytest

pytest.importorskip("PySide6")           # skip cleanly if the gui extra isn't installed
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cs260.config import Config
from cs260.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _fast_cfg():
    cfg = Config()
    cfg.sim.slew_nm_per_s_at_1200 = 20000.0
    cfg.sim.grating_change_s = 0.1
    cfg.motion.poll_s = 0.02
    return cfg


def _pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def test_window_builds_and_drives(app):
    from cs260.apps.gui import MainWindow
    cfg = _fast_cfg()
    mono, _ = build_sim_system(cfg)
    win = MainWindow(mono, cfg)
    win.resize(1120, 700)
    win._refresh()
    assert win.wl_value.text() not in ("", "-")

    win.wl_spin.setValue(640.0); win._go_wavelength()
    _pump(app, 0.4); win._refresh()
    assert mono.status().wavelength_nm == pytest.approx(640.0, abs=0.01)

    win.grat_combo.setCurrentIndex(1); win._set_grating()
    _pump(app, 0.6); win._refresh()
    assert mono.status().grating == 2

    was = mono.status().shutter_open
    win._toggle_shutter(); _pump(app, 0.1); win._refresh()
    assert mono.status().shutter_open is (not was)

    win._set_filter()                 # not fitted: refusal goes to the log, no crash
    assert "filter wheel" in win.log.toPlainText()
    win._abort()

    # the indicator paints in both states without throwing
    win.disp.set_state(700, 800, 0, 1400, True, False, 1, 1200)
    win.disp._tick(); win.disp.grab()
    win.disp.set_state(0, 0, 0, 1400, False, True, 2, 600)
    win.disp.grab()
    win.close()
    assert mono.status().connected is False


def test_wavelength_colors_cover_the_range():
    from cs260.apps.gui import wavelength_color
    for nm in (0, 200, 380, 450, 532, 600, 700, 780, 1500):
        c = wavelength_color(nm)
        assert c.isValid()
    assert wavelength_color(532).green() > wavelength_color(532).red()


def test_settings_dialog_builds_and_applies(app):
    from cs260.apps.settings_dialog import SettingsDialog
    cfg = Config()
    mono, _ = build_sim_system(cfg)
    mono.start(poll=False)
    applied = []
    dlg = SettingsDialog(mono, cfg, lambda: applied.append(True))
    dlg.w[("gratings", "g1_max_nm")].setValue(1000.0)
    dlg.w[("accessories", "filter_wheel")].setChecked(True)
    dlg._apply_and_close()
    assert cfg.gratings.g1_max_nm == 1000.0
    assert cfg.accessories.filter_wheel is True
    assert applied == [True]
    mono.shutdown()
