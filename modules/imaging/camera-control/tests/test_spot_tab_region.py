"""The Spot tab judges a grabbed frame like the service: only in the search
region around the calibrated laser (Lukas 2026-11-10, screenshot: a bright
illuminated patch 125 px from the calibrated laser was reported as "spot
found" and the zoom centred on it, right after calibrating)."""

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from camera.apps.spot_tab import analyse   # noqa: E402
from camera.config import Config            # noqa: E402


def _frame():
    g = np.full((1080, 1440), 20, np.uint8)
    yy, xx = np.ogrid[:1080, :1440]
    g[(xx - 384) ** 2 + (yy - 356) ** 2 <= 22 ** 2] = 255     # the laser, ~1500 px
    g[290:600, 420:560] = 230                                 # lit patch, ~43 000 px
    return g


def _spot(calibrated=True):
    sp = Config().spot
    sp.thr_lower, sp.thr_upper = 200, 255
    sp.lookup_region_px, sp.lookup_region_y_px, sp.search_shape = 100, 0, "rect"
    sp.max_area_px = 0
    if calibrated:
        sp.ref_x, sp.ref_y, sp.ref_set = 384.1, 355.6, True
    return sp


def test_the_calibrated_laser_is_found_not_the_bigger_patch():
    a = analyse(_frame(), _spot())
    rep = a["spot"]
    assert rep.found
    assert np.hypot(rep.cx - 384, rep.cy - 356) < 2.0, (rep.cx, rep.cy)


def test_nothing_outside_the_region_is_counted():
    sp = _spot()
    sp.ref_x, sp.ref_y = 1200.0, 900.0          # region far from both
    a = analyse(_frame(), sp)
    assert not a["spot"].found
    assert a["blobs"] == 0 and a["too_large"] == 0


def test_uncalibrated_searches_around_the_frame_centre():
    g = _frame()
    g[530:550, 710:730] = 255                     # a spot at the centre
    a = analyse(g, _spot(calibrated=False))
    assert a["spot"].found
    assert np.hypot(a["spot"].cx - 719.5, a["spot"].cy - 539.5) < 2.0


def test_the_zoom_centres_on_the_calibrated_position():
    from PySide6.QtWidgets import QApplication
    from camera.apps.spot_tab import SpotTab

    app = QApplication.instance() or QApplication([])
    cfg = Config()
    sp = _spot()
    cfg.spot = sp
    g = _frame()

    class Ctrl:                                   # the tab only reads here
        def set_config(self, *_a, **_k):
            pass

    tab = SpotTab(Ctrl(), cfg, lambda *a: None, lambda: g)
    tab._gray = g
    tab._reanalyse()
    app.processEvents()
    from camera.apps.spot_tab import ZOOM_HALF
    ox, oy = tab.zoom._origin                     # top-left of the 96x96 crop
    assert (ox + ZOOM_HALF, oy + ZOOM_HALF) == (384, 356), (ox, oy)
