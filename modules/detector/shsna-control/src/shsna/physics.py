"""The simulator's physics and the scalar arithmetic every trace goes through.

Pure numpy functions -- no state, no threads -- so they can be tested one at a
time and plotted by hand.

THE SIMULATED CHAIN (copied from signalhound-control's tracking-mode model,
2026-09-28, and extended with the lab's fixed attenuator):

    P(f) = TG ripple - cable loss(f) - pad [- DUT loss + band-pass(f)]

in dB RELATIVE TO THE TG OUTPUT -- the unit the real TG44A reports a TG sweep
in (measured 2026-09-28: -19.4 dB flat through the bench's 20 dB pad) -- on
top of the analyser's noise floor for the narrow bandwidth a TG sweep
measures in. The TG output is not flat and the cable loss rises as sqrt(f);
both are identical in the thru and in the DUT sweep, so

    T(f) [dB] = P_dut(f) - P_thru(f)

leaves the band-pass alone. That subtraction is the whole reason for the
reference.

THE FILM (optional, `sim.fmr_on`, 2026-09-28): the DUT becomes a waveguide
carrying a magnetic film whose Kittel line moves with the field -- see the
FMR section further down.

AVERAGING is done in linear POWER, never in dB: the mean of the dB values
of a noisy signal reads low (about 2.5 dB for pure noise), because the log of
a mean is not the mean of the logs. A spectrum analyser's "power average" is
the mean of linear power, and so is ours (a dB ratio converts exactly like
a dBm value: 10^(x/10)).
"""

from __future__ import annotations

import math

import numpy as np

_FLOOR_LIN = 1e-30       # what log10 is clipped at: -300 dB, far below any real floor


def db_to_lin(db):
    """dB (a ratio, or dBm) -> linear power (a ratio, or mW)."""
    return 10.0 ** (np.asarray(db, dtype=float) / 10.0)


def lin_to_db(p):
    """linear power -> dB, clipped at -300 dB so a zero never becomes -inf."""
    return 10.0 * np.log10(np.clip(np.asarray(p, dtype=float), _FLOOR_LIN, None))


def power_mean_db(traces_db) -> np.ndarray:
    """Average several dB (or dBm) traces in linear power; returns the same unit."""
    stack = np.asarray(traces_db, dtype=float)
    return lin_to_db(db_to_lin(stack).mean(axis=0))


def butterworth_bandpass_dB(f_Hz, center_Hz, bandwidth_Hz, order):
    """|H|^2 of an n-th order Butterworth band-pass, in dB (0 at the centre,
    -3 dB at the band edges). Uses the usual low-pass -> band-pass mapping
    x = Q (f/f0 - f0/f)."""
    f = np.clip(np.asarray(f_Hz, dtype=float), 1.0, None)
    q = center_Hz / max(bandwidth_Hz, 1.0)
    x = q * (f / center_Hz - center_Hz / f)
    return -10.0 * np.log10(1.0 + x ** (2 * int(max(1, order))))


# ---- the optional magnetic film (sim.fmr_on, added 2026-09-28) --------------
#
# WHY: FMR in field with the SA + TG. A full-band sweep per field point is
# slow, so scan-core predicts the resonance from the field and the film and
# asks for a window of bins around it. To test that chain with no hardware,
# the simulated DUT can be a waveguide carrying a magnetic film, which absorbs
# at the Kittel frequency of the field it sits in.
#
# gamma' = gamma/2pi = g * mu_B / h = g * 13.996 GHz/T, i.e. g * 13.996e6 Hz/mT.
# All fields below are mu0*H in mT.
#
# IN-PLANE (field in the film plane, thin film, Smit-Beljers). The film has an
# in-plane uniaxial anisotropy Hk along the easy axis; angles are measured
# from that easy axis. The magnetisation lies in the plane at angle theta,
# where its energy per moment
#
#     E(theta) = -B cos(theta - phi) - (Hk/2) cos^2(theta)
#
# is lowest (phi = field angle). It does NOT simply follow the field: at a
# low field along the hard axis it stays tilted towards the easy axis. On
# that equilibrium the resonance is
#
#     f = gamma' sqrt(B1 B2)
#     B1 = B cos(theta - phi) + Hk cos(2 theta)            (in-plane stiffness)
#     B2 = B cos(theta - phi) + Hk cos^2(theta) + Meff     (out-of-plane stiffness)
#
# With Hk = 0 this is the plain Kittel formula f = gamma' sqrt(B (B + Meff)).
# Along the easy axis the line sits HIGHER than that, along the hard axis
# lower. No hysteresis is modelled: the global energy minimum is taken.
#
# OUT-OF-PLANE (field along the film normal). The film is saturated along the
# normal only above B = Meff; there
#
#     f = gamma' (B - Meff)
#
# and below it there is no uniform-mode line in this model (none is drawn).
# The in-plane anisotropy is ignored for this geometry.
#
# LINEWIDTH (frequency-swept FWHM). Gilbert damping alpha gives
#     in-plane      alpha * gamma' (B1 + B2)
#     out-of-plane  2 alpha f
# (the same expressions as vna-control's Polder susceptibility), unless
# `fmr_linewidth_Hz` > 0 fixes it. The dip is a Lorentzian IN dB,
#     dip(f) = depth / (1 + (2 (f - f_res) / FWHM)^2),
# of constant depth: good enough to test that a window catches a line, and
# no claim about how the absorption scales with field.

#: gamma/2pi per unit g, in Hz per mT (mu_B / h = 13.996 GHz/T)
GAMMA_HZ_PER_MT_PER_G = 13.996245e6


def gamma_Hz_per_mT(g: float) -> float:
    return float(g) * GAMMA_HZ_PER_MT_PER_G


def _polar(field_mT: float, angle_deg: float) -> tuple[float, float]:
    """A signed field on an axis -> (|B|, angle). A negative 1-axis field at
    0 deg IS the field at 180 deg."""
    b, a = float(field_mT), float(angle_deg)
    if b < 0:
        b, a = -b, a + 180.0
    return b, a


def inplane_equilibrium_deg(field_mT: float, angle_deg: float, hk_mT: float,
                            easy_axis_deg: float = 0.0) -> float:
    """The in-plane magnetisation angle (same convention as the field angle)
    that minimises E(theta) = -B cos(theta - phi) - (Hk/2) cos^2(theta - easy).

    Found on a 0.5 deg grid (the global minimum; two minima exist below the
    switching field and there is no hysteresis here), then polished with
    Newton on dE/dtheta = B sin(theta - phi) + (Hk/2) sin 2(theta - easy)."""
    b, a = _polar(field_mT, angle_deg)
    phi = math.radians(a - easy_axis_deg)
    hk = float(hk_mT)
    th = np.linspace(0.0, 2 * np.pi, 721)
    e = -b * np.cos(th - phi) - 0.5 * hk * np.cos(th) ** 2
    t = float(th[int(np.argmin(e))])
    for _ in range(30):
        g1 = b * math.sin(t - phi) + 0.5 * hk * math.sin(2 * t)
        g2 = b * math.cos(t - phi) + hk * math.cos(2 * t)
        if g2 <= 0:
            break                 # not near a minimum: keep the grid value
        step = g1 / g2
        t -= step
        if abs(step) < 1e-13:
            break
    return math.degrees(t) + easy_axis_deg


def fmr_resonance(field_mT: float, angle_deg: float, sim) -> tuple[float, float]:
    """(f_res, FWHM) in Hz for the film of `sim` in this field, or (nan, nan)
    when there is no line (out-of-plane below saturation, zero field without
    anisotropy, an unstable state)."""
    nan = float("nan")
    if not (math.isfinite(field_mT) and math.isfinite(angle_deg)):
        return nan, nan
    k = gamma_Hz_per_mT(sim.fmr_g)
    b, a = _polar(field_mT, angle_deg)
    meff = float(sim.fmr_meff_mT)
    if str(sim.fmr_geometry).lower() == "outofplane":
        f = k * (b - meff)
        if not f > 0:
            return nan, nan
        fwhm = 2 * sim.fmr_alpha * f
    else:
        hk = float(sim.fmr_hk_mT)
        easy = float(sim.fmr_easy_axis_deg)
        th = math.radians(inplane_equilibrium_deg(b, a, hk, easy) - easy)
        phi = math.radians(a - easy)
        along = b * math.cos(th - phi)
        b1 = along + hk * math.cos(2 * th)
        b2 = along + hk * math.cos(th) ** 2 + meff
        if not (b1 > 0 and b2 > 0):
            return nan, nan
        f = k * math.sqrt(b1 * b2)
        fwhm = sim.fmr_alpha * k * (b1 + b2)
    if sim.fmr_linewidth_Hz and sim.fmr_linewidth_Hz > 0:
        fwhm = float(sim.fmr_linewidth_Hz)
    return f, max(float(fwhm), 1.0)


def fmr_dip_dB(freqs_Hz, f_res_Hz: float, fwhm_Hz: float, depth_dB: float) -> np.ndarray:
    """The film's absorption, in dB to SUBTRACT: a Lorentzian of FWHM
    `fwhm_Hz` and height `depth_dB` at f_res. Zero everywhere when there is no
    line (f_res NaN)."""
    f = np.asarray(freqs_Hz, dtype=float)
    if not (math.isfinite(f_res_Hz) and math.isfinite(fwhm_Hz) and fwhm_Hz > 0):
        return np.zeros(f.shape)
    x = 2.0 * (f - f_res_Hz) / fwhm_Hz
    return float(depth_dB) / (1.0 + x * x)


def chain_dB(freqs_Hz, sim, field_mT: float = float("nan"),
             angle_deg: float = 0.0) -> np.ndarray:
    """The noise-free power reaching the analyser, per frequency, in dB
    relative to the TG output.

    With `sim.fmr_on` and the DUT inserted, the DUT is a broadband waveguide
    (flat insertion loss, NO band-pass) with the film on it, absorbing at the
    Kittel frequency of (field_mT, angle_deg). A NaN field = no line."""
    f = np.asarray(freqs_Hz, dtype=float)
    p = (sim.tg_ripple_dB * np.sin(2 * np.pi * f / 370e6)                 # TG flatness
         - sim.cable_loss_dB_at_1GHz * np.sqrt(np.clip(f, 0, None) / 1e9)  # skin effect
         - sim.pad_dB)
    if sim.dut_inserted:
        if getattr(sim, "fmr_on", False):
            fr, fwhm = fmr_resonance(field_mT, angle_deg, sim)
            p = p - sim.dut_loss_dB - fmr_dip_dB(f, fr, fwhm, sim.fmr_depth_dB)
        else:
            p = p - sim.dut_loss_dB + butterworth_bandpass_dB(
                f, sim.dut_center_Hz, sim.dut_bandwidth_Hz, sim.dut_order)
    return p


def floor_dB(sim, rbw_Hz: float) -> float:
    """Mean noise power in one bin, relative to the TG output. `sim.floor_dB`
    is the floor at the analyser's default TG bandwidth (taken as 1 kHz); a
    set RBW moves it by 10 log10(rbw / 1 kHz), as noise power scales with
    bandwidth."""
    bw = rbw_Hz if rbw_Hz and rbw_Hz > 0 else 1e3
    return sim.floor_dB + 10.0 * math.log10(bw / 1e3)


def tg_sweep_dB(freqs_Hz, rbw_Hz: float, sim, rng, field_mT: float = float("nan"),
                angle_deg: float = 0.0) -> np.ndarray:
    """ONE simulated TG sweep in dB relative to the TG output: the chain on its
    noise floor, with the floor fluctuating like noise does (Gamma-distributed
    power) and a 0.02 dB rms jitter on the tone itself. The field matters only
    for the optional film (sim.fmr_on)."""
    f = np.asarray(freqs_Hz, dtype=float)
    floor = db_to_lin(floor_dB(sim, rbw_Hz))
    noise = floor * rng.gamma(4.0, 1.0 / 4.0, size=f.size)
    total = db_to_lin(chain_dB(f, sim, field_mT, angle_deg)) + noise
    total = total * 10 ** (rng.normal(0.0, 0.02, size=f.size) / 10)
    return lin_to_db(total)


# ---- what a scan wants to know about a transmission trace ------------------

def summarise_transmission(freqs_Hz, t_dB) -> dict:
    """The scalar detectors of one transmission trace.

    peak_transmission_db  the largest |S21| in the band (a filter's pass band,
                          a resonator's peak)
    peak_freq_hz          where it is
    mean_transmission_db  the band-averaged POWER transmission, 10 log10 of the
                          mean linear ratio -- so a deep stop band does not drag
                          it to minus infinity, as a mean of dB values would
    bw3_hz                width of the contiguous region around the peak that
                          stays within 3 dB of it (a filter's -3 dB bandwidth);
                          NaN when that region touches the edge of the sweep,
                          because then the true width is not measured
    """
    f = np.asarray(freqs_Hz, dtype=float)
    t = np.asarray(t_dB, dtype=float)
    ok = np.isfinite(t)
    nan = float("nan")
    if not ok.any():
        return {"peak_transmission_db": nan, "peak_freq_hz": nan,
                "mean_transmission_db": nan, "bw3_hz": nan}
    i = int(np.nanargmax(np.where(ok, t, -np.inf)))
    peak = float(t[i])
    mean = float(lin_to_db(np.mean(db_to_lin(t[ok]))))
    inside = ok & (t >= peak - 3.0)
    lo = i
    while lo > 0 and inside[lo - 1]:
        lo -= 1
    hi = i
    while hi < t.size - 1 and inside[hi + 1]:
        hi += 1
    if lo == 0 or hi == t.size - 1:
        bw = nan
    else:
        # Interpolate each edge linearly between the last bin inside and the
        # first bin outside, so the width is not quantised to whole bins.
        level = peak - 3.0

        def cross(a, b):          # bins a (inside) and b (outside)
            ta, tb = t[a], t[b]
            if not math.isfinite(tb) or ta == tb:
                return f[a]
            return f[a] + (f[b] - f[a]) * (ta - level) / (ta - tb)
        bw = float(cross(hi, hi + 1) - cross(lo, lo - 1))
    return {"peak_transmission_db": peak, "peak_freq_hz": float(f[i]),
            "mean_transmission_db": mean, "bw3_hz": bw}
