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

from afg.config import Config
from afg.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _settle(app, gen, win, pred, timeout=2.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        app.processEvents()
        if pred(gen.status()):
            win._refresh()
            return
        time.sleep(0.01)
    raise AssertionError(f"GUI action had no effect: {gen.status()}")


def test_window_builds_and_drives_both_channels(app):
    from afg.apps.gui import MainWindow
    cfg = Config()
    gen, backend = build_sim_system(cfg)
    win = MainWindow(gen, cfg)
    try:
        win._refresh()
        c1, c2 = win.cards["ch1"], win.cards["ch2"]
        # read-only start: CH1 was left driving 30 Hz and still is, and the
        # input box shows that value, not the config's 1 kHz
        assert c1.out_btn.text() == "Output Off" and backend.ch[0]["output"]
        assert c1.current_freq_hz() == pytest.approx(30.0)
        assert c1.freq_value.text().startswith("30.0")
        assert backend.writes == []

        c1.unit_combo.setCurrentText("kHz")
        c1.freq_spin.setValue(2.5); c1.freq_btn.click()
        c1.amp_spin.setValue(1.25); c1.amp_row[1].click()
        c2.off_spin.setValue(-0.2); c2.off_row[1].click()
        c2.out_btn.click()
        _settle(app, gen, win, lambda s: s["ch1_frequency_Hz"] == 2500.0
                and s["ch1_amplitude_Vpp"] == 1.25 and s["ch2_offset_V"] == -0.2
                and s["ch2_output"])
        assert c2.out_btn.text() == "Output Off"

        # waveform combo: a USER choice sends a command; pulse shows the duty row
        i = c2.wave_combo.findData("pulse")
        c2.wave_combo.setCurrentIndex(i)
        c2.wave_combo.activated.emit(i)
        _settle(app, gen, win, lambda s: s["ch2_waveform"] == "pulse")
        assert c2.duty_spin.isVisibleTo(c2) and not c2.sym_spin.isVisibleTo(c2)

        # coupling: follow on -> CH2's frequency box is disabled and tracks CH1
        win.follow_box.click()                     # a real user click
        _settle(app, gen, win, lambda s: s["follow"] and s["ch2_frequency_Hz"] == 2500.0)
        win._refresh()
        assert not c2.freq_spin.isEnabled()
        win.align_btn.click()

        # a refused command lands in the log, not in an exception
        gen.set_follow(True)
        win._safe(gen.set_frequency, "ch2", 5.0)
        assert "follows" in win.log.toPlainText()

        # the indicator paints without throwing; the C locale (gotcha #18)
        win.view.repaint()
        assert "," not in c1.freq_spin.text()
    finally:
        win.close()
    assert not backend.ch[0]["output"] and not backend.ch[1]["output"]   # closing = off


def test_settings_dialog_builds_and_applies(app):
    from afg.apps.settings_dialog import SettingsDialog
    cfg = Config()
    gen, backend = build_sim_system(cfg)
    gen.start()
    try:
        applied = []
        dlg = SettingsDialog(gen, cfg, lambda: applied.append(True))
        backend.writes.clear()
        dlg._apply_and_close()                      # nothing changed: nothing sent
        time.sleep(0.2)
        assert backend.writes == []
        dlg = SettingsDialog(gen, cfg, lambda: applied.append(True))
        dlg.w[("limits_1", "peak_max_V")].setValue(0.5)
        dlg.w[("hardware", "timeout_ms")].setValue(4000)
        dlg.w[("coupling", "ch2_follows_ch1")].setChecked(True)
        dlg._apply_and_close()
        assert cfg.limits_1.peak_max_V == 0.5
        assert cfg.hardware.timeout_ms == 4000 and isinstance(cfg.hardware.timeout_ms, int)
        assert cfg.coupling.ch2_follows_ch1 is True
        assert applied == [True, True]
        t_end = time.monotonic() + 2
        while gen.status()["ch1_amplitude_Vpp"] != 1.0 and time.monotonic() < t_end:
            time.sleep(0.02)
        assert gen.status()["ch1_amplitude_Vpp"] == 1.0        # re-clamped and sent
    finally:
        gen.shutdown()


def test_boxes_follow_changes_made_elsewhere(app):
    """Lab PC 2026-10-07, a scan sweeping CH1's phase: the Phase box stayed
    at the value seen when the window opened. Every box now shows the
    instrument's setpoint -- except one being edited (typed in < 2 s ago, or
    focused) -- and the card has a phase readout."""
    from afg.apps.gui import MainWindow
    cfg = Config()
    gen, backend = build_sim_system(cfg)
    win = MainWindow(gen, cfg)
    try:
        win._refresh()
        c1 = win.cards["ch1"]
        from PySide6 import QtTest
        c1.off_spin.selectAll()                   # the user is typing an offset (number part)
        QtTest.QTest.keyClicks(c1.off_spin, "0.123")
        assert c1.off_spin.text().startswith("0.123")
        gen.set_phase("ch1", 47.0)                # a scan / another client
        gen.set_amplitude("ch1", 0.8)
        gen.set_offset("ch1", 0.05)
        _settle(app, gen, win, lambda s: s["ch1_phase_deg"] == 47.0
                and s["ch1_amplitude_Vpp"] == 0.8 and s["ch1_offset_V"] == 0.05)
        assert c1.phase_spin.value() == pytest.approx(47.0)
        assert c1.amp_spin.value() == pytest.approx(0.8)
        assert c1.phase_value.text() == "47"
        assert c1.off_spin.text().startswith("0.123")         # being edited: kept
        gen.set_frequency("ch1", 440.0)
        _settle(app, gen, win, lambda s: s["ch1_frequency_Hz"] == 440.0)
        assert c1.current_freq_hz() == pytest.approx(440.0)
        assert backend.writes.count(("set_phase", 0, 47.0)) <= 1  # the refresh sent nothing
    finally:
        win.close()
