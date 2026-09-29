"""The laser spot among OTHER bright light, and only in the safety area (2026-09-29).

Rig check of 6da4e31 (lab PC, 63x, IDS Mono8). Two exposures:
  * AF exposure 65 us: background ~2 counts, spot peak ~200, nothing else
    bright -- and locate = blob found NOTHING ("no blob above background + 5 x
    noise"), even with the true calibration;
  * working exposure 2480 us: background ~144, the laser spot AND a large
    illuminated block SATURATED (the block's top-left ~915 px from the spot,
    reaching to within tens of px of it) -- blob picked a point 53 px off, peak
    14 px off, the why-text pointed at the block's corner (236, 0), and the
    "saturated" calibration was ACCEPTED with +-277 px jitter.
Lukas's rule, the same day: "Always look for the laser spot in the safety area
around the laser only!" -- every search (locate, why-text, calibration) stays in
the search region around the calibrated laser position (the frame centre when
there is no calibration yet).

The frames here are synthetic copies of those rig frames (1936 x 1096, 8 bit).
Every test failed on 6da4e31 (the round-trip one for want of the new field).
"""

import math
import time

import numpy as np
import pytest

from camera import vision as V
from camera.backends.sim import SimCamera, SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config, load_config, save_config

H, W = 1096, 1936
LASER = (973.3, 464.9)               # the rig's unsaturated calibration


def _grid():
    yy, xx = np.mgrid[:H, :W].astype(np.float64)
    return xx, yy


def _u8(f):
    return np.clip(np.rint(f), 0, 255).astype(np.uint8)


def _spot(peak, sigma, c=LASER):
    xx, yy = _grid()
    return peak * np.exp(-((xx - c[0]) ** 2 + (yy - c[1]) ** 2) / (2 * sigma * sigma))


def _spot_cfg(region=100, **kw):
    sp = Config().spot
    sp.lookup_region_px = region
    for k, v in kw.items():
        setattr(sp, k, v)
    return sp


def _af_exposure_frame(seed=0):
    """65 us: an almost black, integer background (2 counts, MAD 0 -- most
    pixels read exactly 2), the illuminated sample still faintly there (+2
    counts over half of the view), and the laser spot, D4sigma 30 px, peak 200."""
    xx, _yy = _grid()
    rng = np.random.default_rng(seed)
    bg = 2.0 + rng.normal(0.0, 0.3, (H, W)) + 2.0 * (xx < LASER[0] - 15)
    return _u8(bg + _spot(198.0, 7.5))


def _working_frame(seed=0, spot=True, block=True, block_right=LASER[0] - 55):
    """2480 us: background ~144 with the sample's texture, the laser spot
    saturated (flat top), and a large saturated illuminated block from the
    frame's top edge (its top-left at (236, 0)) to within ~50 px of the spot."""
    import cv2
    rng = np.random.default_rng(seed)
    tex = cv2.GaussianBlur(rng.normal(0, 1, (H, W)).astype(np.float32), (0, 0), 6.0)
    f = 144.0 + 12.0 * tex / tex.std() + rng.normal(0.0, 2.0, (H, W))
    if spot:
        f += _spot(900.0, 7.5)
    if block:
        f[0:int(LASER[1]) + 70, 236:int(block_right)] = 255.0
    return _u8(f)


# --------------------------------------------------------------------------- #
# 1. the AF exposure: a dim spot on a black, quantised background
# --------------------------------------------------------------------------- #
def test_blob_finds_the_spot_at_the_af_exposure():
    f = _af_exposure_frame()
    sp = _spot_cfg(region=150)
    loc = V.locate_spot(f, LASER, sp, "blob")
    assert loc.ok, loc.why
    assert (loc.x, loc.y) == pytest.approx(LASER, abs=0.5)
    loc = V.locate_spot(f, LASER, sp, "peak")
    assert loc.ok and (loc.x, loc.y) == pytest.approx(LASER, abs=0.5)


def test_the_noise_floor_is_never_below_one_grey_level():
    """MAD 0 on an integer background must not become a 1.5-count threshold."""
    flat = np.full((400, 400), 2, np.uint8)
    _sm, _bg, noise = V._smooth_signal(flat, (0, 0, 400, 400), _spot_cfg())
    assert noise >= 1.0                                  # one 8-bit grey level
    # 12-bit frame in a uint16 container: one grey level = 4095 / 255 counts
    flat16 = np.full((400, 400), 30, np.uint16)
    _sm, _bg, noise = V._smooth_signal(flat16, (0, 0, 400, 400), _spot_cfg(), max_value=4095)
    assert noise >= 4095 / 255 - 1e-6


# --------------------------------------------------------------------------- #
# 2. the working exposure: the saturated block must not beat the laser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["blob", "peak"])
def test_locate_picks_the_laser_not_the_saturated_block(mode):
    f = _working_frame()
    loc = V.locate_spot(f, LASER, _spot_cfg(), mode)
    assert loc.ok, loc.why
    assert (loc.x, loc.y) == pytest.approx(LASER, abs=1.0), mode


@pytest.mark.parametrize("mode", ["blob", "peak"])
def test_locate_with_a_stale_calibration_prefers_the_laser(mode):
    """The rig: the calibration ~50 px off. Both the block's corner and the
    laser are in the region; the block reaches out of it (not a spot)."""
    f = _working_frame()
    cal = (LASER[0] - 50.0, LASER[1])
    loc = V.locate_spot(f, cal, _spot_cfg(), mode)
    assert loc.ok and (loc.x, loc.y) == pytest.approx(LASER, abs=1.0)


def test_of_two_compact_spots_the_one_nearest_the_calibration_wins():
    f = _u8(10.0 + _spot(900.0, 5.0, (1000.0, 500.0)) + _spot(900.0, 5.0, (1060.0, 500.0)))
    loc = V.locate_spot(f, (1052.0, 500.0), _spot_cfg(), "blob")
    assert loc.ok and loc.x == pytest.approx(1060.0, abs=0.5)
    loc = V.locate_spot(f, (1008.0, 500.0), _spot_cfg(), "blob")
    assert loc.ok and loc.x == pytest.approx(1000.0, abs=0.5)


def test_a_block_outside_the_region_is_never_considered():
    """No laser in the region, a saturated block 900 px away: nothing found,
    and nothing about the block in the answer."""
    f = _working_frame(spot=False, block_right=LASER[0] - 300)
    for mode in ("blob", "peak"):
        loc = V.locate_spot(f, LASER, _spot_cfg(), mode)
        assert not loc.ok
        assert "region" in loc.why
    r = V.find_spot_for_calibration(f, _spot_cfg(), "saturated", calib_xy=LASER)
    assert not r.ok and "region" in r.why and "by hand" in r.why
    r = V.find_spot_for_calibration(f, _spot_cfg(), "unsaturated", calib_xy=LASER)
    assert not r.ok and "region" in r.why and "by hand" in r.why


def test_an_elongated_streak_is_not_a_spot():
    f = np.full((H, W), 20.0)
    f[456:466, 900:1050] = 255.0                       # a bright scratch, 10 x 150 px
    f += _spot(200.0, 4.0, (1000.0, 520.0))
    loc = V.locate_spot(_u8(f), (975.0, 470.0), _spot_cfg(), "blob")
    assert loc.ok and (loc.x, loc.y) == pytest.approx((1000.0, 520.0), abs=0.5)


# --------------------------------------------------------------------------- #
# 3. the calibration at the working exposure
# --------------------------------------------------------------------------- #
def test_saturated_calibration_finds_the_laser_beside_the_block():
    f = _working_frame()
    sp = _spot_cfg(thr_lower=200)
    r = V.find_spot_for_calibration(f, sp, "saturated", calib_xy=(LASER[0] - 30, LASER[1]))
    assert r.ok, r.why
    assert (r.x, r.y) == pytest.approx(LASER, abs=1.0)


def test_unsaturated_calibration_at_the_af_exposure_is_not_dragged_by_the_lit_sample():
    """The moment refine's box reaches the faintly lit half of the view: its
    centroid moved 12.5 px toward it. The position must stay on the laser."""
    r = V.find_spot_for_calibration(_af_exposure_frame(), _spot_cfg(), "unsaturated",
                                    calib_xy=LASER)
    assert r.ok, r.why
    assert (r.x, r.y) == pytest.approx(LASER, abs=0.5)


def test_calibration_searches_only_the_region_around_the_frame_centre_when_uncalibrated():
    far = (300.0, 300.0)                                 # a spot far from the frame centre
    f = _u8(10.0 + _spot(220.0, 5.0, far))
    for mode in ("saturated", "unsaturated"):
        r = V.find_spot_for_calibration(f, _spot_cfg(thr_lower=150), mode, calib_xy=None)
        assert not r.ok
        assert "frame centre" in r.why and "no calibration" in r.why and "by hand" in r.why
    near = _u8(10.0 + _spot(220.0, 5.0, (W / 2 + 40, H / 2 - 30)))
    r = V.find_spot_for_calibration(near, _spot_cfg(), "unsaturated", calib_xy=None)
    assert r.ok and (r.x, r.y) == pytest.approx((W / 2 + 40, H / 2 - 30), abs=0.5)


# --------------------------------------------------------------------------- #
# the brain: why-text, jitter refusal, one saturation line per episode
# --------------------------------------------------------------------------- #
class FrameCam(SimCamera):
    """A simulator camera whose frames are given (cycled): the rig's frames."""

    def __init__(self, frames, *a, **k):
        super().__init__(*a, **k)
        self.frames = frames
        self.i = 0

    def grab(self):
        super().grab()
        f = self.frames[self.i % len(self.frames)]
        self.i += 1
        return f.copy()


def _wait(cond, timeout=30.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


def _brain(frames):
    cfg = Config()
    cfg.camera.frame_rate = 100.0
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = SimZFocus(z0=30.0, z_focus=30.0, vmin=cfg.limits.z_min_v, vmax=cfg.limits.z_max_v)
    cam = FrameCam(frames, xy, z, width=W, height=H, spot_px=LASER,
                   pixel_size_x_um=cfg.image.pixel_size_x_um,
                   pixel_size_y_um=cfg.image.pixel_size_y_um)
    brain = Camera(cam, xy, z, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    return brain, events


def test_the_why_text_points_at_the_laser_not_the_block():
    brain, _ev = _brain([_working_frame()])
    try:
        sp = brain.cfg.spot
        brain.set_spot_position(LASER[0] + 50.0, LASER[1])   # the rig: calibration +50 px
        f = _working_frame()
        why = brain._why_no_spot(f, None, (sp.ref_x, sp.ref_y), "no spot above the noise")
        assert "50 px" in why and "(973, 465)" in why, why
        assert "236" not in why
    finally:
        brain.shutdown()


def test_the_why_text_never_looks_outside_the_region():
    """No laser in the region (it is off), the saturated block 900 px away: the
    why-text says nothing is in the region, never where the block is."""
    f = _working_frame(spot=False, block_right=LASER[0] - 300)
    brain, _ev = _brain([f])
    try:
        brain.set_spot_position(*LASER)
        why = brain._why_no_spot(f, None, LASER, "no spot above the noise")
        assert "search region" in why and "by hand" in why, why
        assert "236" not in why and "(0" not in why
    finally:
        brain.shutdown()


@pytest.mark.parametrize("mode", ["blob", "peak"])
def test_a_spot_outside_the_region_is_not_found_and_says_what_to_do(mode):
    f = _u8(10.0 + _spot(220.0, 5.0, (LASER[0] + 180.0, LASER[1])))    # 180 px off
    loc = V.locate_spot(f, LASER, _spot_cfg(), mode)
    assert not loc.ok
    assert "search region" in loc.why and "enlarge" in loc.why and "by hand" in loc.why


def test_calibration_refuses_when_the_found_position_jumps():
    """Frames alternate between the laser and a compact competitor 40 px away
    (a flickering reflection): the old code averaged them into a point on
    neither and ACCEPTED it."""
    a = _u8(20.0 + _spot(900.0, 5.0))
    b = _u8(20.0 + _spot(900.0, 5.0, (LASER[0] + 40.0, LASER[1])))
    brain, _ev = _brain([a, b])
    try:
        brain.cfg.spot.calib_mode = "saturated"
        brain.cfg.spot.thr_lower = 200
        with pytest.raises(RuntimeError, match=r"jumps by 4\d px between frames"):
            brain.calibrate_spot(8)
        assert not brain.cfg.spot.ref_set
    finally:
        brain.shutdown()


def test_calibration_with_a_steady_laser_passes_the_jitter_check():
    a = _u8(20.0 + _spot(900.0, 5.0))
    brain, _ev = _brain([a])
    try:
        brain.cfg.spot.calib_mode = "saturated"
        brain.cfg.spot.thr_lower = 200
        res = brain.calibrate_spot(6)
        assert (res["x"], res["y"]) == pytest.approx(LASER, abs=0.5)
        assert res["jitter_px"] < brain.cfg.spot.calib_max_jitter_px
    finally:
        brain.shutdown()


def test_calib_max_jitter_round_trips(tmp_path):
    cfg = Config()
    cfg.spot.calib_max_jitter_px = 3.5
    p = tmp_path / "c.ini"
    save_config(cfg, p)
    assert load_config(p).spot.calib_max_jitter_px == 3.5
    assert Config().spot.calib_max_jitter_px == 2.0


def test_there_is_no_whole_frame_search_at_all():
    """Lukas 2026-09-29: 'Always look for the laser spot in the safety area
    around the laser only!' -- a search region of 0 (it used to mean 'the
    whole frame') is not allowed, however it is set."""
    from camera.config import Config, Spot
    cfg = Config()
    cfg.spot.lookup_region_px = 0
    assert cfg.spot.lookup_region_px >= 10
    assert Spot(lookup_region_px=-5).lookup_region_px >= 10
    cfg.spot.lookup_region_px = 150
    assert cfg.spot.lookup_region_px == 150
