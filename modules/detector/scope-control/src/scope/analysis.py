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

def reduce_points(t: np.ndarray, ys: dict, points: int) -> tuple[np.ndarray, dict]:
    """Average neighbouring samples so every trace has `points` samples.
    Fewer samples than `points` are interpolated onto `points` (no new
    information, but every trace of a scan then has the same length)."""
    t = np.asarray(t, dtype=float)
    n = t.size
    points = max(2, int(points))
    if n == points:
        return t.copy(), {k: np.asarray(v, dtype=float).copy() for k, v in ys.items()}
    if n > points:
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
    near the level does not add crossings. NaN with fewer than two edges."""
    y = np.asarray(y, dtype=float)
    t = np.asarray(t, dtype=float)
    if y.size < 4:
        return _NAN
    mean = float(np.mean(y))
    hyst = 0.1 * float(np.max(y) - np.min(y))
    if hyst <= 0:
        return _NAN
    edges = []
    state = 0                 # -1 below the band, +1 above, 0 not known yet
    last_cross = None         # time of the latest mean crossing
    for i in range(1, y.size):
        if (y[i - 1] - mean) * (y[i] - mean) < 0 or (y[i] == mean != y[i - 1]):
            frac = (mean - y[i - 1]) / (y[i] - y[i - 1])
            last_cross = t[i - 1] + frac * (t[i] - t[i - 1])
        new = 1 if y[i] > mean + hyst else (-1 if y[i] < mean - hyst else state)
        if new != state:
            if state != 0 and last_cross is not None:
                edges.append(last_cross)
            state = new
    if len(edges) < 2:
        return _NAN
    return (len(edges) - 1) / (2.0 * (edges[-1] - edges[0]))


def phase_deg(t: np.ndarray, ref: np.ndarray, y: np.ndarray) -> float:
    """Phase of `y` relative to `ref` at ref's fundamental, in degrees
    (-180..180; positive = y LEADS ref). From one DFT bin at the frequency
    found in ref; NaN if ref has no clear frequency."""
    f0 = frequency(t, ref)
    if not (f0 > 0) or len(t) < 4:
        return _NAN
    t = np.asarray(t, dtype=float)
    # use a whole number of periods so the DFT bin is clean
    periods = math.floor((t[-1] - t[0]) * f0)
    if periods < 1:
        return _NAN
    sel = t <= t[0] + periods / f0
    w = np.exp(-2j * np.pi * f0 * t[sel])
    a = np.sum((np.asarray(ref, float)[sel] - np.mean(np.asarray(ref, float)[sel])) * w)
    b = np.sum((np.asarray(y, float)[sel] - np.mean(np.asarray(y, float)[sel])) * w)
    if abs(a) == 0 or abs(b) == 0:
        return _NAN
    d = math.degrees(np.angle(b / a))
    return (d + 180.0) % 360.0 - 180.0
