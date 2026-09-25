"""One axis of the loop: clamp, slew limit, anti-windup, bumpless start."""

from mag2d.pid import AxisPI, ramp_toward_zero

KW = dict(kp=0.02, ki=0.4, ff_mT_per_V=20.0, limit_V=10.0, slew_V_per_s=2.0)


def test_feed_forward_alone_at_zero_error():
    pi = AxisPI()
    pi.output = 5.0
    assert abs(pi.update(100.0, 100.0, 0.02, **KW) - 5.0) < 1e-12


def test_slew_limits_the_step_and_integral_does_not_wind_up():
    pi = AxisPI()
    for _ in range(10):                          # 0.2 s of a huge step
        out = pi.update(150.0, 0.0, 0.02, **KW)
    assert abs(out - 0.4) < 1e-9                 # 2 V/s * 0.2 s
    assert pi.integral == 0.0                    # limited every tick: no integration


def test_output_clamped_and_no_windup_at_the_rail():
    pi = AxisPI()
    pi.output = 10.0
    for _ in range(50):
        out = pi.update(180.0, 150.0, 0.02, **{**KW, "ff_mT_per_V": 10.0})
    assert out == 10.0 and pi.integral == 0.0


def test_integrates_when_free():
    pi = AxisPI()
    pi.output = 0.0
    pi.update(0.1, 0.0, 0.02, **KW)             # tiny error: not limited
    assert pi.integral > 0.0


def test_bumpless_start_reproduces_the_present_output():
    pi = AxisPI()
    pi.bumpless(50.0, 48.0, 3.3, 0.02, 0.4, 20.0)
    out = pi.update(50.0, 48.0, 1e-9, **KW)
    assert abs(out - 3.3) < 1e-6


def test_ramp_toward_zero():
    assert ramp_toward_zero(1.0, 0.1, 2.0) == 0.8
    assert ramp_toward_zero(-1.0, 0.1, 2.0) == -0.8
    assert ramp_toward_zero(0.1, 0.1, 2.0) == 0.0
