"""The simulator's physics: what a swept spectrum analyser displays.

Pure numpy functions of (frequencies, settings, scene) -- no state, no
threads -- so they can be tested one at a time and plotted by hand.

Spectrum mode, per frequency bin:
  * every signal (the tone and its harmonics) seen through the RBW filter, a
    Gaussian whose -3 dB width is the RBW. With the PEAK detector a bin shows
    the largest value anywhere inside it, so a tone is never lost between
    coarse bins; the AVERAGE detector samples the bin centre.
  * the tone's phase-noise skirt (a real source is not a delta function).
  * the noise floor: DANL [dBm/Hz] + 10 log10(RBW), raised 1 dB per dB of
    reference level above -30 dBm (a higher reference level means more input
    attenuation, and attenuation raises the floor by the same amount).
  * noise fluctuation: the power of one FFT sample is exponentially
    distributed; a bin combines a few of them, and a VBW narrower than the RBW
    averages ~RBW/VBW times more (a Gamma distribution) -- which smooths the
    trace but does NOT lower the mean floor.
  * compression: signals above ~ref + 5 dB are squashed and flagged OVERLOAD.

Tracking mode: TG level + the TG's own (not flat) output + cable loss
(rising as sqrt f) + the band-pass filter if inserted, on a floor set by the
narrow bandwidth the TG sweep uses. Dividing by a thru taken with the filter
removed leaves the filter alone -- the whole point of the reference.
"""

from __future__ import annotations

import math

import numpy as np

from .instruments import SweepSettings, model_range, TG_RANGE_HZ

_LN2x4 = 4.0 * math.log(2.0)


def dbm_to_mw(dbm):
    return 10.0 ** (np.asarray(dbm, dtype=float) / 10.0)


def mw_to_dbm(mw):
    return 10.0 * np.log10(np.clip(np.asarray(mw, dtype=float), 1e-30, None))


def rbw_response(df_Hz, rbw_Hz):
    """Power response of the RBW filter at an offset df: Gaussian, 0.5 at +-RBW/2."""
    x = np.asarray(df_Hz, dtype=float) / rbw_Hz
    return np.exp(-_LN2x4 * x * x)


def phase_noise_dBc_per_Hz(df_Hz):
    """A plausible synthesiser: -80 dBc/Hz close in, falling 20 dB/decade
    beyond 100 kHz."""
    df = np.abs(np.asarray(df_Hz, dtype=float))
    return -80.0 - 20.0 * np.log10(1.0 + df / 100e3)


def noise_floor_dBm(s: SweepSettings, scene) -> float:
    """Mean displayed noise in one bin, spectrum mode."""
    return (scene.danl_dBm_per_Hz + 10.0 * math.log10(s.rbw_Hz)
            + max(0.0, s.ref_level_dBm + 30.0))


def signals(scene) -> list[tuple[float, float]]:
    """(frequency, level in dBm) of everything the generator puts out."""
    if not scene.tone_on:
        return []
    f0, p0, h = scene.tone_Hz, scene.tone_dBm, scene.harmonic_dBc
    return [(f0, p0), (2 * f0, p0 + h), (3 * f0, p0 + h - 10.0)]


def butterworth_bandpass_dB(f_Hz, center_Hz, bandwidth_Hz, order):
    """|H|^2 of an n-th order Butterworth band-pass, in dB (0 at the centre).
    Uses the usual low-pass -> band-pass mapping x = Q (f/f0 - f0/f)."""
    f = np.clip(np.asarray(f_Hz, dtype=float), 1.0, None)
    q = center_Hz / max(bandwidth_Hz, 1.0)
    x = q * (f / center_Hz - center_Hz / f)
    return -10.0 * np.log10(1.0 + x ** (2 * int(max(1, order))))


def _fluctuate(mean_mw, k, rng, peak: bool):
    """Noise-like power: Gamma(k) around the mean (k = number of independent
    samples averaged). The peak detector keeps the largest of three, which is
    why a peak-detected floor sits a few dB higher."""
    k = max(1.0, float(k))
    draw = rng.gamma(k, 1.0 / k, size=(3 if peak else 1, np.size(mean_mw)))
    return np.asarray(mean_mw) * (draw.max(axis=0) if peak else draw[0])


def spectrum_dBm(freqs_Hz, bin_Hz, s: SweepSettings, scene, model: str, rng):
    """One simulated spectrum sweep. Returns (trace in dBm, overload flag)."""
    f = np.asarray(freqs_Hz, dtype=float)
    peak = s.detector == "peak"
    fmin, fmax, _ = model_range(model)
    sig = np.zeros_like(f)
    for fs, ps in signals(scene):
        if not (fmin <= fs <= fmax):
            continue
        df = np.abs(f - fs)
        if peak:                         # the largest value inside the bin
            df = np.clip(df - bin_Hz / 2, 0.0, None)
        p = dbm_to_mw(ps)
        sig += p * rbw_response(df, s.rbw_Hz)
        # phase-noise skirt, integrated over the RBW, outside the main lobe
        skirt = dbm_to_mw(ps + phase_noise_dBc_per_Hz(df)) * s.rbw_Hz
        sig += np.where(df > s.rbw_Hz, skirt, 0.0)
    floor = dbm_to_mw(noise_floor_dBm(s, scene))
    # Each displayed bin already combines a few FFT samples (~4), and a VBW
    # narrower than the RBW averages ~RBW/VBW more of them.
    k = 4.0 * max(1.0, s.rbw_Hz / max(s.vbw_Hz, 1e-9))
    total = sig + _fluctuate(np.full_like(f, floor), k, rng, peak)
    db = mw_to_dbm(total)
    # compression: above ref + 5 dB the display stops following the input
    knee = s.ref_level_dBm + 5.0
    over = db > knee
    db = np.where(over, knee + 0.1 * (db - knee), db)
    # outside what this model can measure there is nothing but floor
    db = np.where((f < fmin) | (f > fmax), mw_to_dbm(floor), db)
    return db, bool(over.any())


def tracking_dBm(freqs_Hz, s: SweepSettings, scene, rng):
    """One simulated tracking-generator sweep. Returns (trace in dBm, overload)."""
    f = np.asarray(freqs_Hz, dtype=float)
    level = (s.tg_level_dBm
             + scene.tg_ripple_dB * np.sin(2 * np.pi * f / 370e6)       # TG flatness
             - scene.cable_loss_dB_at_1GHz * np.sqrt(np.clip(f, 0, None) / 1e9))
    if scene.dut_inserted:
        level = level - scene.dut_loss_dB + butterworth_bandpass_dB(
            f, scene.dut_center_Hz, scene.dut_bandwidth_Hz, scene.dut_order)
    # the TG sweep measures in a narrow bandwidth: ~1 kHz with high dynamic range
    bw = 1e3 if s.tg_high_dynamic_range else 10e3
    floor = scene.danl_dBm_per_Hz + 10 * math.log10(bw) + max(0.0, s.ref_level_dBm + 30.0)
    total = dbm_to_mw(level) + _fluctuate(np.full_like(f, dbm_to_mw(floor)), 20.0, rng, False)
    # a little measurement jitter on the tone itself (0.02 dB rms)
    total = total * 10 ** (rng.normal(0.0, 0.02, size=f.size) / 10)
    db = mw_to_dbm(total)
    lo, hi = TG_RANGE_HZ
    db = np.where((f < lo) | (f > hi), floor, db)
    return db, bool((db > s.ref_level_dBm + 5.0).any())
