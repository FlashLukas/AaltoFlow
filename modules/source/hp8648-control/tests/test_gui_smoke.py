"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise it is skipped, so the headless core suite is unaffected).

It builds the window against the simulator offscreen, refreshes it, and drives
the controls -- enough to catch import errors, layout crashes, and signal-wiring
mistakes without a display. It also paints the SpectrumIndicator in both themes
and in every state it can show."""

import os
import time

import pytest

pytest.importorskip("PySide6")           # skip cleanly if the gui extra isn't installed
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from hp8648.config import Config
from hp8648.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def test_window_builds_and_drives_the_generator(app):
    from hp8648.apps.gui import MainWindow
    cfg = Config()
    cfg.hardware.switch_settle_s = 0.0
    src, _ = build_sim_system(cfg)
    win = MainWindow(src, cfg)
    win.resize(1180, 700)
    try:
        win._refresh()
        assert win.freq_value.text() not in ("", "-")

        win._change_freq_unit("GHz")
        win._change_freq_unit("MHz")
        win.freq_spin.setValue(3200.0); win._set_frequency()
        win.power_spin.setValue(-8.0); win._set_power()
        win._toggle_rf()
        assert src.wait_idle()
        _pump(app, 0.2)
        win._refresh()
        s = src.status()
        assert s.rf_on is True and s.frequency_Hz == 3.2e9
        # the power box's top followed the ceiling above 2500 MHz
        assert win.power_spin.maximum() == 10.0
        # events reached the log through the thread bridge
        assert "frequency" in win.log.toPlainText()
    finally:
        win.close()
    assert src.status().rf_on is False           # closing the window = RF off


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_spectrum_paints_every_state(app, theme):
    from PySide6 import QtGui
    from hp8648.apps.gui import SpectrumIndicator
    from hp8648.apps.theme import set_theme
    set_theme(theme)
    try:
        w = SpectrumIndicator()
        w.resize(700, 260)
        for args in [(False, 1e9, -30, 13, 13),
                     (True, 3.2e9, 4, 10, 13),
                     (True, 9e3, -136, 13, 13),
                     (True, 4e9, 10, 10, 13, True, False),
                     (False, 2e9, -10, 13, 13, False, True)]:
            w.set_state(*args)
            w._tick()
            img = QtGui.QImage(w.size(), QtGui.QImage.Format_ARGB32)
            w.render(img)
            assert not img.isNull()
        w.set_state(False, 1e9, -30, 13, 13)
        assert not w._timer.isActive()           # no animation while RF is off
    finally:
        set_theme("dark")


def test_settings_dialog_applies(app):
    from hp8648.apps.settings_dialog import SettingsDialog
    cfg = Config()
    src, _ = build_sim_system(cfg)
    applied = []
    dlg = SettingsDialog(src, cfg, lambda: applied.append(True))
    dlg.w[("limits", "power_max_dBm")].setValue(5.0)
    dlg.w[("hardware", "option_1ea")].setChecked(True)
    dlg._apply_and_close()
    assert cfg.limits.power_max_dBm == 5.0
    assert cfg.hardware.option_1ea is True
    assert applied == [True]
