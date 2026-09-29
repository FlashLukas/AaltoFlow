"""The arithmetic of the Z STEP CALIBRATION (pure numpy, no camera, no Qt).

Why a module of its own (2026-09-29): the rig showed that the first method --
one parabola per walk, step ratio from the ratio of the two curvatures -- is
wrong for the KIM101 + PIA25 Z, and the replacement has to be testable on
recorded numbers (the rig's up walk) without a camera.

THE RIG FINDING. At 63x on the real kim Z the up walk (counter um -> sigma^2
px^2, 0.25 um levels) was LOPSIDED: sigma^2 fell 136 -> 56 over 4.2 counter um
before focus and rose 56 -> 110 over only 1.8 after it -- the left arm about 4x
less curved than the right. The spot's sigma^2 is symmetric in the TRUE Z (a
coherent beam: sigma^2 = s0 + K (z - z0)^2), so the only way to get a lopsided
curve in COUNTER units is a step size that changes during the walk (by
position, load or run length) -- about 2x between the two arms here. One
parabola per walk then does not fit (R^2 0.955), and its curvature is some
average of two different step sizes: its ratio between the walks means little.

THE WIDTH METHOD (Lukas, 2026-09-29). Take a level L of sigma^2. It is crossed
at two TRUE heights, z_a below focus and z_b above -- the same two heights in
the up walk and in the down walk, because they are a property of the beam.
Counting how many counter units it takes to get from z_a to z_b:

    up walk:    W_up(L)   = integral dz / (s_up   f(z))  over [z_a, z_b]
    down walk:  W_down(L) = integral dz / (s_down f(z))  over [z_a, z_b]

where f(z) is ANY position dependence of the step size that both directions
share. It cancels in the ratio:  W_down(L) / W_up(L) = s_up / s_down  -- the
step ratio, at EVERY level, without assuming a parabola or a constant step.
(A walk with smaller steps needs MORE counter to cross the same true distance.)
If the per-level ratios DISAGREE, the two directions do not share one f(z) --
then there is no single ratio to write, and the calibration refuses.

Which crossing: from the curve's minimum outwards, the FIRST time the smoothed
curve reaches L on each side (linear interpolation between two levels). Far
out the faint wings can read low again (dynamic range); the first crossing is
before that.

Smoothing: Tukey's "3RH" -- a running median of 3 (kills a single outlier, e.g.
one noisy frame; Tukey's end-point rule at the two ends), then a 1-2-1 running
mean (Hanning). No global shape is assumed. Its bias on a smooth valley is a
quarter of the curvature x step^2 -- on the rig ~0.3 px^2 on ~60, negligible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


def parse_levels(text) -> list[float]:
    """"1.3, 1.5, 1.8, 2.0" -> [1.3, 1.5, 1.8, 2.0] (a list stays a list).
    Levels <= 1 are meaningless (the minimum itself has no width) and dropped."""
    if isinstance(text, (list, tuple)):
        vals = [float(v) for v in text]
    else:
        vals = []
        for part in str(text).replace(";", ",").split(","):
            part = part.strip()
            if part:
                vals.append(float(part))
    return sorted(v for v in vals if math.isfinite(v) and v > 1.0)


def smooth_3rh(y) -> np.ndarray:
    """Tukey's 3RH smoother: running median of 3, then a 1-2-1 running mean.

    Ends of the median: Tukey's rule, median(y0, y1, 3 y1 - 2 y2) -- the end
    value is kept unless it disagrees with the straight line through its two
    neighbours (so a jumped end point is pulled back, a real rise is kept).
    Ends of the mean: left as they are (there is no neighbour on one side).
    """
    y = np.asarray(y, float)
    n = len(y)
    if n < 3:
        return y.copy()
    m = y.copy()
    for i in range(1, n - 1):
        m[i] = float(np.median(y[i - 1:i + 2]))
    m[0] = float(np.median([y[0], m[1], 3.0 * m[1] - 2.0 * m[2]]))
    m[-1] = float(np.median([y[-1], m[-2], 3.0 * m[-2] - 2.0 * m[-3]]))
    h = m.copy()
    h[1:-1] = 0.25 * m[:-2] + 0.5 * m[1:-1] + 0.25 * m[2:]
    return h


@dataclass
class WalkShape:
    """One walk, ready for the width analysis: counter ascending, the measured
    and the smoothed sigma^2, and where the smoothed minimum is."""
    n: np.ndarray            # counter (ascending), levels kept
    raw: np.ndarray          # sigma^2 as measured
    smooth: np.ndarray       # sigma^2 after 3RH
    i_min: int               # index of the smoothed minimum
    m_min: float             # smoothed minimum sigma^2
    dropped: int = 0         # levels left out (skip_first + not measurable)


def walk_shape(ns, ms, skip_first: int = 0) -> WalkShape | None:
    """``ns`` / ``ms`` in WALK ORDER (as measured). The first ``skip_first``
    levels are left out: they follow a direction change, and on the rig the
    first level of the up walk jumped (172 px^2 where the next read 136 --
    slip-stick's first steps after a reversal are not like the others).
    None when fewer than 5 measurable levels are left."""
    ns = np.asarray(ns, float)
    ms = np.asarray(ms, float)
    k = max(0, int(skip_first))
    keep = np.zeros(len(ns), bool)
    keep[k:] = True
    keep &= np.isfinite(ms) & np.isfinite(ns)
    if keep.sum() < 5:
        return None
    order = np.argsort(ns[keep], kind="stable")
    n = ns[keep][order]
    raw = ms[keep][order]
    sm = smooth_3rh(raw)
    i_min = int(np.argmin(sm))
    return WalkShape(n=n, raw=raw, smooth=sm, i_min=i_min, m_min=float(sm[i_min]),
                     dropped=int(len(ns) - keep.sum()))


def crossings(shape: WalkShape, level: float):
    """(left, right) counter positions where the smoothed curve first reaches
    ``level`` going outwards from its minimum; None on a side it never does."""
    n, s, i0 = shape.n, shape.smooth, shape.i_min

    def interp(j_out, j_in):
        # the curve is >= level at j_out and < level at j_in
        a, b = s[j_out], s[j_in]
        if b == a:
            return float(n[j_out])
        t = (level - b) / (a - b)
        return float(n[j_in] + t * (n[j_out] - n[j_in]))

    left = right = None
    for j in range(i0 - 1, -1, -1):
        if s[j] >= level:
            left = interp(j, j + 1)
            break
    for j in range(i0 + 1, len(s)):
        if s[j] >= level:
            right = interp(j, j - 1)
            break
    return left, right


@dataclass
class WidthResult:
    """The width analysis of two walks. ``ratio`` = s_up / s_down (the median
    over the usable levels), or NaN when refused (``why`` says why)."""
    ratio: float = float("nan")
    mean: float = float("nan")
    spread: float = float("nan")     # (max - min) / median of the per-level ratios
    err: float = float("nan")        # standard error of the mean of the ratios
    m_ref: float = float("nan")      # the sigma^2 the levels are multiples of
    levels: list = field(default_factory=list)    # one dict per usable level
    skipped: list = field(default_factory=list)   # (k, reason) of unusable ones
    why: str = ""                    # "" = accepted

    @property
    def ok(self) -> bool:
        return not self.why and math.isfinite(self.ratio)

    def summary(self) -> str:
        """"1.3x 1.428, 1.5x 1.433" -- ASCII, for events and the status."""
        return ", ".join(f"{d['k']:g}x {d['ratio']:.3f}" for d in self.levels)

    def detail(self) -> str:
        """Why the unusable levels were left out: "1.8x not reached: ..."."""
        return "; ".join(f"{k:g}x {why}" for k, why in self.skipped)


def width_ratio(up: WalkShape, down: WalkShape, ks, max_spread: float = 0.10,
                min_levels: int = 2) -> WidthResult:
    """Step ratio s_up / s_down from the widths of the two walks at the SAME
    sigma^2 levels L = k x m_ref.

    m_ref = the LARGER of the two smoothed minima: the levels are the same
    ABSOLUTE sigma^2 in both walks (the crossing heights in true Z are a
    property of the beam, so the same L must be used), and taking the larger
    minimum keeps the lowest level above both bottoms. A level counts only if
    BOTH walks reach it on BOTH sides.
    """
    res = WidthResult()
    ks = parse_levels(ks)
    m_ref = max(up.m_min, down.m_min)
    res.m_ref = float(m_ref)
    if not (math.isfinite(m_ref) and m_ref > 0):
        res.why = "no sigma^2 minimum"
        return res
    for k in ks:
        L = k * m_ref
        lu, ru = crossings(up, L)
        ld, rd = crossings(down, L)
        missing = [name for name, v in (("up walk before focus", lu),
                                        ("up walk after focus", ru),
                                        ("down walk before focus", rd),
                                        ("down walk after focus", ld)) if v is None]
        if missing:
            res.skipped.append((k, "not reached: " + ", ".join(missing)))
            continue
        w_up, w_dn = ru - lu, rd - ld
        if w_up <= 0 or w_dn <= 0:
            res.skipped.append((k, "no width"))
            continue
        res.levels.append({"k": float(k), "level": float(L), "up": (lu, ru),
                           "down": (ld, rd), "w_up": float(w_up), "w_down": float(w_dn),
                           "ratio": float(w_dn / w_up)})
    if len(res.levels) < max(1, int(min_levels)):
        # short (it becomes zcal_state); detail() says which level missed what
        res.why = f"too few levels ({len(res.levels)} usable, need {int(min_levels)})"
        return res
    r = np.array([d["ratio"] for d in res.levels])
    res.ratio = float(np.median(r))
    res.mean = float(r.mean())
    res.spread = float((r.max() - r.min()) / res.ratio)
    res.err = float(r.std(ddof=1) / math.sqrt(len(r))) if len(r) > 1 else float("nan")
    if res.spread > float(max_spread):
        res.why = (f"levels disagree (spread {100 * res.spread:.0f} % > "
                   f"{100 * float(max_spread):.0f} %)")
    return res


def fit_parabola(ns, ms, window: float, n_side: int, skip_first: int = 0) -> dict:
    """The ORIGINAL method, kept as a DIAGNOSTIC: a parabola in counter units
    over the contiguous levels within ``window`` x the smallest sigma^2.

    Returns a dict (a, var_a, r2, vertex, min, n) or {"why": reason} when
    there is no fit to make (too few levels, minimum not bracketed, opening
    downwards). Its R^2 says how LOPSIDED a walk is (a constant step gives a
    clean parabola; the rig's up walk 0.955), its curvature ratio is what the
    old method would have reported.
    """
    ns = np.asarray(ns, float)[max(0, int(skip_first)):]
    ms = np.asarray(ms, float)[max(0, int(skip_first)):]
    ok = np.isfinite(ms)
    if ok.sum() < 2 * n_side + 1:
        return {"why": "too few measurable levels"}
    i_min = int(np.nanargmin(np.where(ok, ms, np.nan)))
    # the CONTIGUOUS run of levels around the minimum within the window: far
    # out the 8-bit wings read low and can dip back INTO the window
    inside = ok & (ms <= window * ms[i_min])
    sel = np.zeros_like(ok)
    for rng in (range(i_min, -1, -1), range(i_min, len(ms))):
        for i in rng:
            if not inside[i]:
                break
            sel[i] = True
    x = ns - ns[i_min]                      # centred: well-conditioned
    below, above = int((sel & (x < 0)).sum()), int((sel & (x > 0)).sum())
    if min(below, above) < n_side:
        return {"why": "not bracketed", "below": below, "above": above}
    p, cov = np.polyfit(x[sel], ms[sel], 2, cov=True)
    res = ms[sel] - np.polyval(p, x[sel])
    ss = float(((ms[sel] - ms[sel].mean()) ** 2).sum())
    r2 = 1.0 - float((res ** 2).sum()) / ss if ss > 0 else 0.0
    if p[0] <= 0:
        return {"why": "opens downwards", "r2": r2}
    return {"a": float(p[0]), "var_a": float(cov[0, 0]), "r2": r2,
            "vertex": float(ns[i_min] - p[1] / (2.0 * p[0])),
            "min": float(np.polyval(p, -p[1] / (2.0 * p[0]))), "n": int(sel.sum())}


def bottom_position(shape: WalkShape, k: float = 1.3) -> float:
    """Where the walk's minimum is on the counter: a LOCAL parabola over the
    kept levels whose smoothed sigma^2 is within k x the minimum (near the
    bottom the step size hardly changes, so a parabola is fine there even on a
    lopsided walk); the smoothed argmin when that fit is not usable."""
    sel = shape.smooth <= k * shape.m_min
    x, y = shape.n[sel], shape.raw[sel]
    if len(x) >= 4:
        x0 = float(shape.n[shape.i_min])
        p = np.polyfit(x - x0, y, 2)
        if p[0] > 0:
            v = x0 - p[1] / (2.0 * p[0])
            if x.min() <= v <= x.max():
                return float(v)
    return float(shape.n[shape.i_min])
