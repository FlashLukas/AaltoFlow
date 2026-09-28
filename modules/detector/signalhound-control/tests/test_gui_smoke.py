"""GUI smoke test, offscreen; skipped when the gui extra is not installed."""

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from signalhound.config import Config
from signalhound.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _NoThread:
    """The real brain, but start() does not launch the sweep thread, so the
    test decides exactly when a sweep happens."""

    def __init__(self, brain):
        self._v = brain

    def start(self):
        self._v.start(run=False)

    def __getattr__(self, name):
        return getattr(self._v, name)

    def __setattr__(self, name, value):
        if name == "_v":
            object.__setattr__(self, name, value)
        else:
            setattr(self._v, name, value)


def _fresh(win):
    win._last_fetch = 0.0
    win._refresh()


def test_window_builds_sweeps_and_uses_the_brains_thru(app):
    from signalhound.apps.gui import MainWindow
    cfg = Config()
    cfg.sweep.span_Hz = 20e6
    cfg.acquisition.sweep_on_start = True          # as gui.main() does for its own simulator
    sa, sim = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(sa), cfg)
    try:
        sa.step()
        win._refresh()
        assert win._trace is not None and win.curve.getData()[0].size == 401
        assert win.kind_label.text() == "SIMULATED SA44B"
        assert win.big["level"].text().startswith("-35")
        assert not win.ref_btn.isEnabled()                 # no thru without the TG

        # the TG checkbox (a user click) drives the brain
        win.tg_chk.click()
        assert sa.status().tg_on is True and sim.tg_output_on is False  # until it sweeps
        sa.set_scene("dut_inserted", False)
        win._refresh()
        assert win.ref_btn.isEnabled()
        win.ref_btn.click()
        while sa.status().acquiring:
            sa.step()
        win._refresh()
        assert sa.status().reference["present"]
        assert win.ref_label.text().startswith("#1:")

        sa.set_scene("dut_inserted", True)
        sa.step()
        win.view_combo.setCurrentIndex(1)                  # transmission vs thru
        _fresh(win)
        x, y = win.curve.getData()
        assert "vs thru #1" in win.trace_label.text()
        tx = sa.get_trace("last", "transmission")["transmission"]
        assert np.allclose(y, tx)

        win.clear_ref_btn.click()                          # no thru: spectrum, and it says why
        sa.step()
        _fresh(win)
        assert "SPECTRUM" in win.trace_label.text()

        # the detector selector applies at once
        win.det_combo.setCurrentIndex(win.det_combo.findText("peak"))
        win.det_combo.activated.emit(win.det_combo.currentIndex())
        assert sa.status().detector == "peak"

        # the sweep form sends only what changed
        win.rbw_spin.setValue(30.0)
        win.vbw_spin.setValue(10.0)
        win._apply_sweep()
        assert sa.status().rbw_Hz == 30e3 and sa.status().vbw_Hz == 10e3

        win.cont_chk.click()                               # user click -> continuous off
        assert sa.status().continuous is False
        win.acq_btn.click()
        while sa.status().acquiring:
            sa.step()
        win._refresh()
        assert "#2" in win.sample_label.text()

        win.indicator._tick()
        win.indicator.grab()                               # runs paintEvent
    finally:
        win.close()
    assert sim.tg_output_on is False                       # closing the window shut it down


def test_settings_dialog_parses_and_applies(app):
    from signalhound.apps.settings_dialog import SettingsDialog
    cfg = Config()
    sa, _ = build_sim_system(cfg, realtime=False)
    sa.start(run=False)
    applied = []
    dlg = SettingsDialog(sa, cfg, lambda: applied.append(True))
    dlg.w[("scene", "dut_loss_dB")][0].setText("3.5")
    dlg.w[("sweep", "rbw_Hz")][0].setText("3e3")
    dlg.w[("hardware", "dll_path")][0].setText(r"C:\SH\sa_api.dll")
    dlg._apply_and_close()
    assert cfg.scene.dut_loss_dB == 3.5 and cfg.sweep.rbw_Hz == 3e3
    assert cfg.hardware.dll_path.endswith("sa_api.dll")
    assert applied == [True]

    dlg = SettingsDialog(sa, cfg, lambda: None)
    dlg.w[("sweep", "rbw_Hz")][0].setText("narrow")
    dlg._apply_and_close()
    assert cfg.sweep.rbw_Hz == 3e3                  # nothing half-applied
    sa.shutdown()


def test_untouched_analyser_says_so(app):
    """Start-up rule: nothing configured yet, and the panel says why it is empty."""
    from signalhound.apps.gui import MainWindow
    cfg = Config()
    sa, sim = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(sa), cfg)
    try:
        sa.step()
        win._refresh()
        assert sim.configure_calls == 0 and not win.cont_chk.isChecked()
        assert "not configured" in win.sweep_time_label.text()
    finally:
        win.close()
