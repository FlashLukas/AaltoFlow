"""The one-way autofocus routine against a HYSTERETIC Z (2026-09-24).

SimSlipStickZ moves 1.0x the commanded distance up and 0.7x down, like the
PIA25 on kim: the step counter and the real position drift apart on every
reversal. What matters is where the sample REALLY ends up (``true_z``), not
what the counter says -- so every assertion here is on ``true_z``.
"""

import time

import pytest

from camera.backends.sim import SimCamera, SimSlipStickZ, SimXYStage
from camera.camera import Camera
from camera.config import Config

Z_FOCUS = 30.0          # true Z of best focus (scene units)


def _wait(cond, timeout=30.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


@pytest.fixture
def rig():
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.hardware.z_step_time_ms = 0.0
    cfg.autofocus.averages_per_level = 1
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = SimSlipStickZ(z0=Z_FOCUS, z_focus=Z_FOCUS, vmin=cfg.limits.z_min_v,
                      vmax=cfg.limits.z_max_v)
    # base_sigma 8: an in-focus spot of ~100 px^2. The default 3 gives 13 px^2,
    # where one pixel more or less is already a 10 % change -- the rig's spot
    # is ~1000 px^2.
    cam = SimCamera(xy, z, pixel_size_x_um=cfg.image.pixel_size_x_um,
                    pixel_size_y_um=cfg.image.pixel_size_y_um, base_sigma=8.0)
    brain = Camera(cam, xy, z, cfg)
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    brain.calibrate_spot(10)            # calibrated IN focus: ref_area = the focused spot
    yield brain, z
    brain.shutdown()


def _defocus(z, by: float) -> None:
    """Move the sample by ``by`` in TRUE units, whatever the counter does."""
    gain = z.up_gain if by > 0 else z.down_gain
    z.set_z(z.read_z() + by / gain)


def _run(brain):
    brain.autofocus()
    assert _wait(lambda: not brain.status().af_running)
    return brain.status()


@pytest.mark.parametrize("side", ["below", "above"])
@pytest.mark.parametrize("start", [-9.0, +9.0])
def test_one_way_lands_in_focus_on_a_hysteretic_z(rig, side, start):
    """From either side of focus, approaching from either side: the sample ends
    up in focus although the counter and the truth have drifted apart."""
    brain, z = rig
    brain.cfg.autofocus.routine = "one_way"
    brain.cfg.autofocus.approach_from = side
    _defocus(z, start)
    s = _run(brain)
    assert s.af_error == "OK"
    assert abs(z.true_z() - Z_FOCUS) < 0.6
    # the counter did drift: parking by it would have been wrong
    assert abs(z.read_z() - z.true_z()) > 0.5
    c = brain.get_af_curve()
    assert c["phases"]["fine"]["z"] and c["phases"]["park"]["z"]
    # every counted level of the fine walk travelled the approach direction
    fz = c["phases"]["fine"]["z"]
    d = 1 if side == "below" else -1
    assert all((b - a) * d > 0 for a, b in zip(fz, fz[1:]))


def test_the_sweep_really_suffers_from_the_hysteresis(rig):
    """Why the new routine exists: the same start, the old sweep parks by the
    counter after reversing and ends up out of focus."""
    brain, z = rig
    brain.cfg.autofocus.routine = "sweep"
    brain.cfg.autofocus.drive_amplitude_v = 30.0
    brain.cfg.autofocus.steps = 31
    _defocus(z, -9.0)
    s = _run(brain)
    assert s.af_error == "OK"
    assert abs(z.true_z() - Z_FOCUS) > 1.5


def test_far_out_of_focus_the_spot_is_found_and_the_slope_followed(rig):
    """So blurred the spot is not even detected (larger than max_area): the
    routine first searches for it, then follows the slope with growing steps."""
    brain, z = rig
    brain.cfg.spot.lookup_region_px = 400     # room for a big blurred spot...
    brain.cfg.spot.max_area_px = 30000        # ...which is only seen below this
    brain.cfg.autofocus.routine = "one_way"
    brain.cfg.autofocus.max_travel_v = 60.0
    _defocus(z, -28.0)
    s = _run(brain)
    assert s.af_error == "OK", s.af_error
    assert abs(z.true_z() - Z_FOCUS) < 0.6
    cz = brain.get_af_curve()["phases"]["coarse"]["z"]
    steps = [abs(b - a) for a, b in zip(cz, cz[1:])]
    assert max(steps) > 2 * brain.cfg.autofocus.coarse_step_v   # it did speed up


def test_no_spot_anywhere_is_an_error_not_a_guess(rig):
    brain, z = rig
    brain.cfg.autofocus.routine = "one_way"
    brain.cfg.autofocus.max_travel_v = 6.0
    brain.cfg.spot.min_area_px = 10**7       # nothing is ever big enough to count
    before = z.read_z()
    s = _run(brain)
    assert s.af_error == "RuntimeError"
    assert abs(z.read_z() - before) <= 6.0 + 2.0   # did not wander beyond max_travel


@pytest.mark.parametrize("routine", ["one_way", "sweep"])
def test_a_failed_run_puts_z_back_where_it_started(rig, routine):
    """Lukas: "if AF fails carry on with old Z". A scan routine that carries
    on after a failure must not measure the next row at whatever level the
    search gave up on. Z is back (by the counter; the image cannot say where
    focus is, since the spot was never seen) BEFORE the run reports finished."""
    brain, z = rig
    brain.cfg.autofocus.routine = routine
    brain.cfg.autofocus.max_travel_v = 6.0
    brain.cfg.spot.min_area_px = 10**7       # the spot never counts
    before = z.read_z()
    s = _run(brain)
    assert s.af_error == "RuntimeError"
    assert abs(z.read_z() - before) < 1e-6
