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
    vna = Analyzer(PnaVna(cfg, resource=FakePna(sweep_polls=0), sleep=lambda s: None), cfg)
    win = MainWindow(_NoThreadVna(vna), cfg)
    try:
        # a real analyser starts hands-off with ITS sweep adopted (201 points);
        # a user sets ours and switches Continuous on
        assert vna.status().continuous is False and vna.status().points == 201
        vna.set_points(11)
        vna.set_continuous(True)
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


def test_settings_fixed_choices_are_drop_downs(app):
    """A string with a fixed set of values (the theme, ...) is a drop-down, not
    free text; the selection lands in cfg, and a value the list does not know
    (a hand-edited .ini) is kept and marked, never silently replaced."""
    from PySide6 import QtWidgets
    from vna.apps.settings_dialog import SettingsDialog
    cfg = Config()
    cfg.field.source = "manual"
    ctrl, _ = build_sim_system(cfg, realtime=False)
    dlg = SettingsDialog(ctrl, cfg, lambda: None)
    theme, _ = dlg.w[("ui", "theme")]
    assert isinstance(theme, QtWidgets.QComboBox)
    assert [theme.itemText(i) for i in range(theme.count())] == ["dark", "light"]
    theme.setCurrentIndex(theme.findData("light"))
    box, _ = dlg.w[("sweep", "sparam")]
    assert isinstance(box, QtWidgets.QComboBox)
    assert [box.itemText(i) for i in range(box.count())] == ['S11', 'S12', 'S21', 'S22']
    box.setCurrentIndex(box.findData("S11"))
    dlg._apply_and_close()
    assert cfg.ui.theme == "light"
    assert cfg.sweep.sparam == "S11"
    cfg.ui.theme = "sepia"
    dlg._refresh_widgets_from_cfg()
    assert theme.currentData() == "sepia" and "not a known value" in theme.currentText()
    assert dlg._pull_into_cfg() and cfg.ui.theme == "sepia"
    # the fly-scan stream has its own tab, generated from the config group
    assert ("stream", "points_Hz") in dlg.w


def test_fly_points_box_sets_the_stream_points_in_ghz(app):
    """The sidebar's 'Fly points' box: GHz in, Hz in the config, and the box
    then shows the grid points the analyser really uses."""
    from vna.apps.gui import MainWindow
    cfg = Config()
    cfg.field.source = "manual"
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz, cfg.sweep.points = 1e9, 5e9, 401
    vna, _ = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThreadVna(vna), cfg)
    try:
        win.points_edit.setText("2.5, 3.0049")
        win._apply_stream_points()
        assert [p["channel"] for p in vna.stream_points()] == ["p1", "p2"]
        assert cfg.stream.points_Hz.startswith("2500000000")
        win._sync_inputs()
        assert win.points_edit.text() == "2.5, 3"           # snapped to the 10 MHz grid
        win.points_edit.setText("2.5, two")
        win._apply_stream_points()                           # refused, logged, unchanged
        assert len(vna.stream_points()) == 2
    finally:
        win.close()
