"""What the module computes from traces: no hardware, no threads, numpy only.

Three parts, each a pure function so it can be tested on its own:

  * `reduce_points` -- the scope's record (thousands of points) down to the
    configured trace length, by averaging neighbours (a boxcar).
  * `zero_phase` -- the low/high-pass filter. Applied identically to every
    channel and with ZERO phase, because any lag between the field channel and
    the intensity channel would tilt or open the loop and fake a coercive field.
  * `channel_values` and `loop_numbers` -- the numbers a scan records.

THE LOOP NUMBERS, in words (X = field or current, Y = signal):
  1. Split the record into the UP branch (X rising) and the DOWN branch.
  2. Background: in the high-field ends (|X| > sat_fraction * max|X|) the
     sample is saturated, so whatever still changes there is linear in X
     (Faraday effect, substrate). Fit Y = slope * X + c through BOTH ends with
     one common slope and one offset per end -- the two ends sit at +Ms and
     -Ms, so they need their own offsets, but share the slope. Subtract it
     (if asked); the slope is reported either way.
  3. Saturation levels: the mean Y of each (background-free) end. Ms (the
     Kerr amplitude) = half their difference; mid = their mean.
  4. Coercive fields: where each branch crosses `mid` (linear interpolation).
     Hc+ on the up branch, Hc- on the down branch; Hc = (Hc+ - Hc-)/2, the
     exchange-bias shift = (Hc+ + Hc-)/2.
  5. Remanence: Y - mid at X = 0, half the difference of the two branches
     (positive for a normal loop); squareness = Mr / Ms.
  6. Area: |closed integral Y dX| over the record (the energy loss per cycle,
     in units of X * Y), NaN if the record is not a closed cycle.
Anything that cannot be determined (no crossing, no saturation) is NaN, never
a made-up number.
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
    has gone 10 % of its peak-to-peak beyond the level (a hysteresis), so noise
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


# ---- the loop -------------------------------------------------------------------------

LOOP_KEYS = ("hc_plus", "hc_minus", "hc", "bias", "ms", "mr", "squareness",
             "slope", "area")


def loop_numbers(x: np.ndarray, y: np.ndarray, sat_fraction: float = 0.8,
                 subtract_background: bool = True) -> dict:
    """The loop numbers of Y(X) (see the module docstring). Returns a dict with
    LOOP_KEYS, plus "y_corrected" (Y after background subtraction, or Y) and
    "mid" (the level halfway between the saturation levels)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    out = {k: _NAN for k in LOOP_KEYS}
    out["y_corrected"] = y.copy()
    out["mid"] = _NAN
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 8:
        return out
    xmax = float(np.max(np.abs(x[ok])))
    if xmax <= 0:
        return out
    hi = ok & (x > sat_fraction * xmax)
    lo = ok & (x < -sat_fraction * xmax)
    if hi.sum() >= 2 and lo.sum() >= 2:
        # common slope, own offset per end: least squares on [x, 1_hi, 1_lo]
        sel = hi | lo
        A = np.column_stack([x[sel], hi[sel].astype(float), lo[sel].astype(float)])
        (slope, c_hi, c_lo), *_ = np.linalg.lstsq(A, y[sel], rcond=None)
        out["slope"] = float(slope)
        yc = y - slope * x if subtract_background else y
        top = float(np.mean(yc[hi]))
        bottom = float(np.mean(yc[lo]))
    else:
        yc = y
        top = bottom = _NAN
    out["y_corrected"] = yc
    if math.isfinite(top) and math.isfinite(bottom):
        out["ms"] = (top - bottom) / 2.0
        mid = (top + bottom) / 2.0
    else:
        mid = float(np.nanmean(yc[ok]))

    dx = np.gradient(x)
    up = ok & (dx > 0)
    down = ok & (dx < 0)

    def crossing(branch, level, direction):
        """X where Y crosses `level` on one branch, averaged over every
        crossing in the expected direction (one per cycle in the record)."""
        idx = np.flatnonzero(branch)
        found = []
        for i0, i1 in zip(idx[:-1], idx[1:]):
            if i1 != i0 + 1:
                continue                 # not neighbours: the branch was interrupted
            y0, y1 = yc[i0] - level, yc[i1] - level
            if direction * y0 < 0 <= direction * y1 or (y0 == 0 and direction * y1 > 0):
                frac = y0 / (y0 - y1) if y0 != y1 else 0.0
                found.append(x[i0] + frac * (x[i1] - x[i0]))
        return float(np.mean(found)) if found else _NAN

    sign = 1.0 if not math.isfinite(out["ms"]) or out["ms"] >= 0 else -1.0
    hp = crossing(up, mid, +sign)       # rising X: Y goes from low to high
    hm = crossing(down, mid, -sign)     # falling X: Y goes from high to low
    out["hc_plus"], out["hc_minus"] = hp, hm
    if math.isfinite(hp) and math.isfinite(hm):
        out["hc"] = (hp - hm) / 2.0
        out["bias"] = (hp + hm) / 2.0
    # remanence: Y at X = 0 on each branch (crossing of X through zero)
    def y_at_x0(branch, direction):
        idx = np.flatnonzero(branch)
        vals = []
        for i0, i1 in zip(idx[:-1], idx[1:]):
            if i1 != i0 + 1:
                continue
            if direction * x[i0] < 0 <= direction * x[i1]:
                frac = x[i0] / (x[i0] - x[i1]) if x[i0] != x[i1] else 0.0
                vals.append(yc[i0] + frac * (yc[i1] - yc[i0]))
        return float(np.mean(vals)) if vals else _NAN
    y_up0 = y_at_x0(up, +1)              # on the up branch, coming from negative field
    y_dn0 = y_at_x0(down, -1)
    if math.isfinite(y_up0) and math.isfinite(y_dn0):
        out["mr"] = sign * (y_dn0 - y_up0) / 2.0
        if math.isfinite(out["ms"]) and out["ms"] != 0:
            out["squareness"] = out["mr"] / abs(out["ms"])
    # area PER CYCLE: only for a record that closes (first and last X within
    # 5 % of the span). The record may hold several cycles, counted by the
    # distance X travels: one cycle goes up and down once, 2 x peak-to-peak.
    # (Counting zero crossings missed the first cycle of a record that starts
    # exactly at X = 0 -- the test caught a doubled area.)
    xs, ys = x[ok], yc[ok]
    span = float(np.max(xs) - np.min(xs))
    cycles = float(np.sum(np.abs(np.diff(xs)))) / (2.0 * span) if span > 0 else 0.0
    if cycles >= 0.9 and abs(xs[0] - xs[-1]) < 0.05 * (2 * xmax):
        out["area"] = float(abs(0.5 * np.sum((ys[1:] + ys[:-1]) * np.diff(xs)))) / cycles
    out["mid"] = mid
    return out


def normalised(y: np.ndarray, ms: float, mid: float | None = None) -> np.ndarray:
    """Y scaled to -1..1 by the saturation levels (NaN when Ms is unknown)."""
    y = np.asarray(y, dtype=float)
    if not (math.isfinite(ms) and ms != 0):
        return np.full_like(y, np.nan)
    if mid is None:
        mid = 0.0
    return (y - mid) / ms
