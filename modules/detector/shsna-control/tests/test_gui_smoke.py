"""GUI smoke test, offscreen; skipped when the gui extra is not installed."""

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from shsna.config import Config
from shsna.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _NoThread:
    """The real Analyzer, but start() does not launch the sweep thread, so the
    test decides exactly when a sweep happens."""

    def __init__(self, sna):
        self._v = sna

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


def _finish(sna):
    while sna.status().acquiring:
        sna.step()


def test_window_takes_a_reference_and_shows_the_transmission(app):
    from shsna.apps.gui import MainWindow
    cfg = Config()
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz, cfg.sweep.points = 700e6, 1300e6, 401
    sna, _ = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(sna), cfg)
    try:
        win._refresh()
        assert win.kind_label.text() == "SIMULATED" and win.sim_card.isVisibleTo(win)
        assert win.ref_label.text() == "none"

        # the simulation card removes the DUT: the thru
        win.dut_chk.click()
        assert sna.status().sim_dut_inserted is False
        win.ref_btn.click()                          # the REFERENCE card drives the brain
        _finish(sna)
        _fresh(win)
        assert sna.status().reference["present"]
        assert win.ref_label.text().startswith("#1: 700-1300 MHz, 401 pts")
        assert win.ref_curve.getData()[0].size == 401

        win.dut_chk.click()                          # DUT back in
        win.which_combo.setCurrentIndex(1)           # last acquisition
        win.acq_btn.click()
        _finish(sna)
        _fresh(win)
        x, y = win.tx_curve.getData()
        assert x.size == 401 and x[int(np.argmax(y))] == pytest.approx(1000, abs=20)   # a flat top
        assert float(win.big["peak"].text()) == pytest.approx(-1.5, abs=0.3)
        assert float(win.big["bw3"].text()) == pytest.approx(60, abs=3)
        assert "against reference #1" in win.trace_label.text()
        assert "#2 acquisition" in win.sample_label.text()

        win.clear_ref_btn.click()                    # no reference: raw only, and it says why
        _fresh(win)
        assert win.tx_curve.getData()[0] is None or win.tx_curve.getData()[0].size == 0
        assert "no transmission" in win.trace_label.text()
        assert win.big["peak"].text() == "—"

        # the sweep form sends only what changed
        win.points_spin.setValue(201)
        win._apply_sweep()
        assert sna.status().points == 201

        win.cont_chk.click()                         # user click -> continuous on
        assert sna.status().continuous is True

        win.indicator.set_state(True, True, False, True)
        win.indicator._tick()
        win.indicator.grab()                         # runs paintEvent
    finally:
        win.close()


def test_a_failed_acquisition_and_an_error_are_shown_in_red(app):
    from shsna.apps.gui import MainWindow
    cfg = Config()
    cfg.sweep.points = 101
    sna, sim = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(sna), cfg)
    try:
        sim.fail_next = "TG not answering"
        win.acq_btn.click()
        _finish(sna)
        _fresh(win)
        assert "FAILED: TG not answering" in win.sample_label.text()
        assert win.error_label.isVisibleTo(win) and "TG not answering" in win.error_label.text()
        assert win.conn_dot.text().endswith("error")
    finally:
        win.close()


def test_on_the_real_backend_the_simulation_is_hidden(app):
    from shsna.analyzer import Analyzer
    from shsna.apps.gui import MainWindow
    from shsna.backends.remote_sa import RemoteSa
    cfg = Config()
    cfg.hardware.owner_cmd_port, cfg.hardware.owner_pub_port = 17736, 17737   # nobody there
    sna = Analyzer(RemoteSa(cfg), cfg)
    win = MainWindow(_NoThread(sna), cfg)
    try:
        sna.step()
        win._refresh()
        assert not win.sim_card.isVisibleTo(win)
        assert "signalhound" in win.kind_label.text()
        assert "NOT reachable" in win.owner_label.text()
        assert "has not been heard" in win.error_label.text()
    finally:
        win.close()


def test_settings_dialog_parses_and_applies(app):
    from shsna.apps.settings_dialog import SettingsDialog
    cfg = Config()
    sna, _ = build_sim_system(cfg, realtime=False)
    applied = []
    dlg = SettingsDialog(sna, cfg, lambda: applied.append(True))
    dlg.w[("sim", "dut_center_Hz")][0].setText("1.2e9")
    dlg.w[("sweep", "points")][0].setText("801")
    dlg.w[("hardware", "owner_host")][0].setText("192.168.0.10")
    dlg._apply_and_close()
    assert cfg.sim.dut_center_Hz == 1.2e9 and cfg.sweep.points == 801
    assert cfg.hardware.owner_host == "192.168.0.10"
    assert applied == [True]

    dlg = SettingsDialog(sna, cfg, lambda: None)
    dlg.w[("sweep", "points")][0].setText("many")
    dlg._apply_and_close()
    assert cfg.sweep.points == 801                 # nothing half-applied
