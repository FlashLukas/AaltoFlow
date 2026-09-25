"""The physics, checked against numbers worked out by hand."""

import math

import numpy as np
import pytest

from vna import model
from vna.config import Config, Line, Sample


def _quiet_line():
    """A perfect line: no loss, no delay, no ripple -- isolates the sample."""
    return Line(loss_dB_at_10GHz=0.0, delay_ns=0.0, ripple_dB=0.0)


def test_kittel_in_plane_by_hand():
    s = Sample()                                    # 176 mT, 28 GHz/T
    # f = 28 GHz/T * sqrt(0.050 T * 0.226 T) = 2.97642 GHz
    assert model.kittel_Hz(50.0, s) == pytest.approx(28e9 * math.sqrt(0.05 * 0.226), rel=1e-12)
    assert model.kittel_Hz(-50.0, s) == model.kittel_Hz(50.0, s)   # the line ignores the sign
    assert math.isnan(model.kittel_Hz(0.0, s))


def test_kittel_out_of_plane_needs_saturation():
    s = Sample(geometry="out_of_plane")
    assert math.isnan(model.kittel_Hz(150.0, s))
    assert model.kittel_Hz(250.0, s) == pytest.approx(28e9 * 0.074)


@pytest.mark.parametrize("geometry,field", [("in_plane", 37.0), ("out_of_plane", 310.0)])
def test_field_for_Hz_inverts_kittel(geometry, field):
    s = Sample(geometry=geometry, h_anis_mT=4.0)
    assert model.field_for_Hz(model.kittel_Hz(field, s), s) == pytest.approx(field)


def test_linewidth_matches_the_field_swept_formula():
    """mu0 dH = 2 alpha f / (gamma/2pi) + mu0 dH0, mapped through df/dH."""
    s = Sample(alpha=1e-3, dh0_mT=0.5)
    H = 60.0
    fr = model.kittel_Hz(H, s)
    dH_mT = 2 * s.alpha * fr / (s.gamma_GHz_per_T * 1e9) * 1e3 + s.dh0_mT
    df_dH = (model.kittel_Hz(H + 1e-4, s) - model.kittel_Hz(H - 1e-4, s)) / 2e-4   # Hz per mT
    assert model.linewidth_Hz(H, s) == pytest.approx(df_dH * dH_mT, rel=1e-3)


def test_the_dip_has_the_set_depth_at_the_reference_field_and_is_at_kittel():
    s, line = Sample(dip_dB=4.0), _quiet_line()
    fr = model.kittel_Hz(50.0, s)                   # 50 mT above saturation (in-plane: 0)
    f = np.linspace(fr - 100e6, fr + 100e6, 20001)
    mag_dB = 20 * np.log10(np.abs(model.s21_clean(f, 50.0, s, line)))
    assert mag_dB.min() == pytest.approx(-4.0, abs=1e-3)
    assert f[np.argmin(mag_dB)] == pytest.approx(fr, abs=2 * (f[1] - f[0]))


def test_measured_fwhm_matches_linewidth():
    s, line = Sample(alpha=5e-4, dh0_mT=0.3, dip_dB=0.05), _quiet_line()   # shallow: |S21| ~ linear in Im chi
    H = 40.0
    fr, lw = model.kittel_Hz(H, s), model.linewidth_Hz(H, s)
    f = np.linspace(fr - 10 * lw, fr + 10 * lw, 40001)
    absorption = -np.log(np.abs(model.s21_clean(f, H, s, line)))
    above = f[absorption >= absorption.max() / 2]
    assert above[-1] - above[0] == pytest.approx(lw, rel=0.02)


def test_absorption_is_positive_everywhere():
    """|S21| never exceeds the empty line: a passive sample cannot add power."""
    s, line = Sample(), Config().line
    f = np.linspace(0.5e9, 8e9, 3001)
    for H in (0.0, 5.0, 50.0, 150.0):
        ratio = np.abs(model.s21_clean(f, H, s, line) / model.line_s21(f, line))
        assert ratio.max() <= 1.0 + 1e-12


def test_line_background_is_not_flat():
    line = Line()
    f = np.array([1e9, 10e9])
    z = model.line_s21(f, line)
    # sqrt(f) loss (ripple adds a little)
    assert 20 * np.log10(abs(z[1])) == pytest.approx(-6.0, abs=line.ripple_dB + 1e-9)
    # 2.5 ns of delay winds the phase: exp(-i 2 pi f tau). Compared as phasors:
    # 2.5 turns lands on +-pi exactly, where np.angle may give either sign.
    assert z[0] / abs(z[0]) == pytest.approx(np.exp(-2j * np.pi * 1e9 * 2.5e-9))


def test_noise_follows_ifbw_power_and_is_complex():
    line = Line(noise=1e-3)
    assert model.noise_rms(40e3, -10, line) == pytest.approx(2e-3)       # sqrt(4)
    assert model.noise_rms(10e3, 10, line) == pytest.approx(1e-4)        # +20 dB -> /10
    s, rng = Sample(), np.random.default_rng(0)
    f = np.linspace(1e9, 2e9, 20000)
    n = model.s21_measured(f, 0.0, s, line, 10e3, -10, rng) - model.s21_clean(f, 0.0, s, line)
    assert np.sqrt(np.mean(np.abs(n) ** 2)) == pytest.approx(1e-3, rel=0.03)
    assert np.std(n.real) == pytest.approx(np.std(n.imag), rel=0.05)


def test_the_plain_minimum_is_fooled_by_the_line_loss_at_low_field():
    """Why find_dip exists: at 15 mT the line is ~2 dB deep at 1.5 GHz, while
    the line loss alone is ~2.4 dB worse at 6 GHz. argmin |S21| picks the top of
    the band. (At 50 mT the 3 dB dip happens to win -- which is exactly why a
    plain minimum looks fine until it silently isn't.)"""
    cfg = Config()
    f = np.linspace(1e9, 6e9, 1601)
    z = model.s21_measured(f, 15.0, cfg.sample, cfg.line, 10e3, -10, np.random.default_rng(3))
    assert f[np.argmin(np.abs(z))] > 5e9


@pytest.mark.parametrize("field", [15.0, 50.0, 95.0])
def test_find_dip_locates_the_resonance_in_a_raw_noisy_trace(field):
    """Raw means: sloping loss, ripple, phase winding."""
    cfg = Config()
    f = np.linspace(1e9, 6e9, 1601)
    z = model.s21_measured(f, field, cfg.sample, cfg.line, 10e3, -10, np.random.default_rng(3))
    f_dip, depth = model.find_dip(f, z)
    assert f_dip == pytest.approx(model.kittel_Hz(field, cfg.sample), abs=1.5e6)
    assert depth < -1.0


def test_sweep_time():
    assert model.sweep_time_s(1601, 10e3) == pytest.approx(0.19212)


# ---- in-plane uniaxial anisotropy -------------------------------------------------------

def test_uniaxial_anisotropy_by_hand():
    """f = gamma sqrt((H + Hk cos 2(phi-phi_u)) (H + Ms + Hk cos^2(phi-phi_u)))."""
    s = Sample(hk_mT=10.0, easy_axis_deg=15.0)
    # phi = 45 deg is 30 deg from the easy axis: cos 60 = 0.5, cos^2 30 = 0.75
    by_hand = 28e9 * math.sqrt((0.050 + 0.005) * (0.050 + 0.176 + 0.0075))
    assert model.kittel_Hz(50.0, s, 45.0) == pytest.approx(by_hand, rel=1e-12)
    # hard axis (90 deg away): cos 180 = -1, cos^2 90 = 0
    assert model.kittel_Hz(50.0, s, 105.0) == pytest.approx(28e9 * math.sqrt(0.040 * 0.226), rel=1e-12)
    # easy axis: +Hk in both brackets
    assert model.kittel_Hz(50.0, s, 15.0) == pytest.approx(28e9 * math.sqrt(0.060 * 0.236), rel=1e-12)
    # uniaxial: phi and phi + 180 are the same line (so is a negative 1-axis field)
    assert model.kittel_Hz(50.0, s, 225.0) == pytest.approx(model.kittel_Hz(50.0, s, 45.0), rel=1e-12)
    assert model.kittel_Hz(-50.0, s, 45.0) == model.kittel_Hz(50.0, s, 45.0)


def test_zero_anisotropy_changes_nothing_at_any_angle():
    iso, line, f = Sample(), Config().line, np.linspace(1e9, 6e9, 501)
    for angle in (0.0, 37.0, 90.0):
        assert model.kittel_Hz(50.0, iso, angle) == model.kittel_Hz(50.0, iso)
        assert np.array_equal(model.s21_clean(f, 50.0, iso, line, angle),
                              model.s21_clean(f, 50.0, iso, line))


def test_out_of_plane_ignores_the_angle():
    s = Sample(geometry="out_of_plane", hk_mT=10.0)
    assert model.kittel_Hz(250.0, s, 0.0) == model.kittel_Hz(250.0, s, 70.0)


@pytest.mark.parametrize("angle", [0.0, 30.0, 90.0])
def test_field_for_Hz_inverts_kittel_with_anisotropy(angle):
    s = Sample(hk_mT=12.0, easy_axis_deg=10.0, h_anis_mT=2.0)
    assert model.field_for_Hz(model.kittel_Hz(40.0, s, angle), s, angle) == pytest.approx(40.0)


def test_the_anisotropic_dip_sits_at_the_anisotropic_kittel():
    s, line = Sample(hk_mT=15.0, dip_dB=4.0), _quiet_line()
    for angle in (0.0, 90.0):
        fr = model.kittel_Hz(50.0, s, angle)
        f = np.linspace(fr - 60e6, fr + 60e6, 12001)
        mag = np.abs(model.s21_clean(f, 50.0, s, line, angle))
        assert f[np.argmin(mag)] == pytest.approx(fr, abs=2 * (f[1] - f[0]))


# ---- the other S-parameters (loose on purpose) ----------------------------------------------

def test_s12_is_s21_and_the_reflections_are_small_but_see_the_film():
    cfg = Config()
    s, line = cfg.sample, cfg.line
    fr = model.kittel_Hz(50.0, s)
    f = np.linspace(1e9, 6e9, 2001)
    assert np.array_equal(model.s_clean(f, 50.0, s, line, "S12"), model.s21_clean(f, 50.0, s, line))
    s11 = model.s_clean(f, 50.0, s, line, "S11")
    s22 = model.s_clean(f, 50.0, s, line, "S22")
    assert np.abs(s11).max() < 0.35 and not np.allclose(s11, s22)
    empty = model.s_clean(f, 0.0, s, line, "S11")   # no line in the band at 0 mT
    i = np.argmin(np.abs(f - fr))
    assert abs(s11[i] - empty[i]) > 0.05            # the film reflects on resonance
    with pytest.raises(ValueError):
        model.s_clean(f, 50.0, s, line, "S31")
