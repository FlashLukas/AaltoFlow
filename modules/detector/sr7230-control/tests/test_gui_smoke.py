"""GUI smoke test: only runs when the PySide6 extra is installed. Builds the
window offscreen against the simulator, drives the controls, repaints."""

import os
import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sr7230.config import Config
from sr7230.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_window_builds_refreshes_and_drives(app):
    from sr7230.apps.gui import MainWindow
    cfg = Config()
    cfg.signal.sensitivity_index = 20
    li, sim = build_sim_system(cfg, seed=5)
    win = MainWindow(li, cfg)
    try:
        assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == \
            ["Lock-in", "ADC in", "Instrument"]
        time.sleep(0.4)
        win._refresh()
        tab = win.lockin_tab
        assert tab.r_val.text() != "--"
        c = win.controls
        assert c.sens_combo.currentText() == "5 mV"
        assert c.tc_combo.currentText() == "100 ms"
        assert c.slope_combo.count() == 4

        # pick a time constant the way a user does (activated fires the call)
        i = c.tc_combo.findText("20 ms")
        c.tc_combo.setCurrentIndex(i)
        c._tc_picked()
        assert li.status().tc_s == 0.02

        # fast mode: the lists follow on the next refresh
        c.fast_box.click()
        win._refresh()
        assert li.status().fast_mode is True
        assert c.slope_combo.count() == 2 and c.tc_combo.findText("10 us") >= 0

        # reference buttons are user-only and reflect the state
        c.ref_btns["ext_ttl"].click()
        win._refresh()
        assert li.status().ref_source == "ext_ttl"
        assert c.ref_btns["ext_ttl"].isChecked() and not c.ref_btns["internal"].isChecked()
        c.ref_btns["internal"].click()

        # a refusal is logged, not raised
        win.call(li.set_slope, 9)
        assert "slope" in win.log.toPlainText()
        assert "slope" in win.last_msg.text()

        # auto-measure from the button, done by the poll thread
        c.btn_asm.click()
        deadline = time.monotonic() + 3
        while li.status().auto_busy and time.monotonic() < deadline:
            time.sleep(0.02)
        win._refresh()
        assert li.status().auto_id == 1

        win.acq_btn.click()
        win._refresh()
        win.meter._tick()
        win.meter.grab()                       # paint must not throw
        assert 0.0 <= win.meter.target

        # every plot quantity draws, and the ADC plots fill from the history
        for _ in range(5):
            time.sleep(0.07)
            win._refresh()
        for name in tab.QUANTITIES:
            tab.quantity.setCurrentText(name)
        win.tabs.setCurrentIndex(1)
        win._refresh()
        x, y = win.adc_tab.curves[0].getData()
        assert x is not None and len(x) >= 5
        assert "mean" in win.adc_tab.stats[0].text()

        # pausing freezes the plot but not the recording
        win.adc_tab.pc.pause.setChecked(True)
        n_before = len(win.adc_tab.curves[0].getData()[0])
        time.sleep(0.07); win._refresh()
        assert len(win.adc_tab.curves[0].getData()[0]) == n_before
        assert len(win.history.t) > n_before

        # current mode: the unit and the list of ranges change
        li.set_input("I high-BW")
        win.tabs.setCurrentIndex(0)
        win._refresh()
        assert tab.r_unit.text().endswith("A")
        assert c.sens_combo.findText("100 nA") >= 0

        # the Instrument tab reloads settings and applies them
        win.tabs.setCurrentIndex(2)
        panel = win.inst_tab.settings
        panel.w[("limits", "amplitude_max_V")][0].setText("0.5")
        assert panel.apply()
        assert cfg.limits.amplitude_max_V == 0.5
        assert c.amp_spin.maximum() == 0.5
        win._refresh()
        assert "connected" in win.inst_tab.f["conn"].text()
    finally:
        win.close()


def test_meter_paints_overload_and_pegs(app):
    from sr7230.apps.gui import SensitivityMeter
    m = SensitivityMeter()
    m.resize(320, 280)
    m.set_state(5.0, "1 mV", "5.000 mV", 45.0, True, False, True)
    for _ in range(40):
        m._tick()
    assert m.shown <= m.SPAN * 1.03 + 1e-9          # pegged against the stop
    m.grab()
    m.set_state(float("nan"), "--", "--", None, False, None, False)
    m.grab()


def test_settings_dialog_applies_and_rejects_bad_numbers(app):
    from sr7230.apps.settings_dialog import SettingsDialog
    cfg = Config()
    li, _ = build_sim_system(cfg)
    applied = []
    dlg = SettingsDialog(li, cfg, lambda: applied.append(True))
    dlg.w[("limits", "tc_max_s")][0].setText("0.5")
    dlg.w[("reference", "source")][0].setCurrentText("ext_analog")
    dlg._apply_and_close()
    assert cfg.limits.tc_max_s == 0.5 and cfg.reference.source == "ext_analog"
    assert applied == [True]

    dlg = SettingsDialog(li, cfg, lambda: applied.append(True))
    dlg.w[("reference", "frequency_Hz")][0].setText("not a number")
    dlg._apply_and_close()
    assert "frequency_Hz" in dlg.error.text()
    assert applied == [True]                   # not applied a second time
