"""GUI smoke test, offscreen; skipped when the gui extra is not installed."""

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vna.config import Config
from vna.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _NoThreadVna:
    """The real Analyzer, but start() does not launch the sweep thread, so the
    test decides exactly when a sweep happens."""

    def __init__(self, vna):
        self._v = vna

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


def test_window_builds_sweeps_and_uses_the_brains_reference(app):
    from vna.apps.gui import MainWindow
    cfg = Config()
    cfg.field.source = "manual"
    cfg.sweep.points = 401
    vna, _ = build_sim_system(cfg, realtime=False, seed=3)
    ctrl = _NoThreadVna(vna)
    win = MainWindow(ctrl, cfg)
    try:
        vna.set_manual_field(0.0)                   # line out of the band: a background
        vna.step()
        win._refresh()
        assert win._trace is not None and win.mag_curve.getData()[0].size == 401
        assert win.indicator.isVisibleTo(win) and win.kind_label.text() == "SIMULATED"
        assert win.ref_label.text() == "none"

        # the REFERENCE card drives the brain
        win.ref_btn.click()
        while vna.status().acquiring:
            vna.step()
        win._refresh()
        assert vna.status().reference["present"]
        assert win.ref_label.text().startswith("#1: 0.00 mT")

        vna.set_manual_field(50.0)
        vna.step()
        win.view_combo.setCurrentIndex(1)           # divided by reference
        _fresh(win)
        x, y = win.mag_curve.getData()
        assert "divided by reference #1" in win.trace_label.text()
        # divided by the empty line, the dip is the deepest point, at Kittel
        assert x[y.argmin()] == pytest.approx(2.976, abs=0.02)
        assert win.big["dip"].text().startswith("2.97")

        win.view_combo.setCurrentIndex(2)           # u, real and imaginary
        _fresh(win)
        u = vna.get_trace("last", "u")["u"]
        assert np.allclose(win.mag_curve.getData()[1], u.real)
        assert np.allclose(win.phase_curve.getData()[1], u.imag)
        assert "u against reference #1" in win.trace_label.text()

        win.clear_ref_btn.click()                   # no reference: raw, and it says why
        vna.step()
        _fresh(win)
        assert vna.status().reference["present"] is False
        assert "RAW" in win.trace_label.text() and "reference" in win.trace_label.text()

        # the S-parameter selector applies at once
        win.sparam_combo.setCurrentIndex(win.sparam_combo.findText("S12"))
        win.sparam_combo.activated.emit(win.sparam_combo.currentIndex())
        assert vna.status().sparam == "S12"

        # the sweep form sends only what changed
        win.points_spin.setValue(201)
        win._apply_sweep()
        assert vna.status().points == 201

        win.cont_chk.click()                        # user click -> continuous off
        assert vna.status().continuous is False
        win.acq_btn.click()
        while vna.status().acquiring:
            vna.step()
        win._refresh()
        assert "#2" in win.sample_label.text()

        win.indicator._tick()
        win.indicator.grab()                        # runs paintEvent
    finally:
        win.close()


def test_on_a_real_analyser_the_model_is_hidden(app):
    from fake_visa import FakePna
    from vna.analyzer import Analyzer
    from vna.apps.gui import MainWindow
    from vna.backends.pna import PnaVna
    cfg = Config()
    cfg.field.source = "manual"
    cfg.sweep.points = 11
    vna = Analyzer(PnaVna(cfg, resource=FakePna(sweep_polls=0), sleep=lambda s: None), cfg)
    win = MainWindow(_NoThreadVna(vna), cfg)
    try:
        vna.step()
        win._refresh()
        assert not win.indicator.isVisibleTo(win) and not win.model_label.isVisibleTo(win)
        assert "PNA-X" in win.windowTitle() and win._trace["s"].shape == (11,)
    finally:
        win.close()


def test_settings_dialog_parses_and_applies(app):
    from vna.apps.settings_dialog import SettingsDialog
    cfg = Config()
    cfg.field.source = "manual"
    vna, _ = build_sim_system(cfg, realtime=False)
    applied = []
    dlg = SettingsDialog(vna, cfg, lambda: applied.append(True))
    dlg.w[("sample", "alpha")][0].setText("2e-4")
    dlg.w[("sweep", "points")][0].setText("801")
    dlg.w[("hardware", "visa_resource")][0].setText("TCPIP0::192.168.0.10::hislip0::INSTR")
    dlg._apply_and_close()
    assert cfg.sample.alpha == 2e-4 and cfg.sweep.points == 801
    assert cfg.hardware.visa_resource.startswith("TCPIP0::")
    assert applied == [True]

    dlg = SettingsDialog(vna, cfg, lambda: None)
    dlg.w[("sweep", "points")][0].setText("many")
    dlg._apply_and_close()
    assert cfg.sweep.points == 801                 # nothing half-applied
