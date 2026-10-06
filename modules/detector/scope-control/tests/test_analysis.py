"""The arithmetic of analysis.py on traces whose answer is known exactly."""

import math

import numpy as np
import pytest

from scope import analysis as A


def _loop(hc=12.0, bias=3.0, ms=0.2, slope=0.001, level=0.5, periods=2, n=4000, f=30.0):
    t = np.linspace(0, periods / f, n, endpoint=False)
    x = 50 * np.sin(2 * np.pi * f * t)
    rising = np.cos(2 * np.pi * f * t) > 0
    centre = np.where(rising, hc + bias, -hc + bias)
    y = level + ms * np.tanh((x - centre) / 2.5) + slope * x
    return t, x, y


def test_loop_numbers_recover_the_loop():
    t, x, y = _loop()
    r = A.loop_numbers(x, y, sat_fraction=0.8)
    assert r["hc"] == pytest.approx(12.0, abs=0.05)
    assert r["bias"] == pytest.approx(3.0, abs=0.05)
    assert r["hc_plus"] == pytest.approx(15.0, abs=0.05)
    assert r["hc_minus"] == pytest.approx(-9.0, abs=0.05)
    assert r["ms"] == pytest.approx(0.2, rel=0.01)
    assert r["slope"] == pytest.approx(0.001, rel=0.02)
    assert r["squareness"] == pytest.approx(1.0, abs=0.02)
    # the record closes (two whole periods): an area, per cycle
    assert r["area"] == pytest.approx(4 * 12.0 * 0.2, rel=0.05)


def test_background_is_removed_only_when_asked():
    t, x, y = _loop(slope=0.004)
    on = A.loop_numbers(x, y, subtract_background=True)
    off = A.loop_numbers(x, y, subtract_background=False)
    assert on["slope"] == pytest.approx(0.004, rel=0.02)
    assert off["slope"] == pytest.approx(0.004, rel=0.02)     # reported either way
    assert np.ptp(on["y_corrected"]) < np.ptp(off["y_corrected"])


def test_inverted_loop_still_gives_positive_hc():
    # a negative Kerr signal (Y falls with field): same coercive field
    t, x, y = _loop(ms=-0.2)
    r = A.loop_numbers(x, y)
    assert r["hc"] == pytest.approx(12.0, abs=0.05)
    assert r["ms"] == pytest.approx(-0.2, rel=0.01)


def test_no_saturation_gives_nan_not_a_guess():
    t = np.linspace(0, 1, 500)
    x = np.sin(2 * np.pi * 3 * t)
    r = A.loop_numbers(x, np.zeros_like(x) + np.nan)
    assert all(math.isnan(r[k]) for k in A.LOOP_KEYS)


def test_open_record_has_no_area():
    t, x, y = _loop(periods=1.3)
    assert math.isnan(A.loop_numbers(x, y)["area"])


def test_zero_phase_filter_keeps_phase_and_edges():
    t = np.linspace(0, 2 / 30, 2000, endpoint=False)
    dt = t[1] - t[0]
    x = 50 * np.sin(2 * np.pi * 30 * t)
    y = A.zero_phase(x, dt, lowpass_Hz=300)
    assert np.max(np.abs(y - x)) < 0.5                    # edges included
    assert A.phase_deg(t, x, y) == pytest.approx(0.0, abs=0.05)
    rng = np.random.default_rng(0)
    noisy = x + rng.normal(0, 1, x.size)
    assert np.std(A.zero_phase(noisy, dt, lowpass_Hz=300) - x) < 0.35
    # high-pass removes the DC level
    assert abs(np.mean(A.zero_phase(x + 7.0, dt, highpass_Hz=3))) < 0.2
    # off = a copy, untouched
    assert np.array_equal(A.zero_phase(x, dt), x)


def test_channel_values_and_phase():
    t = np.linspace(0, 2 / 30, 3000, endpoint=False)
    y = 1.0 + 2.0 * np.sin(2 * np.pi * 30 * t)
    v = A.channel_values(t, y)
    assert v["mean"] == pytest.approx(1.0, abs=1e-3)
    assert v["pk2pk"] == pytest.approx(4.0, rel=1e-3)
    assert v["amplitude"] == pytest.approx(2.0, rel=1e-3)
    assert v["frequency"] == pytest.approx(30.0, rel=1e-3)
    lead = np.cos(2 * np.pi * 30 * t)
    assert A.phase_deg(t, y, lead) == pytest.approx(90.0, abs=0.1)


def test_reduce_points_averages_neighbours():
    t = np.arange(10.0)
    tr, ys = A.reduce_points(t, {"a": np.arange(10.0)}, 5)
    assert np.allclose(tr, [0.5, 2.5, 4.5, 6.5, 8.5])
    assert np.allclose(ys["a"], tr)
    tr, ys = A.reduce_points(t, {"a": 2 * t}, 20)        # fewer -> interpolated
    assert tr.size == 20 and ys["a"][-1] == pytest.approx(18.0)
