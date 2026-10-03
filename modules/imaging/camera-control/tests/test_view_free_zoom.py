"""Free zoom and pan on the live image (ROADMAP 2026-10-02).

Before, the view zoomed only to the spot search region. Now: the mouse wheel
zooms about the cursor (the image point under the cursor stays under it),
the middle button or Space + left drag pans (the left button alone is taken:
click-to-go, template ROI, scan rectangle), "Fit" shows the whole frame and
"1:1 pixels" one camera pixel per screen pixel, and the zoom level is shown
on the view. The autofocus zoom lies over the user's zoom and gives it back.

The zoom is DISPLAY only: the camera, the brain and every client see nothing
of it, so it works the same in a viewer window.
"""

import dataclasses
import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from camera.apps import gui as G  # noqa: E402
from camera.apps.camera_view import WHEEL_STEP, CameraView  # noqa: E402
from camera.apps.gui import MainWindow  # noqa: E402
from camera.config import Config  # noqa: E402
from camera.sim_system import build_sim_system  # noqa: E402


def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view():
    app = _app()
    v = CameraView()
    v.resize(800, 600)
    v.set_frame(np.zeros((480, 640), np.uint8))     # whole frame: 1.25 px per px
    v.show()
    app.processEvents()
    yield v, app
    v.close()


def _wheel(v, wx, wy, notches=1):
    """A wheel turn of ``notches`` (positive = away from the user = zoom in)."""
    pos = QPointF(wx, wy)
    ev = QWheelEvent(pos, QPointF(v.mapToGlobal(pos.toPoint())), QPoint(0, 0),
                     QPoint(0, 120 * notches), Qt.NoButton, Qt.NoModifier,
                     Qt.NoScrollPhase, False)
    QApplication.sendEvent(v, ev)


def _mouse(v, kind, button, wx, wy, buttons=None):
    pos = QPointF(wx, wy)
    ev = QMouseEvent(kind, pos, QPointF(v.mapToGlobal(pos.toPoint())), button,
                     button if buttons is None else buttons, Qt.NoModifier)
    QApplication.sendEvent(v, ev)


def _drag(v, button, start, end):
    _mouse(v, QEvent.MouseButtonPress, button, *start)
    mid = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    _mouse(v, QEvent.MouseMove, Qt.NoButton, *mid, buttons=button)
    _mouse(v, QEvent.MouseMove, Qt.NoButton, *end, buttons=button)
    _mouse(v, QEvent.MouseButtonRelease, button, *end, buttons=Qt.NoButton)


# --------------------------------------------------------------------------- #
# the wheel
# --------------------------------------------------------------------------- #
def test_the_wheel_zooms_in_about_the_cursor(view):
    v, _app_ = view
    assert v.zoom() is None and v.zoom_level() == pytest.approx(1.25)
    under = v.widget_to_image(300, 200)
    _wheel(v, 300, 200)
    assert v.zoom() is not None
    assert v.zoom_level() == pytest.approx(1.25 * WHEEL_STEP)
    # the image point under the cursor is still under it
    assert v.image_to_widget(*under) == pytest.approx((300.0, 200.0), abs=1e-6)
    _wheel(v, 300, 200, 3)
    assert v.zoom_level() == pytest.approx(1.25 * WHEEL_STEP ** 4)
    assert v.image_to_widget(*under) == pytest.approx((300.0, 200.0), abs=1e-6)


def test_the_zoomed_view_fills_the_widget_no_bars(view):
    v, _app_ = view
    _wheel(v, 400, 300, 2)
    assert v.image_to_widget(*v.widget_to_image(0, 0)) == pytest.approx((0.0, 0.0))
    x0, y0, x1, y1 = v.zoom()
    assert v.image_to_widget(x0, y0) == pytest.approx((0.0, 0.0), abs=1e-6)
    assert v.image_to_widget(x1, y1) == pytest.approx((800.0, 600.0), abs=1e-6)


def test_near_the_edge_the_view_slides_instead_of_showing_outside(view):
    v, _app_ = view
    _wheel(v, 2, 2, 2)                    # at the top-left corner of the picture
    x0, y0, x1, y1 = v.zoom()
    assert x0 >= 0 and y0 >= 0 and x1 <= 640 and y1 <= 480
    assert v.zoom_level() == pytest.approx(1.25 * WHEEL_STEP ** 2)


def test_zooming_out_stops_at_fit_or_1_to_1_and_in_at_a_limit(view):
    """Out: down to the whole frame -- or, for a frame SMALLER than the view
    (fit = magnified), down to one camera pixel per screen pixel. In: to
    MAX_ZOOM screen px per camera px."""
    v, _app_ = view
    _wheel(v, 300, 200, 2)
    _wheel(v, 300, 200, -10)
    assert v.zoom() is None and v.zoom_level() == pytest.approx(1.0)   # fit is 1.25
    v.fit()
    assert v.zoom_level() == pytest.approx(1.25)
    _wheel(v, 300, 200, 60)
    from camera.apps.camera_view import MAX_ZOOM
    assert v.zoom_level() == pytest.approx(MAX_ZOOM)
    _wheel(v, 300, 200, 3)                # no further
    assert v.zoom_level() == pytest.approx(MAX_ZOOM)


def test_a_big_frame_zooms_out_only_to_fit():
    app = _app()
    v = CameraView()
    v.resize(480, 360)                                 # the view's minimum size
    v.set_frame(np.zeros((1440, 1920), np.uint8))      # fit = 0.25
    v.show(); app.processEvents()
    try:
        _wheel(v, 240, 180, 3)
        _wheel(v, 240, 180, -20)
        assert v.zoom() is None and v.zoom_level() == pytest.approx(0.25)
        assert v.zoom_text() == "fit 25 %"
    finally:
        v.close()


def test_every_user_zoom_says_so(view):
    v, _app_ = view
    got = []
    v.user_zoomed.connect(lambda: got.append(1))
    _wheel(v, 300, 200)
    v.fit()
    v.one_to_one()
    assert len(got) == 3
    v.set_zoom((10, 10, 100, 100))        # programmatic (the window): silent
    assert len(got) == 3


# --------------------------------------------------------------------------- #
# pan
# --------------------------------------------------------------------------- #
def test_middle_drag_pans_the_picture_with_the_mouse(view):
    v, app = view
    _wheel(v, 400, 300, 3)
    p = v.widget_to_image(400, 300)
    _drag(v, Qt.MiddleButton, (400, 300), (350, 260))
    # the picture moved WITH the mouse: what was at (400, 300) is now at (350, 260)
    assert v.image_to_widget(*p) == pytest.approx((350.0, 260.0), abs=1e-6)


def test_pan_stops_at_the_frame_edge(view):
    v, _app_ = view
    _wheel(v, 400, 300, 3)
    _drag(v, Qt.MiddleButton, (100, 100), (790, 590))     # far to the bottom-right
    x0, y0, x1, y1 = v.zoom()
    assert (x0, y0) == pytest.approx((0.0, 0.0))
    assert x1 < 640 and y1 < 480


def test_pan_on_the_whole_frame_does_nothing(view):
    v, _app_ = view
    _drag(v, Qt.MiddleButton, (400, 300), (300, 200))
    assert v.zoom() is None


def test_space_plus_left_drag_pans_and_is_not_a_click(view):
    v, _app_ = view
    _wheel(v, 400, 300, 3)
    clicks = []
    v.clicked.connect(lambda x, y: clicks.append((x, y)))
    p = v.widget_to_image(400, 300)
    QApplication.sendEvent(v, QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier, " "))
    _drag(v, Qt.LeftButton, (400, 300), (420, 330))
    QApplication.sendEvent(v, QKeyEvent(QEvent.KeyRelease, Qt.Key_Space, Qt.NoModifier, " "))
    assert clicks == []
    assert v.image_to_widget(*p) == pytest.approx((420.0, 330.0), abs=1e-6)
    # Space released: the left button is click-to-go again
    QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier, QPoint(200, 200))
    assert len(clicks) == 1


def test_middle_drag_does_not_draw_a_scan_rectangle_or_roi(view):
    v, _app_ = view
    _wheel(v, 400, 300, 2)
    got = []
    v.scan_area_selected.connect(lambda *a: got.append(a))
    v.roi_selected.connect(lambda *a: got.append(a))
    v.set_scan_mode(True)
    _drag(v, Qt.MiddleButton, (300, 300), (400, 400))
    v.set_roi_mode(True)
    _drag(v, Qt.MiddleButton, (300, 300), (400, 400))
    assert got == []


@pytest.mark.parametrize("notches,pan", [(1, (0, 0)), (3, (-120, 75)), (5, (200, -150))])
def test_a_click_on_a_zoomed_and_panned_view_names_the_right_pixel(view, notches, pan):
    v, app = view
    _wheel(v, 250, 380, notches)
    _drag(v, Qt.MiddleButton, (400, 300), (400 + pan[0], 300 + pan[1]))
    got = []
    v.clicked.connect(lambda x, y: got.append((x, y)))
    x0, y0, x1, y1 = v.zoom()
    target = (x0 + 0.37 * (x1 - x0), y0 + 0.61 * (y1 - y0))
    wx, wy = v.image_to_widget(*target)
    QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier, QPointF(wx, wy).toPoint())
    app.processEvents()
    assert len(got) == 1
    tol = 1.0 / v._scale                  # QTest clicks on whole widget px
    assert got[0] == pytest.approx(target, abs=tol)


# --------------------------------------------------------------------------- #
# Fit, 1:1, the level on the view
# --------------------------------------------------------------------------- #
def test_fit_and_one_to_one(view):
    v, _app_ = view
    _wheel(v, 300, 200, 4)
    v.fit()
    assert v.zoom() is None and v.zoom_level() == pytest.approx(1.25)
    # 1:1 = one camera pixel per DEVICE pixel (offscreen: device px = widget
    # px). This frame is smaller than the view, so 1:1 shows all of it, small.
    v.one_to_one()
    assert v.zoom_level() == pytest.approx(1.0)
    assert v.zoom() is None
    assert v.image_to_widget(0, 0) == pytest.approx((80.0, 60.0))     # centred
    assert v.zoom_text() == "100 %"


def test_one_to_one_keeps_the_centre_of_a_zoomed_view():
    app = _app()
    v = CameraView()
    v.resize(480, 360)                                 # the view's minimum size
    v.set_frame(np.zeros((1440, 1920), np.uint8))      # big frame: fit = 0.25
    v.show(); app.processEvents()
    try:
        v.set_zoom((200, 200, 440, 380))               # 240 x 180 = 2x, centre (320, 290)
        assert v.zoom_level() == pytest.approx(2.0)
        v.one_to_one()
        assert v.zoom_level() == pytest.approx(1.0)
        x0, y0, x1, y1 = v.zoom()
        assert (x1 - x0, y1 - y0) == pytest.approx((480.0, 360.0))
        assert ((x0 + x1) / 2, (y0 + y1) / 2) == pytest.approx((320.0, 290.0))
    finally:
        v.close()


def test_the_level_is_painted_on_the_view(view):
    v, _app_ = view
    assert v.zoom_text() == "fit 125 %"
    _wheel(v, 300, 200, 2)
    assert v.zoom_text() == f"{1.25 * WHEEL_STEP ** 2 * 100:.0f} %"
    v.grab()                                           # paints without errors


# --------------------------------------------------------------------------- #
# the window: buttons, the autofocus zoom over the user's zoom
# --------------------------------------------------------------------------- #
FRAME_W, FRAME_H = 1000, 800


@pytest.fixture
def win():
    app = _app()
    brain, *_ = build_sim_system(Config())
    brain.start()
    w = MainWindow(brain, brain.cfg, remote=False)
    w.resize(1600, 1000)
    w.show()
    app.processEvents()
    w._timer.stop()
    w.view.set_frame(np.zeros((FRAME_H, FRAME_W), np.uint8))
    yield w, brain, app
    w.close()
    brain.shutdown()
    app.processEvents()


def _st(brain, **kw):
    base = dict(spot_calibrated=True, spot_x=400.0, spot_y=300.0,
                af_running=False, af_id=0, zcal_running=False, zcal_id=0)
    base.update(kw)
    return dataclasses.replace(brain.status(), **base)


def test_fit_and_one_to_one_buttons_are_view_only(win):
    w, brain, app = win
    from camera.apps.control_bar import ALWAYS_PROPERTY
    for b in (w.b_fit, w.b_one):
        assert b.property(ALWAYS_PROPERTY)            # usable in a viewer window
    w.b_one.click()
    assert w.view.zoom_level() == pytest.approx(1.0)
    w.b_fit.click()
    assert w.view.zoom() is None


def test_the_autofocus_zoom_gives_back_a_wheel_zoom(win):
    w, brain, _app_ = win
    _wheel(w.view, 200, 150, 3)
    mine = w.view.zoom()
    assert mine is not None
    w._refresh_zoom(_st(brain))
    w._refresh_zoom(_st(brain, af_running=True, af_id=1))
    assert w.view.zoom() != pytest.approx(mine)        # the spot region now
    assert w.view.zoom_note() == G.AF_ZOOM_NOTE
    w._refresh_zoom(_st(brain, af_id=1))
    assert w.view.zoom() == pytest.approx(mine)


def test_a_wheel_during_the_af_zoom_takes_over_and_is_kept(win):
    w, brain, _app_ = win
    w._refresh_zoom(_st(brain))
    w._refresh_zoom(_st(brain, af_running=True, af_id=1))
    _wheel(w.view, 300, 200, 1)                        # the user zooms further in
    mine = w.view.zoom()
    assert w.view.zoom_note() == ""
    w._refresh_zoom(_st(brain, af_running=True, af_id=1))
    assert w.view.zoom() == pytest.approx(mine)        # not snapped back
    w._refresh_zoom(_st(brain, af_id=1))
    assert w.view.zoom() == pytest.approx(mine)        # and kept after the run


def test_a_wheel_zoom_ends_the_follow_the_spot_toggle(win):
    w, brain, _app_ = win
    w._refresh_zoom(_st(brain))
    w.b_zoom.click()                                   # Zoom to spot region
    assert w.b_zoom.text() == G.ZOOM_OUT_TEXT
    _wheel(w.view, 300, 200, 1)
    mine = w.view.zoom()
    assert w.b_zoom.text() == G.ZOOM_IN_TEXT
    w._refresh_zoom(_st(brain, spot_x=450.0))          # the spot moved: view stays
    assert w.view.zoom() == pytest.approx(mine)
