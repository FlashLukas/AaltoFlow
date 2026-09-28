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


def chain_dB(freqs_Hz, sim) -> np.ndarray:
    """The noise-free power reaching the analyser, per frequency, in dB
    relative to the TG output."""
    f = np.asarray(freqs_Hz, dtype=float)
    p = (sim.tg_ripple_dB * np.sin(2 * np.pi * f / 370e6)                 # TG flatness
         - sim.cable_loss_dB_at_1GHz * np.sqrt(np.clip(f, 0, None) / 1e9)  # skin effect
         - sim.pad_dB)
    if sim.dut_inserted:
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


def tg_sweep_dB(freqs_Hz, rbw_Hz: float, sim, rng) -> np.ndarray:
    """ONE simulated TG sweep in dB relative to the TG output: the chain on its
    noise floor, with the floor fluctuating like noise does (Gamma-distributed
    power) and a 0.02 dB rms jitter on the tone itself."""
    f = np.asarray(freqs_Hz, dtype=float)
    floor = db_to_lin(floor_dB(sim, rbw_Hz))
    noise = floor * rng.gamma(4.0, 1.0 / 4.0, size=f.size)
    total = db_to_lin(chain_dB(f, sim)) + noise
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
