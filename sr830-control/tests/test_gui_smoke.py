"""GUI smoke test: only runs when the PySide6 extra is installed. Builds the
window offscreen against the simulator, drives the controls, repaints."""

import os
import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sr830.config import Config
from sr830.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(win, n=5, dt=0.07):
    from PySide6 import QtWidgets
    for _ in range(n):
        time.sleep(dt)
        QtWidgets.QApplication.processEvents()
        win._refresh()


def test_window_builds_refreshes_and_drives(app):
    from sr830.apps.gui import MainWindow
    cfg = Config()
    li, sim = build_sim_system(cfg, seed=5)
    sim.auto_gain_s = 0.05
    win = MainWindow(li, cfg)
    try:
        assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == \
            ["Lock-in", "Aux I/O", "Instrument"]
        _pump(win)
        assert win.main_tab.r_val.text() != "--"
        c = win.controls

        # a drop-down pick goes to the instrument (activated = user only)
        c.tc.setCurrentText("100 ms")
        c.tc.activated.emit(c.tc.currentIndex())
        assert li.status().time_constant == "100 ms"
        c.sens.setCurrentText("1 mV")
        c.sens.activated.emit(c.sens.currentIndex())
        _pump(win, 3)
        assert li.status().sensitivity == "1 mV"

        # the status follows back into a combo changed elsewhere
        li.set_slope("6 dB/oct")
        win._refresh()
        assert c.slope.currentText() == "6 dB/oct"

        # a refusal is logged, not raised, and visible from every tab
        c.btn_ext.click()
        win.call(li.set_frequency, 1000.0)
        assert "EXTERNAL" in win.log.toPlainText()
        assert "EXTERNAL" in win.last_msg.text()
        c.btn_int.click()
        win._refresh()
        assert c.freq_set.isEnabled()

        # auto gain from its button: disabled while it runs, a result afterwards
        c.auto_btns[0].click()
        deadline = time.monotonic() + 3
        while li.status().auto_busy and time.monotonic() < deadline:
            time.sleep(0.02)
        win._refresh()
        assert "sensitivity" in c.auto_note.text()

        # current input relabels the sensitivity list in amps
        c.src.setCurrentText("I1M")
        c.src.activated.emit(c.src.currentIndex())
        win._refresh()
        assert c.sens.itemText(0) == "2 fA"

        win.acq_btn.click()
        _pump(win)
        win.main_tab.meter._tick()
        win.main_tab.meter.grab()               # paint must not throw
        for name in win.main_tab.QUANTITIES:
            win.main_tab.quantity.setCurrentText(name)

        # aux tab: plot fills from the history, aux out sets
        win.tabs.setCurrentIndex(1)
        _pump(win)
        x, _ = win.aux_tab.curves[0].getData()
        assert x is not None and len(x) >= 5
        win.aux_tab.out_spins[2].setValue(1.5)
        # the Set button next to spin 3
        from PySide6 import QtWidgets
        btns = [b for b in win.aux_tab.findChildren(QtWidgets.QPushButton) if b.text() == "Set"]
        btns[2].click()
        assert li.status().aux_out_set_V[2] == 1.5

        # pausing freezes the plot but not the recording
        win.aux_tab.pc.pause.setChecked(True)
        n_before = len(win.aux_tab.curves[0].getData()[0])
        _pump(win, 1)
        assert len(win.aux_tab.curves[0].getData()[0]) == n_before
        assert len(win.history.t) > n_before

        # the Instrument tab reloads settings and applies them
        win.tabs.setCurrentIndex(2)
        panel = win.inst_tab.settings
        panel.w[("limits", "sine_max_V")][0].setText("1.0")
        assert panel.apply()
        assert cfg.limits.sine_max_V == 1.0
        assert win.controls.sine_spin.maximum() == 1.0
    finally:
        win.close()


def test_range_meter_paints_overload_and_extremes(app):
    from sr830.apps.gui import RangeMeter
    m = RangeMeter()
    m.resize(360, 300)
    for x, y, fs in ((0.0, 0.0, 1e-2), (2e-3, 1e-3, 1e-2), (2e-2, -3e-2, 1e-2),
                     (float("nan"), 0.0, 1e-2)):
        m.set_state(x, y, fs, "10 mV", {"output": abs(x) > fs, "input": False},
                    unlocked=True, acquiring=True)
        for _ in range(3):
            m._tick()
        img = m.grab()
        assert not img.isNull()


def test_settings_dialog_applies_and_rejects_bad_numbers(app):
    from sr830.apps.settings_dialog import SettingsDialog
    cfg = Config()
    li, _ = build_sim_system(cfg)
    applied = []
    dlg = SettingsDialog(li, cfg, lambda: applied.append(True))
    dlg.w[("limits", "aux_out_max_V")][0].setText("5")
    dlg.w[("demod", "reserve")][0].setCurrentText("high")
    dlg._apply_and_close()
    assert cfg.limits.aux_out_max_V == 5.0 and cfg.demod.reserve == "high"
    assert applied == [True]

    dlg = SettingsDialog(li, cfg, lambda: applied.append(True))
    dlg.w[("reference", "frequency_Hz")][0].setText("not a number")
    dlg._apply_and_close()
    assert "frequency_Hz" in dlg.error.text()
    assert applied == [True]                   # not applied a second time
