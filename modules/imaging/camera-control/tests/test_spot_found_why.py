"""WHY the per-frame threshold check did not see the spot (2026-09-29).

Lukas's screenshot: the main view said "spot not seen" and the Spot tab's LIVE
card "spot NOT seen by the threshold ... / threshold area: --", while the
threshold-free sizes (relative area 5155 px^2, D4sigma 93.7 px) measured the
spot fine. The rig's camera.ini has spot.max_area_px = 2000: the thresholded
(200-255) spot is ~1500 px^2 in focus but 2600-4400 px^2 at +-2 um, so the
per-frame check rejected it -- and said nothing. Now vision.find_spot says why
(SpotReport.why / why_short), the brain publishes it (status spot_found_why /
spot_found_why_short), the LIVE card shows the sentence and the image label
the short form. The user's explicit max_area_px is NOT changed.

Synthetic here (a saturated disc with a dark centre on a lit film, like the
rig at the working exposure); the real frames are in test_spot_real_frames.py
(skipped without the private file).
"""

import math
import os
import time

import numpy as np
import pytest

from camera import vision as V
from camera.backends.sim import SimCamera, SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config

W = H = 401
C = (200.0, 200.0)


def _frame(radius=35, hole=0, film=145, spot_at=C):
    """A lit film + a SATURATED disc (optionally with a dark centre, a
    defocused coherent spot) -- ~3850 px^2 above 200 for radius 35."""
    f = np.full((H, W), film, np.uint8)
    yy, xx = np.mgrid[:H, :W]
    r = np.hypot(xx - spot_at[0], yy - spot_at[1])
    f[r <= radius] = 255
    if hole:
        f[r <= hole] = 120
    return f


def _find(frame, max_area=2000, min_area=4, auto=False, symmetric=True, thr=200):
    return V.find_spot(frame, thr, 255, True, 200, C, min_area, max_area, True, "circle", 0,
                       symmetric=symmetric, max_area_is_auto=auto)


# --------------------------------------------------------------------------- #
# vision.find_spot: the reason, with its numbers and what to change
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("symmetric", [True, False])
@pytest.mark.parametrize("hole", [0, 8])
def test_a_spot_larger_than_max_area_says_so(symmetric, hole):
    det = _find(_frame(hole=hole), symmetric=symmetric)
    assert not det.found
    assert det.why_short == "larger than max area"
    assert "the blob at the calibrated position is" in det.why
    assert "larger than max area 2000 px" in det.why
    assert "set max area to 0 (automatic)" in det.why
    area = int(det.why.split("is ")[1].split(" px")[0])
    assert 3000 < area < 4500


def test_with_the_automatic_limit_the_same_spot_is_seen():
    sp = Config().spot
    reg = V.search_region(C, 200, 0, "circle", (W, H))
    limit = V.max_area_limit(sp, reg)            # max_area_px 0 -> a quarter of the region
    det = _find(_frame(), max_area=limit, auto=True)
    assert det.found and det.why == "" and det.area > 3000


def test_too_large_for_the_automatic_limit_names_the_search_region():
    det = _find(_frame(radius=60), max_area=3000, auto=True)
    assert det.why_short == "larger than max area"
    assert "a quarter of the search region" in det.why and "set max area to 0" not in det.why


def test_smaller_than_min_area_and_nothing_above_the_threshold():
    det = _find(_frame(radius=2), min_area=50)
    assert det.why_short == "smaller than min area"
    assert "smaller than min area 50 px" in det.why
    det = _find(np.full((H, W), 145, np.uint8))
    assert det.why_short == "nothing above the threshold"
    assert "nothing above the threshold 200" in det.why and "brightest there 145" in det.why


def test_a_found_spot_has_no_why():
    det = _find(_frame(radius=20))
    assert det.found and det.why == "" and det.why_short == ""


# --------------------------------------------------------------------------- #
# the brain publishes it; the user's max area stays
# --------------------------------------------------------------------------- #
class _FrameCam(SimCamera):
    def __init__(self, frame, *a, **k):
        super().__init__(*a, **k)
        self.frame = frame

    def grab(self):
        super().grab()
        return self.frame.copy()


def _wait(cond, timeout=30.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _brain(frame, max_area):
    cfg = Config()
    cfg.camera.frame_rate = 100.0
    for k, v in dict(lookup_region_px=200, search_shape="circle", min_area_px=4,
                     thr_lower=200, max_area_px=max_area).items():
        setattr(cfg.spot, k, v)
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = SimZFocus(z0=30.0, z_focus=30.0, vmin=cfg.limits.z_min_v, vmax=cfg.limits.z_max_v)
    cam = _FrameCam(frame, xy, z, width=W, height=H, spot_px=C,
                    pixel_size_x_um=cfg.image.pixel_size_x_um,
                    pixel_size_y_um=cfg.image.pixel_size_y_um)
    return Camera(cam, xy, z, cfg)


def run_brain(frame, max_area):
    brain = _brain(frame, max_area)
    brain.start()
    try:
        brain.set_spot_position(*C)
        f0 = brain.status().frame_number
        assert _wait(lambda: brain.status().frame_number > f0 + 5)
        return brain.status(), brain.cfg.spot.max_area_px
    finally:
        brain.shutdown()


def test_the_brain_publishes_why_and_keeps_the_users_max_area():
    s, kept = run_brain(_frame(hole=8), 2000)
    assert not s.spot_found
    assert s.spot_found_why_short == "larger than max area"
    assert "larger than max area 2000 px" in s.spot_found_why
    assert kept == 2000                                   # not changed behind the user
    assert math.isfinite(s.spot_rel_area) and s.spot_rel_area > 0   # the new sizes see it
    s, _ = run_brain(_frame(hole=8), 0)                   # automatic: seen, no why
    assert s.spot_found and s.spot_found_why == "" and s.spot_found_why_short == ""


# --------------------------------------------------------------------------- #
# the GUI: the LIVE card (sentence) and the image label (short form)
# --------------------------------------------------------------------------- #
def _status(**kw):
    from camera.camera import CameraStatus
    s = CameraStatus()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_the_live_card_and_the_image_label_say_why():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from camera.apps.camera_view import CameraView
    from camera.apps.spot_tab import SpotTab, sizes_summary
    app = QApplication.instance() or QApplication([])
    why = ("the blob at the calibrated position is 3419 px, larger than max area 2000 px "
           "-- set max area to 0 (automatic) or raise it (Spot tab)")
    s = _status(spot_found=False, spot_calibrated=True, spot_x=200.0, spot_y=200.0,
                spot_found_why=why, spot_found_why_short="larger than max area")
    assert "threshold area: -- (larger than max area)" in sizes_summary(s)

    cfg = Config()
    cfg.spot.ref_set, cfg.spot.ref_x, cfg.spot.ref_y = True, 200.0, 200.0
    logs = []
    tab = SpotTab(None, cfg, lambda lvl, msg: logs.append(msg), lambda: None)
    tab.update_status(s)
    assert "larger than max area 2000 px -- set max area to 0 (automatic)" in \
        tab.lab_live.text()

    view = CameraView()
    view.resize(600, 600)
    view.set_frame(np.zeros((H, W), np.uint8))
    texts = []
    real = view._label
    view._label = lambda p, at, text, colour, avoid=(): (texts.append(text),
                                                         real(p, at, text, colour, avoid))[1]
    view.set_show_spot_info(True)
    view.set_overlay(s, cfg)
    view.grab()
    assert "spot not seen: larger than max area" in texts
    view.close()
    tab.close()
    app.processEvents()
