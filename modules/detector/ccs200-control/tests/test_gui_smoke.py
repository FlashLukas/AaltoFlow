"""GUI smoke test, offscreen; skipped when the gui extra is not installed."""

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ccs200.config import Config
from ccs200.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _NoThread:
    """The real Spectrometer, but start() does not launch the scan thread, so
    the test decides exactly when a scan happens."""

    def __init__(self, spec):
        self._v = spec

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


def _done(spec):
    while spec.status().acquiring:
        spec.step()


def test_window_builds_scans_and_drives_the_brain(app):
    from ccs200.apps.gui import MainWindow
    cfg = Config()
    spec, _ = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(spec), cfg)
    try:
        spec.step()
        _fresh(win)
        x, y = win.curve.getData()
        assert x.size == 3648 and win.kind_label.text() == "SIMULATED"
        assert win.sim_card.isVisibleTo(win)
        assert win.big["peak"].text().startswith("546.")
        assert win.indicator._levels.max() == pytest.approx(1.0)

        # the scan form sends only what changed
        win.int_spin.setValue(5.0)
        win.avg_spin.setValue(2)
        win._apply_scan()
        assert spec.status().integration_time_s == pytest.approx(0.005)
        assert spec.status().averages == 2

        # dark: cap the fibre, take it, subtract
        win.light_chk.click()
        assert spec.status().light_on is False
        win.cont_chk.click()                          # user click -> continuous off
        assert spec.status().continuous is False
        win.dark_btn.click()
        _done(spec)
        win.sub_chk.click()
        assert spec.status().dark_subtract is True
        _fresh(win)
        assert win.dark_label.text().startswith("#1:") and "fits" in win.dark_label.text()

        win.light_chk.click()
        win.acq_btn.click()
        _done(spec)
        win.show_combo.setCurrentIndex(1)            # last acquisition
        _fresh(win)
        assert "#2 acquisition" in win.sample_label.text()
        assert "dark subtracted" in win.trace_label.text()
        t = spec.get_trace("sample")
        assert np.allclose(win.curve.getData()[1], t["spectrum"])

        win.show_combo.setCurrentIndex(2)            # the dark itself
        _fresh(win)
        assert win.curve.getData()[1].max() < 0.02

        # the analysis window: not shaded while it spans everything, shaded once set
        assert not win.window_region.isVisible()
        win.wmin_spin.setValue(750.0); win.wmax_spin.setValue(780.0)
        [b for b in win.findChildren(type(win.acq_btn)) if b.text() == "Set"][0].click()
        assert (spec.status().window_min_nm, spec.status().window_max_nm) == (750.0, 780.0)
        _fresh(win)
        assert win.window_region.isVisible() and win.window_region.getRegion() == (750.0, 780.0)

        win.log_chk.setChecked(True)
        win.indicator._tick()
        win.indicator.grab()                          # runs paintEvent
    finally:
        win.close()


def test_on_the_real_instrument_the_sim_card_is_hidden(app):
    from fake_tlccs import FakeTlccs
    from ccs200.apps.gui import MainWindow
    from ccs200.backends.tlccs import TlccsSpectrometer
    from ccs200.spectrometer import Spectrometer
    cfg = Config()
    spec = Spectrometer(TlccsSpectrometer(resource="USB0::x", dll=FakeTlccs(polls_until_ready=0)), cfg)
    win = MainWindow(_NoThread(spec), cfg)
    try:
        spec.step()
        _fresh(win)
        assert not win.sim_card.isVisibleTo(win)
        assert "CCS200/M" in win.windowTitle() and win._trace["spectrum"].shape == (3648,)
    finally:
        win.close()


def test_indicator_colours_and_binning(app):
    from ccs200.apps.gui import PrismIndicator, wavelength_rgb
    assert wavelength_rgb(650)[0] > 200 and wavelength_rgb(650)[2] == 0      # red
    assert wavelength_rgb(450)[2] > 200                                        # blue
    assert wavelength_rgb(300) == wavelength_rgb(250)                          # UV: neutral
    ind = PrismIndicator()
    wl = np.linspace(200, 1000, 3648)
    y = np.exp(-0.5 * ((wl - 546.0) / 0.6) ** 2)          # a line narrower than a bin
    ind.set_spectrum(wl, y)
    assert ind._levels.max() == pytest.approx(1.0, abs=0.01)   # MAX per bin: not averaged away
    ind.set_state(1.0, True, 546.0, True)
    ind.resize(330, 130)
    ind.grab()


def test_settings_dialog_parses_and_applies(app):
    from ccs200.apps.settings_dialog import SettingsDialog
    cfg = Config()
    spec, _ = build_sim_system(cfg, realtime=False)
    applied = []
    dlg = SettingsDialog(spec, cfg, lambda: applied.append(True))
    dlg.w[("sim", "line_level_per_s")][0].setText("120")
    dlg.w[("scan", "averages")][0].setText("8")
    dlg.w[("hardware", "resource")][0].setText("USB0::0x1313::0x8089::M00000000::RAW")
    dlg._apply_and_close()
    assert cfg.sim.line_level_per_s == 120.0 and cfg.scan.averages == 8
    assert cfg.hardware.resource.startswith("USB0::")
    assert applied == [True]

    dlg = SettingsDialog(spec, cfg, lambda: None)
    dlg.w[("scan", "averages")][0].setText("many")
    dlg._apply_and_close()
    assert cfg.scan.averages == 8                     # nothing half-applied
