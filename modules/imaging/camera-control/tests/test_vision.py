"""Vision engine unit tests (pure functions, no hardware)."""

import cv2
import numpy as np

from camera import vision as V


def _scene(spot=(200, 150), tpl=(400, 300)):
    img = np.zeros((480, 640), np.uint8)
    yy, xx = np.ogrid[:480, :640]
    g = 255 * np.exp(-(((xx - spot[0]) ** 2 + (yy - spot[1]) ** 2) / (2 * 6.0 ** 2)))
    img = np.maximum(img, g.astype(np.uint8))
    cv2.rectangle(img, (tpl[0] - 10, tpl[1] - 10), (tpl[0] + 10, tpl[1] + 10), 120, -1)
    cv2.circle(img, (tpl[0] + 6, tpl[1] + 6), 3, 90, -1)
    return img


def test_find_spot_center_of_mass():
    img = _scene(spot=(200, 150))
    r = V.find_spot(img, 200, 255, True)
    assert r.found
    assert abs(r.cx - 200) < 0.5
    assert abs(r.cy - 150) < 0.5
    assert r.area > 0


def test_find_spot_none_when_dark():
    r = V.find_spot(np.zeros((100, 100), np.uint8), 200, 255, True)
    assert not r.found


def test_template_match_locates_and_scores():
    img = _scene(tpl=(400, 300))
    tpl = img[280:320, 380:420].copy()
    m = V.match_template(img, tpl, 0.6)
    assert m.found
    assert abs(m.x - 400) < 1.0
    assert abs(m.y - 300) < 1.0
    assert m.score > 0.9


def test_focus_metric_edges_peaks_at_focus():
    img = _scene()
    sharp = V.focus_metric(img, "edges")
    blurred = V.focus_metric(cv2.GaussianBlur(img, (0, 0), 4), "edges")
    assert sharp > blurred   # sharper image scores higher


def _spot(sigma, peak=255):
    yy, xx = np.ogrid[:200, :200]
    g = peak * np.exp(-(((xx - 100) ** 2 + (yy - 100) ** 2) / (2 * sigma ** 2)))
    return g.astype(np.uint8)


def test_focus_metric_spot_area_grows_with_defocus():
    # A real defocused spot spreads at ~constant peak -> more pixels over the
    # threshold -> bigger area.  So the metric is MINIMISED at best focus.
    tight = V.focus_metric(_spot(3.0), "spot_area")
    wide = V.focus_metric(_spot(6.0), "spot_area")
    assert wide > tight
    assert not V.focus_is_maximised("spot_area")


def test_scanning_array_geometry():
    off = V.scanning_array_pixel_offsets(3, 3, 1.0, 1.0, 0.0, 0.5, 0.5)
    assert off.shape == (3, 3, 2)
    # centre point is at (0, 0); corner at +/- 1 um -> +/- 2 px at 0.5 um/px.
    assert np.allclose(off[1, 1], [0, 0])
    assert np.allclose(off[2, 2], [2.0, 2.0])
    assert np.allclose(off[0, 0], [-2.0, -2.0])


def test_pin_array_distance_direction():
    off = V.scanning_array_pixel_offsets(3, 3, 1.0, 1.0, 0.0, 0.5, 0.5)
    # template at (400,300); array centre pinned onto the spot at (300,200)
    geo = V.pin_array_and_distance((400, 300), (-100, -100), off, 2, 2, (300, 200))
    # selected corner (2,2) sits +2px from centre -> (302,202); minus spot(300,200)
    assert np.allclose(geo.point_minus_spot_px, [2.0, 2.0])
    assert geo.spot_at_index == (1, 1)   # spot nearest the centre point


def test_best_focus_parabola_vertex():
    z = [0, 1, 2, 3, 4]
    m = [5.0, 2.0, 1.0, 2.0, 5.0]          # minimum at z=2
    assert abs(V.best_focus_from_sweep(z, m, maximise=False) - 2.0) < 1e-6
    m2 = [1.0, 4.0, 9.0, 4.0, 1.0]         # maximum at z=2
    assert abs(V.best_focus_from_sweep(z, m2, maximise=True) - 2.0) < 1e-6
