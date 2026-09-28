"""The simulator's physics and the instrument arithmetic, one function at a time."""

import math

import numpy as np
import pytest

from signalhound import physics
from signalhound.config import Config, Scene
from signalhound.instruments import (SweepSettings, estimate_sweep_time_s, model_range,
                                     sim_grid, snap_rbw)


def _settings(**kw):
    cfg = Config()
    for k, v in kw.items():
        grp, name = k.split("__")
        setattr(getattr(cfg, grp), name, v)
    return SweepSettings.from_config(cfg)


def _tg(points=401, start=0.9e9, stop=1.1e9, level=-20.0):
    """A TG sweep's settings (what shsna asks the owner for)."""
    return SweepSettings.for_tg_sweep(Config(), start, stop, level, 100e3, points)


def test_model_ranges_follow_the_data_sheets():
    assert model_range("SA44B") == (1.0, 4.4e9, 250e3)
    assert model_range("SA124B") == (100e3, 12.4e9, 6e6)
    assert model_range("SA124A") == model_range("SA124B")     # first generation, same range
    assert model_range("nonsense") == model_range("SA44B")


def test_rbw_snaps_to_what_exists():
    assert snap_rbw(3000.0, 250e3) == 3000.0                  # continuous below 100 kHz
    assert snap_rbw(140e3, 250e3) == 100e3                    # nearer 100k than 250k (log)
    assert snap_rbw(200e3, 250e3) == 250e3
    assert snap_rbw(5e6, 250e3) == 250e3                      # SA44B: no 6 MHz
    assert snap_rbw(5e6, 6e6) == 6e6                          # SA124B has it


def test_sim_grid_two_bins_per_rbw_and_capped():
    g = sim_grid(_settings(sweep__span_Hz=10e6, sweep__rbw_Hz=10e3), 100001)
    assert g.bin_Hz == 5e3 and g.points == 2001
    assert g.start_Hz == pytest.approx(1e9 - 5e6) and g.stop_Hz == pytest.approx(1e9 + 5e6)
    big = sim_grid(_settings(sweep__span_Hz=4e9, sweep__center_Hz=2.2e9, sweep__rbw_Hz=10.0), 1001)
    assert big.points == 1001 and big.bin_Hz == pytest.approx(4e6)
    tg = sim_grid(_tg(points=301), 100001)
    assert tg.points == 301 and tg.stop_Hz == pytest.approx(1.1e9)


def test_sweep_time_grows_as_rbw_narrows():
    wide = estimate_sweep_time_s(_settings(sweep__rbw_Hz=100e3), 0)
    narrow = estimate_sweep_time_s(_settings(sweep__rbw_Hz=10e3), 0)
    assert narrow == pytest.approx((wide - 0.02) * 100 + 0.02)
    assert estimate_sweep_time_s(_settings(sweep__rbw_Hz=1.0), 0) == 600.0     # capped
    assert estimate_sweep_time_s(_tg(), 401) > 0.5


def test_noise_floor_follows_rbw_and_ref_level():
    sc = Scene()
    a = physics.noise_floor_dBm(_settings(sweep__rbw_Hz=100e3, sweep__ref_level_dBm=-40), sc)
    b = physics.noise_floor_dBm(_settings(sweep__rbw_Hz=10e3, sweep__ref_level_dBm=-40), sc)
    c = physics.noise_floor_dBm(_settings(sweep__rbw_Hz=10e3, sweep__ref_level_dBm=0), sc)
    assert a - b == pytest.approx(10.0)                        # 10 dB per decade of RBW
    assert c - b == pytest.approx(30.0)                        # attenuation raises the floor


def test_tone_reads_its_level_and_frequency():
    s = _settings(sweep__span_Hz=20e6, sweep__rbw_Hz=30e3)
    g = sim_grid(s, 100001)
    db, over = physics.spectrum_dBm(g.freqs(), g.bin_Hz, s, Scene(), "SA44B",
                                    np.random.default_rng(1))
    i = int(np.argmax(db))
    assert abs(g.freqs()[i] - Scene().tone_Hz) <= g.bin_Hz
    assert db[i] == pytest.approx(Scene().tone_dBm, abs=0.3)
    assert not over


def test_peak_detector_never_misses_a_tone_between_coarse_bins():
    s = _settings(sweep__span_Hz=4e9, sweep__center_Hz=2.2e9, sweep__rbw_Hz=1e3,
                  sweep__detector="peak")
    g = sim_grid(s, 2001)                                      # 2 MHz bins, 1 kHz RBW
    db, _ = physics.spectrum_dBm(g.freqs(), g.bin_Hz, s, Scene(), "SA44B",
                                 np.random.default_rng(2))
    assert db.max() == pytest.approx(Scene().tone_dBm, abs=0.3)


def test_vbw_smooths_noise_but_keeps_the_mean():
    sc = Scene(); sc.tone_on = False
    rng = np.random.default_rng(3)
    wide = _settings(sweep__rbw_Hz=100e3, sweep__vbw_Hz=100e3)
    narrow = _settings(sweep__rbw_Hz=100e3, sweep__vbw_Hz=1e3)
    g = sim_grid(wide, 100001)
    a, _ = physics.spectrum_dBm(g.freqs(), g.bin_Hz, wide, sc, "SA44B", rng)
    b, _ = physics.spectrum_dBm(g.freqs(), g.bin_Hz, narrow, sc, "SA44B", rng)
    assert b.std() < a.std() / 5
    mean = lambda x: 10 * np.log10(np.mean(10 ** (x / 10)))    # noqa: E731  power mean
    assert mean(a) == pytest.approx(mean(b), abs=0.3)


def test_overload_above_the_reference_level():
    sc = Scene(); sc.tone_dBm = 0.0
    s = _settings(sweep__ref_level_dBm=-30.0)
    g = sim_grid(s, 100001)
    db, over = physics.spectrum_dBm(g.freqs(), g.bin_Hz, s, sc, "SA44B", np.random.default_rng(4))
    assert over and db.max() < -20.0                           # compressed, and flagged


def test_sa124b_sees_harmonics_the_sa44b_cannot():
    sc = Scene(); sc.tone_Hz = 3e9; sc.tone_dBm = -15.0; sc.harmonic_dBc = -30.0
    s = _settings(sweep__center_Hz=9e9, sweep__span_Hz=20e6, sweep__rbw_Hz=100e3,
                  sweep__ref_level_dBm=-10.0)
    g = sim_grid(s, 100001)
    rng = np.random.default_rng(5)
    hi, _ = physics.spectrum_dBm(g.freqs(), g.bin_Hz, s, sc, "SA124B", rng)
    lo, _ = physics.spectrum_dBm(g.freqs(), g.bin_Hz, s, sc, "SA44B", rng)
    third = sc.tone_dBm + sc.harmonic_dBc - 10.0
    assert hi.max() == pytest.approx(third, abs=0.5)
    assert lo.max() < third - 20


def test_butterworth_is_3db_at_the_band_edge():
    f0, bw = 1e9, 60e6
    at = physics.butterworth_bandpass_dB([f0, f0 + bw / 2, f0 - bw / 2], f0, bw, 3)
    assert at[0] == pytest.approx(0.0)
    # -3 dB at the edges, within the slight asymmetry of the geometric
    # (f/f0 - f0/f) band-pass mapping
    assert at[1] == pytest.approx(-3.0, abs=0.3) and at[2] == pytest.approx(-3.0, abs=0.3)
    far = physics.butterworth_bandpass_dB([2 * f0], f0, bw, 3)[0]
    assert far < -60


def test_tracking_divided_by_thru_is_the_filter():
    s = _tg(points=401)
    g = sim_grid(s, 100001)
    f = g.freqs()
    thru_scene = Scene(); thru_scene.dut_inserted = False
    rng = np.random.default_rng(6)
    thru, _ = physics.tracking_dB(f, s, thru_scene, rng)
    dut, _ = physics.tracking_dB(f, s, Scene(), rng)
    tx = dut - thru
    expect = -Scene().dut_loss_dB + physics.butterworth_bandpass_dB(f, 1e9, 60e6, 3)
    band = expect > -30                                        # well above the floor
    assert np.abs(tx[band] - expect[band]).max() < 0.3
    assert np.ptp(thru) > 0.5                                  # the raw thru is NOT flat
    assert math.isfinite(tx.min())


def test_spectrum_settings_never_have_the_tg_on():
    """Since 2026-09-28 the config has no tracking mode: a spectrum sweep is a
    spectrum sweep. The fixed TG placeholders compare equal (no NaN), so the
    brain does not reconfigure every sweep."""
    a, b = _settings(), _settings()
    assert a.tg_on is False and a == b


def test_tg_sweep_settings_centre_span_and_headroom():
    s = _tg(points=301, start=0.9e9, stop=1.1e9, level=-20.0)
    assert s.tg_on and s.center_Hz == 1e9 and s.span_Hz == pytest.approx(0.2e9)
    assert s.tg_points == 301 and s.tg_level_dBm == -20.0
    assert s.ref_level_dBm == -10.0                            # 10 dB above the TG level


def test_the_tg_cw_path_is_cable_plus_filter():
    sc = Scene()
    at = physics.tg_path_dB([1e9, 0.9e9], sc)
    assert at[0] == pytest.approx(-sc.cable_loss_dB_at_1GHz - sc.dut_loss_dB, abs=1e-6)
    assert at[1] < at[0] - 20                                  # outside the pass band
    sc.dut_inserted = False
    assert physics.tg_path_dB([1e9], sc)[0] == pytest.approx(-sc.cable_loss_dB_at_1GHz)


def test_a_tg_sweep_reads_db_relative_to_the_tg_and_ignores_the_level():
    """Measured on the TG44A (2026-09-28): the sweep ignores the level set and
    returns the transmission in dB relative to the TG's calibrated output --
    a thru reads ~0 dB minus the cable, whatever the level."""
    sc = Scene(); sc.dut_inserted = False; sc.tg_ripple_dB = 0.0
    f = sim_grid(_tg(), 100001).freqs()
    a, _ = physics.tracking_dB(f, _tg(level=-30.0), sc, np.random.default_rng(7))
    b, _ = physics.tracking_dB(f, _tg(level=-10.0), sc, np.random.default_rng(7))
    assert np.allclose(a, b)
    assert np.median(a) == pytest.approx(-sc.cable_loss_dB_at_1GHz, abs=0.2)


def test_the_measured_tg_sweep_time():
    """0.2 s + 1.3 ms per point (measured, high dynamic range)."""
    assert estimate_sweep_time_s(_tg(points=1001), 1001) == pytest.approx(0.2 + 1.3013)
