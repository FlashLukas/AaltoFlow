"""one_way park against a ROBUST fine-walk minimum (rig finding 2026-09-29).

What the rig showed (lab PC, 63x, 8-bit Mono8, one_way + spot_d4sigma,
park_tolerance_d4sigma 0.04): the run FAILED to park. The fine walk's best was
51.2 px^2 at ONE level whose neighbours read 56.2 / 55.9 -- an 8-bit noise dip.
The park then had to get within 4 % of that single noisy sample (<= 53.2) and
three park walks reached 57.3 / 55.1 / 56.4. 4 % of one noisy sample is
tighter than the frame-to-frame noise: the park could never succeed.

Proved here (each test failed on the code of commit 429497c):
  1. In the simulator (coherent spot, 8 bit, noise 1.5 grey levels = ~1 % of
     sigma^2 per frame, like the rig) with one fine-walk level dipped 9 % (the
     rig's 51.2 vs ~56), the old code fails the park; the new one parks, near
     the TRUE focus.
  2. The target is a parabola fitted to the fine walk (sigma^2 IS a parabola
     in Z), with a single outlying level dropped; fallback = the best 3-level
     running mean when no parabola fits.
  3. The park tolerance is never tighter than the measured level-to-level
     noise: max(park_tolerance, park_noise_k x noise/level), logged.
"""

import time

import numpy as np
import pytest

from camera import vision as V
from camera.backends.sim import SimCamera, SimSlipStickZ, SimXYStage
from camera.camera import Camera
from camera.config import Config, load_config, save_config

ZF = 30.0          # true focus (the sigma^2 waist) in scene units (Rayleigh range 3)


def _wait(cond, timeout=60.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


def _rig(seed=None):
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.hardware.z_step_time_ms = 0.0
    cfg.autofocus.averages_per_level = 2
    cfg.spot.lookup_region_px = 150
    cfg.spot.thr_lower = 150          # finds the in-focus spot, not the pattern (grey 120)
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = SimSlipStickZ(z0=ZF, z_focus=ZF, vmin=cfg.limits.z_min_v, vmax=cfg.limits.z_max_v)
    cam = SimCamera(xy, z, pixel_size_x_um=cfg.image.pixel_size_x_um,
                    pixel_size_y_um=cfg.image.pixel_size_y_um, spot_model="coherent",
                    noise=1.5)
    if seed is not None and hasattr(cam, "_rng"):
        cam._rng = np.random.default_rng(seed)
    brain = Camera(cam, xy, z, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    brain.calibrate_spot(10)          # in focus
    af = brain.cfg.autofocus
    af.mechanism, af.routine, af.max_travel_v = "spot_d4sigma", "one_way", 40.0
    return brain, z, events


def _dip_one_fine_level(brain, z, factor=0.91):
    """The rig's single-level noise dip: the fine-walk level closest to the
    TRUE focus (within half a fine step) reads ``factor`` x its value -- every
    frame of that level, as a level mean of 51.2 among ~56 did on the rig."""
    orig = brain._focus_metric
    fine = brain.cfg.autofocus.fine_step_v
    state = {"at": None, "done": False}

    def phase():
        c = brain._af_curve or {}
        ph = c.get("phases") or {}
        if c.get("best") is None and (ph.get("fine") or {}).get("z") \
                and not (ph.get("park") or {}).get("z"):
            return "fine"
        return ""

    def dipped(gray, *a, **k):
        m = orig(gray, *a, **k)
        if phase() != "fine" or not np.isfinite(m):
            return m
        here = float(z.read_z())
        if state["at"] is None and not state["done"] and abs(z.true_z() - ZF) <= 0.52 * fine:
            state["at"], state["done"] = here, True
        return factor * m if state["at"] is not None and here == state["at"] else m
    brain._focus_metric = dipped
    return state


# --------------------------------------------------------------------------- #
# 1. the rig's case, end to end
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("start", [-6.0, 6.0])
def test_a_single_level_noise_dip_no_longer_fails_the_park(start):
    brain, z, events = _rig()
    try:
        gain = z.up_gain if start > 0 else z.down_gain
        z.set_z(z.read_z() + start / gain)
        state = _dip_one_fine_level(brain, z)
        brain.autofocus()
        assert _wait(lambda: not brain.status().af_running, 120)
        s = brain.status()
        assert state["done"], "the fine walk never passed the true focus"
        assert s.af_error == "OK", (s.af_error, [m for l, m in events if l != "info"][-3:])
        # parked by the image near the TRUE focus (Rayleigh range 3 units).
        # Measured 2026-09-29, 40 runs (starts -6/+6/-3/+3): |error| median
        # 0.09, 90 % <= 0.21, worst 0.54 -- the bound is the one_way tests' 0.6
        # (0.2 Rayleigh range) because single runs are noise-limited.
        assert abs(z.true_z() - ZF) < 0.6, z.true_z() - ZF
        # the park target and the effective tolerance are logged
        msg = next(m for lvl, m in events if lvl == "info" and "one way" in m)
        assert "target" in msg and "noise" in msg, msg
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# 2. the robust target (pure function)
# --------------------------------------------------------------------------- #
def _walk(n=13, step=0.25, z0=-1.5, vmin=58.0, a=3.6, noise=0.4, seed=1):
    rng = np.random.default_rng(seed)
    z = z0 + step * np.arange(n)
    return z, vmin + a * z ** 2 + rng.normal(0.0, noise, n)


def test_the_target_is_the_fitted_minimum_with_a_dip_dropped():
    z, m = _walk()
    i0 = int(np.argmin(np.abs(z)))
    m[i0] *= 0.91                                    # the rig's dip
    t = V.robust_focus_target(z, m, maximise=False, rel_window=2.0)
    assert t.method == "parabola" and t.n_dropped == 1
    assert t.value == pytest.approx(58.0, rel=0.01)   # not 52.8 (the dipped level)
    assert t.z == pytest.approx(0.0, abs=0.1)
    assert 0.2 < t.noise < 0.8                        # ~0.4 per level, the dip not in it
    assert t.curvature == pytest.approx(3.6, rel=0.25)


def test_the_target_of_a_maximised_metric_is_the_fitted_maximum():
    z, m = _walk()
    t = V.robust_focus_target(z, 200.0 - m, maximise=True)
    assert t.method == "parabola"
    assert t.value == pytest.approx(142.0, rel=0.01)


def test_no_parabola_falls_back_to_a_three_level_running_mean():
    # too few levels for a fit with a noise estimate
    t = V.robust_focus_target([0.0, 0.25, 0.5, 0.75], [60.0, 52.0, 59.0, 61.0], maximise=False)
    assert t.method == "running mean"
    assert t.value == pytest.approx((60.0 + 52.0 + 59.0) / 3.0)
    assert t.z == pytest.approx(0.25)
    # a curve that opens the wrong way is no minimum
    z = np.linspace(-1, 1, 9)
    t = V.robust_focus_target(z, 60.0 - 3.0 * z ** 2, maximise=False)
    assert t.method == "running mean"
    # one level: that level, no noise known
    t = V.robust_focus_target([1.0], [55.0], maximise=False)
    assert t.method == "single level" and t.value == 55.0 and t.noise == 0.0


def test_a_parabola_that_dips_below_the_data_is_not_the_target():
    """A kinked or V-shaped metric (the relative area, a Gaussian fit's sigma^2
    on the coherent spot) is no parabola: fitted anyway, the vertex dives
    BELOW every level measured (sim: 190 against a best level of 228), and no
    park can get there -- the run failed where it used to park. Such a fit is
    rejected: the target falls back to the 3-level running mean."""
    z = np.linspace(-1.5, 1.5, 13)
    m = 58.0 + 20.0 * np.abs(z)
    t = V.robust_focus_target(z, m, maximise=False, rel_window=2.0)
    assert t.method == "running mean"
    assert t.value >= m.min()
    # ... the same for a maximised metric (a peak that is a cusp)
    t = V.robust_focus_target(z, 200.0 - 40.0 * np.abs(z), maximise=True)
    assert t.method == "running mean" and t.value <= 200.0


# --------------------------------------------------------------------------- #
# 3. the tolerance is never tighter than the noise
# --------------------------------------------------------------------------- #
def test_the_park_tolerance_is_not_tighter_than_the_measured_noise(tmp_path):
    cfg = Config()
    assert cfg.autofocus.park_noise_k == pytest.approx(2.0)
    assert cfg.autofocus.park_centre is True
    cfg.autofocus.park_noise_k = 2.5
    cfg.autofocus.park_centre = False
    save_config(cfg, str(tmp_path / "c.ini"))
    back = load_config(str(tmp_path / "c.ini")).autofocus
    assert back.park_noise_k == pytest.approx(2.5) and back.park_centre is False

    brain = Camera(None, None, None, Config())
    af = brain.cfg.autofocus
    af.mechanism = "spot_d4sigma"                       # configured 4 %
    tol, why = brain.effective_park_tolerance(56.0, 1.68)   # 3 % noise per level
    assert tol == pytest.approx(0.06) and "noise" in why    # 2 x 3 %
    tol, why = brain.effective_park_tolerance(56.0, 0.28)   # 0.5 %: the configured one wins
    assert tol == pytest.approx(0.04)
    tol, _ = brain.effective_park_tolerance(56.0, 0.0)      # noise unknown
    assert tol == pytest.approx(0.04)
    # capped at the fine walk's own "worse" band (rise_fraction, 10 %): a noise
    # estimate that large means the walk itself could not tell up from down
    tol, why = brain.effective_park_tolerance(56.0, 20.0)
    assert tol == pytest.approx(af.rise_fraction) and "cap" in why
    af.park_noise_k = 0.0                                   # 0 = off
    assert brain.effective_park_tolerance(56.0, 1.68)[0] == pytest.approx(0.04)


# --------------------------------------------------------------------------- #
# 4. the coarse walk's aimed step on a FLAT bottom (found by the benchmark)
# --------------------------------------------------------------------------- #
def test_the_coarse_walk_does_not_jump_away_from_a_flat_bottom():
    """Found while benchmarking the park (sim, averages_per_level 3, start +3:
    2 of 40 runs, the old code too). Coarse levels at counter 29 and 27 read
    59.7 and 59.7 - a hair (focus between them, noise). The aimed step divides
    the distance still to go (sqrt(59.7) - the calibrated sigma, a noise-sized
    number) by the slope between the last two levels (~0), so it asked for a
    huge step and jumped the maximum -- 8 coarse steps, 16 units -- AWAY from
    focus. The fine walk then started 16 units out and crawled back through
    the noisy far wings, where a false "rise" ended it 8.6 units from focus,
    and the run reported OK. Rule now: aim only while the last step improved
    the metric by more than rise_fraction (far out of focus, where the square
    root is linear); near the bottom, plain coarse steps."""
    step = Camera._coarse_step
    kw = dict(coarse=2.0, max_step=16.0, rise=0.10)
    # the rig-sim case: flat between the last two levels -> a plain coarse step
    assert step(m_prev=59.70, m_now=59.69, dz=2.0, r_ref=30.43 / 4, **kw) == 2.0
    # far out, improving fast: aimed, bigger than coarse, capped
    far = step(m_prev=400.0, m_now=250.0, dz=2.0, r_ref=30.43 / 4, **kw)
    assert 2.0 < far <= 16.0
    # no calibrated size to aim at: the x1.5 growth is the caller's business
    assert step(m_prev=400.0, m_now=250.0, dz=2.0, r_ref=None, **kw) is None
