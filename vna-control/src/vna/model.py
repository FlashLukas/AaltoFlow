"""model.py -- the pretend physics: S-parameters of a waveguide with a YIG film on top.

Pure numpy, no Qt, no ZeroMQ, so every formula here is tested on its own. Used
only by the SIMULATOR (and the GUI's Kittel indicator); the real analyser
measures instead.

Frequency units throughout: the fields are turned into frequencies once,
    f_H = (gamma/2pi) mu0 H      f_M = (gamma/2pi) mu0 Ms      f_0 = (gamma/2pi) mu0 dH0
and everything after that is in Hz.

Resonance (Kittel)
    in-plane:      f_r = sqrt(f_1 f_2)
                   f_1 = f_H + f_K cos 2(phi - phi_u)
                   f_2 = f_H + f_M + f_K cos^2(phi - phi_u)
    out-of-plane:  f_r = f_H - f_M          (only above saturation; angle ignored)
with H = |H| + h_anis, f_K = (gamma/2pi) mu0 Hk the in-plane UNIAXIAL anisotropy,
phi the field angle and phi_u the easy axis. The magnetisation is assumed to lie
along the field (true well above Hk). With Hk = 0 (the default): f_1 = f_H and
f_2 = f_H + f_M, the plain isotropic Kittel formula -- nothing moves.
Along the easy axis the line sits HIGHER (the anisotropy adds to the field),
along the hard axis lower.

Susceptibility seen by the in-plane rf field of the waveguide (Polder, small
damping, e^{-i omega t} so that Im chi > 0 is absorption):
    in-plane:      chi = f_M f_2 / (f_1 f_2 - f^2 - i D)
                   D   = (alpha f + f_0/2) (f_1 + f_2)
    out-of-plane:  chi = f_M f_i / (f_i^2 - f^2 - i D),  f_i = f_H - f_M
                   D   = (alpha f + f_0/2) 2 f_i
D is written so the FIELD-swept FWHM is  mu0 dH = 2 alpha f / (gamma/2pi) + mu0 dH0,
the usual linear-in-frequency linewidth, and the FREQUENCY-swept FWHM is D / f_r.
(With Hk = 0, f_1 + f_2 = 2 f_H + f_M and f_M f_2 = f_M (f_H + f_M), i.e. the
isotropic expressions exactly.)

S21 through the line
    S21 = S21_line(f) * exp(i eta f chi(f) / N)
The film absorbs (|S21| dips) and shifts the phase dispersively. N is the value
of f Im(chi) on resonance at a reference field 50 mT above saturation (isotropic,
so the anisotropy does not change the coupling), and eta = dip_dB ln(10)/20
makes the dip exactly `dip_dB` deep THERE; at other fields the depth follows
f Im(chi), i.e. the physics, not a constant.

Other S-parameters, loosely (the point is a plausible trace, not an EM solver):
    S12 = S21                      the line is reciprocal
    S11 = small connector mismatches + half of what the film removes from S21,
          reflected back through the first half of the line
    S22 = the same with the two mismatches swapped
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np

from .config import Line, Sample

#: field above the saturation threshold at which `Sample.dip_dB` holds
REFERENCE_FIELD_ABOVE_SAT_mT = 50.0

#: the S-parameters a two-port analyser measures
SPARAMS = ("S11", "S12", "S21", "S22")


def _f_per_mT(s: Sample) -> float:
    """Hz per mT: gamma/2pi [GHz/T] * 1e9 [Hz/GHz] * 1e-3 [T/mT]."""
    return s.gamma_GHz_per_T * 1e6


def _h_eff_mT(field_mT: float, s: Sample) -> float:
    """|H| + anisotropy, never negative. The sign of the field does not move a
    Kittel line: the magnetisation follows the field round."""
    return max(0.0, abs(float(field_mT)) + s.h_anis_mT)


def _in_plane_f12(field_mT: float, s: Sample, angle_deg: float) -> tuple[float, float]:
    """(f_1, f_2) of the in-plane Kittel formula, uniaxial anisotropy included.

    cos 2(phi - phi_u) and cos^2(phi - phi_u) do not change when phi moves by
    180 deg, so a negative field on a 1-axis magnet (angle 0) is the same line
    as a positive one -- as it must be."""
    k = _f_per_mT(s)
    h = _h_eff_mT(field_mT, s)
    d = math.radians(float(angle_deg) - s.easy_axis_deg)
    f1 = k * (h + s.hk_mT * math.cos(2 * d))
    f2 = k * (h + s.ms_mT + s.hk_mT * math.cos(d) ** 2)
    return f1, f2


def saturation_mT(s: Sample) -> float:
    """The field below which there is no uniform-mode resonance in this model."""
    return s.ms_mT if s.geometry == "out_of_plane" else 0.0


def kittel_Hz(field_mT: float, s: Sample, angle_deg: float = 0.0) -> float:
    """Resonance frequency of the uniform mode, or NaN when there is none."""
    k = _f_per_mT(s)
    if s.geometry == "out_of_plane":
        fi = k * (_h_eff_mT(field_mT, s) - s.ms_mT)
        return fi if fi > 0 else math.nan
    f1, f2 = _in_plane_f12(field_mT, s, angle_deg)
    return math.sqrt(f1 * f2) if f1 > 0 and f2 > 0 else math.nan


def field_for_Hz(f_Hz: float, s: Sample, angle_deg: float = 0.0) -> float:
    """Inverse Kittel: the field (mT) that puts the resonance at f_Hz.

    In-plane with anisotropy terms a = Hk cos 2d, b = Hk cos^2 d (in mT):
        (h + a)(h + Ms + b) = (f/k)^2   ->   the positive root of a quadratic in h.
    """
    k = _f_per_mT(s)
    if s.geometry == "out_of_plane":
        return f_Hz / k + s.ms_mT - s.h_anis_mT
    d = math.radians(float(angle_deg) - s.easy_axis_deg)
    a = s.hk_mT * math.cos(2 * d)
    b = s.hk_mT * math.cos(d) ** 2
    F = f_Hz / k
    p = a + s.ms_mT + b
    h = (-p + math.sqrt(p * p - 4 * (a * (s.ms_mT + b) - F * F))) / 2
    return h - s.h_anis_mT


def linewidth_Hz(field_mT: float, s: Sample, angle_deg: float = 0.0) -> float:
    """Frequency-swept FWHM on resonance, D / f_r. NaN when there is no resonance."""
    fr = kittel_Hz(field_mT, s, angle_deg)
    if not math.isfinite(fr):
        return math.nan
    return float(_damping(np.array([fr]), field_mT, s, angle_deg)[0]) / fr


def _damping(freqs_Hz: np.ndarray, field_mT: float, s: Sample,
             angle_deg: float = 0.0) -> np.ndarray:
    k = _f_per_mT(s)
    per_f = s.alpha * freqs_Hz + k * s.dh0_mT / 2
    if s.geometry == "out_of_plane":
        return per_f * 2 * (k * _h_eff_mT(field_mT, s) - k * s.ms_mT)
    f1, f2 = _in_plane_f12(field_mT, s, angle_deg)
    return per_f * (f1 + f2)


def susceptibility(freqs_Hz, field_mT: float, s: Sample, angle_deg: float = 0.0) -> np.ndarray:
    """Complex chi(f) of the film. All zeros when there is no uniform mode
    (an unsaturated out-of-plane film, or an in-plane state the anisotropy
    makes unstable)."""
    f = np.asarray(freqs_Hz, dtype=float)
    k = _f_per_mT(s)
    fm = k * s.ms_mT
    d = _damping(f, field_mT, s, angle_deg)
    if s.geometry == "out_of_plane":
        fi = k * _h_eff_mT(field_mT, s) - fm
        if fi <= 0:
            return np.zeros(f.shape, dtype=complex)
        return fm * fi / (fi * fi - f * f - 1j * d)
    f1, f2 = _in_plane_f12(field_mT, s, angle_deg)
    if f1 <= 0 or f2 <= 0:
        return np.zeros(f.shape, dtype=complex)
    return fm * f2 / (f1 * f2 - f * f - 1j * d)


def _coupling_norm(s: Sample) -> float:
    """f Im(chi) on resonance at the reference field (see the module docstring).
    Computed WITHOUT the uniaxial anisotropy, so Hk moves the line but does not
    re-scale how strongly the film couples to the waveguide."""
    iso = dataclasses.replace(s, hk_mT=0.0)
    h_ref = saturation_mT(iso) + REFERENCE_FIELD_ABOVE_SAT_mT - iso.h_anis_mT
    fr = kittel_Hz(h_ref, iso)
    chi = susceptibility(np.array([fr]), h_ref, iso)[0]
    return fr * chi.imag


def line_s21(freqs_Hz, line: Line) -> np.ndarray:
    """S21 of the empty line: sqrt(f) loss, standing-wave ripple, electrical delay."""
    f = np.asarray(freqs_Hz, dtype=float)
    amp_dB = (-line.loss_dB_at_10GHz * np.sqrt(np.clip(f, 0, None) / 10e9)
              + line.ripple_dB * np.sin(2 * np.pi * f / (line.ripple_period_MHz * 1e6)))
    phase = -2 * np.pi * f * line.delay_ns * 1e-9
    return 10 ** (amp_dB / 20) * np.exp(1j * phase)


def _film_factor(freqs_Hz, field_mT: float, s: Sample, angle_deg: float) -> np.ndarray:
    """exp(i eta f chi / N): what the film multiplies the transmission by."""
    f = np.asarray(freqs_Hz, dtype=float)
    eta = s.dip_dB * math.log(10) / 20
    chi = susceptibility(f, field_mT, s, angle_deg)
    return np.exp(1j * eta * f * chi / _coupling_norm(s))


def s21_clean(freqs_Hz, field_mT: float, s: Sample, line: Line,
              angle_deg: float = 0.0) -> np.ndarray:
    """Noiseless S21 at one field."""
    return line_s21(freqs_Hz, line) * _film_factor(freqs_Hz, field_mT, s, angle_deg)


#: return losses of the two connectors of the pretend line, in dB (port 1, port 2)
_MISMATCH_DB = (-30.0, -20.0)


def s_clean(freqs_Hz, field_mT: float, s: Sample, line: Line, sparam: str = "S21",
            angle_deg: float = 0.0) -> np.ndarray:
    """Noiseless S-parameter `sparam` at one field (see the module docstring)."""
    if sparam in ("S21", "S12"):
        return s21_clean(freqs_Hz, field_mT, s, line, angle_deg)
    if sparam not in ("S11", "S22"):
        raise ValueError(f"sparam must be one of {SPARAMS}, got {sparam!r}")
    f = np.asarray(freqs_Hz, dtype=float)
    near_dB, far_dB = _MISMATCH_DB if sparam == "S11" else _MISMATCH_DB[::-1]
    through = line_s21(f, line)
    half = line_s21(f, dataclasses.replace(line, loss_dB_at_10GHz=line.loss_dB_at_10GHz / 2,
                                           delay_ns=line.delay_ns / 2, ripple_dB=0.0))
    film = _film_factor(f, field_mT, s, angle_deg)
    return (10 ** (near_dB / 20)                         # the near connector
            + 10 ** (far_dB / 20) * through ** 2         # the far one, there and back
            + 0.5 * (1 - film) * half ** 2)              # the film, from the middle


def noise_rms(ifbw_Hz: float, power_dBm: float, line: Line) -> float:
    """Trace noise: grows as sqrt(IFBW), falls as the source power rises."""
    return line.noise * math.sqrt(ifbw_Hz / 10e3) * 10 ** (-(power_dBm + 10.0) / 20)


def s21_measured(freqs_Hz, field_mT: float, s: Sample, line: Line,
                 ifbw_Hz: float, power_dBm: float, rng: np.random.Generator,
                 angle_deg: float = 0.0, sparam: str = "S21") -> np.ndarray:
    """One sweep: the clean trace plus complex Gaussian noise. (Named for S21,
    its first job; `sparam` picks any of the four.)"""
    clean = s_clean(freqs_Hz, field_mT, s, line, sparam, angle_deg)
    sigma = noise_rms(ifbw_Hz, power_dBm, line) / math.sqrt(2)
    return clean + sigma * (rng.standard_normal(clean.shape)
                            + 1j * rng.standard_normal(clean.shape))


def sweep_time_s(points: int, ifbw_Hz: float) -> float:
    """Roughly what a real VNA takes: ~1.2 / IFBW per point."""
    return 1.2 * int(points) / float(ifbw_Hz)


def find_dip(freqs_Hz, s21) -> tuple[float, float]:
    """The FMR dip in a raw trace: (frequency Hz, depth dB below the baseline).

    The raw |S21| slopes down by several dB across a broad sweep, so its
    plain minimum is usually the top end of the band, not the resonance. Fit a
    smooth baseline (cubic in f, in dB), rejecting points far BELOW it until it
    stops changing -- the dip rejects itself -- and take the deepest residual,
    refined with a parabola through its neighbours.

    Needs the span to be much wider than the line. Zoomed in to a span of a
    few linewidths the cubic follows the dip and the depth reads too small.
    Returns (nan, nan) for a trace too short to fit.
    """
    f = np.asarray(freqs_Hz, dtype=float)
    z = np.asarray(s21)
    if f.size < 8 or z.shape != f.shape:
        return math.nan, math.nan
    mag = 20 * np.log10(np.clip(np.abs(z), 1e-15, None))
    x = (f - f.mean()) / max(np.ptp(f) / 2, 1e-30)
    keep = np.isfinite(mag)
    resid = np.zeros_like(mag)
    for _ in range(8):
        if keep.sum() < 8:
            break
        coef = np.polyfit(x[keep], mag[keep], 3)
        resid = mag - np.polyval(coef, x)
        sd = float(np.std(resid[keep])) or 1e-12
        new = np.isfinite(mag) & (resid > -2.5 * sd)
        if np.array_equal(new, keep):
            break
        keep = new
    i = int(np.nanargmin(resid))
    fi, di = float(f[i]), float(resid[i])
    if 0 < i < f.size - 1:
        y0, y1, y2 = resid[i - 1], resid[i], resid[i + 1]
        den = y0 - 2 * y1 + y2
        if den > 0:
            off = 0.5 * (y0 - y2) / den            # in bins, within +-0.5
            fi += off * (f[i + 1] - f[i - 1]) / 2
            di = float(y1 - 0.25 * (y0 - y2) * off)
    return fi, di
