"""The couplings and the simulator's physics, checked against what a
spectrum-analyser user expects to see."""

import math

import numpy as np
import pytest

from gsp818 import model
from gsp818.config import Config


def _settings(**kw):
    cfg = Config()
    for k, v in kw.items():
        grp = cfg.tracking if k in ("tg_on", "level_dBm") else cfg.sweep
        setattr(grp, k, v)
    return model.resolve(cfg), cfg


def test_auto_couplings_follow_the_preset():
    s, _ = _settings()                         # full span, everything auto
    assert s.rbw_Hz == 3e6 and s.vbw_Hz == 3e6
    assert s.atten_dB == 10.0                  # ref 0 dBm -> 10 dB, as the preset
    assert s.sweep_time_s == pytest.approx(0.01)
    assert model.auto_rbw(1e6) == 10e3 and model.auto_rbw(50.0) == 10.0
    assert model.auto_atten(30.0) == 40.0 and model.auto_atten(-50.0) == 0.0
    # narrow RBW over a wide span is slow: k * span / RBW^2
    assert model.auto_sweep_time(1e6, 1e3, 1e3, 601) == pytest.approx(2.0)


def test_manual_values_win_over_auto():
    s, _ = _settings(rbw_auto=False, rbw_Hz=30e3, atten_auto=False, atten_dB=25.0,
                     sweep_time_auto=False, sweep_time_s=0.5)
    assert (s.rbw_Hz, s.atten_dB, s.sweep_time_s) == (30e3, 25.0, 0.5)


def test_auto_detector_is_the_manuals_rule():
    assert model.effective_detector("auto", 2e6) == "normal"
    assert model.effective_detector("auto", 1e6) == "pos_peak"
    assert model.effective_detector("sample", 1e9) == "sample"


def _floor(**kw):
    s, cfg = _settings(**kw)
    cfg.bench.carriers = ""
    y, _ = model.simulate(s, cfg.bench, np.random.default_rng(0))
    return float(np.median(y))


def test_noise_floor_moves_10_dB_per_decade_of_rbw():
    common = dict(start_Hz=10e6, stop_Hz=20e6, rbw_auto=False, detector="sample",
                  vbw_auto=False, vbw_Hz=10.0)
    a = _floor(rbw_Hz=1e3, **common)
    b = _floor(rbw_Hz=100e3, **common)
    assert b - a == pytest.approx(20.0, abs=0.5)
    # absolute: DANL -130 dBm/Hz + 30 dB (1 kHz) + 10 dB attenuation
    assert a == pytest.approx(-90.0, abs=1.0)


def test_attenuation_and_preamp_move_the_floor():
    common = dict(start_Hz=10e6, stop_Hz=20e6, detector="sample", vbw_auto=False, vbw_Hz=10.0)
    base = _floor(**common)
    assert _floor(atten_auto=False, atten_dB=30.0, **common) - base == pytest.approx(20.0, abs=0.5)
    assert base - _floor(preamp=True, **common) == pytest.approx(20.0, abs=0.5)


def test_peak_detector_catches_a_carrier_the_sample_detector_misses():
    """1 MHz display bins, 10 kHz RBW, a carrier half a bin off a point."""
    base = dict(start_Hz=10e6, stop_Hz=610e6, points=601, rbw_auto=False, rbw_Hz=10e3)
    for det, seen in (("pos_peak", True), ("sample", False)):
        s, cfg = _settings(detector=det, **base)
        cfg.bench.carriers = "300.5e6:-30"
        y, _ = model.simulate(s, cfg.bench, np.random.default_rng(1))
        assert bool(y.max() > -35) is seen


def test_carrier_level_and_overload():
    s, cfg = _settings(start_Hz=99e6, stop_Hz=101e6)
    cfg.bench.carriers = "100e6:-20"
    y, meta = model.simulate(s, cfg.bench, np.random.default_rng(2))
    f, p = model.find_peak(s.freqs(), y)
    assert abs(f - 100e6) < 5e3 and p == pytest.approx(-20.0, abs=0.3)
    assert meta["overload"] is False
    # +10 dBm with 0 dB attenuation drives the mixer 8 dB past compression
    s, cfg = _settings(start_Hz=99e6, stop_Hz=101e6, atten_auto=False, atten_dB=0.0)
    cfg.bench.carriers = "100e6:10"
    y, meta = model.simulate(s, cfg.bench, np.random.default_rng(2))
    assert meta["overload"] is True and y.max() < 5.0


def test_tracking_generator_through_the_dut():
    # 30 kHz RBW keeps the noise (-75 dBm) below the filter's stop band
    s, cfg = _settings(start_Hz=10e6, stop_Hz=1.8e9, tg_on=True, level_dBm=-10.0,
                       rbw_auto=False, rbw_Hz=30e3)
    cfg.bench.carriers = ""
    cfg.bench.dut, cfg.bench.tg_ripple_dB, cfg.bench.cable_loss_dB_at_1GHz = "thru", 0.0, 0.0
    y, _ = model.simulate(s, cfg.bench, np.random.default_rng(3))
    assert np.allclose(y, -10.0, atol=0.05)             # thru, flat, lossless
    cfg.bench.dut = "bandpass"
    y, _ = model.simulate(s, cfg.bench, np.random.default_rng(3))
    f = s.freqs()
    at = lambda hz: y[int(np.argmin(abs(f - hz)))]      # noqa: E731
    assert at(900e6) == pytest.approx(-11.5, abs=0.1)   # passband: -10 - 1.5 loss
    assert at(200e6) < -60                              # deep in the stop band
    # the TG starts at 100 kHz: nothing below
    s2, _ = _settings(start_Hz=9e3, stop_Hz=90e3, tg_on=True)
    y2, _ = model.simulate(s2, cfg.bench, np.random.default_rng(3))
    assert y2.max() < -60


def test_power_average_is_not_a_log_average():
    rng = np.random.default_rng(4)
    noise = [10 * np.log10(rng.exponential(1.0, 2000)) for _ in range(100)]
    # the mean of log(exponential) sits 2.5 dB below the log of the mean ...
    assert np.mean(noise) == pytest.approx(-2.5, abs=0.1)
    # ... while a power average of 100 sweeps lands on the true level, 0 dB
    assert np.mean(model.power_average_dBm(noise)) == pytest.approx(0.0, abs=0.1)


def test_parse_carriers():
    assert model.parse_carriers("100e6:-20, 1e9:-50") == [(1e8, -20.0), (1e9, -50.0)]
    assert model.parse_carriers("") == []
    with pytest.raises(ValueError):
        model.parse_carriers("100e6")
    with pytest.raises(ValueError):
        model.parse_carriers("-5:-20")
    assert math.isnan(model.find_peak(np.array([]), np.array([]))[0])
