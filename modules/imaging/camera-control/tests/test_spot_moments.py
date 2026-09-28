"""The spot SIZE without a fixed threshold (2026-09-28).

Lukas's decision: "the laser is highly coherent, so a defocused spot has RINGS
and can have a HOLE in the centre ... any threshold (fixed, FWHM, Otsu) breaks.
Use the SECOND MOMENT sigma^2 = sum(I r^2)/sum(I) about the intensity centroid
(ISO 11146 D4sigma). For ANY coherent beam w^2(z) = w0^2 + c (z-z0)^2 exactly."

What is proved here, in this order:
  * vision.spot_second_moment on shapes whose moments are known exactly
    (Gaussian, ellipse, donut), with background, noise, a neighbour, and a
    saturated spot -- and why the textbook per-pixel clip was not used;
  * vision.spot_relative_area (area above 1/e^2 of the spot's own peak);
  * the coherent simulator (backends/sim.py CoherentSpot): its analytic
    sigma^2 is the true second moment, it really has a hole, and through the
    hole the MEASURED sigma^2 is one parabola -- while the fixed-threshold area
    dips and vanishes and the relative area jumps;
  * the brain: status fields every frame, calibration, the two new autofocus
    metrics in both routines, from both sides and from far out of focus;
  * config, wire, describe, GUI, and the per-frame cost on a full-size frame.
"""

import math
import time

import numpy as np
import pytest

from camera import vision as V
from camera.backends.sim import CoherentSpot, SimCamera, SimSlipStickZ, SimXYStage, SimZFocus
from camera.camera import Camera, status_to_dict
from camera.config import Config, load_config, save_config

H, W = 480, 640
C = (320.0, 240.0)          # spot centre in the synthetic frames


def _grid():
    yy, xx = np.mgrid[:H, :W].astype(np.float64)
    return xx, yy


def _frame(intensity, bg=10.0, noise=0.0, seed=0, as_uint8=True):
    """Background + light + camera noise, digitised like an 8-bit camera."""
    f = bg + intensity + np.random.default_rng(seed).normal(0.0, noise, intensity.shape)
    return np.clip(np.rint(f), 0, 255).astype(np.uint8) if as_uint8 else f


def _gauss(sx, sy=None, peak=200.0, c=C, theta=0.0):
    sy = sx if sy is None else sy
    xx, yy = _grid()
    dx, dy = xx - c[0], yy - c[1]
    ct, st = math.cos(theta), math.sin(theta)
    u, v = ct * dx + st * dy, -st * dx + ct * dy
    return peak * np.exp(-u * u / (2 * sx * sx) - v * v / (2 * sy * sy))


# --------------------------------------------------------------------------- #
# vision: exact shapes
# --------------------------------------------------------------------------- #
def test_second_moment_of_a_gaussian_is_its_sigma_squared():
    m = V.spot_second_moment(_frame(_gauss(6.0), as_uint8=False), C, {"lookup_region_px": 100})
    assert m.ok and m.converged
    assert m.sigma2_x == pytest.approx(36.0, rel=0.01)
    assert m.sigma2_y == pytest.approx(36.0, rel=0.01)
    assert m.d4sigma == pytest.approx(24.0, rel=0.005)          # D4sigma = 4 sigma
    assert (m.cx, m.cy) == pytest.approx(C, abs=0.02)


def test_an_ellipse_gives_its_two_widths_and_the_rotated_one_its_cross_term():
    m = V.spot_second_moment(_frame(_gauss(8.0, 4.0), as_uint8=False), C,
                             {"lookup_region_px": 100})
    assert (m.sigma2_x, m.sigma2_y) == pytest.approx((64.0, 16.0), rel=0.01)
    assert abs(m.sigma2_xy) < 0.2
    # rotated by 30 deg: the covariance matrix rotates, its eigenvalues stay
    th = math.radians(30)
    m = V.spot_second_moment(_frame(_gauss(8.0, 4.0, theta=th), as_uint8=False), C,
                             {"lookup_region_px": 100})
    ev = np.linalg.eigvalsh([[m.sigma2_x, m.sigma2_xy], [m.sigma2_xy, m.sigma2_y]])
    assert ev == pytest.approx([16.0, 64.0], rel=0.015)
    assert m.sigma2_xy == pytest.approx((64 - 16) * math.sin(th) * math.cos(th), rel=0.03)


def test_a_donut_has_its_known_width_and_its_centroid_in_the_hole():
    """A ring with a dark centre: I ~ (r/w)^2 exp(-2 r^2 / w^2), the intensity
    of the LG01 'doughnut' mode. <r^2> = w^2, so sigma_x^2 = w^2 / 2 -- and a
    threshold anywhere would cut the ring into an annulus with a hole."""
    w = 14.0
    xx, yy = _grid()
    r2 = ((xx - C[0]) ** 2 + (yy - C[1]) ** 2) / (w * w)
    donut = 200.0 / math.exp(-1) * r2 * np.exp(-2 * r2) / 1.0     # peak 200 on the ring
    frame = _frame(donut, as_uint8=False)
    assert frame[int(C[1]), int(C[0])] - 10.0 < 1.0                 # the centre IS dark
    m = V.spot_second_moment(frame, (C[0] + 3, C[1] - 2), {"lookup_region_px": 100})
    assert m.ok and m.converged
    assert m.sigma2_x == pytest.approx(w * w / 2, rel=0.01)
    assert m.sigma2_y == pytest.approx(w * w / 2, rel=0.01)
    assert (m.cx, m.cy) == pytest.approx(C, abs=0.05)                # in the hole


def test_background_and_noise_are_measured_and_removed():
    frame = _frame(_gauss(6.0), bg=50.0, noise=2.0, seed=3)
    m = V.spot_second_moment(frame, C, {"lookup_region_px": 100})
    assert m.background == pytest.approx(50.0, abs=0.2)
    assert m.noise == pytest.approx(2.0, abs=0.3)
    assert m.sigma2 == pytest.approx(36.0, rel=0.03)


def test_the_textbook_per_pixel_clip_is_biased_for_a_dim_spot():
    """Why clip_mode "local" is the default: a dim, wide spot (peak 20 counts on
    2 counts of noise). Deleting every pixel below 3 noise sigmas removes the
    wings, which carry most of r^2 -> sigma^2 half its true value (measured
    74 vs 144). Deciding on a 2-px-blurred copy (noise ~7x lower) and keeping
    the raw values: 136.5, -5 %."""
    frames = [_frame(_gauss(12.0, peak=20.0), noise=2.0, seed=s) for s in range(6)]
    local = np.mean([V.spot_second_moment(f, C, {"lookup_region_px": 150}).sigma2
                     for f in frames])
    textbook = np.mean([V.spot_second_moment(f, C, {"lookup_region_px": 150,
                                                    "clip_mode": "pixel"}).sigma2
                        for f in frames])
    assert local == pytest.approx(144.0, rel=0.07)
    assert textbook < 0.6 * 144.0


def test_the_box_iteration_converges_and_does_not_care_where_it_starts():
    frame = _frame(_gauss(7.0), noise=1.5, seed=5)
    results = []
    for region, guess in ((60, C), (200, C), (120, (C[0] + 6, C[1] - 5)), (200, (C[0] - 9, C[1]))):
        m = V.spot_second_moment(frame, guess, {"lookup_region_px": region})
        assert m.ok and m.converged and m.n_iter <= 10
        results.append(m.sigma2)
        # the final box is ~3 x D4sigma wide (box_factor 1.5 each way), centred on the spot
        x0, y0, x1, y1 = m.box
        assert (x1 - x0) == pytest.approx(3 * m.d4sigma_x, abs=4)   # + whole-pixel rounding
        assert ((x0 + x1 - 1) / 2, (y0 + y1 - 1) / 2) == pytest.approx(C, abs=1.0)
    assert max(results) / min(results) < 1.01
    assert np.mean(results) == pytest.approx(49.0, rel=0.03)


def test_a_box_that_would_leave_the_search_region_says_so():
    m = V.spot_second_moment(_frame(_gauss(15.0)), C, {"lookup_region_px": 40})
    assert m.clipped


def test_saturation_is_flagged_and_inflates_sigma_squared():
    ok = V.spot_second_moment(_frame(_gauss(6.0, peak=200.0)), C, {"lookup_region_px": 100})
    sat = V.spot_second_moment(_frame(_gauss(6.0, peak=900.0)), C, {"lookup_region_px": 100})
    assert not ok.saturated and sat.saturated
    assert sat.sigma2 > 1.3 * 36.0          # the clipped peak lost power at small r


def test_a_neighbour_without_a_mirror_twin_is_left_out():
    """A bright feature of the sample 45 px from the spot: it has no twin on the
    other side of the calibrated centre, the spot does."""
    light = _gauss(6.0)
    xx, yy = _grid()
    light = light + 120.0 * ((abs(xx - (C[0] + 45)) < 8) & (abs(yy - C[1]) < 8))
    frame = _frame(light, noise=1.0)
    good = V.spot_second_moment(frame, C, {"lookup_region_px": 100})
    bad = V.spot_second_moment(frame, C, {"lookup_region_px": 100, "reject_asymmetric": False})
    assert good.sigma2 == pytest.approx(36.0, rel=0.03)
    assert bad.sigma2 > 3 * 36.0
    rel = V.spot_relative_area(frame, C, {"lookup_region_px": 100})
    assert rel.area == pytest.approx(4 * math.pi * 36.0, rel=0.05)   # the square is not in it


def test_no_spot_is_not_ok_and_not_a_number():
    m = V.spot_second_moment(_frame(np.zeros((H, W)), noise=2.0), C, {"lookup_region_px": 80})
    r = V.spot_relative_area(_frame(np.zeros((H, W)), noise=2.0), C, {"lookup_region_px": 80})
    assert not m.ok and m.why and math.isnan(m.sigma2)
    assert not r.ok and math.isnan(r.area)


def test_relative_area_of_a_gaussian_is_the_one_over_e_squared_disc():
    """1/e^2 of the peak of exp(-r^2 / 2 sigma^2) is at r = 2 sigma: area 4 pi sigma^2
    -- whatever the peak, i.e. it follows the spot as defocus dims it."""
    for peak in (200.0, 40.0):
        r = V.spot_relative_area(_frame(_gauss(6.0, peak=peak), noise=1.0), C,
                                 {"lookup_region_px": 100})
        assert r.ok and r.area == pytest.approx(4 * math.pi * 36.0, rel=0.06)
        assert (r.cx, r.cy) == pytest.approx(C, abs=0.2)


def test_the_focus_metric_without_a_calibrated_position():
    frame = _frame(_gauss(6.0), noise=1.0)
    assert V.focus_metric(frame, "spot_d4sigma") == pytest.approx(36.0, rel=0.03)
    assert V.focus_metric(frame, "spot_relative") == pytest.approx(4 * math.pi * 36, rel=0.06)
    assert not V.focus_is_maximised("spot_d4sigma") and not V.focus_is_maximised("spot_relative")


def test_the_sweep_fit_may_use_the_whole_bottom_of_a_parabola():
    z = np.arange(-6.0, 6.01, 0.5)
    m = 50.0 + 3.0 * (z - 0.37) ** 2 + np.random.default_rng(2).normal(0, 1.0, z.size)
    wide = V.best_focus_from_sweep(z, m, False, True, rel_window=2.0)
    assert wide == pytest.approx(0.37, abs=0.1)


# --------------------------------------------------------------------------- #
# the coherent simulator
# --------------------------------------------------------------------------- #
def test_the_analytic_sigma_squared_is_the_true_second_moment_of_the_model():
    """No camera, no noise: integrate the model's intensity on a big canvas and
    compare with CoherentSpot.true_sigma2 -- the formula the tests below trust."""
    c = CoherentSpot()
    yy, xx = np.mgrid[-300:301, -300:301].astype(np.float64)
    for dz in (-6.0, -2.0, 0.0, c.hole_defocus(), 7.0):
        i = c.intensity(xx, yy, 0.0, 0.0, dz)
        s2 = float((i * xx * xx).sum() / i.sum())
        assert s2 == pytest.approx(c.true_sigma2(dz), rel=0.002), dz
    zs = np.linspace(-8, 8, 9)
    s2 = [c.true_sigma2(z) for z in zs]
    p = np.polyfit(zs, s2, 2)
    assert np.allclose(np.polyval(p, zs), s2)               # exactly a parabola...
    assert -p[1] / (2 * p[0]) == pytest.approx(0.0, abs=1e-9)   # ...with its vertex at focus


def test_the_coherent_spot_has_a_hole():
    c = CoherentSpot()
    dz = c.hole_defocus()
    assert 0.5 * c.zr < dz < 1.5 * c.zr                    # a hole near focus, not far out
    r = np.linspace(0.0, 4 * c.w(dz), 400)
    prof = c.intensity(r, 0.0 * r, 0.0, 0.0, dz)
    assert prof[0] < 0.01 * prof.max()                      # a dark centre inside a ring
    focus = c.intensity(r, 0.0 * r, 0.0, 0.0, 0.0)
    assert focus[0] > 0.9 * focus.max()                     # in focus: a peaked spot


def _coherent_camera(noise=1.5, peak=220.0):
    xy = SimXYStage()
    z = SimZFocus(z0=30.0, z_focus=30.0, vmin=0.0, vmax=75.0)
    cam = SimCamera(xy, z, spot_model="coherent", noise=noise, coherent_peak=peak)
    return cam, z


def test_sigma_squared_through_the_hole_is_one_parabola_but_the_threshold_area_is_not():
    """THE test of the decision. Z from 3 units before focus to 6 after, through
    the plane where the spot is a ring with a dark centre (+2.9). Measured on
    simulated 8-bit frames with noise and the sample's pattern 120 px away:
      * sigma^2 is ONE parabola (R^2 > 0.999), its vertex at the true focus,
        every point within 5 % of the exact value (measured 2026-09-28: within
        1.3 % from -1.5 to +6 incl. the hole, 3-4 % at -3..-2, where the light
        sits in a faint outer ring and part of it is below one grey level);
      * the fixed-threshold area (threshold 60) is not even monotonic on one
        side of focus: it shrinks towards the hole, grows again, then vanishes
        altogether as the ring dims below the threshold -- an autofocus on it
        sees a 'small spot' in the wrong place;
      * the relative-threshold area (1/e^2 of the peak) jumps where the ring
        overtakes the centre as the brightest part -- documented, expected:
        any threshold breaks there, relative or not."""
    cam, z = _coherent_camera()
    sp = Config().spot
    sp.lookup_region_px = 150
    zs = np.arange(-3.0, 6.01, 0.5)
    s2, true, thr_area, rel_area = [], [], [], []
    for dz in zs:
        z.set_z(30.0 + dz)
        vals, rels, areas = [], [], []
        for _ in range(4):
            f = cam.grab()
            vals.append(V.spot_second_moment(f, cam.spot_px, sp).sigma2)
            rels.append(V.spot_relative_area(f, cam.spot_px, sp).area)
            d = V.find_spot(f, 60, 255, True, 150, cam.spot_px, 4, 20000, True, "rect", 0,
                            symmetric=True)
            areas.append(d.area if d.found else np.nan)
        s2.append(np.mean(vals)); rel_area.append(np.mean(rels)); thr_area.append(np.mean(areas))
        true.append(cam.coherent.true_sigma2(dz))
    s2, true = np.array(s2), np.array(true)
    p = np.polyfit(zs, s2, 2)
    res = s2 - np.polyval(p, zs)
    r2 = 1.0 - (res ** 2).sum() / ((s2 - s2.mean()) ** 2).sum()
    assert r2 > 0.999, r2
    assert -p[1] / (2 * p[0]) == pytest.approx(0.0, abs=0.25)      # vertex = true focus
    assert np.all(np.abs(s2 / true - 1.0) < 0.05)
    assert np.all(np.abs(s2 / true - 1.0)[zs >= -1.5] < 0.02)
    # the fixed threshold: dips, recovers, then the spot is gone for it
    a = dict(zip(np.round(zs, 2), thr_area))
    assert a[1.5] < 0.5 * a[0.5]
    assert a[2.5] > 2.0 * a[1.5]
    assert all(np.isnan(a[k]) for k in (3.5, 4.0, 5.0))
    # the relative level: a jump of more than 1.8x within half a unit
    ratios = [b / a_ for a_, b in zip(rel_area[:-1], rel_area[1:])]
    assert max(ratios) > 1.8
    assert max(s2[1:] / s2[:-1]) < 1.25                         # sigma^2 never jumps


# --------------------------------------------------------------------------- #
# the brain
# --------------------------------------------------------------------------- #
ZF = 30.0          # true focus (the sigma^2 waist) in scene units


def _wait(cond, timeout=60.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


def _rig(slip=False, peak=220.0):
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.hardware.z_step_time_ms = 0.0
    cfg.autofocus.averages_per_level = 2
    cfg.spot.lookup_region_px = 150
    cfg.spot.thr_lower = 150          # finds the in-focus spot, not the pattern (grey 120)
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = (SimSlipStickZ if slip else SimZFocus)(z0=ZF, z_focus=ZF, vmin=cfg.limits.z_min_v,
                                               vmax=cfg.limits.z_max_v)
    cam = SimCamera(xy, z, pixel_size_x_um=cfg.image.pixel_size_x_um,
                    pixel_size_y_um=cfg.image.pixel_size_y_um, spot_model="coherent",
                    noise=1.5, coherent_peak=peak)
    brain = Camera(cam, xy, z, cfg)
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    return brain, cam, z


@pytest.fixture
def rig():
    brain, cam, z = _rig()
    brain.calibrate_spot(10)          # in focus
    yield brain, cam, z
    brain.shutdown()


def test_every_frame_carries_the_three_sizes(rig):
    brain, cam, z = rig
    assert _wait(lambda: math.isfinite(brain.status().spot_d4sigma_px))
    s = brain.status()
    true = cam.coherent.true_sigma2(0.0)
    assert s.spot_sigma2_px2 == pytest.approx(true, rel=0.03)
    assert s.spot_d4sigma_px == pytest.approx(4 * math.sqrt(s.spot_sigma2_px2))
    assert (s.spot_centroid_x, s.spot_centroid_y) == pytest.approx(cam.spot_px, abs=0.3)
    assert s.spot_rel_area > 0 and not s.spot_saturated and s.spot_peak > 100
    assert s.spot_size_method == "threshold" and s.spot_size == s.spot_area
    brain.cfg.spot.size_method = "d4sigma"
    f0 = brain.status().frame_number
    assert _wait(lambda: brain.status().frame_number > f0 + 1)
    s = brain.status()
    assert s.spot_size_method == "d4sigma" and s.spot_size == s.spot_d4sigma_px
    # the position used for motion is still the calibrated one
    assert (s.spot_x, s.spot_y) == (brain.cfg.spot.ref_x, brain.cfg.spot.ref_y)


def test_calibration_records_the_in_focus_sizes(rig):
    brain, cam, z = rig
    sp = brain.cfg.spot
    assert sp.ref_d4sigma_px == pytest.approx(4 * math.sqrt(cam.coherent.true_sigma2(0.0)),
                                              rel=0.02)
    assert sp.ref_rel_area > 0
    res = brain.set_spot_position(300.0, 200.0)        # by hand: nothing measured
    assert res["d4sigma_px"] == 0.0 and sp.ref_d4sigma_px == 0.0 and sp.ref_rel_area == 0.0


def test_a_saturated_spot_is_flagged_and_warned_about():
    brain, cam, z = _rig(peak=700.0)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    try:
        brain.cfg.spot.size_method = "d4sigma"
        assert _wait(lambda: brain.status().spot_saturated)
        assert _wait(lambda: any("SATURATED" in m for _l, m in events))
    finally:
        brain.shutdown()


def _true(z):
    return z.true_z() if hasattr(z, "true_z") else z.read_z()


def _run_af(brain):
    brain.autofocus()
    assert _wait(lambda: not brain.status().af_running, 120)
    return brain.status()


@pytest.mark.parametrize("start", [-6.0, 6.0, -12.0, 12.0])
def test_sweep_on_sigma_squared_finds_the_true_focus_where_the_area_does_not(start):
    """Closed-loop Z. The second moment lands on the true focus from either side
    and from far out; the thresholded area, on the same frames, lands on the
    plane where the centre looks brightest and smallest -- 2 units off."""
    errs = {}
    for mech in ("spot_d4sigma", "spot_area"):
        brain, cam, z = _rig()
        try:
            brain.calibrate_spot(10)
            af = brain.cfg.autofocus
            af.mechanism, af.routine = mech, "sweep"
            af.drive_amplitude_v, af.steps = 30.0, 31
            z.set_z(ZF + start)
            s = _run_af(brain)
            assert s.af_error == "OK", s.af_error
            errs[mech] = _true(z) - ZF
        finally:
            brain.shutdown()
    assert abs(errs["spot_d4sigma"]) < 0.5, errs
    assert abs(errs["spot_area"]) > 1.0, errs


@pytest.mark.parametrize("start", [-6.0, 6.0, -12.0, 12.0])
def test_one_way_on_sigma_squared_lands_in_focus_on_a_hysteretic_z(start):
    """The slip-stick Z (1.0x up, 0.7x down): assertions on where the sample
    REALLY is. The coarse walk aims with sigma = sqrt(sigma^2), linear in the
    defocus far out, like the spot radius sqrt(area) of spot_area."""
    brain, cam, z = _rig(slip=True)
    try:
        brain.calibrate_spot(10)
        af = brain.cfg.autofocus
        af.mechanism, af.routine, af.max_travel_v = "spot_d4sigma", "one_way", 40.0
        gain = z.up_gain if start > 0 else z.down_gain
        z.set_z(z.read_z() + start / gain)
        s = _run_af(brain)
        assert s.af_error == "OK", s.af_error
        assert abs(_true(z) - ZF) < 0.6
        assert brain.get_af_curve()["phases"]["fine"]["z"]
    finally:
        brain.shutdown()


def test_the_relative_metric_runs_in_the_sweep(rig):
    brain, cam, z = rig
    af = brain.cfg.autofocus
    af.mechanism, af.routine, af.drive_amplitude_v, af.steps = "spot_relative", "sweep", 12.0, 25
    z.set_z(ZF + 3.0)
    s = _run_af(brain)
    assert s.af_error == "OK"
    # it lands near focus here, but not on it: the jump where the ring takes
    # over pulls its minimum (see the parabola test) -- a candidate to compare
    assert abs(_true(z) - ZF) < 1.5


def test_the_new_metrics_need_a_calibrated_spot():
    brain, cam, z = _rig()
    try:
        brain.cfg.autofocus.mechanism = "spot_d4sigma"
        s = _run_af(brain)
        assert s.af_error == "RuntimeError"
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# config, wire, describe
# --------------------------------------------------------------------------- #
def test_the_new_settings_survive_ini_and_wire(tmp_path):
    from camera.net.protocol import apply_config_dict, config_to_dict
    cfg = Config()
    sp = cfg.spot
    sp.size_method, sp.rel_level, sp.clip_sigma, sp.clip_mode = "d4sigma", 0.5, 2.5, "pixel"
    sp.detect_px, sp.min_blob_px, sp.mask_grow_px, sp.box_factor = 1.5, 12, 3, 2.0
    sp.max_iter, sp.smooth_px, sp.reject_asymmetric = 7, 1.0, False
    sp.ref_d4sigma_px, sp.ref_rel_area = 31.5, 812.0
    cfg.camera.sim_spot_model = "coherent"
    cfg.autofocus.mechanism = "spot_d4sigma"
    path = tmp_path / "c.ini"
    save_config(cfg, str(path))
    back = load_config(str(path))
    wire = Config()
    apply_config_dict(wire, config_to_dict(cfg))
    for other in (back, wire):
        assert other.spot == cfg.spot
        assert other.camera.sim_spot_model == "coherent"
        assert other.autofocus.mechanism == "spot_d4sigma"
    assert back.spot.reject_asymmetric is False                     # the bool trap
    # unknown enum values are sanitised, as for every other enum
    cfg.spot.size_method, cfg.spot.clip_mode, cfg.camera.sim_spot_model = "x", "y", "z"
    save_config(cfg, str(path))
    back = load_config(str(path))
    assert (back.spot.size_method, back.spot.clip_mode,
            back.camera.sim_spot_model) == ("threshold", "local", "gaussian")


def test_defaults_change_nothing_for_existing_setups():
    cfg = Config()
    assert cfg.spot.size_method == "threshold"
    assert cfg.autofocus.mechanism == "spot_area"
    assert cfg.camera.sim_spot_model == "gaussian"


def test_describe_lists_the_new_indicators_and_they_resolve(rig):
    pytest.importorskip("zmq")
    from camera.net.describe import build_manifest, read_path
    brain, cam, z = rig
    m = {p["id"]: p for p in build_manifest(brain)["parameters"]}
    st = status_to_dict(brain.status())
    for pid in ("spot_d4sigma", "spot_sigma2", "spot_rel_area", "spot_saturated"):
        assert m[pid]["kind"] == "indicator"
        assert read_path(st, m[pid]["read_path"]) is not None


def test_the_sizes_travel_over_the_wire():
    pytest.importorskip("zmq")
    from camera.net.client import CameraClient
    from camera.net.service import CameraService
    brain, cam, z = _rig()
    svc = CameraService(brain, host="127.0.0.1", cmd_port=15698, pub_port=15699, status_hz=20)
    svc.start()
    cli = CameraClient("127.0.0.1", 15698, 15699, timeout_ms=3000)
    cli.start()
    try:
        res = cli.calibrate_spot(8)
        assert res["d4sigma_px"] > 0 and res["rel_area"] > 0
        cli.set_config({"spot": {"size_method": "d4sigma"}})
        assert brain.cfg.spot.size_method == "d4sigma"
        assert _wait(lambda: cli.status().spot_size_method == "d4sigma"
                     and math.isfinite(cli.status().spot_d4sigma_px))
        assert cli.status().spot_size == pytest.approx(cli.status().spot_d4sigma_px)
    finally:
        cli.close()
        svc.stop()


def test_per_frame_cost_on_a_full_size_frame():
    """Only the search region is processed, so a 1936 x 1096 frame costs about
    what a 640 x 480 one does. Measured on the dev PC 2026-09-28: ~2-4 ms for
    the second moment and ~1 ms for the relative area at the default region
    (+-100 px), ~9 ms / ~2 ms at +-200 px. The bound here is loose (CI)."""
    c = CoherentSpot(w0_px=18.0)
    f = np.full((1096, 1936), 8.0)
    c.add_to(f, (968.0, 548.0), 3.0)
    f += np.random.default_rng(0).normal(0.0, 1.5, f.shape)
    g = np.clip(np.rint(f), 0, 255).astype(np.uint8)
    sp = Config().spot                           # lookup_region_px 100 (the default)
    V.spot_second_moment(g, (968.0, 548.0), sp)
    t0 = time.perf_counter()
    for _ in range(10):
        V.spot_second_moment(g, (968.0, 548.0), sp)
        V.spot_relative_area(g, (968.0, 548.0), sp)
    per_frame = (time.perf_counter() - t0) / 10
    assert per_frame < 0.030, per_frame


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
def test_the_gui_shows_the_method_and_the_numbers():
    import os
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QComboBox
    from camera.apps.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    brain, cam, z = _rig()
    try:
        brain.calibrate_spot(8)
        win = MainWindow(brain, brain.cfg, remote=False)
        tab = win.spot_tab
        # a COMBO with exactly the three methods, never a free text box
        assert isinstance(tab.cmb_size, QComboBox)
        assert [tab.cmb_size.itemData(i) for i in range(tab.cmb_size.count())] == \
            ["threshold", "relative", "d4sigma"]
        tab.cmb_size.setCurrentIndex(2)
        tab._push_threshold()
        assert brain.cfg.spot.size_method == "d4sigma"
        win.tabs.setCurrentWidget(win.spot_page)
        tab.grab_frame()
        assert "D4σ" in tab.lab_size.text()
        assert tab.zoom._moments is not None                   # the ellipse is drawn
        assert "D4σ" in tab.lab_ref.text()                      # calibrated D4sigma shown
        # the AutoFocus tab: the mechanism combo offers the new metrics, and the
        # live line shows the D4sigma readout
        combo = win._form_widgets["autofocus"]["mechanism"]
        items = [combo.itemText(i) for i in range(combo.count())]
        assert "spot_d4sigma" in items and "spot_relative" in items
        win.tabs.setCurrentIndex(0)
        win._refresh()
        assert "D4σ" in win.lab_af_sizes.text() and "px" in win.lab_af_sizes.text()
        pm = win.grab()
        assert not pm.isNull()
        win.close()
    finally:
        brain.shutdown()
        app.processEvents()
