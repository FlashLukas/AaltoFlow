"""window.py -- the RESONANCE WINDOW: sweep only where the line is.

Lukas, 2026-09-28: "some devices are terribly slow and if I want to see e.g.
FMR in field I scan most of the time in the dark. Can we cheat? The SA with TG
knows the field, the field angle, and based on assumed material parameters ...
only scans within a margin around the assumed resonance. The rest of the scan
is filled with the baseline."

His three decisions, and where they live here:

  1. Outside the window the data is FILLED WITH THE BASELINE, and a MEASURED
     MASK goes into the file (`<det>_measured`), so nobody later mistakes the
     filled part for a measurement.
  2. The window follows a MODEL and SELF-CORRECTS from the measured dip: every
     clean dip gives a new Meff estimate (resonance.meff_from_f), smoothed.
  3. The model SWITCHES between in-plane and out-of-plane (resonance.py).

How one point goes (the engine calls plan -> trigger/read -> assess):

  * plan(): read B (and the angle) for this point, predict f_res with the
    CURRENT Meff estimate. A FULL sweep instead of a window when: this is the
    first point, every `full_every`-th point (to refresh the baseline), the
    model is not valid here (below saturation, hysteretic region), or the last
    point's line was not found. Otherwise the window is the bins within
    f_res +- margin (at least `min_bins`, clamped to the band).
  * The detector sweeps only those bins. The window is given in BIN INDICES of
    the detector's own full grid, never in Hz: then the measured bins land on
    the dataset's regular frequency axis exactly, with no interpolation ever.
  * assess(): find the dip in the measured bins. It must sit at least 10 % of
    the window width inside each edge (a "dip" at the edge is usually the
    flank of a line that is really outside) and stand out from the local
    baseline by more than 5 x its noise (median absolute deviation). If not:
    WIDEN the window x2 and measure this point again, up to the full band.
  * commit(): the point is kept. Update Meff (track), the baseline (after a
    full sweep), the counters.

The baseline is the last FULL sweep with its own resonance region cut out and
replaced by a straight line between the two sides -- otherwise the filled
trace would carry a ghost of the old line at the old field.

Nothing here talks to hardware: the engine does the triggering and reading, so
a pause-on-fault or an abort in the middle of a point simply throws away the
plan and the point is planned again from the committed state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from . import resonance

#: frequency units an array detector's axis may carry, in Hz
FREQ_UNITS = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}
#: field units the `field` parameter may carry, in mT ("" = mT)
FIELD_UNITS = {"": 1.0, "mt": 1.0, "t": 1e3, "g": 0.1, "oe": 0.1, "ka/m": 4 * math.pi / 10}
#: angle units ("" = degrees)
ANGLE_UNITS = {"": 1.0, "deg": 1.0, "°": 1.0, "rad": 180.0 / math.pi}

#: the dip must stand this many noise-sigmas (MAD) out of the local baseline
PROMINENCE_MAD = 5.0
#: ... and sit at least this fraction of the window width inside each edge
EDGE_FRACTION = 0.10
#: Meff smoothing: new = old + SMOOTHING * (measured - old). The FIRST clean
#: estimate replaces the assumed value outright -- the assumption is a guess,
#: a measurement is not.
SMOOTHING = 0.5
#: a window estimate further than this from the running median (after
#: OUTLIER_AFTER estimates) is ignored as an outlier
OUTLIER_MAD = 6.0
OUTLIER_FLOOR_MT = 5.0
OUTLIER_AFTER = 3

DEFAULTS = {"model": "inplane", "margin_MHz": 300.0, "dip": "min",
            "track": True, "full_every": 20, "angle": None}


def normalize(block: dict) -> dict:
    """The window block with every default filled in (what the engine uses)."""
    out = dict(DEFAULTS)
    out.update(block or {})
    out["params"] = resonance._params(out.get("params"))
    return out


def unit_factor(unit: str, table: dict) -> float | None:
    return table.get((unit or "").strip().lower())


def validate(recipe, registry) -> list[str]:
    """Problems with `recipe.window` ([] = fine, or no window at all)."""
    w = getattr(recipe, "window", None)
    if not w:
        return []
    if not isinstance(w, dict):
        return [f"window must be a mapping, not {w!r}"]
    errs = []
    known = {"detector", "field", "angle", "model", "params", "margin_MHz",
             "dip", "track", "full_every"}
    for k in w:
        if k not in known:
            errs.append(f"window: unknown key {k!r} (known: {', '.join(sorted(known))})")
    det = w.get("detector")
    g = registry.get(det) if det else None
    if not det:
        errs.append("window: `detector` is required")
    elif g is None:
        errs.append(f"window: unknown detector '{det}'")
    else:
        if det not in (recipe.detectors or []):
            errs.append(f"window: detector '{det}' is not one of the scan's detectors")
        axes = list(getattr(g, "axes", None) or [])
        if len(axes) != 1:
            errs.append(f"window: '{det}' is not a 1-D array detector (a trace over "
                        f"frequency); a window needs one")
        elif unit_factor(axes[0].unit, FREQ_UNITS) is None:
            errs.append(f"window: the axis of '{det}' is in {axes[0].unit!r}, not a "
                        f"frequency unit (Hz, kHz, MHz, GHz)")
        if not getattr(g, "window", None):
            errs.append(f"window: '{det}' does not declare window support in its "
                        f"describe (`window` key); it would sweep the full band anyway")
        if getattr(g, "acquire", None) is None:
            errs.append(f"window: '{det}' has no acquire step to pass the window to")
    fid = w.get("field")
    if not fid or not isinstance(fid, str):
        errs.append("window: `field` (the parameter holding B) is required")
    else:
        p = registry.get(fid)
        if p is None:
            errs.append(f"window: unknown field parameter '{fid}'")
        elif not hasattr(p, "get"):
            errs.append(f"window: field parameter '{fid}' cannot be read")
        elif unit_factor(getattr(p, "unit", ""), FIELD_UNITS) is None:
            errs.append(f"window: field parameter '{fid}' is in {p.unit!r}; "
                        f"need mT, T, G or Oe")
    ang = w.get("angle")
    if ang is not None and not isinstance(ang, (int, float)):
        if not isinstance(ang, str):
            errs.append(f"window: angle must be a parameter id or a number, not {ang!r}")
        else:
            p = registry.get(ang)
            if p is None:
                errs.append(f"window: unknown angle parameter '{ang}'")
            elif unit_factor(getattr(p, "unit", ""), ANGLE_UNITS) is None:
                errs.append(f"window: angle parameter '{ang}' is in {p.unit!r}; need deg")
    elif isinstance(ang, (int, float)) and not math.isfinite(float(ang)):
        errs.append("window: angle is not finite")
    if w.get("model", DEFAULTS["model"]) not in resonance.MODELS:
        errs.append(f"window: model must be one of {', '.join(resonance.MODELS)}")
    errs += resonance.validate_params(w.get("params"))
    try:
        m = float(w.get("margin_MHz", DEFAULTS["margin_MHz"]))
        if not (math.isfinite(m) and m > 0):
            errs.append("window: margin_MHz must be a positive number")
    except (TypeError, ValueError):
        errs.append("window: margin_MHz must be a positive number")
    if w.get("dip", "min") not in ("min", "max"):
        errs.append("window: dip must be min or max")
    if not isinstance(w.get("track", True), bool):
        errs.append("window: track must be true or false")
    fe = w.get("full_every", DEFAULTS["full_every"])
    if isinstance(fe, bool) or not isinstance(fe, int) or fe < 0:
        errs.append("window: full_every must be a whole number >= 0 (0 = only the first point)")
    for ax in recipe.axes or []:
        if isinstance(ax, dict) and ax.get("type") == "fly":
            errs.append("window: a resonance window works on stepped scans only, "
                        "not with a fly axis")
            break
    return errs


# ───────────────────────────── dip finding ───────────────────────────────────

def _real(y) -> np.ndarray:
    """What the dip is looked for in: the magnitude of a complex trace."""
    y = np.asarray(y)
    return np.abs(y) if np.iscomplexobj(y) else y.astype(float)


def find_dip(f_hz: np.ndarray, y, dip: str = "min") -> tuple[float, dict]:
    """(fitted f in Hz or NaN, diagnostics) of the line in (f_hz, y).

    Robust on purpose:
      * the local baseline is a straight line through the medians of the
        outer 10 % on each side (a spectrum-analyser trace is rarely flat);
      * the "noise" is the larger of two robust spreads: the bin-to-bin
        scatter (median absolute DIFFERENCE / sqrt 2 -- white noise), and the
        MAD of the residual away from the candidate line (every bin not
        deeper than a quarter of it -- slow structure: the TG's ripple, a
        standing wave). A line must beat both; without the second, a ripple
        trough in a band with no line is "found" and teaches the model a
        nonsense Meff;
      * the extremum must stand out by > PROMINENCE_MAD x noise and sit at
        least EDGE_FRACTION of the width inside both edges;
      * its position is refined with a parabola through the extremum and its
        two neighbours (sub-bin accuracy).
    """
    f = np.asarray(f_hz, dtype=float)
    v = _real(y)
    ok = np.isfinite(v) & np.isfinite(f)
    f, v = f[ok], v[ok]
    n = len(v)
    info = {"reason": ""}
    if n < 7:
        info["reason"] = "too few bins"
        return math.nan, info
    s = v if dip == "min" else -v             # always look for a MINIMUM of s
    m = max(2, int(round(0.10 * n)))
    xl, xr = np.median(np.arange(m)), np.median(np.arange(n - m, n))
    yl, yr = np.median(s[:m]), np.median(s[n - m:])
    base = yl + (yr - yl) * (np.arange(n) - xl) / (xr - xl)
    r = s - base
    k = int(np.argmin(r))
    depth = float(-r[k])
    white = 1.4826 * float(np.median(np.abs(np.diff(r)))) / math.sqrt(2)
    rest = r[r > -0.25 * depth] if depth > 0 else r
    slow = 1.4826 * float(np.median(np.abs(rest - np.median(rest)))) if len(rest) else 0.0
    sigma = max(white, slow)
    info.update(depth=depth, noise=sigma, index=k)
    if not (depth > PROMINENCE_MAD * sigma and depth > 1e-12 * (1 + float(np.max(np.abs(s))))):
        info["reason"] = f"no dip above {PROMINENCE_MAD:g} x noise"
        return math.nan, info
    edge = EDGE_FRACTION * (n - 1)
    if k < edge or (n - 1 - k) < edge:
        info["reason"] = "dip at the window edge"
        return math.nan, info
    # parabola through (k-1, k, k+1): vertex offset in bins
    a, b, c = r[k - 1], r[k], r[k + 1]
    den = a - 2 * b + c
    off = 0.5 * (a - c) / den if den > 0 else 0.0
    off = max(-0.5, min(0.5, off))
    step = (f[k + 1] - f[k - 1]) / 2
    return float(f[k] + off * step), info


def bridge(y: np.ndarray, i0: int, i1: int, side: int = 3) -> np.ndarray:
    """A copy of `y` with bins i0..i1 replaced by a straight line.

    The line runs between the medians of `side` bins just outside each end,
    so one noisy bin does not tilt it. Real and imaginary parts of a complex
    trace are bridged separately (linear in both = linear in the complex
    plane). An end at the band edge takes the other side's level.
    """
    y = np.array(y, copy=True)
    n = len(y)
    i0, i1 = max(0, int(i0)), min(n - 1, int(i1))
    if i1 < i0:
        return y
    left = y[max(0, i0 - side):i0]
    right = y[i1 + 1:i1 + 1 + side]

    def level(part):
        part = part[np.isfinite(part)]
        if not len(part):
            return None
        if np.iscomplexobj(part):
            return np.median(part.real) + 1j * np.median(part.imag)
        return np.median(part)

    lv, rv = level(left), level(right)
    if lv is None and rv is None:
        return y
    lv = rv if lv is None else lv
    rv = lv if rv is None else rv
    # each level sits at the CENTRE of the bins it was taken from
    xl = i0 - (len(left) + 1) / 2 if len(left) else i0 - 1
    xr = i1 + (len(right) + 1) / 2 if len(right) else i1 + 1
    t = (np.arange(i0, i1 + 1) - xl) / (xr - xl)
    y[i0:i1 + 1] = lv + (rv - lv) * t
    return y


# ───────────────────────────── the per-scan state ────────────────────────────

@dataclass
class Plan:
    """What one attempt at one point measures."""
    full: bool
    i0: int
    i1: int
    f_pred: float               # Hz, NaN if the model is not valid here
    B_mT: float
    angle_deg: float
    margin_hz: float
    attempt: int = 0
    reason: str = ""
    out_of_band: bool = False   # the model puts the line outside the band

    def args(self, arg_name: str) -> dict:
        """The extra trigger argument ({} for a full sweep)."""
        if self.full:
            return {}
        # plain ints: numpy ints are not JSON
        return {arg_name: [int(self.i0), int(self.i1)]}


@dataclass
class Outcome:
    """What a point produced, committed only when the point is kept."""
    plan: Plan
    f_fit: float
    meff_used: float
    raw: dict                   # det -> measured trace (full length, NaN outside)
    filled: dict = field(default_factory=dict)
    mask: np.ndarray | None = None
    retry: bool = False         # widen and measure again


class WindowRunner:
    """The window's memory across the points of one scan."""

    def __init__(self, block: dict, registry, grid, axis_unit: str,
                 group_dets: list[str], min_bins: int = 3, arg: str = "window"):
        w = normalize(block)
        self.block = w
        self.det = w["detector"]
        self.field_id = w["field"]
        self.angle = w.get("angle")
        self.model = w["model"]
        self.params = dict(w["params"])
        self.margin_hz = float(w["margin_MHz"]) * 1e6
        self.dip = w["dip"]
        self.track = bool(w["track"])
        self.full_every = int(w["full_every"])
        self.registry = registry
        self.f = np.asarray(grid, dtype=float) * unit_factor(axis_unit, FREQ_UNITS)
        self.n = len(self.f)
        self.group_dets = list(group_dets)      # array detectors filled the same way
        self.min_bins = max(1, min(int(min_bins or 1), self.n))
        self.arg = arg or "window"
        # committed state
        self.meff = float(self.params["meff_mT"])
        self.estimates: list[float] = []
        self.points = 0
        self.last_failed = False
        self.baseline: dict = {}                # det -> full-length baseline
        self.last = None                        # the last committed Outcome (GUI)
        self.bins_measured = 0
        self.bins_total = 0

    # ---- inputs ----------------------------------------------------------
    def _read(self, pid, current: dict, table: dict) -> float:
        if pid in current:                      # an axis or a condition: the setpoint
            v = current[pid]
        else:                                   # anything else: read it NOW
            v = self.registry.get(pid).get()
        try:
            v = float(v)
        except (TypeError, ValueError):
            return math.nan
        return v * (unit_factor(getattr(self.registry.get(pid), "unit", ""), table) or 1.0)

    def field_and_angle(self, current: dict) -> tuple[float, float]:
        B = self._read(self.field_id, current, FIELD_UNITS)
        if self.angle is None:
            ang = 0.0
        elif isinstance(self.angle, (int, float)):
            ang = float(self.angle)
        else:
            ang = self._read(self.angle, current, ANGLE_UNITS)
        return B, ang

    def predict(self, B, ang, meff=None) -> float:
        p = dict(self.params, meff_mT=self.meff if meff is None else meff)
        try:
            return resonance.kittel_hz(self.model, B, ang, p)
        except Exception:
            return math.nan

    def in_band(self, f_hz) -> bool:
        return (math.isfinite(f_hz) and float(np.nanmin(self.f)) <= f_hz
                <= float(np.nanmax(self.f)))

    # ---- planning ----------------------------------------------------------
    def _bins(self, lo_hz, hi_hz) -> tuple[int, int]:
        idx = np.nonzero((self.f >= lo_hz) & (self.f <= hi_hz))[0]
        if len(idx):
            i0, i1 = int(idx.min()), int(idx.max())
        else:                                   # narrower than one bin
            c = int(np.argmin(np.abs(self.f - 0.5 * (lo_hz + hi_hz))))
            i0 = i1 = c
        while i1 - i0 + 1 < self.min_bins:      # grow symmetrically, clamped
            if i0 > 0:
                i0 -= 1
            if i1 - i0 + 1 < self.min_bins and i1 < self.n - 1:
                i1 += 1
            if i0 == 0 and i1 == self.n - 1:
                break
        return i0, i1

    def plan(self, current: dict, attempt: int = 0, prev: Plan | None = None) -> Plan:
        B, ang = self.field_and_angle(current)
        f_pred = self.predict(B, ang)
        margin = self.margin_hz * (2 ** attempt)
        full_reason = ""
        if not self.baseline:
            full_reason = "first point"
        elif self.full_every and self.points % self.full_every == 0:
            full_reason = f"every {self.full_every} points"
        elif not math.isfinite(f_pred):
            full_reason = "model not valid here"
        elif self.last_failed:
            full_reason = "line lost at the last point"
        if full_reason:
            return Plan(True, 0, self.n - 1, f_pred, B, ang, margin, attempt, full_reason)
        fmin, fmax = float(np.nanmin(self.f)), float(np.nanmax(self.f))
        if f_pred + self.margin_hz < fmin or f_pred - self.margin_hz > fmax:
            # The model puts the line OUTSIDE the band: there is nothing to
            # look for. A minimal window at the nearest edge keeps the trace
            # measured where it joins the band; the periodic full sweeps would
            # show a line the model misplaced.
            edge = fmin if f_pred < fmin else fmax
            i0, i1 = self._bins(edge, edge)
            return Plan(False, i0, i1, f_pred, B, ang, margin, attempt,
                        "line outside the band", out_of_band=True)
        i0, i1 = self._bins(f_pred - margin, f_pred + margin)
        if i0 == 0 and i1 == self.n - 1:
            return Plan(True, 0, self.n - 1, f_pred, B, ang, margin, attempt,
                        "window covers the band")
        return Plan(False, i0, i1, f_pred, B, ang, margin, attempt)

    # ---- after the read ----------------------------------------------------
    def assess(self, plan: Plan, values: dict) -> Outcome:
        """Look for the line in what was measured; decide widen / fill."""
        raw = {d: np.asarray(values[d]) for d in self.group_dets if d in values}
        y = raw[self.det]
        sl = slice(plan.i0, plan.i1 + 1)
        f_fit = math.nan
        if not plan.out_of_band:
            f_fit, _info = find_dip(self.f[sl], y[sl], self.dip)
        out = Outcome(plan, f_fit, self.meff, raw)
        if not math.isfinite(f_fit) and not plan.full and not plan.out_of_band:
            out.retry = True                    # WIDEN and measure again
            return out
        mask = np.zeros(self.n, dtype=bool)
        mask[sl] = True
        out.mask = mask
        for d, trace in raw.items():
            if plan.full:
                out.filled[d] = trace
            else:
                base = self.baseline.get(d)
                filled = np.array(base, copy=True) if base is not None else \
                    np.full(trace.shape, np.nan, dtype=trace.dtype)
                filled[sl] = trace[sl]
                out.filled[d] = filled
        return out

    def commit(self, out: Outcome) -> None:
        """The point is kept: learn from it."""
        plan = out.plan
        self.points += 1
        self.bins_total += self.n
        self.bins_measured += int(out.mask.sum()) if out.mask is not None else self.n
        found = math.isfinite(out.f_fit)
        # "Lost" only when the model said the line is IN the band and it was
        # not found there. A line the model puts outside the band (a field
        # sweep that carries it off the end), or no valid prediction at all,
        # is not a failure -- forcing a full sweep for every such point would
        # throw away the whole point of the window.
        self.last_failed = (not found and not plan.out_of_band
                            and self.in_band(plan.f_pred))
        # Learn only from a line the model says EXISTS here: a "dip" found
        # below saturation is structure in the baseline, not the resonance.
        if found and self.track and math.isfinite(plan.f_pred):
            est = resonance.meff_from_f(self.model, out.f_fit, plan.B_mT,
                                        plan.angle_deg, self.params)
            if math.isfinite(est) and not self._outlier(est):
                self.estimates.append(est)
                if len(self.estimates) == 1:
                    self.meff = est
                else:
                    self.meff += SMOOTHING * (est - self.meff)
        if plan.full:
            # The new baseline: this sweep with its resonance region bridged.
            # Region = around the fitted line, else around where the model
            # (now corrected) puts it, else nothing to cut.
            centre = out.f_fit if found else self.predict(plan.B_mT, plan.angle_deg)
            for d, trace in out.raw.items():
                if math.isfinite(centre):
                    i0, i1 = self._bins(centre - self.margin_hz, centre + self.margin_hz)
                    self.baseline[d] = bridge(trace, i0, i1)
                else:
                    self.baseline[d] = np.array(trace, copy=True)
        self.last = out

    def _outlier(self, est: float) -> bool:
        if len(self.estimates) < OUTLIER_AFTER:
            return False
        recent = np.asarray(self.estimates[-9:])
        med = float(np.median(recent))
        mad = 1.4826 * float(np.median(np.abs(recent - med)))
        return abs(est - med) > max(OUTLIER_MAD * mad, OUTLIER_FLOOR_MT)

    def state(self) -> dict:
        """The live readout: what the last KEPT point did (GUI, logs)."""
        out = self.last
        d = {"points": self.points, "meff_mT": self.meff,
             "fraction_measured": (self.bins_measured / self.bins_total
                                   if self.bins_total else math.nan)}
        if out is not None:
            d.update(self.record(out))
            d["meff_used_mT"] = d.pop("meff_mT")
            d["meff_mT"] = self.meff
            d["reason"] = out.plan.reason
            d["B_mT"] = out.plan.B_mT
            d["angle_deg"] = out.plan.angle_deg
        return d

    # ---- per-point record ----------------------------------------------------
    def record(self, out: Outcome) -> dict:
        """The per-point scalars stored in the dataset (names without the det)."""
        plan = out.plan
        return {
            "fres_pred_Hz": plan.f_pred,
            "fres_fit_Hz": out.f_fit,
            "window_lo_Hz": float(self.f[plan.i0]),
            "window_hi_Hz": float(self.f[plan.i1]),
            "meff_mT": out.meff_used,
            "full_sweep": bool(plan.full),
        }


#: per-point variables: suffix -> (dtype, units, long name)
RECORD_VARS = {
    "fres_pred_Hz": (float, "Hz", "resonance predicted by the model"),
    "fres_fit_Hz": (float, "Hz", "resonance fitted in the measured bins (NaN = none)"),
    "window_lo_Hz": (float, "Hz", "lowest measured frequency"),
    "window_hi_Hz": (float, "Hz", "highest measured frequency"),
    "meff_mT": (float, "mT", "mu0 Meff the prediction used"),
    "full_sweep": (bool, "", "this point was a full-band sweep"),
}


def var_names(det: str) -> dict:
    """Every dataset variable the window adds for detector `det`."""
    out = {"mask": f"{det}_measured"}
    out.update({k: f"{det}_{k}" for k in RECORD_VARS})
    return out
