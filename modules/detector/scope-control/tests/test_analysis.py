"""The arithmetic of analysis.py on traces whose answer is known exactly."""

import numpy as np
import pytest

from scope import analysis as A


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
