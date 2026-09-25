"""GUI smoke test: only runs when the PySide6 extra is installed. Builds the
window offscreen against the simulator, drives the controls, repaints."""

import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from hf2.config import Config
from hf2.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_window_builds_refreshes_and_drives(app):
    from hf2.apps.gui import MainWindow
    cfg = Config()
    li, sim = build_sim_system(cfg, seed=5)
    win = MainWindow(li, cfg)
    try:
        import time
        assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == \
            ["Channel 1", "Channel 2", "Aux in", "Instrument"]
        time.sleep(0.2)
        win._refresh()
        assert win.ch_tabs[0].r_val.text() != "--"
        assert win.ch_tabs[1].phasor.channels == (1,)

        ch1 = win.channels[0]
        ch1.tc_unit.setCurrentText("ms")
        ch1.tc_spin.setValue(3.0)
        win.call(li.set_time_constant, 1, ch1.tc_spin.value() * ch1._tc_scale)
        assert li.status().tc_set_s[0] == pytest.approx(3e-3)

        ch1.btn_int.click()                    # user click -> internal reference
        win._refresh()
        assert li.status().reference[0] == "internal"
        assert ch1.freq_set.isEnabled()

        # a refusal is logged, not raised
        win.channels[1].btn_ext.click()
        win.call(li.set_frequency, 2, 1000.0)
        assert "EXTERNAL" in win.log.toPlainText()

        assert "EXTERNAL" in win.last_msg.text()   # visible from every tab

        win.acq_btn.click()
        win._refresh()
        for tab in win.ch_tabs:
            tab.phasor._tick()
            tab.phasor.grab()                  # paint must not throw

        # every plot quantity draws, and the aux plots fill from the history
        for _ in range(5):
            time.sleep(0.07)
            win._refresh()
        for name in win.ch_tabs[0].QUANTITIES:
            win.ch_tabs[0].quantity.setCurrentText(name)
        win.tabs.setCurrentIndex(2)
        win._refresh()
        x, y = win.aux_tab.curves[0].getData()
        assert x is not None and len(x) >= 5
        assert "mean" in win.aux_tab.stats[0].text()

        # pausing freezes the plot but not the recording
        win.aux_tab.pc.pause.setChecked(True)
        n_before = len(win.aux_tab.curves[0].getData()[0])
        time.sleep(0.07); win._refresh()
        assert len(win.aux_tab.curves[0].getData()[0]) == n_before
        assert len(win.history.t) > n_before

        # the Instrument tab reloads settings and applies them
        win.tabs.setCurrentIndex(3)
        panel = win.inst_tab.settings
        panel.w[("limits", "order_max")][0].setValue(6)
        assert panel.apply()
        assert cfg.limits.order_max == 6
        assert win.channels[0].order_spin.maximum() == 6
        assert win.inst_tab.routing.item(1, 1).text() == "2"   # ch2 on input 2
    finally:
        win.close()


def test_settings_dialog_applies_and_rejects_bad_numbers(app):
    from hf2.apps.settings_dialog import SettingsDialog
    cfg = Config()
    li, _ = build_sim_system(cfg)
    applied = []
    dlg = SettingsDialog(li, cfg, lambda: applied.append(True))
    dlg.w[("limits", "tc_max_s")][0].setText("0.5")
    dlg.w[("ch1", "reference")][0].setCurrentText("internal")
    dlg._apply_and_close()
    assert cfg.limits.tc_max_s == 0.5 and cfg.ch1.reference == "internal"
    assert applied == [True]

    dlg = SettingsDialog(li, cfg, lambda: applied.append(True))
    dlg.w[("ch2", "frequency_Hz")][0].setText("not a number")
    dlg._apply_and_close()
    assert "frequency_Hz" in dlg.error.text()
    assert applied == [True]                   # not applied a second time
