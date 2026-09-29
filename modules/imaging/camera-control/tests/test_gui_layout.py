"""The tidy settings layout (Lukas's screenshots, 2026-09-29).

The AutoFocus and Camera settings tabs were one long single-column form with
full-width fields, and the autofocus metric's knobs lived in the Spot tab. Now:
three columns of compact group boxes, fields sized to their content, the
metric and ITS knobs together in the AutoFocus tab (shown for the chosen metric
only), settings the chosen routine does not use greyed (values kept), a driver
combo, the Auto exposure button next to the exposure, simulator-only fields
only with the simulator. The Spot tab keeps the live readout (default: the
area relative to the peak), where it is measured, and the calibration switch.
"""

import os
import types

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,  # noqa: E402
                               QSpinBox)

from camera.apps.gui import MainWindow  # noqa: E402
from camera.apps.spot_tab import sizes_summary  # noqa: E402
from camera.config import Config  # noqa: E402
from camera.sim_system import build_sim_system  # noqa: E402


@pytest.fixture
def win():
    app = QApplication.instance() or QApplication([])
    brain, *_ = build_sim_system(Config())
    brain.start()
    w = MainWindow(brain, brain.cfg, remote=False)
    w.resize(1600, 1000)
    w.show()
    app.processEvents()
    yield w, brain, app
    w.close()
    brain.shutdown()
    app.processEvents()


def _show(w, app, page):
    w.tabs.setCurrentWidget(page)
    app.processEvents()


def test_autofocus_tab_is_three_columns_of_compact_boxes(win):
    w, brain, app = win
    _show(w, app, w.af_page)
    boxes = w._boxes
    xs = {t: boxes[t].mapTo(w, boxes[t].rect().topLeft()).x()
          for t in ("Metric", "Routine", "Park")}
    assert xs["Metric"] < xs["Routine"] < xs["Park"]            # side by side, not stacked
    for key in ("autofocus", "spot"):
        for name, wdg in w._form_widgets[key].items():
            if isinstance(wdg, (QSpinBox, QDoubleSpinBox)):
                assert wdg.maximumWidth() <= 110, name           # no 1900-px number boxes


def test_the_routine_greys_what_it_does_not_use(win):
    w, brain, app = win
    _show(w, app, w.af_page)
    afw = w._form_widgets["autofocus"]
    afw["routine"].setCurrentText("sweep")
    assert not afw["coarse_step_v"].isEnabled() and not afw["park_centre"].isEnabled()
    assert "sweep" in afw["coarse_step_v"].toolTip()
    assert afw["drive_amplitude_v"].isEnabled()
    afw["routine"].setCurrentText("one_way")
    assert afw["coarse_step_v"].isEnabled() and afw["park_tolerance_d4sigma"].isEnabled()
    assert not afw["drive_amplitude_v"].isEnabled() and not afw["steps"].isEnabled()
    assert afw["fit_curve"].isEnabled()                           # both routines fit
    afw["continuous_enabled"].setChecked(False)
    assert not afw["continuous_gain"].isEnabled()
    afw["continuous_enabled"].setChecked(True)
    assert afw["continuous_gain"].isEnabled()
    assert not afw["continuous_target"].isEnabled()               # not used at all
    # greyed, not cleared: the value is still there
    assert afw["steps"].value() == brain.cfg.autofocus.steps


def test_only_the_chosen_metrics_knobs_are_shown_and_apply_reaches_the_spot_group(win):
    w, brain, app = win
    _show(w, app, w.af_page)
    afw, spw = w._form_widgets["autofocus"], w._form_widgets["spot"]
    for mech, shown, hidden in (("spot_relative", "rel_level", "clip_mode"),
                                ("spot_d4sigma", "clip_mode", "rel_level"),
                                ("spot_encircled", "encircled_fraction", "thr_lower"),
                                ("spot_area", "thr_lower", "detect_px"),
                                ("edges", None, "clip_sigma")):
        afw["mechanism"].setCurrentText(mech)
        app.processEvents()
        if shown:
            assert spw[shown].isVisibleTo(w), (mech, shown)
        assert not spw[hidden].isVisibleTo(w), (mech, hidden)
    assert isinstance(afw["mechanism"], QComboBox)
    items = [afw["mechanism"].itemText(i) for i in range(afw["mechanism"].count())]
    assert {"spot_encircled", "spot_gauss", "spot_peak"} <= set(items)
    afw["mechanism"].setCurrentText("spot_relative")
    spw["rel_level"].setValue(0.5)
    w._apply_settings([("Autofocus", brain.cfg.autofocus), ("Spot", brain.cfg.spot)])
    assert brain.cfg.spot.rel_level == pytest.approx(0.5)
    assert brain.cfg.autofocus.mechanism == "spot_relative"


def test_camera_settings_driver_combo_auto_exposure_and_simulator_fields(win):
    w, brain, app = win
    cam = w._form_widgets["camera"]
    assert isinstance(cam["driver"], QComboBox)
    assert [cam["driver"].itemText(i) for i in range(cam["driver"].count())] == ["ids",
                                                                                   "genicam"]
    # the button sits right next to the exposure it sets
    assert w.b_auto_expo.parent() is cam["exposure_us"].parent()
    assert not w._boxes["Simulator"].isHidden()                   # the sim IS the camera
    img = w._form_widgets["image"]
    img["clip_enabled"].setChecked(False)
    assert not img["clip_left"].isEnabled()
    img["clip_enabled"].setChecked(True)
    assert img["clip_left"].isEnabled()
    old = brain.backend.get_feature("ExposureTime")
    brain.cfg.camera.auto_exposure_percentile = 99.9
    w.b_auto_expo.click()
    assert brain.backend.get_feature("ExposureTime") != old
    assert cam["exposure_us"].value() == pytest.approx(brain.backend.get_feature("ExposureTime"))


def test_other_generated_tabs_are_multi_column_too(win):
    w, brain, app = win
    for title in ("Matching", "Backup patterns", "Losing the pattern", "Limits", "Rig",
                  "Objective & pixels", "Image geometry", "Camera"):
        assert title in w._boxes, title


def test_spot_tab_live_readout_locate_and_calibration_switch(win):
    w, brain, app = win
    tab = w.spot_tab
    assert tab.cmb_size.currentData() == "relative"               # the default live size
    assert [tab.cmb_locate.itemData(i) for i in range(tab.cmb_locate.count())] == \
        ["calibrated", "peak", "blob"]
    assert [tab.cmb_calib.itemData(i) for i in range(tab.cmb_calib.count())] == \
        ["saturated", "unsaturated"]
    assert isinstance(tab.chk_calib_afx, QCheckBox)
    tab.cmb_locate.setCurrentIndex(2)
    tab.cmb_calib.setCurrentIndex(1)
    tab._push_threshold()
    assert brain.cfg.spot.locate == "blob" and brain.cfg.spot.calib_mode == "unsaturated"
    # the knobs moved to the AutoFocus tab: the Spot tab never sends them
    assert "rel_level" not in tab.values() and "clip_mode" not in tab.values()


def test_saturation_reads_as_information():
    app = QApplication.instance() or QApplication([])
    nan = float("nan")
    st = types.SimpleNamespace(spot_found=True, spot_area=900.0, spot_rel_area=800.0,
                               spot_d4sigma_px=30.0, spot_sigma2_px2=56.0, spot_d86_px=31.0,
                               spot_gauss_sigma2_px2=20.0, spot_peak_avg=240.0,
                               spot_saturated=True, spot_sat_fraction=0.12,
                               spot_exposure_hint=0.4, spot_size_why="", spot_offset_px=nan)
    txt = sizes_summary(st, highlight="spot_d4sigma")
    assert txt.count("-- (saturated)") == 2                       # Gaussian fit + peak
    assert "<b>D4σ: 30.0 px" in txt and "unaffected" in txt and "×0.40" in txt
    assert "wrong" not in txt
    st.spot_saturated, st.spot_size_why = False, "no spot above the noise; the brightest ..."
    txt = sizes_summary(st, highlight="relative")
    assert "-- (saturated)" not in txt and "-- no spot above the noise" in txt
