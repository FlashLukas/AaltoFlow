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

from windfreak.config import Config
from windfreak.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _settle(app, synth, pred, timeout=2.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        app.processEvents()
        if pred(synth.status()):
            return
        time.sleep(0.01)
    raise AssertionError("GUI action had no effect")


def test_window_builds_and_drives_both_channels(app):
    from windfreak.apps.gui import MainWindow
    cfg = Config()
    synth, backend = build_sim_system(cfg)
    win = MainWindow(synth, cfg)
    try:
        win._refresh()
        a, b = win.cards["a"], win.cards["b"]
        assert a.freq_value.text() not in ("", "-")
        # read-only start: A was left radiating 2.45 GHz and still is, and
        # the input box shows that value, not the config's 1 GHz
        assert a.rf_btn.text() == "Turn RF Off" and backend.output_on(0)
        assert a.current_freq_hz() == pytest.approx(2.45e9)
        assert backend.writes == []

        a.unit_combo.setCurrentText("GHz")
        assert a.freq_spin.value() == pytest.approx(cfg.channel_a.frequency_Hz / 1e9)
        a.freq_spin.setValue(2.5); a._set_frequency()
        a.unit_combo.setCurrentText("MHz")
        b.power_spin.setValue(-8.0); b._set_power()
        b.phase_spin.setValue(45.0); b._set_phase()
        b._toggle_rf()
        _settle(app, synth, lambda s: s["b_rf_on"] and s["a_frequency_Hz"] == 2.5e9
                and s["b_power_dBm"] == -8.0 and s["b_phase_deg"] == 45.0)
        win._refresh()
        assert b.rf_btn.text() == "Turn RF Off"
        assert backend.output_on(1)

        # reference combo: a user choice sends a command
        i = win.ref_combo.findData("internal_27MHz")
        win.ref_combo.setCurrentIndex(i)
        win._set_reference(i)
        _settle(app, synth, lambda s: s["reference"] == "internal_27MHz")

        # the indicator animates and paints without throwing
        win.tone._tick()
        win.tone.repaint()
        # the C locale: no group separator in number widgets (gotcha #18)
        assert "," not in a.freq_spin.text()
    finally:
        win.close()
    assert not backend.output_on(0) and not backend.output_on(1)   # closing = RF off


def test_settings_dialog_builds_and_applies(app):
    from windfreak.apps.settings_dialog import SettingsDialog
    cfg = Config()
    synth, _ = build_sim_system(cfg)
    synth.start()
    try:
        applied = []
        dlg = SettingsDialog(synth, cfg, lambda: applied.append(True))
        dlg.w[("limits", "power_max_dBm")].setValue(5.0)
        dlg.w[("hardware", "pll_off_when_rf_off")].setChecked(True)
        dlg._apply_and_close()
        assert cfg.limits.power_max_dBm == 5.0
        assert cfg.hardware.pll_off_when_rf_off is True
        assert applied == [True]
    finally:
        synth.shutdown()
