"""Autofocus rules from the D4sigma rig test (lab PC, 63x, 2026-09-28).

What the rig showed, and what is proved here (every test failed on the code of
commit c6f60be):

  1. A one_way run whose park walk never gets back within park_tolerance used
     to park BY THE STEP COUNTER and still report af_error "OK" (only an event
     said so) -- on the rig up to 3.7 focal depths off, 5 times in ~90 runs, with
     a waiting scan none the wiser. Now it is a FAILED run: af_error says why,
     so describe's wait.check makes a scan routine raise, and -- like every
     failed run -- Z goes back to where it started.
  2. park_tolerance per MECHANISM: sigma^2 is flat at the bottom (a parabola),
     so the 10 % that suits spot_area parks ~0.3 Rayleigh ranges early for
     spot_d4sigma. The lab's number is 0.04 for spot_d4sigma / spot_relative.
  3. spot_area on an UNSATURATED spot: the code minimises the thresholded area,
     which assumes a saturated spot (it only grows with defocus). Unsaturated,
     the fixed-threshold area is LARGEST at focus -- rig: 0/15 good runs, 14
     parked 1.2 focal depths off. Not silently flipped: a warning (event +
     status af_hint) that points to spot_d4sigma.
"""

import time

import numpy as np
import pytest

from camera.backends.sim import SimCamera, SimSlipStickZ, SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config, load_config, save_config

ZF = 30.0


def _wait(cond, timeout=60.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


def _rig(coherent=False, slip=True):
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.hardware.z_step_time_ms = 0.0
    cfg.autofocus.averages_per_level = 1
    if coherent:
        cfg.spot.lookup_region_px = 150
        cfg.spot.thr_lower = 150
        cfg.autofocus.averages_per_level = 2
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = (SimSlipStickZ if slip else SimZFocus)(z0=ZF, z_focus=ZF, vmin=cfg.limits.z_min_v,
                                               vmax=cfg.limits.z_max_v)
    kw = ({"spot_model": "coherent", "noise": 1.5} if coherent else {"base_sigma": 8.0})
    cam = SimCamera(xy, z, pixel_size_x_um=cfg.image.pixel_size_x_um,
                    pixel_size_y_um=cfg.image.pixel_size_y_um, **kw)
    brain = Camera(cam, xy, z, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    return brain, z, events


def _run(brain):
    brain.autofocus()
    assert _wait(lambda: not brain.status().af_running, 120)
    return brain.status()


# --------------------------------------------------------------------------- #
# 1. a park that never gets there is a FAILED run
# --------------------------------------------------------------------------- #
def test_a_park_that_never_gets_within_tolerance_fails_the_run():
    brain, z, events = _rig()
    try:
        brain.calibrate_spot(10)
        brain.cfg.autofocus.routine = "one_way"
        z.set_z(z.read_z() - 6.0 / z.down_gain)            # 6 below focus (true)
        before = z.read_z()
        # Spoil the image during the PARK only (the fine walk has published its
        # best by then): every park level reads 50 % worse than the fine best,
        # so no level can ever be within park_tolerance -- the rig's case of a
        # park walk that passes focus without seeing it.
        orig = brain._focus_metric

        def spoiled(gray, *a, **k):
            m = orig(gray, *a, **k)
            c = brain._af_curve
            parking = (c.get("best") is not None
                       or bool(c.get("phases", {}).get("park", {}).get("z")))
            return 1.5 * m if parking and np.isfinite(m) else m
        brain._focus_metric = spoiled
        s = _run(brain)
        # FAILED, and it says why (not just an exception type)
        assert s.af_error != "OK"
        assert "park" in s.af_error and "tolerance" in s.af_error, s.af_error
        # the failed-run rule: Z back where it started (by the counter)
        assert abs(z.read_z() - before) < 1e-6
        assert any(lvl == "error" and "Z back to" in m for lvl, m in events)
        # describe's wait block raises on it (check af_error == "OK")
        from camera.net.describe import build_manifest
        pytest.importorskip("zmq")
        af = next(p for p in build_manifest(brain)["parameters"] if p["id"] == "autofocus")
        assert af["wait"]["check"] == {"key": "af_error", "equals": "OK"}
    finally:
        brain.shutdown()


def test_a_good_park_is_still_ok():
    brain, z, events = _rig()
    try:
        brain.calibrate_spot(10)
        brain.cfg.autofocus.routine = "one_way"
        z.set_z(z.read_z() - 6.0 / z.down_gain)
        s = _run(brain)
        assert s.af_error == "OK"
        assert abs(z.true_z() - ZF) < 0.6
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 2. park_tolerance per mechanism
# --------------------------------------------------------------------------- #
def test_park_tolerance_defaults_per_mechanism(tmp_path):
    cfg = Config()
    af = cfg.autofocus
    assert af.park_tolerance == pytest.approx(0.10)          # spot_area / edges / fft
    assert af.park_tolerance_d4sigma == pytest.approx(0.04)
    assert af.park_tolerance_relative == pytest.approx(0.04)
    brain = Camera(None, None, None, cfg)
    for mech, want in (("spot_area", 0.10), ("edges", 0.10), ("fft", 0.10),
                       ("spot_d4sigma", 0.04), ("spot_relative", 0.04)):
        af.mechanism = mech
        assert brain.park_tolerance() == pytest.approx(want), mech
    # 0 = "no own value": the generic one applies (the way to go back to it)
    af.park_tolerance_d4sigma = 0.0
    af.mechanism = "spot_d4sigma"
    assert brain.park_tolerance() == pytest.approx(0.10)
    # and they are saved like every other setting
    af.park_tolerance_relative = 0.07
    save_config(cfg, str(tmp_path / "c.ini"))
    back = load_config(str(tmp_path / "c.ini")).autofocus
    assert (back.park_tolerance_d4sigma, back.park_tolerance_relative) == (0.0, 0.07)


def test_one_way_on_d4sigma_parks_with_its_own_tolerance():
    brain, z, events = _rig(coherent=True)
    try:
        brain.calibrate_spot(10)
        af = brain.cfg.autofocus
        af.mechanism, af.routine, af.max_travel_v = "spot_d4sigma", "one_way", 40.0
        z.set_z(z.read_z() - 6.0 / z.down_gain)
        s = _run(brain)
        assert s.af_error == "OK", s.af_error
        c = brain.get_af_curve()["phases"]
        fine = [m for m in c["fine"]["metric"] if np.isfinite(m)]
        parked = c["park"]["metric"][-1]
        assert parked <= 1.04 * min(fine) + 1e-9           # 4 %, not the generic 10 %
        assert any("tolerance 4 %" in m for _l, m in events), events[-3:]
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 3. spot_area expects a SATURATED spot -- say so when it is not
# --------------------------------------------------------------------------- #
def test_spot_area_on_an_unsaturated_spot_warns_and_names_d4sigma():
    brain, z, events = _rig(coherent=True, slip=False)       # coherent: peak 220 < 255
    try:
        brain.calibrate_spot(10)
        # at calibration already: the in-focus spot is not saturated
        assert _wait(lambda: "spot_d4sigma" in brain.status().af_hint)
        af = brain.cfg.autofocus
        af.mechanism, af.routine, af.drive_amplitude_v, af.steps = "spot_area", "sweep", 6.0, 7
        s = _run(brain)
        assert s.af_error == "OK"            # not refused, and not flipped: a warning
        assert any(lvl == "warn" and "saturated" in m and "spot_d4sigma" in m
                   for lvl, m in events), events[-4:]
        assert "spot_d4sigma" in brain.status().af_hint
        # another mechanism: the hint is gone
        af.mechanism = "spot_d4sigma"
        f0 = brain.status().frame_number
        assert _wait(lambda: brain.status().frame_number > f0 + 1)
        assert brain.status().af_hint == ""
    finally:
        brain.shutdown()


def test_spot_area_on_a_saturated_spot_says_nothing():
    brain, z, events = _rig(coherent=False, slip=False)      # the toy spot peaks at 255
    try:
        brain.calibrate_spot(10)
        af = brain.cfg.autofocus
        af.mechanism, af.routine, af.drive_amplitude_v, af.steps = "spot_area", "sweep", 6.0, 7
        s = _run(brain)
        assert s.af_error == "OK"
        assert brain.status().af_hint == ""
        assert not any("spot_d4sigma" in m for _l, m in events)
    finally:
        brain.shutdown()
