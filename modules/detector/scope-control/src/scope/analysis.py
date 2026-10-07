"""What the module computes from traces: no hardware, no threads, numpy only.

Three parts, each a pure function so it can be tested on its own:

  * `reduce_points` -- the scope's record (thousands of points) down to the
    configured trace length, by averaging neighbours (a boxcar).
  * `zero_phase` -- the low/high-pass filter. Applied identically to every
    channel and with ZERO phase, so the channels stay time-aligned with each
    other (an ordinary filter delays a signal by a frequency-dependent amount
    and would shift one channel against the other).
  * `channel_values`, `frequency`, `phase_deg` -- the numbers a scan records.

Deliberately NOT here: any analysis of what the signals MEAN for an
experiment. That belongs in the AaltoView processing module; the scope
records the traces it needs.
"""

from __future__ import annotations

import math

import numpy as np

_NAN = float("nan")


# ---- trace length -------------------------------------------------------------

def reduce_points(t: np.ndarray, ys: dict, points: int,
                  max_bin_s: float = 0.0) -> tuple[np.ndarray, dict]:
    """The scope's record down to `points` samples for the STORED / shown trace.

    Averaging neighbours (a boxcar) lowers the noise -- but only while a bin
    is short against the signal's period. Lab PC 2026-10-07: 20480 points of
    a 50 Hz sine at 1250 Sa/s averaged down to 1000 = bins of 0.8 period,
    and the 1 Vpp sine came out as 0.26 Vpp at "11 Hz". So when a bin would
    be longer than `max_bin_s` (the caller passes ~1/20 of the measured
    period; 0 = no limit) the trace is SAMPLED at the `points` instants
    instead (linear interpolation): no amplitude is lost, though a trace
    with too few points per period shows an alias -- the numbers never come
    from it (they are computed on the full record). Fewer samples than
    `points` are interpolated onto `points` (every trace of a scan then has
    the same length)."""
    t = np.asarray(t, dtype=float)
    n = t.size
    points = max(2, int(points))
    if n == points:
        return t.copy(), {k: np.asarray(v, dtype=float).copy() for k, v in ys.items()}
    bin_s = (t[-1] - t[0]) / points if n > 1 else 0.0
    if n > points and not (max_bin_s > 0 and bin_s > max_bin_s):
        edges = np.linspace(0, n, points + 1).astype(int)
        idx = np.repeat(np.arange(points), np.diff(edges))
        counts = np.bincount(idx, minlength=points).astype(float)

        def red(v):
            return np.bincount(idx, weights=np.asarray(v, dtype=float), minlength=points) / counts
        return red(t), {k: red(v) for k, v in ys.items()}
    tn = np.linspace(t[0], t[-1], points)
    return tn, {k: np.interp(tn, t, np.asarray(v, dtype=float)) for k, v in ys.items()}


# ---- zero-phase filter -----------------------------------------------------------

def zero_phase(y: np.ndarray, dt: float, lowpass_Hz: float = 0.0,
               highpass_Hz: float = 0.0, order: int = 2) -> np.ndarray:
    """Butterworth low/high-pass with ZERO phase: the squared magnitude |H(f)|^2
    applied to the spectrum (what scipy's filtfilt does in the time domain).

    Before the FFT the record is extended at both ends by its POINT
    reflection (2*y[0] - y reversed, as filtfilt's "odd" padding): value AND
    slope stay continuous at the edges, so a record that does not hold a whole
    number of periods does not ring there. (A plain mirror keeps the value but
    kinks the slope -- the filter then rounds the kink off and the first
    samples come out wrong by a large fraction of the amplitude.) 0 = off."""
    y = np.asarray(y, dtype=float)
    if (lowpass_Hz <= 0 and highpass_Hz <= 0) or y.size < 4 or not dt > 0:
        return y.copy()
    n = y.size
    ext = np.concatenate([2 * y[0] - y[::-1], y, 2 * y[-1] - y[::-1]])
    f = np.fft.rfftfreq(ext.size, dt)
    gain = np.ones_like(f)
    p = 2 * max(1, int(order))
    if lowpass_Hz > 0:
        gain *= 1.0 / (1.0 + (f / lowpass_Hz) ** p)
    if highpass_Hz > 0:
        with np.errstate(divide="ignore"):
            r = np.where(f > 0, (highpass_Hz / np.where(f > 0, f, 1.0)) ** p, np.inf)
        gain *= 1.0 / (1.0 + r)
    out = np.fft.irfft(np.fft.rfft(ext) * gain, ext.size)
    return out[n:2 * n]


# ---- per-channel numbers ------------------------------------------------------------

def channel_values(t: np.ndarray, y: np.ndarray) -> dict:
    """mean, rms (about zero), peak-to-peak, amplitude (= pp/2) and the
    frequency from the mean-level crossings (NaN with fewer than two)."""
    y = np.asarray(y, dtype=float)
    if y.size == 0 or not np.isfinite(y).any():
        return {"mean": _NAN, "rms": _NAN, "pk2pk": _NAN, "amplitude": _NAN,
                "frequency": _NAN}
    mean = float(np.nanmean(y))
    pp = float(np.nanmax(y) - np.nanmin(y))
    return {"mean": mean, "rms": float(np.sqrt(np.nanmean(y * y))), "pk2pk": pp,
            "amplitude": pp / 2.0, "frequency": frequency(t, y)}


def frequency(t: np.ndarray, y: np.ndarray) -> float:
    """From the crossings of the mean level -- rising AND falling, so two
    periods already give four edges. A crossing only counts once the signal
    has gone 10 % of its peak-to-peak beyond the level (a dead band), so noise
    near the level does not add crossings. NaN with fewer than two edges.
    (numpy throughout: it runs on full records of tens of thousands of points.)"""
    y = np.asarray(y, dtype=float)
    t = np.asarray(t, dtype=float)
    if y.size < 4:
        return _NAN
    mean = float(np.mean(y))
    hyst = 0.1 * float(np.max(y) - np.min(y))
    if hyst <= 0:
        return _NAN
    # which side of the dead band each sample is on (0 = inside it)
    side = np.where(y > mean + hyst, 1, np.where(y < mean - hyst, -1, 0))
    known = np.nonzero(side)[0]
    if known.size < 2:
        return _NAN
    # an edge = the side changes; it happened at the last mean crossing before
    flips = known[1:][np.diff(side[known]) != 0]
    d = y - mean
    cross = np.nonzero((d[:-1] < 0) != (d[1:] < 0))[0] + 1      # between i-1 and i
    if flips.size < 2 or cross.size == 0:
        return _NAN
    j = np.searchsorted(cross, flips, side="right") - 1
    keep = j >= 0
    j = j[keep]
    up = side[flips[keep]] > 0          # the edge went to the high side
    if j.size < 2:
        return _NAN
    i = cross[j]
    frac = (mean - y[i - 1]) / (y[i] - y[i - 1])
    edges = t[i - 1] + frac * (t[i] - t[i - 1])
    # With few periods (lab PC 2026-10-07: two periods of 50 Hz at 1 ms/div
    # read 49.67 Hz) the record's mean is not the signal's: rising and
    # falling crossings move in OPPOSITE directions, and counting both is
    # biased. Rising-to-rising (and falling-to-falling) is not: average the
    # two when each has two edges.
    est = []
    for sel in (up, ~up):
        e = edges[sel]
        if e.size >= 2 and e[-1] > e[0]:
            est.append((e.size - 1) / (e[-1] - e[0]))
    if est:
        return float(np.mean(est))
    return (edges.size - 1) / (2.0 * (edges[-1] - edges[0]))


def peak_frequency(t: np.ndarray, y: np.ndarray) -> float:
    """The strongest non-DC line of the spectrum (Hann window, the peak bin
    refined by a parabola through its neighbours). A fallback for
    `frequency` when the mean-crossing count fails (a signal with a large
    harmonic, or noise that the dead band does not catch). NaN if the
    strongest line is below two periods per record."""
    y = np.asarray(y, dtype=float)
    t = np.asarray(t, dtype=float)
    n = y.size
    if n < 8 or not (t[-1] > t[0]):
        return _NAN
    dt = (t[-1] - t[0]) / (n - 1)
    spec = np.abs(np.fft.rfft((y - np.mean(y)) * np.hanning(n)))
    k = int(np.argmax(spec[1:])) + 1
    if k < 2 or k >= spec.size - 1:
        return _NAN
    a, b, c = spec[k - 1], spec[k], spec[k + 1]
    den = a - 2 * b + c
    shift = 0.5 * (a - c) / den if den != 0 else 0.0
    return (k + shift) / (n * dt)


def phase_detail(t: np.ndarray, ref: np.ndarray, y: np.ndarray) -> tuple[float, str]:
    """Phase of `y` relative to `ref` at the FUNDAMENTAL, in degrees (-180..180;
    positive = y LEADS ref), and why there is none ("" when there is one).

    A lock-in in software: both signals are projected onto exp(-i 2 pi f0 t)
    over a whole number of periods of f0, and the phase is the angle between
    the two projections. Only the fundamental counts, so a sine against a
    square of the same frequency gives the phase of their edges (lab bench
    2026-10-07: AFG sine on CH1, square on CH2 -> 0 deg). f0 comes from CH1's
    mean crossings, else CH2's, else the strongest line of CH1's spectrum."""
    t = np.asarray(t, dtype=float)
    ref = np.asarray(ref, dtype=float)
    y = np.asarray(y, dtype=float)
    if t.size < 8 or ref.size != t.size or y.size != t.size:
        return _NAN, "too few points"
    if not (np.isfinite(ref).all() and np.isfinite(y).all()):
        return _NAN, "the record holds non-numbers"
    for name, v in (("CH1", ref), ("CH2", y)):
        if np.ptp(v) <= 0:
            return _NAN, f"{name} is flat (no signal, or clipped at the screen edge)"
    f0 = frequency(t, ref)
    if not (f0 > 0):
        f0 = frequency(t, y)
    if not (f0 > 0):
        f0 = peak_frequency(t, ref)
    if not (f0 > 0):
        return _NAN, "no frequency found (fewer than two periods in the record?)"
    periods = math.floor((t[-1] - t[0]) * f0 + 1e-9)
    if periods < 1:
        return _NAN, (f"less than one period of {f0:.4g} Hz in the record -- "
                      f"use a slower time/div")
    # a whole number of periods: the bin is then blind to every harmonic and to DC
    sel = t <= t[0] + periods / f0
    w = np.exp(-2j * np.pi * f0 * t[sel])
    a = np.sum((ref[sel] - np.mean(ref[sel])) * w)
    b = np.sum((y[sel] - np.mean(y[sel])) * w)
    if abs(a) == 0:
        return _NAN, f"CH1 has nothing at {f0:.4g} Hz"
    if abs(b) == 0:
        return _NAN, f"CH2 has nothing at {f0:.4g} Hz"
    d = math.degrees(np.angle(b / a))
    return (d + 180.0) % 360.0 - 180.0, ""


def phase_deg(t: np.ndarray, ref: np.ndarray, y: np.ndarray) -> float:
    """`phase_detail` without the reason (NaN when there is no phase)."""
    return phase_detail(t, ref, y)[0]
