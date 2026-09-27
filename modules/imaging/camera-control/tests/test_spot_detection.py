"""Spot detection filters and search regions (vision.find_spot), no hardware.

The key case is the lab frame of 2026-09-14 (63x): saturated illumination in a
corner of the frame (a huge blob touching the edge) and the real laser spot, a
small blob in the middle. "Largest blob" picked the corner.
"""

import cv2
import numpy as np
import pytest

from camera import vision as V


def lab_like_frame():
    h, w = 1096, 1936
    g = np.full((h, w), 90, np.uint8)
    g[:470, :880] = 255                                  # saturated corner patch
    cv2.circle(g, (972, 463), 20, 255, -1)              # the laser spot, ~1300 px2
    cv2.circle(g, (1500, 800), 2, 255, -1)              # a hot speck
    return g


def test_largest_blob_alone_picks_the_illumination_patch():
    rep = V.find_spot(lab_like_frame(), 251, 255, True, 0, None, 4)
    assert rep.found and rep.area > 100_000             # the old behaviour


@pytest.mark.parametrize("max_area, border", [(20000, False), (0, True), (20000, True)])
def test_max_area_or_edge_rejection_finds_the_laser(max_area, border):
    rep = V.find_spot(lab_like_frame(), 251, 255, True, 0, None, 4, max_area, border)
    assert rep.found
    assert (rep.cx, rep.cy) == pytest.approx((972, 463), abs=0.5)
    assert 1000 < rep.area < 1500


def test_edge_rejection_uses_the_full_frame_not_the_search_box():
    """A spot near the edge of the SEARCH BOX but far from the frame edge counts."""
    g = np.full((400, 600), 50, np.uint8)
    cv2.circle(g, (300, 200), 6, 255, -1)
    rep = V.find_spot(g, 200, 255, True, 8, (300, 200), 4, 0, True)   # box ~ the spot itself
    assert rep.found


def test_rect_region_has_separate_half_width_and_height():
    g = np.full((400, 600), 50, np.uint8)
    cv2.circle(g, (360, 200), 5, 255, -1)                 # 60 px right of the centre
    centre = (300, 200)
    wide = V.find_spot(g, 200, 255, True, 80, centre, 4, 0, True, "rect", 20)
    narrow = V.find_spot(g, 200, 255, True, 40, centre, 4, 0, True, "rect", 200)
    assert wide.found and not narrow.found


def test_circle_region_excludes_the_corners_of_its_box():
    g = np.full((400, 600), 50, np.uint8)
    cv2.circle(g, (350, 250), 4, 255, -1)                 # 50,50 away: dist 70.7
    centre = (300, 200)
    assert V.find_spot(g, 200, 255, True, 60, centre, 4, 0, True, "rect").found
    assert not V.find_spot(g, 200, 255, True, 60, centre, 4, 0, True, "circle").found
    assert V.find_spot(g, 200, 255, True, 75, centre, 4, 0, True, "circle").found


def test_search_region_geometry():
    r = V.search_region((100, 50), 30, 10, "rect", (640, 480))
    assert r["shape"] == "rect" and r["half"] == (30, 10) and r["box"] == (70, 40, 131, 61)
    c = V.search_region((10, 10), 30, 99, "circle", (640, 480))
    assert c["shape"] == "circle" and c["half"] == (30, 30) and c["box"][:2] == (0, 0)
    assert V.search_region((10, 10), 0, 0) is None


def _spot_with_intruder(merge: bool):
    g = np.full((400, 600), 50, np.uint8)
    cv2.circle(g, (300, 200), 10, 255, -1)                # the laser spot, ~317 px2
    if merge:
        cv2.rectangle(g, (308, 150), (360, 260), 255, -1)  # touches the spot's edge
    else:
        cv2.rectangle(g, (330, 150), (380, 260), 255, -1)  # separate, much bigger
    return g


@pytest.mark.parametrize("merge", [False, True])
def test_object_in_the_search_region_is_not_the_spot(merge):
    """Lukas, 2026-09-14: when the search region reaches another object, it was
    taken for the spot (bigger blob) or added to it (touching). Only what is
    symmetric about the calibrated centre counts."""
    g, centre = _spot_with_intruder(merge), (300, 200)
    plain = V.find_spot(g, 200, 255, True, 80, centre, 4, 20000, True)
    assert plain.area > 1000                              # the old behaviour
    sym = V.find_spot(g, 200, 255, True, 80, centre, 4, 20000, True, symmetric=True)
    assert sym.found
    assert 250 < sym.area < 330                           # the disc only
    # the live centroid (information only) may pick up the touching object's
    # rim: a couple of px at most, never the object's own centre
    assert (sym.cx, sym.cy) == pytest.approx(centre, abs=2.0 if merge else 0.6)


def test_symmetric_check_still_reports_a_drifted_spot():
    g = np.full((400, 600), 50, np.uint8)
    cv2.circle(g, (303, 200), 12, 255, -1)                # drifted 3 px from the calibration
    rep = V.find_spot(g, 200, 255, True, 60, (300, 200), 4, 0, True, symmetric=True)
    assert rep.found
    assert rep.cx == pytest.approx(303, abs=0.5)          # the live centroid shows the drift
    assert rep.area < np.pi * 12 ** 2                     # overlap with its mirror only
    far = np.full((400, 600), 50, np.uint8)
    cv2.circle(far, (340, 200), 6, 255, -1)               # an object off-centre, no spot
    assert not V.find_spot(far, 200, 255, True, 60, (300, 200), 4, 0, True,
                           symmetric=True).found
