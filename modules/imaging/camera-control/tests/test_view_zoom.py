"""Zoom to the spot region + "Save config" buttons (2026-09-29).

Lukas: "add an option in autofocus that when you call autofocus the image will
zoom to the spot detection area". The main camera view can show only a part
of the frame (CameraView.set_zoom); every overlay and every click use the SAME
transform, so a click on a zoomed view still names the right image pixel.
While an autofocus / Z step calibration runs (autofocus.zoom_on_af, default
on) the view shows the spot SEARCH REGION around the calibrated spot (+ a
margin) and puts back the user's view when the run ends; a double-click
un-zooms for the rest of that run. A "Zoom to spot region" / "Whole frame"
button does the same by hand. "Save config" sits next to Apply in the
AutoFocus and Camera settings tabs (the Spot tab keeps its own).

The window's refresh is driven BY HAND with made-up status frames (the timer is
stopped), so the start / end of a run is exact, not timing-dependent.
"""

import dataclasses
import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from camera.apps import gui as G  # noqa: E402
from camera.apps.camera_view import CameraView  # noqa: E402
from camera.apps.gui import MainWindow  # noqa: E402
from camera.config import Config, load_config, save_config  # noqa: E402
from camera.net.protocol import apply_config_dict, config_to_dict  # noqa: E402
from camera.sim_system import build_sim_system  # noqa: E402

FRAME_W, FRAME_H = 1000, 800


def _app():
    return QApplication.instance() or QApplication([])


# --------------------------------------------------------------------------- #
# 1. the view: one transform for picture, overlays and clicks
# --------------------------------------------------------------------------- #
@pytest.fixture
def view():
    app = _app()
    v = CameraView()
    v.resize(800, 600)
    v.set_frame(np.zeros((480, 640), np.uint8))
    v.show()
    app.processEvents()
    yield v, app
    v.close()


@pytest.mark.parametrize("zoom", [None, (100, 50, 300, 200), (500, 10, 640, 400)])
def test_image_widget_mapping_round_trips_zoomed_and_not(view, zoom):
    v, _app_ = view
    v.set_zoom(zoom)
    for x, y in ((0, 0), (123.4, 77.7), (639, 479), (250.5, 120.25)):
        wx, wy = v.image_to_widget(x, y)
        bx, by = v.widget_to_image(wx, wy)
        assert (bx, by) == pytest.approx((x, y), abs=1e-9)


def test_the_zoom_rectangle_fills_the_view_with_its_aspect_kept(view):
    v, _app_ = view
    v.set_zoom((100, 50, 300, 200))          # 200 x 150 = 4:3, the view is 800 x 600
    assert v.image_to_widget(100, 50) == pytest.approx((0.0, 0.0))
    assert v.image_to_widget(300, 200) == pytest.approx((800.0, 600.0))
    assert v.image_to_widget(200, 125) == pytest.approx((400.0, 300.0))   # centre
    v.set_zoom((100, 50, 200, 200))          # 100 x 150: taller -> bars left/right
    x0, y0 = v.image_to_widget(100, 50)
    x1, y1 = v.image_to_widget(200, 200)
    assert (y0, y1) == pytest.approx((0.0, 600.0))
    assert x0 == pytest.approx(200.0) and x1 == pytest.approx(600.0)     # 4 px per px


def test_set_zoom_is_clipped_to_the_frame_and_none_is_the_whole_frame(view):
    v, _app_ = view
    v.set_zoom((-50, -20, 100, 90))
    assert v.zoom() == (0.0, 0.0, 100.0, 90.0)
    v.set_zoom((700, 500, 900, 900))         # entirely outside -> whole frame
    assert v.zoom() is None
    v.set_zoom(None)
    assert v.zoom() is None
    # whole frame = the old letter-box: 640 x 480 into 800 x 600 = scale 1.25
    assert v.image_to_widget(640, 480) == pytest.approx((800.0, 600.0))


@pytest.mark.parametrize("zoom", [None, (100, 50, 300, 200)])
def test_a_click_names_the_image_pixel_under_it(view, zoom):
    v, app = view
    v.set_zoom(zoom)
    got = []
    v.clicked.connect(lambda x, y: got.append((x, y)))
    target = (150.5, 80.5)
    wx, wy = v.image_to_widget(*target)
    QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier, QPointF(wx, wy).toPoint())
    app.processEvents()
    assert len(got) == 1
    # QTest clicks on whole widget pixels: within one widget px = < 1 image px
    tol = 1.0 / v._scale
    assert got[0][0] == pytest.approx(target[0], abs=tol)
    assert got[0][1] == pytest.approx(target[1], abs=tol)


def test_the_overlays_follow_the_zoom(view):
    """The overlays use the same _img_to_widget: the calibrated spot's cross
    lands on the spot's pixel zoomed or not (painted without errors)."""
    v, app = view
    cfg = Config()
    s = type("S", (), {"spot_calibrated": True, "spot_x": 200.0, "spot_y": 125.0,
                       "spot_found": False, "match_found": False})()
    v.set_overlay(s, cfg)
    for z in (None, (100, 50, 300, 200)):
        v.set_zoom(z)
        v.grab()                                # paints: overlays + clip
        c = v._img_to_widget(200.0, 125.0)
        assert v.widget_to_image(c.x(), c.y()) == pytest.approx((200.0, 125.0))
    assert v.image_to_widget(200, 125) == pytest.approx((400.0, 300.0))


def test_the_af_note_holds_single_clicks_and_a_double_click_asks_to_unzoom(view):
    v, app = view
    v.set_zoom((100, 50, 300, 200))
    v.set_zoom_note(G.AF_ZOOM_NOTE)
    clicks, unzoom = [], []
    v.clicked.connect(lambda x, y: clicks.append((x, y)))
    v.unzoom_requested.connect(lambda: unzoom.append(1))
    p = QPointF(*v.image_to_widget(150, 80)).toPoint()
    QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier, p)
    QTest.mouseDClick(v, Qt.LeftButton, Qt.NoModifier, p)
    app.processEvents()
    assert clicks == []            # no click-to-go in the middle of an autofocus
    assert unzoom == [1]
    v.grab()                       # the note paints
    v.set_zoom_note("")
    QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier, p)
    assert len(clicks) == 1


# --------------------------------------------------------------------------- #
# 2. the window: autofocus zoom, restore, option, manual un-zoom, button
# --------------------------------------------------------------------------- #
@pytest.fixture
def win():
    app = _app()
    brain, *_ = build_sim_system(Config())
    brain.start()
    w = MainWindow(brain, brain.cfg, remote=False)
    w.resize(1600, 1000)
    w.show()
    app.processEvents()
    w._timer.stop()                              # the test drives the refresh
    w.view.set_frame(np.zeros((FRAME_H, FRAME_W), np.uint8))
    yield w, brain, app
    w.close()
    brain.shutdown()
    app.processEvents()


def _st(brain, **kw):
    """A status frame: the brain's, with the given fields replaced."""
    base = dict(spot_calibrated=True, spot_x=400.0, spot_y=300.0,
                af_running=False, af_id=0, zcal_running=False, zcal_id=0)
    base.update(kw)
    return dataclasses.replace(brain.status(), **base)


def _region_rect(cx, cy, half=100, margin=15):
    """By hand: the +-100 px square search region (config default, rect)
    around the calibrated spot (vision.search_region's box: x1 = cx + half + 1),
    + max(10, 15 % of 100) = 15 px margin."""
    return (cx - half - margin, cy - half - margin, cx + half + 1 + margin,
            cy + half + 1 + margin)


def test_autofocus_start_zooms_to_the_spot_region_and_the_end_restores(win):
    w, brain, _app_ = win
    assert brain.cfg.autofocus.zoom_on_af is True          # default on
    w._refresh_zoom(_st(brain))
    assert w.view.zoom() is None
    w._refresh_zoom(_st(brain, af_running=True, af_id=1))
    assert w.view.zoom() == pytest.approx(_region_rect(400, 300))
    assert w.view.zoom_note() == G.AF_ZOOM_NOTE
    # a recalibrated spot during the run moves the zoom with it
    w._refresh_zoom(_st(brain, af_running=True, af_id=1, spot_x=500.0, spot_y=350.0))
    assert w.view.zoom() == pytest.approx(_region_rect(500, 350))
    w._refresh_zoom(_st(brain, af_id=1))                   # the run ended
    assert w.view.zoom() is None and w.view.zoom_note() == ""


def test_the_region_follows_the_search_region_settings_and_the_frame_edge(win):
    w, brain, _app_ = win
    sp = brain.cfg.spot
    sp.lookup_region_px, sp.lookup_region_y_px = 200, 50
    w._refresh_zoom(_st(brain, af_running=True, af_id=1, spot_x=100.0, spot_y=300.0))
    # x: 100 - 200 - 30 < 0 -> clipped to 0; margin 30 in x, max(10, 7.5) = 10 in y
    assert w.view.zoom() == pytest.approx((0.0, 240.0, 331.0, 361.0))


def test_before_the_first_calibration_the_zoom_is_around_the_frame_centre(win):
    w, brain, _app_ = win
    w._refresh_zoom(_st(brain, spot_calibrated=False, af_running=True, af_id=1))
    assert w.view.zoom() == pytest.approx(_region_rect(FRAME_W // 2, FRAME_H // 2))


def test_the_end_of_a_run_restores_the_users_own_zoom(win):
    w, brain, _app_ = win
    w.view.set_zoom((10, 10, 200, 150))
    w._refresh_zoom(_st(brain))
    w._refresh_zoom(_st(brain, af_running=True, af_id=3))
    assert w.view.zoom() == pytest.approx(_region_rect(400, 300))
    w._refresh_zoom(_st(brain, af_id=3))
    assert w.view.zoom() == (10.0, 10.0, 200.0, 150.0)


def test_option_off_leaves_the_view_alone(win):
    w, brain, _app_ = win
    brain.cfg.autofocus.zoom_on_af = False
    w.view.set_zoom((10, 10, 200, 150))
    w._refresh_zoom(_st(brain))
    w._refresh_zoom(_st(brain, af_running=True, af_id=1))
    assert w.view.zoom() == (10.0, 10.0, 200.0, 150.0)
    assert w.view.zoom_note() == ""
    w._refresh_zoom(_st(brain, af_id=1))
    assert w.view.zoom() == (10.0, 10.0, 200.0, 150.0)


def test_a_manual_unzoom_sticks_until_the_run_ends_and_the_next_run_zooms_again(win):
    w, brain, _app_ = win
    w._refresh_zoom(_st(brain))
    w._refresh_zoom(_st(brain, af_running=True, af_id=1))
    assert w.view.zoom() is not None
    w.view.unzoom_requested.emit()                  # = the double-click
    assert w.view.zoom() is None and w.view.zoom_note() == ""
    for _ in range(3):                              # still the same run
        w._refresh_zoom(_st(brain, af_running=True, af_id=1))
        assert w.view.zoom() is None
    # a queued run following straight on (af_id changes while running) zooms again
    w._refresh_zoom(_st(brain, af_running=True, af_id=2))
    assert w.view.zoom() == pytest.approx(_region_rect(400, 300))
    w._refresh_zoom(_st(brain, af_id=2))
    assert w.view.zoom() is None


def test_the_button_unzooms_during_the_af_zoom_too(win):
    w, brain, _app_ = win
    w._refresh_zoom(_st(brain))
    w._refresh_zoom(_st(brain, af_running=True, af_id=1))
    assert w.b_zoom.text() == G.ZOOM_OUT_TEXT
    w.b_zoom.click()
    assert w.view.zoom() is None and w.b_zoom.text() == G.ZOOM_IN_TEXT
    w._refresh_zoom(_st(brain, af_running=True, af_id=1))
    assert w.view.zoom() is None


def test_z_step_calibration_zooms_too(win):
    w, brain, _app_ = win
    w._refresh_zoom(_st(brain))
    w._refresh_zoom(_st(brain, zcal_running=True, zcal_id=1))
    assert w.view.zoom() == pytest.approx(_region_rect(400, 300))
    w._refresh_zoom(_st(brain, zcal_id=1))
    assert w.view.zoom() is None


def test_a_recovery_autofocus_after_a_lost_pattern_zooms_too(win):
    """autofocus_on_loss runs an ordinary numbered autofocus: af_running rises."""
    w, brain, _app_ = win
    w._refresh_zoom(_st(brain, af_id=4, fault="pattern lost ...; autofocus recovery running"))
    w._refresh_zoom(_st(brain, af_running=True, af_id=5))
    assert w.view.zoom() is not None


def test_the_manual_toggle_zooms_and_unzooms_and_survives_an_autofocus(win):
    w, brain, _app_ = win
    w._refresh_zoom(_st(brain))
    assert w.b_zoom.text() == G.ZOOM_IN_TEXT
    w.b_zoom.click()
    assert w.view.zoom() == pytest.approx(_region_rect(400, 300))
    assert w.b_zoom.text() == G.ZOOM_OUT_TEXT
    # the user's zoom follows a recalibrated spot
    w._refresh_zoom(_st(brain, spot_x=450.0))
    assert w.view.zoom() == pytest.approx(_region_rect(450, 300))
    # an autofocus in between: the user's spot zoom comes back after it
    w._refresh_zoom(_st(brain, spot_x=450.0, af_running=True, af_id=1))
    w._refresh_zoom(_st(brain, spot_x=450.0, af_id=1))
    assert w.view.zoom() == pytest.approx(_region_rect(450, 300))
    w.b_zoom.click()
    assert w.view.zoom() is None and w.b_zoom.text() == G.ZOOM_IN_TEXT


def test_the_zoom_on_af_box_is_in_the_metric_column_and_applies(win):
    w, brain, app = win
    box = w._form_widgets["autofocus"]["zoom_on_af"]
    metric = w._boxes["Metric"]
    assert metric.isAncestorOf(box)
    box.setChecked(False)
    w._apply_settings([("Autofocus", brain.cfg.autofocus), ("Spot", brain.cfg.spot)])
    assert brain.cfg.autofocus.zoom_on_af is False


def test_zoom_on_af_travels_in_the_ini_and_over_the_wire(tmp_path):
    cfg = Config()
    cfg.autofocus.zoom_on_af = False
    save_config(cfg, str(tmp_path / "c.ini"))
    assert load_config(str(tmp_path / "c.ini")).autofocus.zoom_on_af is False
    d = config_to_dict(cfg)
    assert d["autofocus"]["zoom_on_af"] is False
    back = Config()
    assert back.autofocus.zoom_on_af is True
    apply_config_dict(back, d)
    assert back.autofocus.zoom_on_af is False


# --------------------------------------------------------------------------- #
# 3. Save config buttons
# --------------------------------------------------------------------------- #
def test_every_save_config_button_saves_the_whole_config_and_logs(win, monkeypatch):
    w, brain, app = win
    calls = []

    def fake_save(path=None):
        calls.append(path)
        return "C:/somewhere/camera.ini"

    monkeypatch.setattr(brain, "save_config", fake_save)
    buttons = list(w._save_buttons)
    assert len(buttons) == 2                          # AutoFocus + Camera settings
    assert w.af_page.widget().isAncestorOf(buttons[0]) or \
        w.af_page.widget().isAncestorOf(buttons[1])
    buttons.append(w.spot_tab.b_save)                 # the Spot tab's, kept
    for b in buttons:
        assert "WHOLE" in b.toolTip() and "camera.ini" in b.toolTip()
        w.log.clear()
        b.click()
        app.processEvents()
        assert "camera settings saved to C:/somewhere/camera.ini (loaded at service start)" \
            in w.log.toPlainText()
    assert calls == [None, None, None]


def test_a_failed_save_is_logged_as_an_error(win, monkeypatch):
    w, brain, app = win

    def boom(path=None):
        raise OSError("disk full")

    monkeypatch.setattr(brain, "save_config", boom)
    w.log.clear()
    w._save_buttons[0].click()
    assert "save failed: disk full" in w.log.toPlainText()


# --------------------------------------------------------------------------- #
# 4. The AutoFocus tab in FOUR columns, every settings tab scrolls, the wheel
#    scrolls the page (Lukas 2026-09-29: the third column was cut off at the
#    bottom -- "four panels then... I want to see what is below")
# --------------------------------------------------------------------------- #
def _lab_size_window():
    """The window at about the lab screenshot's size (~1080 x 700 content),
    in the real app's style: Fusion + the theme stylesheet (run_app) and the
    Windows desktop font (Segoe UI 9; offscreen Qt would pick another)."""
    from PySide6.QtGui import QFont
    app = _app()
    app.setStyle("Fusion")
    if os.path.isdir(os.environ.get("QT_QPA_FONTDIR", "")):
        app.setFont(QFont("Segoe UI", 9))
    brain, *_ = build_sim_system(Config())
    brain.start()
    w = MainWindow(brain, brain.cfg, remote=False)
    # the real window's look (run_app sets it on the app; here on the window
    # only, so the other tests keep theirs): sizes depend on it
    from camera.apps import theme as T
    w.setStyleSheet(T.build_stylesheet())
    w.resize(1080, 760)
    w.show()
    app.processEvents()
    w._timer.stop()
    return w, brain, app


def test_the_autofocus_tab_is_four_balanced_columns_that_fit_the_lab_screen():
    w, brain, app = _lab_size_window()
    try:
        w.tabs.setCurrentWidget(w.af_page)
        app.processEvents()
        b = w._boxes
        x = {t: b[t].mapTo(w, b[t].rect().topLeft()).x()
             for t in ("Metric", "Routine", "Park", "Scan & continuous")}
        assert x["Metric"] < x["Routine"] < x["Park"] < x["Scan & continuous"]
        # the pairs that share a column
        for top, below in (("Routine", "One way"), ("Park", "Autofocus exposure"),
                           ("Scan & continuous", "Z step calibration")):
            assert b[below].mapTo(w, b[below].rect().topLeft()).x() == \
                b[top].mapTo(w, b[top].rect().topLeft()).x()
        # no sideways scrolling at ~1080 px: all four columns are on screen
        # (a width measurement: needs the real fonts, see conftest.py)
        if os.path.isdir(os.environ.get("QT_QPA_FONTDIR", "")):
            assert w.af_page.horizontalScrollBar().maximum() == 0
        # and the bottom of the tallest column can be reached by scrolling
        area = w.af_page
        inner = area.widget()
        zc = b["Z step calibration"]
        bottom = zc.mapTo(inner, zc.rect().bottomLeft()).y()
        assert bottom <= inner.height()
        assert area.verticalScrollBar().maximum() >= inner.height() - area.viewport().height()
        area.ensureWidgetVisible(w._form_widgets["autofocus"]["zcal_step_um"])
        app.processEvents()
        assert w._form_widgets["autofocus"]["zcal_step_um"].visibleRegion().isEmpty() is False
    finally:
        w.close()
        brain.shutdown()


def test_every_tab_is_a_resizable_scroll_area():
    from PySide6.QtWidgets import QScrollArea
    w, brain, app = _lab_size_window()
    try:
        for i in range(w.tabs.count()):
            page = w.tabs.widget(i)
            assert isinstance(page, QScrollArea), w.tabs.tabText(i)
            assert page.widgetResizable(), w.tabs.tabText(i)
    finally:
        w.close()
        brain.shutdown()


def test_the_wheel_over_an_unfocused_number_box_scrolls_the_page_not_the_value():
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent
    w, brain, app = _lab_size_window()
    try:
        w.tabs.setCurrentWidget(w.af_page)
        app.processEvents()
        box = w._form_widgets["autofocus"]["steps"]
        before = box.value()
        bar = w.af_page.verticalScrollBar()
        bar.setValue(0)
        pos = QPointF(box.width() / 2, box.height() / 2)
        ev = QWheelEvent(pos, QPointF(box.mapToGlobal(pos.toPoint())), QPoint(0, 0),
                         QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                         Qt.NoScrollPhase, False)
        QApplication.sendEvent(box, ev)
        app.processEvents()
        assert box.value() == before           # the value did NOT change
        assert bar.value() > 0                 # the page moved down
        # focused (clicked into): the wheel is the box's again
        box.setFocus()
        app.processEvents()
        if box.hasFocus():                     # offscreen may refuse focus
            QApplication.sendEvent(box, ev)
            assert box.value() != before
    finally:
        w.close()
        brain.shutdown()


def test_the_z_step_calibration_distances_are_labelled_with_the_z_unit():
    w, brain, app = _lab_size_window()
    try:
        w._label_z_fields("um")
        labs = w._form_labels["autofocus"]
        for name, text in (("zcal_step_v", "zcal_step (um)"),
                           ("zcal_start_offset_v", "zcal_start_offset (um)"),
                           ("zcal_max_travel_v", "zcal_max_travel (um)"),
                           ("fine_step_v", "fine_step (um)")):
            assert labs[name].text().replace("​", "") == text
        assert "zcal_step_v" in brain.cfg.autofocus.__dataclass_fields__   # name kept
    finally:
        w.close()
        brain.shutdown()


def test_no_settings_tab_needs_sideways_scrolling_on_the_lab_screen():
    if not os.path.isdir(os.environ.get("QT_QPA_FONTDIR", "")):
        pytest.skip("width measurement needs the real fonts (conftest.py)")
    w, brain, app = _lab_size_window()
    try:
        for i in range(w.tabs.count()):
            name = w.tabs.tabText(i)
            if name not in ("AutoFocus", "Pattern", "Camera settings", "Positioner"):
                continue
            w.tabs.setCurrentIndex(i)
            app.processEvents()
            assert w.tabs.widget(i).horizontalScrollBar().maximum() == 0, name
    finally:
        w.close()
        brain.shutdown()


def test_the_display_is_stretched_while_the_autofocus_exposure_is_active():
    """Lukas 2026-09-29: at the 65 us AF exposure the zoomed view looked black
    (background ~3, a dim defocused spot). Stretch the DISPLAY only."""
    import numpy as np
    pytest.importorskip("PySide6")
    from camera.apps.camera_view import CameraView
    frame = np.full((120, 160), 3, np.uint8)
    frame[55:65, 75:85] = 40                        # a dim defocused spot
    v = CameraView()
    v.set_frame(frame)
    assert int(v._buf.max()) == 40                  # normal: as measured
    v.set_stretch(True)
    v.set_frame(frame)
    assert int(v._buf.max()) == 255 and int(np.median(v._buf)) == 0
    assert int(frame.max()) == 40                   # the data itself untouched
    flat = np.full((120, 160), 100, np.uint8)
    v.set_frame(flat)
    assert int(v._buf.max()) == 100                 # noise is not stretched
