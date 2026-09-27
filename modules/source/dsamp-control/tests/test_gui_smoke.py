"""A light smoke test for the GUI: it only runs when the optional PySide6 extra
is installed (otherwise it is skipped, so the headless core suite is unaffected).

It builds the window against the simulator offscreen, refreshes it, drives the
controls and paints the indicator -- enough to catch import errors, layout
crashes and signal-wiring mistakes without a display."""

import os

import pytest

pytest.importorskip("PySide6")           # skip cleanly if the gui extra isn't installed
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dsamp.config import Config
from dsamp.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_window_builds_and_refreshes(app):
    from dsamp.apps.gui import MainWindow
    cfg = Config()
    amp, backend = build_sim_system(cfg)
    win = MainWindow(amp, cfg)
    win.resize(1180, 760)
    try:
        win._refresh()
        assert win.gain_value.text() not in ("", "—")
        # the gain box starts from the ADOPTED gain (sim leftover: 6 dB), not 0
        assert win.gain_spin.value() == backend._gain == 6.0
        # the spin box range IS the live envelope
        assert win.gain_spin.maximum() == cfg.limits.gain_max_dB

        win.gain_spin.setValue(4.5)
        win._set_gain()
        win.freq_spin.setValue(3000.0)
        win.input_spin.setValue(-10.0)
        win._set_operating_point()
        win._toggle_amp()
        amp.poll_once()
        win._refresh()
        assert amp.status().amp_on is True
        assert amp.status().gain_dB == 4.5
        assert win.state_badge.text() == "AMP ON"

        # paint the indicator in both states; the animation tick must not throw
        win.indicator._tick()
        win.indicator.grab()
        win.ctrl.amp_off()
        amp.poll_once()
        win._refresh()
        win.indicator.grab()
    finally:
        win.close()
    assert backend._output is False          # closing the window switched it off


def test_settings_dialog_moves_the_ceiling(app):
    from dsamp.apps.settings_dialog import SettingsDialog
    cfg = Config()
    amp, _ = build_sim_system(cfg)
    amp.start()
    try:
        amp.set_gain(9.0)
        applied = []
        dlg = SettingsDialog(amp, cfg, lambda: applied.append(True))
        dlg.w[("limits", "gain_max_dB")].setValue(5.0)
        dlg.w[("hardware", "buttons_on_exit")].setChecked(False)
        dlg._apply_and_close()
        assert cfg.limits.gain_max_dB == 5.0
        assert cfg.hardware.buttons_on_exit is False
        assert applied == [True]
        amp.poll_once()
        assert amp.status().gain_dB == 5.0   # the standing gain was re-clamped
    finally:
        amp.shutdown()


def test_number_widgets_use_the_c_locale(app):
    from PySide6 import QtCore
    from dsamp.apps.settings_dialog import _dspin
    w = _dspin(10.5, 0, 40, 2, 0.5)
    assert w.locale().decimalPoint() == "."
    assert QtCore.QLocale.c().decimalPoint() == "."
