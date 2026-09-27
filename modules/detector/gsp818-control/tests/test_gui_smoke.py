"""GUI smoke test, offscreen; skipped when the gui extra is not installed."""

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gsp818.config import Config
from gsp818.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _NoThread:
    """The real brain, but start() does not launch the sweep thread, so the
    test decides exactly when a sweep happens."""

    def __init__(self, sa):
        self._v = sa

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


def _finish(sa):
    while sa.status().acquiring:
        sa.step()


def test_window_builds_sweeps_and_normalises(app):
    from gsp818.apps.gui import MainWindow
    cfg = Config()
    cfg.sweep.points = 401
    sa, sim = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(sa), cfg)
    try:
        sa.step()
        win._refresh()
        assert win._trace is not None and win.curve.getData()[0].size == 401
        assert win.kind_label.text() == "SIMULATED" and win.dut_row.isVisibleTo(win)
        assert win.ref_label.text() == "none"
        # the -20 dBm carrier, within one 4.5 MHz display bin
        assert float(win.big["freq"].text()) == pytest.approx(100.0, abs=4.5)

        # the frequency card: center/span
        win.center_spin.setValue(433.92); win.span_spin.setValue(2.0)
        win._apply_center_span()
        s = sa.status()
        assert s.center_Hz == pytest.approx(433.92e6) and s.span_Hz == pytest.approx(2e6)

        # bandwidth card: typing a value with auto unticked sends it (manual)
        win.auto_chk["rbw"].setChecked(False)
        win.rbw_spin.setValue(3.0)
        win._apply_bandwidth()
        assert sa.status().rbw_set_Hz == 3e3 and not sa.status().rbw_auto
        win.auto_chk["rbw"].click()                            # user ticks auto again
        assert sa.status().rbw_auto

        # tracking generator: the button drives the brain and turns red
        sa.set_start(100e6); sa.set_stop(1.7e9)
        win.tg_btn.click()
        sa.step(); win._refresh()
        assert sa.status().tg_on and sim.tg_output and win.tg_btn.text() == "TG ON"
        assert win.tg_btn.objectName() == "danger"
        win.dut_combo.setCurrentIndex(win.dut_combo.findText("thru"))
        win.dut_combo.activated.emit(win.dut_combo.currentIndex())
        assert sa.status().dut == "thru"

        win.ref_btn.click(); _finish(sa); win._refresh()
        assert win.ref_label.text().startswith("#1: TG -10 dBm")

        sa.set_dut("bandpass"); sa.step()
        win.view_combo.setCurrentIndex(1)
        _fresh(win)
        x, y = win.curve.getData()
        assert "minus thru reference #1" in win.trace_label.text()
        assert y[np.argmin(abs(x - 900.0))] == pytest.approx(-1.5, abs=0.3)
        assert win.big_unit["level"].text() == "dB"

        win.clear_ref_btn.click(); sa.step(); _fresh(win)
        assert "SPECTRUM" in win.trace_label.text() and "reference" in win.trace_label.text()

        win.cont_chk.click()                                  # user click -> continuous off
        assert sa.status().continuous is False
        win.acq_btn.click(); _finish(sa); win._refresh()
        assert "#2" in win.sample_label.text()

        win.indicator._tick()
        win.indicator.grab()                                  # runs paintEvent
    finally:
        win.close()
    assert sim.tg_output is False                             # closing shut the TG off


def test_on_a_real_analyser_the_bench_is_hidden(app):
    from fake_gsp import FakeGsp
    from gsp818.analyzer import SpectrumAnalyzer
    from gsp818.apps.gui import MainWindow
    from gsp818.backends.gsp import GspAnalyzer
    cfg = Config()
    cfg.hardware.settle_sweeps = 1
    sa = SpectrumAnalyzer(GspAnalyzer(cfg, resource=FakeGsp()), cfg)
    win = MainWindow(_NoThread(sa), cfg)
    try:
        sa.step()
        win._refresh()
        assert not win.dut_row.isVisibleTo(win) and "GSP-818" in win.windowTitle()
        assert win._trace["power_dBm"].shape == (601,)
    finally:
        win.close()


def test_settings_dialog_parses_and_applies(app):
    from gsp818.apps.settings_dialog import SettingsDialog
    cfg = Config()
    sa, _ = build_sim_system(cfg, realtime=False)
    applied = []
    dlg = SettingsDialog(sa, cfg, lambda: applied.append(True))
    dlg.w[("bench", "dut_bw_Hz")][0].setText("5e7")
    dlg.w[("sweep", "points")][0].setText("801")
    dlg.w[("hardware", "resource")][0].setText("TCPIP0::192.168.0.10::inst0::INSTR")
    dlg._apply_and_close()
    assert cfg.bench.dut_bw_Hz == 5e7 and cfg.sweep.points == 801
    assert cfg.hardware.resource.startswith("TCPIP0::")
    assert applied == [True]

    dlg = SettingsDialog(sa, cfg, lambda: None)
    dlg.w[("sweep", "points")][0].setText("many")
    dlg._apply_and_close()
    assert cfg.sweep.points == 801                 # nothing half-applied
