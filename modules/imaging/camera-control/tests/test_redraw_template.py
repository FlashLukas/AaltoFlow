"""Drawing a NEW template must not move the scan array.

Lukas (2026-10-08): "I had a template and a scan array. When I draw a new
template the scan array did not stay at the same position but jumped ... the
array centre and template centre were the same." The array is a place on the
sample; the template is only the handle the camera tracks it by.
"""

import math
import time

from camera.config import Config
from camera.sim_system import build_sim_system


def _wait(cond, timeout=8.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_a_new_template_keeps_the_array_where_it_is():
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, cam, _xy, _z = build_sim_system(cfg)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 2)
        tcx, tcy = cam.template_center_px()
        # first template, array put somewhere of the user's choosing (not on it)
        brain.capture_reference((tcx, tcy, 60, 60), array_center_px=(tcx + 90, tcy - 40))
        brain.set_tracking(True)
        assert _wait(lambda: brain.status().match_found)
        before = brain.get_scan_rect()
        assert math.hypot(before["cx"] - (tcx + 90), before["cy"] - (tcy - 40)) < 2.0

        # a new template drawn at a DIFFERENT place, as in the GUI (no array centre given)
        brain.capture_reference((tcx + 30, tcy + 20, 80, 80))
        after = brain.get_scan_rect()
        assert math.hypot(after["cx"] - before["cx"], after["cy"] - before["cy"]) < 2.0, \
            (before, after)
        # and it stays there once the new template is tracked
        assert _wait(lambda: brain.status().match_found)
        time.sleep(0.2)
        later = brain.get_scan_rect()
        assert math.hypot(later["cx"] - before["cx"], later["cy"] - before["cy"]) < 2.0
    finally:
        brain.shutdown()


def test_the_first_template_still_places_the_array():
    """No array yet: the first template puts it on the spot (or on itself)."""
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, cam, _xy, _z = build_sim_system(cfg)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 2)
        tcx, tcy = cam.template_center_px()
        brain.capture_reference((tcx, tcy, 60, 60))
        ox, oy = brain.reference.array_center_offset_px
        spot = brain.spot_position()
        want = spot if spot is not None else (tcx, tcy)
        assert math.hypot(tcx + ox - want[0], tcy + oy - want[1]) < 2.0
    finally:
        brain.shutdown()
