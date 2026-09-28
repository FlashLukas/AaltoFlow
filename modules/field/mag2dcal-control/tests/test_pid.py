"""One axis of the seek: jump, settle, one-way trim, freeze, clamp, anti-windup.

These are unit tests on AxisSeek alone -- no magnet, no clock, just numbers in
and a voltage out -- so a failure here points at the arithmetic rather than at
the plant. The behavioural argument for the freeze is in test_freeze.py.
"""

import pytest

from mag2dcal.pid import AxisSeek, ramp_toward_zero, step_toward

KW = dict(kp=0.02, ki=0.4, limit_V=10.0, slew_V_per_s=2.0, tolerance_mT=0.5,
          trim_slew_V_per_s=0.08, margin_V=0.1, margin_frac=0.05)


def at_trim(jump_V: float, target_V: float, approach: int = 1) -> AxisSeek:
    """An axis that has already jumped and settled, ready to trim.

    In service the jump phase leaves the output sitting at jump_V; these unit
    tests skip that ramp, so they have to put it there by hand.
    """
    s = AxisSeek()
    s.begin(jump_V, target_V, approach, output_V=jump_V)
    s.phase = "trim"
    return s


def test_step_toward_and_ramp_toward_zero():
    assert step_toward(0.0, 1.0, 0.25) == 0.25
    assert step_toward(0.0, 0.1, 0.25) == 0.1          # arrives, does not overshoot
    assert step_toward(1.0, -1.0, 0.25) == 0.75
    assert ramp_toward_zero(1.0, 0.1, 2.0) == 0.8
    assert ramp_toward_zero(-1.0, 0.1, 2.0) == -0.8
    assert ramp_toward_zero(0.1, 0.1, 2.0) == 0.0


def test_the_jump_ramps_at_the_full_slew_and_ignores_the_error():
    s = AxisSeek()
    s.begin(jump_V=1.0, target_V=1.1, approach=1)
    for _ in range(5):
        out = s.update(50.0, 0.02, **KW)               # a huge error, ignored
    assert out == pytest.approx(0.2)                   # 2 V/s * 0.1 s
    assert s.phase == "jump" and s.integral == 0.0


def test_after_the_jump_it_settles_before_trimming():
    """The wait exists so the trim does not chase a field that is still arriving."""
    s = AxisSeek()
    s.begin(jump_V=0.02, target_V=0.1, approach=1, settle_s=0.1)
    s.update(2.0, 0.02, **KW)                          # arrives at the jump voltage
    assert s.phase == "settle" and s.output == pytest.approx(0.02)
    for _ in range(4):
        out = s.update(2.0, 0.02, **KW)
        assert out == pytest.approx(0.02)              # held still throughout
    assert s.phase == "settle"
    s.update(2.0, 0.02, **KW)
    assert s.phase == "trim"


def test_the_trim_moves_at_its_own_much_slower_rate():
    s = at_trim(0.0, 5.0)
    out = s.update(2.0, 0.02, **KW)
    assert out == pytest.approx(0.08 * 0.02)           # trim slew, not 2 V/s


def test_the_trim_only_pushes_the_way_it_came():
    """Approaching from below, a negative correction is refused: reversing would
    swap the hysteresis branch we deliberately chose."""
    s = at_trim(1.0, 1.1, approach=1)
    out = s.update(-3.0, 0.02, **KW)                   # overshot: wants to pull back
    assert out == pytest.approx(1.0)
    assert s.integral == 0.0                           # and does not wind up doing it

    s = at_trim(-1.0, -1.1, approach=-1)
    assert s.update(+3.0, 0.02, **KW) == pytest.approx(-1.0)


def test_the_trim_is_capped_just_past_the_calibrations_answer():
    """However wrong the gains, the output cannot run away past what the curve
    says the target needs, plus a margin for the curve's own error."""
    s = at_trim(2.0, 2.5)
    for _ in range(2000):
        out = s.update(50.0, 0.02, **KW)               # absurd, sustained error
    cap = 2.5 + (0.1 + 0.05 * 2.5)
    assert out == pytest.approx(cap)


def test_the_output_is_clamped_to_the_amplifier_limit():
    s = at_trim(0.0, 1e6)
    for _ in range(20000):
        out = s.update(50.0, 0.02, **KW)
    assert out == pytest.approx(10.0)


def test_anti_windup_while_the_output_is_rate_limited():
    s = at_trim(0.0, 5.0)
    for _ in range(10):
        s.update(2.0, 0.02, **KW)                      # wants far more than the rate
    assert s.integral == 0.0                           # limited every tick: no windup

    # A small error asks for a move the rate limit allows, so the integral runs.
    free = at_trim(0.0, 5.0)
    free.update(0.001, 0.02, **KW)
    assert free.integral > 0.0


def test_the_freeze_latches_inside_half_tolerance_and_holds_exactly():
    s = at_trim(1.0, 1.1)
    s.update(0.2, 0.02, **KW)                          # inside 0.5/2
    assert s.frozen
    held = s.output
    for _ in range(100):
        assert s.update(0.2, 0.02, **KW) == held       # bit for bit
    # ... until the field genuinely leaves the FULL tolerance band
    s.update(0.8, 0.02, **KW)
    assert s.phase == "trim" and s.output != held


def test_freeze_off_leaves_an_ordinary_two_way_pi():
    """freeze=False, one_way=False is mag2d-control's loop: it never stops."""
    s = at_trim(1.0, 1.1)
    kw = {**KW, "freeze": False, "one_way": False}
    up = s.update(0.2, 0.02, **kw)
    assert not s.frozen and up > 1.0
    down = s.update(-0.2, 0.02, **kw)
    assert down < up                                   # happily reverses


def test_nudge_moves_a_frozen_output_without_thawing_it():
    """The long-term stabilizer's one privilege."""
    s = at_trim(1.0, 1.1)
    s.update(0.1, 0.02, **KW)
    assert s.frozen
    held = s.output
    s.nudge(0.005, 10.0)
    assert s.frozen and s.output == pytest.approx(held + 0.005)
    # and the frozen loop leaves the nudged value alone from then on
    assert s.update(0.1, 0.02, **KW) == s.output
    s.nudge(1e6, 10.0)
    assert s.output == 10.0                            # still clamped


def test_begin_drops_the_old_integral():
    s = AxisSeek()
    s.integral = 42.0
    s.begin(0.0, 1.0, 1)
    assert s.integral == 0.0 and s.phase == "jump"
    s.reset(0.3)
    assert s.phase == "idle" and s.output == 0.3 and s.integral == 0.0
