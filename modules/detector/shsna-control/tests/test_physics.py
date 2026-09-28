"""The simulated chain and the scalar arithmetic, one function at a time."""

import math

import numpy as np
import pytest

from shsna import physics
from shsna.config import Sim


def test_a_thru_reads_minus_the_pad_and_the_cable():
    """The bench: TG -> 20 dB pad -> SA reads ~ -20 dB (the TG44A measured
    -19.4 dB). Relative to the TG output, so no level appears anywhere."""
    sim = Sim(dut_inserted=False, tg_ripple_dB=0.0, cable_loss_dB_at_1GHz=1.0)
    f = np.array([0.25e9, 1e9, 4e9])
    np.testing.assert_allclose(physics.chain_dB(f, sim), -20.0 - np.sqrt(f / 1e9))
    sim.pad_dB = 0.0
    assert physics.chain_dB([1e9], sim)[0] == pytest.approx(-1.0)


def test_the_dut_is_a_band_pass_on_top_of_the_thru():
    thru = Sim(dut_inserted=False)
    dut = Sim(dut_inserted=True)
    f = np.array([dut.dut_center_Hz, 2 * dut.dut_center_Hz])
    diff = physics.chain_dB(f, dut) - physics.chain_dB(f, thru)
    assert diff[0] == pytest.approx(-dut.dut_loss_dB)             # pass band
    assert diff[1] < -60                                          # far skirt


def test_butterworth_is_minus_3_db_at_the_band_edges():
    f0, bw = 1e9, 60e6
    # the band edges of the x = Q (f/f0 - f0/f) mapping solve f/f0 - f0/f = +-1/Q
    q = f0 / bw
    edges = f0 * (np.sqrt(1 + 1 / (4 * q * q)) + np.array([-1, 1]) / (2 * q))
    np.testing.assert_allclose(physics.butterworth_bandpass_dB(edges, f0, bw, 3),
                               -10 * math.log10(2), atol=1e-9)
    assert physics.butterworth_bandpass_dB([f0], f0, bw, 3)[0] == 0.0


def test_the_noise_floor_follows_the_rbw():
    sim = Sim()
    assert physics.floor_dB(sim, 0.0) == sim.floor_dB             # auto = the default bandwidth
    assert physics.floor_dB(sim, 10e3) == pytest.approx(sim.floor_dB + 10.0)


def test_a_sweep_is_the_chain_plus_noise():
    sim = Sim(dut_inserted=False)
    rng = np.random.default_rng(1)
    f = np.linspace(100e6, 4e9, 401)
    db = physics.tg_sweep_dB(f, 0.0, sim, rng)
    assert np.abs(db - physics.chain_dB(f, sim)).max() < 0.2      # 0.02 dB rms jitter
    sim.pad_dB = 150.0                                            # into the floor
    deep = physics.tg_sweep_dB(f, 0.0, sim, rng)
    assert abs(np.mean(deep) - sim.floor_dB) < 3.0


def test_averaging_is_in_linear_power_not_in_db():
    """0 dB and -10 dB: the power mean is 10 log10((1 + 0.1)/2) = -2.6 dB, not -5."""
    m = physics.power_mean_db([[0.0], [-10.0]])[0]
    assert m == pytest.approx(10 * math.log10(0.55))
    assert m != pytest.approx(-5.0)


def test_summarise_a_band_pass():
    f = np.linspace(700e6, 1300e6, 6001)
    t = physics.butterworth_bandpass_dB(f, 1e9, 60e6, 3) - 1.5
    r = physics.summarise_transmission(f, t)
    assert r["peak_transmission_db"] == pytest.approx(-1.5, abs=1e-3)
    assert r["peak_freq_hz"] == pytest.approx(1e9, abs=0.2e6)
    # the -3 dB width of |H|^2 of a Butterworth band-pass is its bandwidth
    assert r["bw3_hz"] == pytest.approx(60e6, rel=2e-3)
    # the mean is of the LINEAR ratio: far above the mean of the dB values
    assert r["mean_transmission_db"] > np.mean(t) + 10


def test_the_bandwidth_is_not_reported_when_the_edge_is_outside_the_sweep():
    f = np.linspace(990e6, 1010e6, 201)                           # inside the pass band
    t = physics.butterworth_bandpass_dB(f, 1e9, 60e6, 3)
    assert math.isnan(physics.summarise_transmission(f, t)["bw3_hz"])


def test_summarise_survives_nan_and_an_empty_trace():
    f = np.linspace(1e6, 2e6, 5)
    r = physics.summarise_transmission(f, [np.nan] * 5)
    assert all(math.isnan(v) for v in r.values())
    r = physics.summarise_transmission(f, [-10, np.nan, -1, np.nan, -10])
    assert r["peak_transmission_db"] == -1 and r["peak_freq_hz"] == 1.5e6
