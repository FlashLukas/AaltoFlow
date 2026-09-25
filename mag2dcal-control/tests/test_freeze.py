"""WHY THE FREEZE EXISTS -- the executable documentation for this whole module.

If you only read one test file here, read this one. Everything else in
mag2dcal-control (the measured calibration, the undershoot, the one-way trim) is
in service of one rule: ONCE THE FIELD IS CLOSE ENOUGH, STOP TOUCHING THE OUTPUT.

WHAT IS BEING MEASURED. Not how accurately the field is set -- how STILL it is
while it is held. A VNA-FMR trace takes seconds, and the resonance it is looking
at moves by roughly 15 MHz per millitesla, so a field that wanders by half a
millitesla during the sweep smears the line by 7 MHz. The number that matters is
therefore the peak-to-peak wander of the TRUE field over a ten-second hold.

THE CLAIM. Same plant, same gains, same calibrated jump, one boolean apart:

  * freeze ON  -- the drive stops dead (travel exactly 0 V) and the field is
    constant to about a hundredth of a millitesla;
  * freeze OFF -- which is mag2d-control's always-on PI, and what clMag-control
    did before its session 3 -- the drive never stops. It chases the probe noise,
    and because the magnet's hysteresis branch follows the direction of travel,
    every one of those little reversals drags the field with it. The field then
    wanders by half a millitesla and crosses its setpoint hundreds of times.

It is not a gain problem. Lowering the gains makes the hunting slower, not
absent, because the hysteresis gives the plant a small-signal gain several times
its large-signal one: the loop is unstable close in however gently you push. The
only stable output near the setpoint is a stationary one.

The long-term stabilizer is DISABLED throughout this file, so what is measured is
the fast loop alone. The stabilizer is allowed to move a frozen output -- slowly,
and only outside its deadband -- and is tested in test_controller.py.
"""

import pytest

from mag2dcal.backends.sim import FakeClock
from mag2dcal.config import Config
from mag2dcal.sim_system import build_sim_system

#: Three noise sequences, because a limit cycle that only turns up for one of
#: them would be an accident rather than a mechanism.
SEEDS = (1, 2, 3)


def hold_trial(freeze: bool, seed: int, tolerance_mT: float = 0.5,
               hysteresis_mT: float | None = None, noise_mT: float | None = None,
               field_mT: float = 40.0, hold_s: float = 10.0):
    """Settle at a field, then watch it BE HELD for `hold_s`.

    Everything returned describes the holding, not the approach:
      field_pp_mT     peak-to-peak wander of the true field (the headline)
      drive_travel_V  total distance the X drive moved (0 = it stopped)
      crossings       how often the field crossed its setpoint
      worst_mT        largest excursion from the setpoint
      unstable_frames how many polled frames had field_stable False
    """
    cfg = Config()
    cfg.control.freeze_enabled = freeze
    cfg.control.tolerance_mT = tolerance_mT
    cfg.stabilizer.enabled = False          # compare the fast loop only
    cfg.calibration.load_newest_on_start = False
    if hysteresis_mT is not None:
        cfg.sim.hysteresis_mT = hysteresis_mT
    if noise_mT is not None:
        cfg.sim.noise_mT = noise_mT
    clock = FakeClock()
    ctrl, sim = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=seed)
    ctrl.start(run_thread=False)

    dt = 1.0 / cfg.control.loop_hz
    ctrl.set_field(field_mT, 0.0)
    t0 = clock()
    while clock() - t0 < 30.0 and not ctrl.status().field_stable:
        clock.advance(dt)
        ctrl.tick()
    assert ctrl.status().field_stable, f"freeze={freeze} never settled at all"

    travel, worst, crossings, unstable = 0.0, 0.0, 0, 0
    lo, hi = float("inf"), float("-inf")
    prev, last_sign = sim.ao[0], 0
    for _ in range(int(hold_s / dt)):
        clock.advance(dt)
        ctrl.tick()
        travel += abs(sim.ao[0] - prev)
        prev = sim.ao[0]
        # The TRUE field, not the noisy Hall reading: the question is what the
        # magnet did, not what the probe happened to report.
        b = sim.true_field()[0]
        lo, hi = min(lo, b), max(hi, b)
        st = ctrl.status()
        err = st.setpoint_bx_mT - b
        worst = max(worst, abs(err))
        sign = 1 if err > 0 else -1
        if last_sign and sign != last_sign:
            crossings += 1
        last_sign = sign
        unstable += 0 if st.field_stable else 1
    return {"field_pp_mT": hi - lo, "drive_travel_V": travel,
            "crossings": crossings, "worst_mT": worst,
            "unstable_frames": unstable, "frames": int(hold_s / dt)}


@pytest.mark.parametrize("seed", SEEDS)
def test_the_freeze_stops_the_output_dead(seed):
    """With the freeze on, the drive does not move at all and the field is still."""
    r = hold_trial(freeze=True, seed=seed)
    assert r["drive_travel_V"] == 0.0, r     # not "small" -- zero
    assert r["crossings"] == 0, r
    assert r["field_pp_mT"] < 0.05, r
    assert r["unstable_frames"] == 0, r
    assert r["worst_mT"] < 0.5, r            # never leaves the tolerance band


@pytest.mark.parametrize("seed", SEEDS)
def test_without_the_freeze_the_same_plant_dithers(seed):
    """The always-on PI never stops: the drive hunts and the field oscillates."""
    r = hold_trial(freeze=False, seed=seed)
    assert r["drive_travel_V"] > 0.3, r      # half a volt of pointless travel
    assert r["crossings"] > 100, r           # a limit cycle, not a settled field
    assert r["field_pp_mT"] > 0.3, r         # and the field wanders with it


@pytest.mark.parametrize("seed", SEEDS)
def test_freeze_versus_no_freeze_side_by_side(seed):
    """The comparison as one test: same plant, same gains, one boolean apart.

    Note what is NOT claimed. The frozen loop does not hold a SMALLER error --
    it parks wherever it was when it came inside tolerance/2, so its offset can
    be a couple of tenths. What it holds is a CONSTANT field, and that is what a
    measurement needs.
    """
    frozen = hold_trial(freeze=True, seed=seed)
    hunting = hold_trial(freeze=False, seed=seed)
    assert frozen["drive_travel_V"] == 0.0
    assert hunting["drive_travel_V"] > 0.3
    assert frozen["crossings"] == 0 and hunting["crossings"] > 100
    assert hunting["field_pp_mT"] > 10 * frozen["field_pp_mT"], (frozen, hunting)


def test_at_a_tight_tolerance_only_the_freeze_holds_stable():
    """Tighten the band to 0.25 mT and the difference stops being cosmetic: the
    always-on PI spends far more of its time outside it, so a scan waiting on
    `field_stable` stalls.

    Aggregated over the seeds ON PURPOSE. At 0.25 mT the freeze band is
    0.125 mT, only 2.5x the probe noise, so where the trim happens to park
    inside that band decides how often noise alone pushes it out -- measured
    2026-09-20: 119 / 36 / 17 frames of 500 across the three seeds, against
    185 / 85 / 163 for the always-on PI. The comparison is solid (2.4x in
    total); one seed of it is not, and a per-seed threshold here was really
    measuring the parking offset.

    That sensitivity is itself the finding: a tolerance this tight wants a
    freeze band scaled to the measured noise, not a fixed half-tolerance.
    """
    frozen = [hold_trial(freeze=True, seed=s, tolerance_mT=0.25) for s in SEEDS]
    hunting = [hold_trial(freeze=False, seed=s, tolerance_mT=0.25) for s in SEEDS]
    for r in frozen:
        assert r["unstable_frames"] < 0.4 * r["frames"], r
    # At 0.5 mT the frozen drive never moves at all. At 0.25 it does move a
    # little -- the band is narrow enough that noise carries the field out of
    # it and the trim resumes for a moment -- but still ~30x less than the
    # always-on loop, which moves the whole time by design.
    assert sum(r["drive_travel_V"] for r in frozen) < 0.1, frozen
    assert (sum(r["drive_travel_V"] for r in hunting)
            > 10 * sum(r["drive_travel_V"] for r in frozen)), (frozen, hunting)
    out_frozen = sum(r["unstable_frames"] for r in frozen)
    out_hunting = sum(r["unstable_frames"] for r in hunting)
    assert out_hunting > 2 * out_frozen, (out_frozen, out_hunting)


def test_the_hysteresis_is_the_amplifier():
    """The other half of the argument: how much of the dither is the hysteresis?

    Run the always-on PI on the SAME plant with the hysteresis removed. It still
    hunts -- any loop that keeps correcting will chase probe noise into its
    output -- but the field wanders far less, because now a reversal only moves
    the field by gain * (the wiggle) instead of also dragging the branch across.
    So: noise-chasing starts it, hysteresis is what makes it matter.

    Averaged over the seeds: the per-seed ratio is 1.78 / 1.56 / 3.51
    (measured 2026-09-20, mean 2.28), because how far a given noise realisation
    drags the branch varies a lot. The claim is about the mean; asserting it
    seed by seed was asserting the noise.
    """
    ratios = []
    for seed in SEEDS:
        with_h = hold_trial(freeze=False, seed=seed)
        without_h = hold_trial(freeze=False, seed=seed, hysteresis_mT=0.0)
        ratios.append(with_h["field_pp_mT"] / without_h["field_pp_mT"])
        # ... and the freeze is better than either, on both plants.
        assert hold_trial(freeze=True, seed=seed)["field_pp_mT"] < without_h["field_pp_mT"]
        assert ratios[-1] > 1.2, (seed, with_h, without_h)
    assert sum(ratios) / len(ratios) > 1.7, ratios
