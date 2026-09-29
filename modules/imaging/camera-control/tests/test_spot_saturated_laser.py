"""The SATURATED laser at the working exposure is the spot, not its fragments
(2026-09-29 late).

Lab analysis of 9da7a07 on real frames (IDS Mono8, 63x, working exposure
2480 us; private data, see test_spot_real_frames.py): the laser is a compact
saturated disc with rings, 3.4-7.8 k px^2 above the locate threshold, on a lit
film at ~145 counts. The rig's Spot config had max_area_px = 2000 -- meant for
the illuminated block, which the region rules reject anyway since 9da7a07 --
so the laser itself was rejected "larger than max area", and locate (blob AND
peak) returned a 30-50 px ring fragment or a 6 px speck 45-52 px away. The
saturated calibration did the same with a ring fragment.

Proved here on SYNTHETIC copies of that geometry (the public suite has no
private data), each failing on 9da7a07:
  * max_area_px 0 = automatic (a quarter of the search region) and the default;
  * with the rig's explicit 2000 the answer is "not found" with the reason
    (the blob at the calibrated position is larger than max area) -- never a
    fragment;
  * a fragment / speck never wins over the real spot (the energy rule), even
    when it is nearer a stale calibration and as bright as the laser's top;
  * the Camera's why-text says each sentence once;
  * the "N px from its calibrated position" warning needs N consecutive frames
    and is never raised on the frames in flight after an exposure change;
  * calibrate "at the autofocus exposure" with no autofocus exposure set says
    so (reply, event, GUI).
"""

import math
import time

import cv2
import numpy as np
import pytest

from camera import vision as V
from camera.backends.sim import SimCamera, SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config, load_config, save_config

H = W = 601
CAL = (300.0, 300.0)                      # calibration = the laser's true centre


def _grid():
    yy, xx = np.mgrid[:H, :W].astype(np.float64)
    return xx, yy


def _u8(f):
    return np.clip(np.rint(f), 0, 255).astype(np.uint8)


def _bump(peak, sigma, c):
    xx, yy = _grid()
    return peak * np.exp(-((xx - c[0]) ** 2 + (yy - c[1]) ** 2) / (2 * sigma * sigma))


def _laser_frame(radius=36.0, c=CAL, seed=0, specks=True, streak=True, laser=True):
    """The rig's working exposure: lit film ~145 with texture, the laser a
    SATURATED disc of ``radius`` plus decaying rings (the first ring merges
    with the disc above the locate threshold, as on the rig), two small bright
    specks just outside the rings (the rig's ring fragments / dust, 30-55
    px^2, 45-50 px from a 33 px disc) and an
    elongated streak 70 px away."""
    rng = np.random.default_rng(seed)
    tex = cv2.GaussianBlur(rng.normal(0, 1, (H, W)).astype(np.float32), (0, 0), 6.0)
    f = 145.0 + 6.0 * tex / tex.std() + rng.normal(0.0, 1.5, (H, W))
    if laser:
        xx, yy = _grid()
        r = np.hypot(xx - c[0], yy - c[1])
        core = np.where(r <= radius, 400.0, 0.0)
        d = np.clip(r - radius, 0.0, None)
        rings = np.where(r > radius, 60.0 * np.cos(np.pi * d / 8.0) ** 2 * np.exp(-d / 6.0), 0.0)
        f += core + rings
    if specks:
        # just outside the rings, like the rig's fragments (45-50 px from a
        # disc of ~33 px radius)
        a = radius + 14.0
        f += _bump(38.0, 2.6, (c[0] + a * 0.97, c[1] - a * 0.25))
        f += _bump(36.0, 2.4, (c[0] + (a + 4.0) * 0.4, c[1] + (a + 4.0) * 0.92))
    if streak:
        f[int(c[1]) - 70:int(c[1]) - 67, int(c[0]) - 30:int(c[0]) + 30] += 60.0   # 3 x 60 px, 70 px up
    return _u8(f)


def _rig_cfg(**kw):
    """The rig's live Spot config (notes of the capture), max area as given."""
    sp = Config().spot
    for k, v in dict(thr_lower=200, min_area_px=4, max_area_px=2000, lookup_region_px=200,
                     search_shape="circle", locate_k=5.0, max_elongation=3.0).items():
        setattr(sp, k, v)
    for k, v in kw.items():
        setattr(sp, k, v)
    return sp


# --------------------------------------------------------------------------- #
# 1. the area limit follows the region
# --------------------------------------------------------------------------- #
def test_max_area_zero_is_automatic_and_the_default():
    assert Config().spot.max_area_px == 0
    circle = V.search_region(CAL, 200, 0, "circle", (W, H))
    assert V.max_area_limit(_rig_cfg(max_area_px=0), circle) == pytest.approx(
        V.AUTO_MAX_AREA_FRACTION * math.pi * 200 ** 2, rel=1e-6)
    rect = V.search_region(CAL, 100, 50, "rect", (W, H))
    assert V.max_area_limit(_rig_cfg(max_area_px=0), rect) == pytest.approx(
        V.AUTO_MAX_AREA_FRACTION * 201 * 101, rel=1e-6)
    assert V.max_area_limit(_rig_cfg(max_area_px=2000), circle) == 2000     # explicit wins
    assert V.AUTO_MAX_AREA_FRACTION == pytest.approx(0.25)


def test_max_area_zero_round_trips(tmp_path):
    cfg = Config()
    p = tmp_path / "c.ini"
    save_config(cfg, p)
    assert load_config(p).spot.max_area_px == 0


@pytest.mark.parametrize("radius", [33.0, 36.0, 45.0, 50.0])      # 3.4 .. 7.9 k px^2
@pytest.mark.parametrize("mode", ["blob", "peak"])
def test_the_saturated_laser_is_found_with_the_automatic_area(radius, mode):
    f = _laser_frame(radius)
    loc = V.locate_spot(f, CAL, _rig_cfg(max_area_px=0), mode)
    assert loc.ok, loc.why
    assert math.hypot(loc.x - CAL[0], loc.y - CAL[1]) < 1.5, (loc.x, loc.y)
    assert loc.area > 3000


@pytest.mark.parametrize("mode", ["blob", "peak"])
def test_an_explicit_small_max_area_says_why_and_never_returns_a_fragment(mode):
    """The rig's max_area_px 2000 rejects the laser: 'not found' with the
    reason, never the fragment 45 px away (9da7a07 returned it)."""
    f = _laser_frame(36.0)
    loc = V.locate_spot(f, CAL, _rig_cfg(max_area_px=2000), mode)
    assert not loc.ok, (loc.x, loc.y)
    assert "max area" in loc.why and "2000" in loc.why, loc.why
    assert "0" in loc.why and "automatic" in loc.why, loc.why


def test_the_saturated_calibration_finds_the_laser_not_a_ring_fragment():
    f = _laser_frame(40.0)
    r = V.find_spot_for_calibration(f, _rig_cfg(max_area_px=0), "saturated", calib_xy=CAL)
    assert r.ok, r.why
    assert math.hypot(r.x - CAL[0], r.y - CAL[1]) < 1.0
    r = V.find_spot_for_calibration(f, _rig_cfg(max_area_px=0), "unsaturated", calib_xy=CAL)
    assert r.ok, r.why
    assert math.hypot(r.x - CAL[0], r.y - CAL[1]) < 1.5
    # the rig's explicit limit: refused with the reason, not a fragment
    r = V.find_spot_for_calibration(f, _rig_cfg(max_area_px=2000), "saturated", calib_xy=CAL)
    assert not r.ok and "max area" in r.why and "automatic" in r.why, (r.x, r.y, r.why)


# --------------------------------------------------------------------------- #
# 2. ranking: a fragment never wins
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["blob", "peak"])
def test_a_saturated_speck_nearer_a_stale_calibration_does_not_win(mode):
    """A 7 x 7 saturated glint (dust) as bright at the top as the laser, and
    NEARER the (stale) calibration: 9da7a07 took it (nearest of the equally
    bright). It carries ~1 % of the laser's light: a fragment."""
    laser = (300.0, 300.0)
    f = _laser_frame(36.0, c=laser, specks=False, streak=False).astype(np.float64)
    stale = (laser[0] + 40.0, laser[1] + 20.0)
    f[int(stale[1]) + 12:int(stale[1]) + 19, int(stale[0]) + 1:int(stale[0]) + 8] = 255.0
    loc = V.locate_spot(_u8(f), stale, _rig_cfg(max_area_px=0), mode)
    assert loc.ok, loc.why
    assert math.hypot(loc.x - laser[0], loc.y - laser[1]) < 1.5, (loc.x, loc.y)


def test_a_dim_big_feature_does_not_make_the_bright_spot_a_fragment():
    """The energy rule compares a candidate with those at least as BRIGHT: a
    large dim sample feature carries more light than a small bright spot and
    must not throw the spot out (the reason blob ranks by peak, not energy)."""
    f = np.full((H, W), 20.0)
    f[200:260, 380:460] += 60.0                                 # 4800 px x 60 counts
    f += _bump(180.0, 4.0, CAL)
    loc = V.locate_spot(_u8(f), CAL, _rig_cfg(max_area_px=20000), "blob")
    assert loc.ok and (loc.x, loc.y) == pytest.approx(CAL, abs=0.5)


def test_only_fragments_is_not_found_with_the_reason():
    """The laser is off; what is left in the region is the specks and the
    streak. The specks are the brightest spot-like things, so one of them is a
    candidate on its own (nothing brighter to be a fragment of) -- but with the
    laser rejected by an explicit limit they ARE fragments of it."""
    f = _laser_frame(36.0)
    loc = V.locate_spot(f, CAL, _rig_cfg(max_area_px=1000), "blob")
    assert not loc.ok and "max area" in loc.why


# --------------------------------------------------------------------------- #
# 3. the brain: why-text once, the persistent offset warning, AF exposure 0
# --------------------------------------------------------------------------- #
class FrameCam(SimCamera):
    """A simulator camera whose frames are given; after an ExposureTime write
    the next ``bad_after_expo`` grabs are ``bad`` (the frames in flight)."""

    def __init__(self, frames, *a, bad=None, bad_after_expo=0, **k):
        super().__init__(*a, **k)
        self.frames = frames
        self.i = 0
        self.bad = bad
        self.bad_after_expo = bad_after_expo
        self._bad_left = 0

    def set_feature(self, name, value):
        super().set_feature(name, value)
        if name == "ExposureTime" and self.bad is not None:
            self._bad_left = self.bad_after_expo

    def grab(self):
        super().grab()
        if self._bad_left > 0:
            self._bad_left -= 1
            return self.bad.copy()
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


def _brain(frames, **kw):
    cfg = Config()
    cfg.camera.frame_rate = 100.0
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = SimZFocus(z0=30.0, z_focus=30.0, vmin=cfg.limits.z_min_v, vmax=cfg.limits.z_max_v)
    cam = FrameCam(frames, xy, z, width=W, height=H, spot_px=CAL,
                   pixel_size_x_um=cfg.image.pixel_size_x_um,
                   pixel_size_y_um=cfg.image.pixel_size_y_um, **kw)
    brain = Camera(cam, xy, z, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    return brain, cam, events


def _frames_after(brain, n):
    f0 = brain.status().frame_number
    assert _wait(lambda: brain.status().frame_number > f0 + n)
    return brain.status()


def _offset_warnings(events):
    return [m for _l, m in events if "px from its calibrated" in m]


def test_the_why_text_says_each_sentence_once():
    """9da7a07: 'no spot-like light ... region; no spot-like light ... region'
    -- the locate's reason and the why-text's own look, the same sentence."""
    dark = _u8(np.full((H, W), 20.0) + np.random.default_rng(3).normal(0, 1.0, (H, W)))
    brain, _cam, _ev = _brain([dark])
    try:
        brain.set_spot_position(*CAL)
        brain.cfg.spot.locate = "blob"
        s = _frames_after(brain, 3)
        why = s.spot_size_why
        assert why, "a why-text is expected"
        parts = [p.strip() for p in why.split(";")]
        assert len(parts) == len(set(parts)), why
    finally:
        brain.shutdown()


def test_a_single_offset_frame_does_not_warn_a_persistent_one_does():
    good = _laser_frame(20.0, specks=False, streak=False)
    off = _laser_frame(20.0, c=(CAL[0] + 40.0, CAL[1]), specks=False, streak=False)
    frames = [good] * 9 + [off]                 # one off frame in ten: never 5 in a row
    brain, cam, events = _brain(frames)
    try:
        brain.set_spot_position(*CAL)
        brain.cfg.spot.locate = "blob"
        assert brain.cfg.spot.offset_warn_frames == 5
        _frames_after(brain, 40)
        assert not _offset_warnings(events), _offset_warnings(events)
        cam.frames = [off]                      # the laser really moved
        assert _wait(lambda: bool(_offset_warnings(events)), timeout=10.0)
    finally:
        brain.shutdown()


def test_no_offset_warning_on_the_frames_in_flight_after_an_exposure_change():
    """The rig, once: a false 'N px from its calibrated position' right at an
    exposure switch 65 -> 2480 us with locate = blob. Even with a 1-frame
    persistence, the frames still exposed with the old value do not count."""
    good = _laser_frame(20.0, specks=False, streak=False)
    off = _laser_frame(20.0, c=(CAL[0] + 40.0, CAL[1]), specks=False, streak=False)
    brain, cam, events = _brain([good], bad=off)
    try:
        n_drop = int(brain.cfg.autofocus.exposure_discard_frames)
        cam.bad_after_expo = n_drop + 1         # the frames in flight
        brain.set_spot_position(*CAL)
        brain.cfg.spot.locate = "blob"
        brain.cfg.spot.offset_warn_frames = 1
        _frames_after(brain, 3)
        exp = float(cam.get_feature("ExposureTime"))
        for k in range(3):
            brain.set_camera_feature("ExposureTime", exp * (2.0 if k % 2 == 0 else 1.0))
            _frames_after(brain, n_drop + 6)
        assert not _offset_warnings(events), _offset_warnings(events)
    finally:
        brain.shutdown()


def test_calibrate_at_af_exposure_without_one_says_so():
    good = _laser_frame(20.0, specks=False, streak=False)
    brain, _cam, events = _brain([good])
    try:
        brain.set_spot_position(*CAL)
        sp = brain.cfg.spot
        sp.calib_mode, sp.thr_lower = "saturated", 200
        sp.calib_at_af_exposure = True
        brain.cfg.autofocus.exposure_us = 0.0
        res = brain.calibrate_spot(4)
        assert res["at_af_exposure"] is False
        assert "no autofocus exposure set" in res.get("warning", ""), res
        assert any(lvl == "warn" and "no autofocus exposure set" in m for lvl, m in events)
        # with an AF exposure: no such warning
        events.clear()
        brain.cfg.autofocus.exposure_us = 100.0
        res = brain.calibrate_spot(4)
        assert not res.get("warning")
        assert not any("no autofocus exposure set" in m for _l, m in events)
    finally:
        brain.shutdown()


def test_offset_warn_frames_round_trips(tmp_path):
    cfg = Config()
    cfg.spot.offset_warn_frames = 7
    p = tmp_path / "c.ini"
    save_config(cfg, p)
    assert load_config(p).spot.offset_warn_frames == 7


def test_the_spot_tab_warns_next_to_the_af_exposure_checkbox():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from camera.apps.spot_tab import SpotTab

    _app = QApplication.instance() or QApplication([])
    cfg = Config()
    cfg.autofocus.exposure_us = 0.0
    tab = SpotTab(ctrl=None, cfg=cfg, log=lambda *a: None, frame_source=lambda: None)
    tab.chk_calib_afx.setChecked(True)
    assert not tab.lab_calib_afx.isHidden()
    assert "no autofocus exposure set" in tab.lab_calib_afx.text()
    cfg.autofocus.exposure_us = 65.0
    tab.update_afx_note()
    assert tab.lab_calib_afx.isHidden()
    cfg.autofocus.exposure_us = 0.0
    tab.chk_calib_afx.setChecked(False)
    assert tab.lab_calib_afx.isHidden()
