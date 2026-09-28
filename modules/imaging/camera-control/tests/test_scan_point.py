"""Scanning the camera's array from scan-core: select a point per step, wait
until the stabiliser has SETTLED on that point, record where the laser is
relative to the main template."""

import math
import time

import pytest

from camera.config import Config
from camera.net.describe import build_manifest
from camera.sim_system import build_sim_system


def _wait(cond, timeout=8.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


def _capture_and_track(brain, cam):
    tcx, tcy = cam.template_center_px()
    brain.capture_reference((tcx, tcy, 60, 60))
    brain.set_tracking(True)
    assert _wait(lambda: brain.status().match_found)


@pytest.fixture
def system():
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, cam, xy, z = build_sim_system(cfg)
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    brain.calibrate_spot(10)
    yield brain, cam
    brain.shutdown()


def test_one_index_at_a_time_keeps_the_other_and_clamps(system):
    brain, cam = system
    sc = brain.cfg.scanning
    sc.points_x, sc.points_y = 3, 3
    assert brain.set_selected_index(2, 1) == (2, 1)
    assert brain.set_selected_index(ix=0) == (0, 1)          # iy kept
    assert brain.set_selected_index(iy=2) == (0, 2)          # ix kept
    assert brain.set_selected_index(ix=99, iy=-5) == (2, 0)  # clamped to the array
    assert brain.set_selected_index(ix=1.0) == (1, 0)        # a scan sends floats


def test_point_settled_needs_a_full_window_at_the_new_point(system):
    brain, cam = system
    _capture_and_track(brain, cam)
    brain.set_selected_index(0, 0)
    brain.set_stabilize(True)
    assert _wait(lambda: brain.status().point_settled, timeout=10.0)

    # Selecting another point clears it at once, before any frame could confirm
    brain.set_selected_index(2, 2)
    s = brain.status()
    assert not s.point_settled or (s.selected_index_x, s.selected_index_y) != (2, 2)
    assert _wait(lambda: (brain.status().selected_index_x, brain.status().selected_index_y)
                 == (2, 2) and brain.status().point_settled, timeout=10.0)
    s = brain.status()
    tol_px = brain.cfg.stabilizer.stable_radius_um / brain.cfg.image.pixel_size_x_um
    assert math.hypot(s.point_minus_spot_x, s.point_minus_spot_y) <= tol_px + 1.0

    brain.set_stabilize(False)
    assert _wait(lambda: not brain.status().point_settled)


def test_spot_from_template_is_spot_minus_main_template_in_um(system):
    brain, cam = system
    assert math.isnan(brain.status().spot_from_template_x_um)   # nothing tracked yet
    _capture_and_track(brain, cam)
    s = brain.status()
    px_x, px_y = brain.cfg.image.pixel_size_x_um, brain.cfg.image.pixel_size_y_um
    assert s.spot_from_template_x_um == pytest.approx((s.spot_x - s.anchor_x) * px_x)
    assert s.spot_from_template_y_um == pytest.approx((s.spot_y - s.anchor_y) * px_y)


def test_manifest_offers_index_axes_and_um_detectors(system):
    brain, cam = system
    brain.cfg.scanning.points_x, brain.cfg.scanning.points_y = 5, 4
    params = {p["id"]: p for p in build_manifest(brain)["parameters"]}
    ix, iy = params["scan_ix"], params["scan_iy"]
    assert (ix["kind"], ix["type"], ix["min"], ix["max"]) == ("control", "int", 0, 4)
    assert iy["max"] == 3                                     # bounds follow the array
    assert ix["set"] == {"verb": "set_selected_index", "arg": "ix"}
    assert ix["settle"]["policy"] == "adopt_then_flag"
    assert ix["settle"]["flag_key"] == "point_settled"
    for pid in ("spot_from_template_x", "spot_from_template_y"):
        assert params[pid]["unit"] == "um" and params[pid]["kind"] == "indicator"
    assert params["point_x_px"]["unit"] == "px"
