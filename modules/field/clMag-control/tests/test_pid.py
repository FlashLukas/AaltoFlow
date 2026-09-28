from clMag.pid import PI


def test_proportional_sign_and_size():
    pi = PI(Kc_A_per_mT=0.01, Ti_s=1e9)   # huge Ti -> integral negligible
    # first call, integral ~ 0, so output ~ Kc * error
    assert abs(pi.update(2.0, 0.01) - 0.02) < 1e-4
    pi.reset()
    assert pi.update(-2.0, 0.01) < 0


def test_integral_accumulates():
    pi = PI(Kc_A_per_mT=0.01, Ti_s=0.1)
    out1 = pi.update(1.0, 0.1)
    out2 = pi.update(1.0, 0.1)
    # constant positive error -> output grows because the integral builds up
    assert out2 > out1


def test_one_direction_clamp_and_antiwindup():
    pi = PI(Kc_A_per_mT=0.01, Ti_s=0.1)
    # approaching from below: output must never go negative
    out = pi.update(-5.0, 0.1, clamp_sign=+1)
    assert out == 0.0
    # anti-windup: the clamped step must not have wound the integral, so a
    # following positive error behaves as if starting fresh
    out_pos = pi.update(1.0, 0.1, clamp_sign=+1)
    fresh = PI(0.01, 0.1).update(1.0, 0.1, clamp_sign=+1)
    assert abs(out_pos - fresh) < 1e-9
