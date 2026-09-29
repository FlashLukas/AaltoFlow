"""REAL rig frames: the saturated laser at the working exposure (2026-09-29 late).

The frames are PRIVATE lab data and are never copied into this (public)
repository: a 401 x 401 crop = the 200 px circular search region around the
rig's spot calibration (973.0, 465.0), crop centre (200, 200), IDS Mono8, 63x,
5 frames each of
  sat_focus / sat_below / sat_above -- working exposure 2480 us: lit film
      ~145 counts, the laser + its rings SATURATED; in focus, ~2 um below
      (-100 kim steps) and ~2 um above (+100);
  af_focus -- the autofocus exposure 65 us: background ~3, peak ~197.
The file is looked for at $AALTOFLOW_LAB_CAPTURES/spot_frames_2026-09-29.npz,
else in the private lab repo next to this checkout
(..\\aaltoflow-lab\\captures\\ from the repo root). Missing -> every test here
is SKIPPED (the public CI has no private data; test_spot_saturated_laser.py
covers the same geometry synthetically).

On 9da7a07, with the rig's live config (max_area_px 2000): the laser + rings
(3.4-7.8 k px^2) were rejected "larger than max area" and blob AND peak
returned a ring fragment / speck 45-52 px off (sat_*), or nothing; only
af_focus was right.

Tolerances (the crop centre is the ROUNDED calibration; the unsaturated
calibration on af_focus itself says the spot is at ~(199.3, 200.3)):
  * in focus: within 2 px of the crop centre;
  * defocused (+-2 um): within 4 px -- the centroid of a SATURATED defocused
    spot is the centre of its clipped, lopsided top and rings (astigmatism:
    the sigma_x^2 and sigma_y^2 foci are 0.7 um apart on this rig), and the
    open-loop kim Z may shift the sample by a pixel or two; measured 1.6-2.6 px.
"""

import math
import os
import time
from pathlib import Path

import numpy as np
import pytest

from camera import vision as V
from camera.backends.sim import SimCamera, SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config

NAME = "spot_frames_2026-09-29.npz"


def _find_npz():
    env = os.environ.get("AALTOFLOW_LAB_CAPTURES", "").strip()
    cands = [Path(env) / NAME] if env else []
    # tests/ -> camera-control -> imaging -> modules -> AaltoFlow checkout -> its parent
    root = Path(__file__).resolve().parents[4]
    cands.append(root.parent / "aaltoflow-lab" / "captures" / NAME)
    for p in cands:
        if p.is_file():
            return p
    return None


NPZ = _find_npz()
pytestmark = pytest.mark.skipif(NPZ is None, reason="private lab frames not available "
                                "(set AALTOFLOW_LAB_CAPTURES to the captures folder)")

CENTRE = (200.0, 200.0)
TOL = {"sat_focus": 2.0, "af_focus": 2.0, "sat_below": 4.0, "sat_above": 4.0}


@pytest.fixture(scope="module")
def frames():
    with np.load(NPZ) as d:
        return {k: np.array(d[k]) for k in ("sat_focus", "sat_below", "sat_above", "af_focus")}


def _rig_cfg(**kw):
    """The rig's Spot config at capture time, with max area automatic (0)."""
    sp = Config().spot
    for k, v in dict(thr_lower=200, min_area_px=4, max_area_px=0, lookup_region_px=200,
                     search_shape="circle", locate_k=5.0, max_elongation=3.0).items():
        setattr(sp, k, v)
    for k, v in kw.items():
        setattr(sp, k, v)
    return sp


def _off(x, y):
    return math.hypot(x - CENTRE[0], y - CENTRE[1])


@pytest.mark.parametrize("key", ["sat_focus", "sat_below", "sat_above", "af_focus"])
@pytest.mark.parametrize("mode", ["blob", "peak"])
def test_locate_finds_the_laser_on_every_real_frame(frames, key, mode):
    for i, f in enumerate(frames[key]):
        loc = V.locate_spot(f, CENTRE, _rig_cfg(), mode)
        assert loc.ok, (key, i, loc.why)
        assert _off(loc.x, loc.y) < TOL[key], (key, i, mode, loc.x, loc.y)
        if key.startswith("sat"):
            assert loc.area > 3000, (key, i, loc.area)      # the disc WITH its rings


@pytest.mark.parametrize("key", ["sat_focus", "sat_below", "sat_above"])
def test_the_rigs_explicit_2000_px_says_why_instead_of_a_fragment(frames, key):
    for f in frames[key]:
        for mode in ("blob", "peak"):
            loc = V.locate_spot(f, CENTRE, _rig_cfg(max_area_px=2000), mode)
            assert not loc.ok, (key, mode, loc.x, loc.y)
            assert "larger than max area 2000" in loc.why and "automatic" in loc.why


def test_calibration_saturated_on_the_working_frames(frames):
    for f in frames["sat_focus"]:
        r = V.find_spot_for_calibration(f, _rig_cfg(), "saturated", calib_xy=CENTRE)
        assert r.ok, r.why
        assert _off(r.x, r.y) < TOL["sat_focus"], (r.x, r.y)


def test_calibration_unsaturated_on_the_af_frames(frames):
    for f in frames["af_focus"]:
        r = V.find_spot_for_calibration(f, _rig_cfg(), "unsaturated", calib_xy=CENTRE)
        assert r.ok, r.why
        assert _off(r.x, r.y) < TOL["af_focus"], (r.x, r.y)


@pytest.mark.parametrize("key", ["sat_focus", "sat_below", "sat_above", "af_focus"])
def test_the_sizes_are_measured_at_the_located_spot(frames, key):
    """What the live size check does per frame: the fixed-threshold area
    (find_spot in the region, max area automatic), the relative area and the
    second moment around the located centre."""
    sp = _rig_cfg()
    for f in frames[key]:
        loc = V.locate_spot(f, CENTRE, sp, "blob")
        assert loc.ok
        rel = V.spot_relative_area(f, (loc.x, loc.y), sp)
        mom = V.spot_second_moment(f, (loc.x, loc.y), sp)
        assert rel.ok and rel.area > 0, rel.why
        assert mom.ok and mom.sigma2 > 0, mom.why
        if key.startswith("sat"):
            det = V.find_spot(f, sp.thr_lower, sp.thr_upper, True, sp.lookup_region_px, CENTRE,
                              sp.min_area_px, sp.max_area_px, sp.reject_border,
                              sp.search_shape, 0, symmetric=True)
            assert det.found and det.area > 1000


class FrameCam(SimCamera):
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


@pytest.mark.parametrize("key", ["sat_focus", "sat_above"])
def test_the_brain_locates_and_sizes_the_saturated_laser_without_a_warning(frames, key):
    cfg = Config()
    cfg.camera.frame_rate = 100.0
    for k, v in dict(lookup_region_px=200, search_shape="circle", min_area_px=4,
                     thr_lower=200, locate="blob").items():
        setattr(cfg.spot, k, v)
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = SimZFocus(z0=30.0, z_focus=30.0, vmin=cfg.limits.z_min_v, vmax=cfg.limits.z_max_v)
    cam = FrameCam(list(frames[key]), xy, z, width=401, height=401, spot_px=CENTRE,
                   pixel_size_x_um=cfg.image.pixel_size_x_um,
                   pixel_size_y_um=cfg.image.pixel_size_y_um)
    brain = Camera(cam, xy, z, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    try:
        brain.set_spot_position(*CENTRE)
        f0 = brain.status().frame_number
        assert _wait(lambda: brain.status().frame_number > f0 + 12)
        s = brain.status()
        assert _off(s.spot_found_x, s.spot_found_y) < TOL[key], (s.spot_found_x, s.spot_found_y)
        assert math.isfinite(s.spot_size) and s.spot_size > 0, s.spot_size_why
        assert s.spot_found                                  # the threshold area too
        assert not [m for _l, m in events if "px from its calibrated" in m]
    finally:
        brain.shutdown()
