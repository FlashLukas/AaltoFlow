"""WHERE the spot is, apart from HOW BIG it is -- and more sizes (2026-09-29).

The rig (Lukas's screenshot): the spot plainly visible ~100 px from the
calibrated position, at the edge of the search region, and the Spot tab said
"D4sigma: no spot above the noise / relative area: no spot above the noise /
SATURATED". The sizes were centred on the CALIBRATED position, and the mirror
test (reject_asymmetric) mirrored the light through it -- a spot not exactly
there has no twin, so all of it was thrown away.

Proved here (the brain-level tests failed on commit 429497c):
  * the rig situation in the simulator: "no spot" with the old centring; found
    and measured right with locate = peak / blob; the mirror test about the
    LOCATED centre keeps the spot and still drops the sample's pattern;
  * the calibrated position is untouched by locating (motion uses it);
  * a why-text that says where the light is when nothing was measured;
  * encircled energy (analytic r86 of a Gaussian, the coherent donut), a 2-D
    Gaussian fit, the peak -- as sizes, as the live readout and as autofocus
    mechanisms;
  * Calibrate spot finds the spot anywhere, saturated or not, and refuses two
    equally good candidates;
  * saturation is information (once per episode), never a refusal.
"""

import math
import time

import numpy as np
import pytest

from camera import vision as V
from camera.backends.sim import SimCamera, SimSlipStickZ, SimXYStage, SimZFocus
from camera.camera import Camera, status_to_dict
from camera.config import (FOCUS_MECHANISMS, LOCATE_MODES, SIZE_METHODS, Config,
                           load_config, save_config)

H, W = 480, 640
C = (320.0, 240.0)
ZF = 30.0


def _grid():
    yy, xx = np.mgrid[:H, :W].astype(np.float64)
    return xx, yy


def _frame(intensity, bg=10.0, noise=0.0, seed=0, as_uint8=True):
    f = bg + intensity + np.random.default_rng(seed).normal(0.0, noise, intensity.shape)
    return np.clip(np.rint(f), 0, 255).astype(np.uint8) if as_uint8 else f


def _gauss(s, peak=200.0, c=C):
    xx, yy = _grid()
    return peak * np.exp(-((xx - c[0]) ** 2 + (yy - c[1]) ** 2) / (2 * s * s))


def _wait(cond, timeout=30.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


# --------------------------------------------------------------------------- #
# vision: the new sizes on exact shapes
# --------------------------------------------------------------------------- #
def test_encircled_radius_of_a_gaussian_is_analytic():
    s = 6.0
    e = V.spot_encircled(_frame(_gauss(s), as_uint8=False), C, {"lookup_region_px": 100})
    assert e.ok
    assert e.r_px == pytest.approx(s * math.sqrt(-2 * math.log(0.14)), rel=0.01)
    assert e.d_px == pytest.approx(2 * e.r_px)
    e = V.spot_encircled(_frame(_gauss(s), as_uint8=False), C,
                         {"lookup_region_px": 100, "encircled_fraction": 0.5})
    assert e.r_px == pytest.approx(s * math.sqrt(-2 * math.log(0.5)), rel=0.01)


def test_encircled_radius_of_a_donut_is_analytic_and_the_hole_does_not_matter():
    """LG01 doughnut I ~ u exp(-u), u = 2 r^2 / w^2: enclosed 1 - (1 + u) e^-u."""
    w = 14.0
    xx, yy = _grid()
    r2 = ((xx - C[0]) ** 2 + (yy - C[1]) ** 2) / (w * w)
    frame = _frame(200.0 / math.exp(-1) * r2 * np.exp(-2 * r2), as_uint8=False)
    lo, hi = 0.1, 20.0
    for _ in range(80):                                   # (1 + u) e^-u = 0.14
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if (1 + mid) * math.exp(-mid) > 0.14 else (lo, mid)
    want = w * math.sqrt(0.5 * lo)
    e = V.spot_encircled(frame, (C[0] + 3, C[1] - 2), {"lookup_region_px": 100})
    assert e.ok and e.r_px == pytest.approx(want, rel=0.01)


def test_the_gaussian_fit_recovers_sigma_on_a_noisy_frame():
    f = _frame(_gauss(5.0, peak=180.0), bg=12.0, noise=2.0, seed=4)
    g = V.spot_gauss_fit(f, C, {"lookup_region_px": 100})
    assert g.ok and not g.saturated
    assert g.sigma2 == pytest.approx(25.0, rel=0.03)
    assert (g.x0, g.y0) == pytest.approx(C, abs=0.05)
    assert g.amplitude == pytest.approx(180.0, rel=0.03)
    assert g.offset == pytest.approx(12.0, abs=0.5)
    assert g.r2 > 0.98


def test_a_saturated_gaussian_is_fitted_but_flagged():
    g = V.spot_gauss_fit(_frame(_gauss(5.0, peak=600.0)), C, {"lookup_region_px": 100})
    assert g.saturated


def test_saturation_info_says_how_much_and_estimates_the_exposure():
    f = _frame(_gauss(6.0, peak=500.0), bg=10.0)          # peak 510 -> clipped at 255
    frac, factor = V.saturation_info(f, C, {"lookup_region_px": 100})
    assert 0.1 < frac < 0.6
    # the true peak is ~500 above background: 0.8 x 255 needs ~x0.39
    assert factor == pytest.approx((0.8 * 255 - 10) / 500.0, rel=0.25)
    frac, factor = V.saturation_info(_frame(_gauss(6.0, peak=100.0)), C,
                                     {"lookup_region_px": 100})
    assert frac == 0.0 and factor == pytest.approx((0.8 * 255 - 10) / 100.0, rel=0.05)


# --------------------------------------------------------------------------- #
# vision: locating
# --------------------------------------------------------------------------- #
OFF = (C[0] - 95.0, C[1])            # a calibration 95 px off: the spot at the region edge


@pytest.mark.parametrize("mode", ["peak", "blob"])
def test_locate_finds_the_spot_at_the_edge_of_the_region(mode):
    f = _frame(_gauss(5.0), noise=1.5, seed=1)
    loc = V.locate_spot(f, OFF, {"lookup_region_px": 100}, mode)
    assert loc.ok and (loc.x, loc.y) == pytest.approx(C, abs=0.6)
    loc = V.locate_spot(f, OFF, {"lookup_region_px": 100}, "calibrated")
    assert loc.ok and (loc.x, loc.y) == OFF                  # "calibrated" = as before


def test_blob_picks_the_brightest_not_the_biggest():
    """A big dim sample feature carries more light than a small bright spot."""
    f = np.full((H, W), 10.0)
    f[200:260, 400:480] += 60.0                           # 4800 px x 60 = 288 000 counts
    f += _gauss(4.0, peak=180.0)                          # ~18 000 counts
    loc = V.locate_spot(_frame(f - 10.0), None, {"lookup_region_px": 0,
                                                "max_area_px": 20000}, "blob")
    assert loc.ok and (loc.x, loc.y) == pytest.approx(C, abs=0.5)


def test_locate_says_why_when_there_is_nothing():
    loc = V.locate_spot(_frame(np.zeros((H, W)), noise=1.5), C, {"lookup_region_px": 80}, "blob")
    assert not loc.ok and "noise" in loc.why


# --------------------------------------------------------------------------- #
# vision: calibration finders
# --------------------------------------------------------------------------- #
def test_calibration_finds_a_saturated_and_an_unsaturated_spot_anywhere():
    spot = (120.5, 380.25)
    sat = _frame(_gauss(5.0, peak=600.0, c=spot), noise=1.5)
    r = V.find_spot_for_calibration(sat, {"thr_lower": 200}, "saturated")
    assert r.ok and (r.x, r.y) == pytest.approx(spot, abs=0.3)
    uns = _frame(_gauss(5.0, peak=90.0, c=spot), noise=1.5)
    r = V.find_spot_for_calibration(uns, {"thr_lower": 200}, "saturated")
    assert not r.ok and "threshold" in r.why              # below the threshold: says so
    r = V.find_spot_for_calibration(uns, {}, "unsaturated")
    assert r.ok and (r.x, r.y) == pytest.approx(spot, abs=0.3)


def test_calibration_refuses_two_equally_good_candidates():
    two = _gauss(5.0, peak=150.0, c=(200.0, 200.0)) + _gauss(5.0, peak=140.0, c=(450.0, 300.0))
    r = V.find_spot_for_calibration(_frame(two, noise=1.0), {}, "unsaturated")
    assert not r.ok and "two" in r.why
    two = _gauss(5.0, peak=600.0, c=(200.0, 200.0)) + _gauss(5.0, peak=600.0, c=(450.0, 300.0))
    r = V.find_spot_for_calibration(_frame(two), {"thr_lower": 200}, "saturated")
    assert not r.ok and "two" in r.why
    one_and_faint = _gauss(5.0, peak=150.0, c=(200.0, 200.0)) + _gauss(5.0, peak=40.0,
                                                                          c=(450.0, 300.0))
    r = V.find_spot_for_calibration(_frame(one_and_faint, noise=1.0), {}, "unsaturated")
    assert r.ok and (r.x, r.y) == pytest.approx((200.0, 200.0), abs=0.3)


# --------------------------------------------------------------------------- #
# the brain, in the simulator: the rig situation
# --------------------------------------------------------------------------- #
def _rig(peak=220.0, slip=False, region=100):
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.hardware.z_step_time_ms = 0.0
    cfg.autofocus.averages_per_level = 2
    cfg.spot.lookup_region_px = region
    cfg.spot.thr_lower = 150
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = (SimSlipStickZ if slip else SimZFocus)(z0=ZF, z_focus=ZF, vmin=cfg.limits.z_min_v,
                                               vmax=cfg.limits.z_max_v)
    cam = SimCamera(xy, z, pixel_size_x_um=cfg.image.pixel_size_x_um,
                    pixel_size_y_um=cfg.image.pixel_size_y_um, spot_model="coherent",
                    noise=1.5, coherent_peak=peak)
    brain = Camera(cam, xy, z, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    return brain, cam, z, events


def _next(brain, n=3):
    f0 = brain.status().frame_number
    assert _wait(lambda: brain.status().frame_number > f0 + n)
    return brain.status()


def test_the_rig_case_old_centring_sees_no_spot_locate_finds_it():
    brain, cam, z, events = _rig()
    try:
        brain.calibrate_spot(8)
        true = cam.coherent.true_sigma2(0.0)
        brain.set_spot_position(cam.spot_px[0] - 95.0, cam.spot_px[1])   # stale calibration
        s = _next(brain)
        assert brain.cfg.spot.locate == "calibrated"                     # the default
        assert not math.isfinite(s.spot_sigma2_px2)                      # the rig: "no spot"
        # ...and it says WHERE the light is instead
        assert "95 px" in s.spot_size_why or "94 px" in s.spot_size_why, s.spot_size_why
        assert "locate" in s.spot_size_why and "recalibrate" in s.spot_size_why
        for mode in ("peak", "blob"):
            brain.cfg.spot.locate = mode
            s = _next(brain)
            assert (s.spot_found_x, s.spot_found_y) == pytest.approx(cam.spot_px, abs=0.6)
            assert s.spot_offset_px == pytest.approx(95.0, abs=0.7)
            assert s.spot_sigma2_px2 == pytest.approx(true, rel=0.05), mode
            assert s.spot_size_why == ""
            # motion keeps the CALIBRATED position
            assert (s.spot_x, s.spot_y) == pytest.approx((cam.spot_px[0] - 95.0,
                                                          cam.spot_px[1]))
        # a located spot far from its calibration is warned about (once)
        assert _wait(lambda: any("calibrat" in m and "px from" in m for _l, m in events))
    finally:
        brain.shutdown()


def test_the_mirror_test_about_the_located_centre_keeps_the_spot_drops_the_pattern():
    brain, cam, z, events = _rig(region=150)          # the pattern glyph IS in the region
    try:
        brain.calibrate_spot(8)
        true = cam.coherent.true_sigma2(0.0)
        brain.set_spot_position(cam.spot_px[0] - 60.0, cam.spot_px[1] + 20.0)
        brain.cfg.spot.locate = "blob"
        s = _next(brain)
        assert s.spot_sigma2_px2 == pytest.approx(true, rel=0.05)
        brain.cfg.spot.reject_asymmetric = False
        s = _next(brain)
        assert s.spot_sigma2_px2 > 1.5 * true                      # the pattern counted
    finally:
        brain.shutdown()


def test_every_size_is_in_status_and_the_live_readout_defaults_to_relative():
    brain, cam, z, events = _rig()
    try:
        brain.calibrate_spot(8)
        s = _next(brain)
        assert brain.cfg.spot.size_method == "relative"
        assert s.spot_size == s.spot_rel_area and s.spot_rel_area > 0
        w = cam.coherent.true_sigma2(0.0)
        assert s.spot_d86_px > 0 and math.isfinite(s.spot_d86_px)
        # the coherent spot is NOT a Gaussian (a centre and a ring): the fit
        # follows the core, the second moment counts the ring's light too --
        # so the fit's sigma^2 is smaller (measured 19 vs 58 px^2 at focus)
        assert 0 < s.spot_gauss_sigma2_px2 < w
        assert 0.5 < s.spot_gauss_r2 <= 1.0
        assert s.spot_peak_avg > 100
        for method, key in (("encircled", "spot_d86_px"), ("gauss", "spot_gauss_sigma2_px2"),
                            ("peak", "spot_peak_avg"), ("threshold", "spot_area")):
            brain.cfg.spot.size_method = method
            s = _next(brain)
            assert s.spot_size == getattr(s, key), method
        d = status_to_dict(s)
        for k in ("spot_found_x", "spot_offset_px", "spot_size_why", "spot_d86_px",
                  "spot_gauss_sigma2_px2", "spot_gauss_r2", "spot_peak_avg",
                  "spot_sat_fraction", "spot_exposure_hint"):
            assert k in d
        # the calibration records the new sizes as references too
        assert brain.cfg.spot.ref_d86_px > 0 and brain.cfg.spot.ref_gauss_sigma2 > 0
    finally:
        brain.shutdown()


def test_saturation_is_information_once_per_episode_and_never_blocks():
    brain, cam, z, events = _rig(peak=700.0)
    try:
        brain.calibrate_spot(8)
        assert _wait(lambda: brain.status().spot_saturated)
        s = _next(brain, 20)
        # measured anyway (a saturated spot is a spot)
        assert math.isfinite(s.spot_sigma2_px2) and math.isfinite(s.spot_d86_px)
        assert 0 < s.spot_sat_fraction < 1
        assert 0 < s.spot_exposure_hint < 1                 # "shorter", an estimate
        said = [(l, m) for l, m in events if "SATURATED" in m]
        assert len(said) == 1, said                         # ONE line for the episode
        lvl, msg = said[0]
        assert lvl == "info"
        assert "unaffected" in msg.lower() and "Gaussian" in msg and "too large" in msg
        assert "size is wrong" not in msg
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# calibrate spot: anywhere, both kinds, reporting where and how far
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode,peak", [("saturated", 700.0), ("unsaturated", 220.0)])
def test_calibrate_spot_finds_it_anywhere_and_reports_the_move(mode, peak):
    brain, cam, z, events = _rig(peak=peak)
    try:
        brain.set_spot_position(100.0, 100.0)               # a stale calibration, far away
        brain.cfg.spot.calib_mode = mode
        res = brain.calibrate_spot(8)
        assert (res["x"], res["y"]) == pytest.approx(cam.spot_px, abs=0.5)
        assert res["moved_px"] == pytest.approx(math.hypot(cam.spot_px[0] - 100.0,
                                                           cam.spot_px[1] - 100.0), abs=1.0)
        assert res["mode"] == mode and res["frames"] >= 2 and res["jitter_px"] < 0.5
        assert brain.cfg.spot.ref_set
    finally:
        brain.shutdown()


def test_calibrate_spot_refuses_cleanly_with_the_reason():
    brain, cam, z, events = _rig(peak=120.0)
    try:
        brain.cfg.spot.calib_mode = "saturated"             # 120 never reaches thr 150
        with pytest.raises(RuntimeError, match="threshold"):
            brain.calibrate_spot(4)
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# autofocus with each new mechanism (coherent sim), landing errors
# --------------------------------------------------------------------------- #
LANDING = {}


@pytest.mark.parametrize("mech", ["spot_encircled", "spot_gauss", "spot_peak"])
@pytest.mark.parametrize("routine", ["sweep", "one_way"])
def test_autofocus_with_the_new_mechanisms(mech, routine):
    brain, cam, z, events = _rig(slip=(routine == "one_way"))
    try:
        brain.calibrate_spot(8)
        af = brain.cfg.autofocus
        af.mechanism, af.routine, af.max_travel_v = mech, routine, 40.0
        af.drive_amplitude_v, af.steps = 16.0, 33
        if routine == "one_way":
            z.set_z(z.read_z() - 4.0 / z.down_gain)
        else:
            z.set_z(ZF - 4.0)
        brain.autofocus()
        assert _wait(lambda: not brain.status().af_running, 120)
        s = brain.status()
        err = (z.true_z() if hasattr(z, "true_z") else z.read_z()) - ZF
        LANDING[(mech, routine)] = err
        assert s.af_error == "OK", s.af_error
        # Measured 2026-09-29 (starts 4 below; true focus = the sigma^2 waist):
        #   sweep:   encircled +1.32, gauss -0.67, peak -0.52 (d4sigma -0.08)
        #   one_way: encircled +1.02, gauss -0.52, peak -0.81 (d4sigma  0.00)
        # Only sigma^2 is a parabola with its vertex AT the waist for this
        # coherent spot; the others have their optimum where the spot's SHAPE
        # (centre vs ring) favours them -- as spot_area / spot_relative do.
        bound = {"spot_encircled": 1.6, "spot_gauss": 1.0, "spot_peak": 1.2}[mech]
        assert abs(err) < bound, err
    finally:
        brain.shutdown()


def test_config_round_trip_of_the_new_fields(tmp_path):
    cfg = Config()
    assert "spot_encircled" in FOCUS_MECHANISMS and "spot_peak" in FOCUS_MECHANISMS
    assert LOCATE_MODES == ("calibrated", "peak", "blob")
    assert set(SIZE_METHODS) >= {"threshold", "relative", "d4sigma", "encircled", "gauss",
                                 "peak"}
    sp = cfg.spot
    sp.locate, sp.encircled_fraction, sp.calib_mode = "blob", 0.9, "unsaturated"
    sp.calib_at_af_exposure, sp.locate_k, sp.size_method = True, 4.0, "gauss"
    save_config(cfg, str(tmp_path / "c.ini"))
    b = load_config(str(tmp_path / "c.ini")).spot
    assert (b.locate, b.encircled_fraction, b.calib_mode, b.calib_at_af_exposure,
            b.locate_k, b.size_method) == ("blob", 0.9, "unsaturated", True, 4.0, "gauss")
    sp.locate, sp.calib_mode = "track", "bright"             # not choices: sanitised
    save_config(cfg, str(tmp_path / "c.ini"))
    b = load_config(str(tmp_path / "c.ini")).spot
    assert (b.locate, b.calib_mode) == ("calibrated", "saturated")
    # an OLD ini without the new keys still loads
    (tmp_path / "old.ini").write_text("[Spot]\nthr_lower = 180\nsize_method = d4sigma\n",
                                      encoding="utf-8")
    b = load_config(str(tmp_path / "old.ini")).spot
    assert b.thr_lower == 180 and b.size_method == "d4sigma" and b.locate == "calibrated"


def test_the_new_fields_travel_over_the_wire():
    pytest.importorskip("zmq")
    from camera.net.client import CameraClient
    from camera.net.service import CameraService
    brain, cam, z, events = _rig()
    svc = CameraService(brain, host="127.0.0.1", cmd_port=15718, pub_port=15719, status_hz=20)
    svc.start()
    cli = CameraClient("127.0.0.1", 15718, 15719, timeout_ms=5000)
    cli.start()
    try:
        cli.set_config({"spot": {"locate": "peak", "size_method": "encircled"}})
        assert brain.cfg.spot.locate == "peak"
        assert _wait(lambda: cli.status().spot_size_method == "encircled"
                     and math.isfinite(cli.status().spot_found_x))
        res = cli.calibrate_spot(4)
        assert "moved_px" in res and "mode" in res
    finally:
        cli.close()
        svc.stop()
        brain.shutdown()
