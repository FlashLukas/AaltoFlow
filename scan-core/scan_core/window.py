"""window.py -- the WINDOW: sweep a slow detector only near the expected peak.

Lukas, 2026-09-28: "some devices are terribly slow and if I want to see e.g.
FMR in field I scan most of the time in the dark. Can we cheat? The SA with TG
knows the field, the field angle, and based on assumed material parameters ...
only scans within a margin around the assumed resonance. The rest of the scan
is filled with the baseline."

And 2026-10-12: "add option for another physics". The window no longer knows
any physics: a PREDICTOR (predictors.py -- Kittel/FMR, linear, a typed
formula, or a lab's own) maps the inputs read at each point to the expected
peak position, and can correct one of its parameters from the measured peak.
Everything below works in the detector's own axis unit.

His decisions, and where they live here:

  1. Outside the window the data is FILLED WITH THE BASELINE, and a MEASURED
     MASK goes into the file (`<det>_measured`), so nobody later mistakes the
     filled part for a measurement.
  2. The window follows a MODEL and SELF-CORRECTS from the measured peak:
     every clean peak gives a new estimate of the predictor's tracked
     parameter (Kittel: mu0 Meff), smoothed.
  3. The model is a choice (Kittel in-plane / out-of-plane, or another
     predictor altogether).

The recipe block (old FMR-only blocks are translated on load, `upgrade`):

    window:
      detector: fmr                    # a 1-D array detector declaring `window`
      predictor: kittel                # predictors.names()
      inputs: {field: field, angle: 30.0}   # role -> parameter id or number
      params: {g: 2.0, meff_mT: 1750, hk_mT: 5}
      options: {model: inplane}        # the predictor's non-numeric settings
      margin: 300                      # half width ...
      margin_unit: MHz                 # ... in this unit ("" = the axis unit)
      dip: min                         # the line is a minimum (or max)
      track: true                      # correct the tracked parameter
      full_every: 20                   # a full sweep every N points

How one point goes (the engine calls plan -> trigger/read -> assess):

  * plan(): read the inputs for this point, predict the peak with the
    CURRENT tracked value. A FULL sweep instead of a window when: this is
    the first point, every `full_every`-th point (to refresh the baseline),
    the model is not valid here (NaN), or the last point's line was not
    found. Otherwise the window is the bins within peak +- margin (at least
    `min_bins`, clamped to the band).
  * The detector sweeps only those bins. The window is given in BIN INDICES of
    the detector's own full grid, never in axis units: then the measured bins
    land on the dataset's regular axis exactly, with no interpolation ever.
  * assess(): find the peak in the measured bins. It must sit at least 10 % of
    the window width inside each edge (a "peak" at the edge is usually the
    flank of a line that is really outside) and stand out from the local
    baseline by more than 5 x its noise (median absolute deviation). If not:
    WIDEN the window x2 and measure this point again, up to the full band.
  * commit(): the point is kept. Update the tracked value, the baseline
    (after a full sweep), the counters.

The baseline is the last FULL sweep with its own peak region cut out and
replaced by a straight line between the two sides -- otherwise the filled
trace would carry a ghost of the old line at the old position.

Nothing here talks to hardware: the engine does the triggering and reading, so
a pause-on-fault or an abort in the middle of a point simply throws away the
plan and the point is planned again from the committed state.

Stored per point (`<det>_<name>`, units = the axis unit): peak_pred,
peak_fit, window_lo, window_hi, tracked (the tracked parameter's value the
prediction used), full_sweep. Files written before 2026-10-12 carry the FMR
names instead (fres_pred_Hz, fres_fit_Hz, window_lo_Hz, window_hi_Hz,
meff_mT); they still open as ordinary variables, and their recipe's window
block is translated on load like any old recipe.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from . import predictors as P

#: the dip must stand this many noise-sigmas (MAD) out of the local baseline
PROMINENCE_MAD = 5.0
#: ... and sit at least this fraction of the window width inside each edge
EDGE_FRACTION = 0.10
#: smoothing of the tracked parameter: new = old + SMOOTHING * (measured - old).
#: The FIRST clean estimate replaces the assumed value outright -- the
#: assumption is a guess, a measurement is not.
SMOOTHING = 0.5
#: an estimate further than max(OUTLIER_MAD x MAD, floor) from the running
#: median (after OUTLIER_AFTER estimates) is ignored as an outlier. The floor
#: is the tracked Param's own `floor` (Kittel's Meff: 5 mT), else
#: OUTLIER_REL x |median|.
OUTLIER_MAD = 6.0
OUTLIER_REL = 0.05
OUTLIER_AFTER = 3

KNOWN_KEYS = ("detector", "predictor", "inputs", "params", "options", "margin",
              "margin_unit", "dip", "track", "full_every")
DEFAULTS = {"predictor": "kittel", "inputs": {}, "params": {}, "options": {},
            "margin_unit": "", "dip": "min", "track": True, "full_every": 20}

#: the keys of the FMR-only block (until 2026-10-12); a block without
#: `predictor` is one of those and is translated by upgrade()
OLD_KEYS = ("field", "angle", "model", "margin_MHz")


def upgrade(block):
    """An old FMR-only window block -> the predictor form (None stays None).

    Until 2026-10-12 the block WAS the Kittel model: field, angle, model,
    params, margin_MHz. Translated exactly (same numbers, margin in MHz), so
    an old definition -- or the recipe inside an old .nc -- runs the same
    scan, and is saved in the new form the next time."""
    if not isinstance(block, dict) or "predictor" in block:
        return block
    old = dict(block)
    new = {"detector": old.pop("detector", None), "predictor": "kittel"}
    inputs = {}
    if "field" in old:
        inputs["field"] = old.pop("field")
    ang = old.pop("angle", None)
    if ang is not None:
        inputs["angle"] = ang
    new["inputs"] = inputs
    new["params"] = dict(old.pop("params", None) or {})
    new["options"] = {"model": old.pop("model", "inplane")}
    new["margin"] = old.pop("margin_MHz", 300.0)        # the old default
    new["margin_unit"] = "MHz"
    for k in ("dip", "track", "full_every"):
        if k in old:
            new[k] = old.pop(k)
    new.update(old)             # anything unknown: kept, so validate names it
    if new["detector"] is None:
        new.pop("detector")
    return new


def normalize(block: dict) -> dict:
    """The window block (upgraded) with every default filled in, including
    the predictor's own parameter and option defaults."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in (upgrade(block) or {}).items():
        out[k] = dict(v) if isinstance(v, dict) else v
    pred = P.get(out.get("predictor"))
    if pred is not None:
        opts = pred.option_defaults()
        opts.update(out["options"] or {})
        out["options"] = opts
        params = pred.param_defaults(out)
        params.update(out["params"] or {})
        out["params"] = params
    return out


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def validate(recipe, registry) -> list[str]:
    """Problems with `recipe.window` ([] = fine, or no window at all)."""
    w = getattr(recipe, "window", None)
    if not w:
        return []
    if not isinstance(w, dict):
        return [f"window must be a mapping, not {w!r}"]
    w = upgrade(w)
    errs = []
    for k in w:
        if k not in KNOWN_KEYS:
            errs.append(f"window: unknown key {k!r} (known: {', '.join(KNOWN_KEYS)})")
    for k in ("inputs", "params", "options"):
        if w.get(k) is not None and not isinstance(w.get(k), dict):
            errs.append(f"window: {k} must be a mapping, not {w.get(k)!r}")
            return errs
    name = w.get("predictor", DEFAULTS["predictor"])
    pred = P.get(name)
    if pred is None:
        errs.append(f"window: unknown predictor {name!r} (known: {', '.join(P.names())})")
        errs += P.plugin_errors
    nw = normalize(w)
    axis_unit = None
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
                        f"one axis); a window needs one")
        else:
            axis_unit = axes[0].unit or ""
        if not getattr(g, "window", None):
            errs.append(f"window: '{det}' does not declare window support in its "
                        f"describe (`window` key); it would sweep the full band anyway")
        if getattr(g, "acquire", None) is None:
            errs.append(f"window: '{det}' has no acquire step to pass the window to")
    if pred is not None:
        errs += _validate_predictor(pred, nw, w, registry, axis_unit)
    m = _num(w.get("margin"))
    if m is None or m <= 0:
        errs.append("window: margin must be a positive number")
    if axis_unit is not None and \
            P.output_factor(nw.get("margin_unit") or "", axis_unit) is None:
        errs.append(f"window: margin_unit {nw.get('margin_unit')!r} does not convert "
                    f"to the detector's axis unit {axis_unit!r}")
    if nw.get("dip", "min") not in ("min", "max"):
        errs.append("window: dip must be min or max")
    if not isinstance(nw.get("track", True), bool):
        errs.append("window: track must be true or false")
    fe = nw.get("full_every", DEFAULTS["full_every"])
    if isinstance(fe, bool) or not isinstance(fe, int) or fe < 0:
        errs.append("window: full_every must be a whole number >= 0 (0 = only the first point)")
    for ax in recipe.axes or []:
        if isinstance(ax, dict) and ax.get("type") == "fly":
            errs.append("window: the window works on stepped scans only, "
                        "not with a fly axis")
            break
    return errs


def _validate_predictor(pred, nw, raw, registry, axis_unit) -> list[str]:
    errs = []
    opts = nw["options"]
    known_opts = {o.name for o in pred.options}
    for k in (raw.get("options") or {}):
        if k not in known_opts:
            errs.append(f"window: {pred.name} has no option {k!r}"
                        + (f" (known: {', '.join(sorted(known_opts))})" if known_opts
                           else ""))
    errs += pred.validate(opts)
    if errs:
        return errs                 # a broken formula: the names are unknown
    if axis_unit is not None and \
            P.output_factor(pred.output_unit_for(opts), axis_unit) is None:
        errs.append(f"window: {pred.name} predicts in {pred.output_unit_for(opts)!r}, "
                    f"which does not convert to the detector's axis unit "
                    f"{axis_unit!r}")
    decl_in = {i.role: i for i in pred.inputs_for(nw)}
    given = raw.get("inputs") or {}
    for role in given:
        if role not in decl_in:
            errs.append(f"window: {pred.name} has no input {role!r}"
                        + (" (not a name in the formula)" if pred.name == "expression"
                           else f" (inputs: {', '.join(decl_in)})" if decl_in else ""))
    for role, inp in decl_in.items():
        v = given.get(role)
        if v is None:
            if inp.required:
                errs.append(f"window: input `{role}` ({inp.label}) is required")
            continue
        if isinstance(v, str):
            p = registry.get(v)
            if p is None:
                errs.append(f"window: unknown {role} parameter '{v}'")
            elif not hasattr(p, "get"):
                errs.append(f"window: {role} parameter '{v}' cannot be read")
            elif inp.family and (getattr(p, "unit", "") or "").strip().lower() \
                    not in P.INPUT_FAMILIES[inp.family]:
                errs.append(f"window: {role} parameter '{v}' is in {p.unit!r}, not a "
                            f"{inp.family} unit")
        elif _num(v) is None:
            errs.append(f"window: input {role} must be a parameter id or a finite "
                        f"number, not {v!r}")
    decl_p = {p.name: p for p in pred.params_for(nw)}
    for k, v in (raw.get("params") or {}).items():
        if k not in decl_p:
            errs.append(f"window params: unknown parameter {k!r} "
                        f"(known: {', '.join(decl_p) or 'none'})")
    for k, par in decl_p.items():
        v = nw["params"].get(k)
        if v is None:
            errs.append(f"window params: {k} needs a value"
                        + (" (or map it to an input)" if pred.name == "expression" else ""))
            continue
        x = _num(v)
        if x is None:
            errs.append(f"window params: {k} = {v!r} is not a finite number")
        elif not (par.lo <= x <= par.hi):
            errs.append(f"window params: {k} = {x:g} is outside {par.lo:g} .. {par.hi:g}"
                        + (f" ({par.help})" if par.help else ""))
    tracked = pred.tracked_for(opts)
    if tracked and tracked not in decl_p:
        errs.append(f"window: the tracked parameter {tracked!r} is not one of the "
                    f"parameters ({', '.join(decl_p) or 'none'})")
    if nw.get("track") is True and not tracked:
        errs.append(f"window: track is on, but {pred.name} has no parameter to track"
                    + (" (choose one in options.tracked)" if pred.name == "expression"
                       else ""))
    return errs


# ───────────────────────────── dip finding ───────────────────────────────────

def _real(y) -> np.ndarray:
    """What the dip is looked for in: the magnitude of a complex trace."""
    y = np.asarray(y)
    return np.abs(y) if np.iscomplexobj(y) else y.astype(float)


def find_dip(f: np.ndarray, y, dip: str = "min") -> tuple[float, dict]:
    """(fitted position in the units of `f`, or NaN; diagnostics) of the line in (f, y).

    Robust on purpose:
      * the local baseline is a straight line through the medians of the
        outer 10 % on each side (a spectrum-analyser trace is rarely flat);
      * the "noise" is the larger of two robust spreads: the bin-to-bin
        scatter (median absolute DIFFERENCE / sqrt 2 -- white noise), and the
        MAD of the residual away from the candidate line (every bin not
        deeper than a quarter of it -- slow structure: a tracking generator's
        ripple, a standing wave). A line must beat both; without the second,
        a ripple trough in a band with no line is "found" and teaches the
        predictor a nonsense value of its tracked parameter;
      * the extremum must stand out by > PROMINENCE_MAD x noise and sit at
        least EDGE_FRACTION of the width inside both edges;
      * its position is refined with a parabola through the extremum and its
        two neighbours (sub-bin accuracy).
    """
    f = np.asarray(f, dtype=float)
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
    """What one attempt at one point measures (positions in axis units)."""
    full: bool
    i0: int
    i1: int
    f_pred: float               # the predicted peak, NaN if the model is not valid here
    inputs: dict                # role -> the value the prediction used
    margin: float
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
    tracked_used: float
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
        self.pred = P.get(w["predictor"])
        if self.pred is None:
            raise ValueError(f"window: unknown predictor {w['predictor']!r}")
        self.options = dict(w["options"])
        self.params = {k: float(v) for k, v in w["params"].items()}
        self.input_decl = self.pred.inputs_for(w)
        self.input_map = dict(w["inputs"])
        self.axis_unit = axis_unit or ""
        # predictor answer -> axis units (validated: never None here)
        self.out_factor = P.output_factor(self.pred.output_unit_for(self.options),
                                          self.axis_unit) or 1.0
        self.margin = float(w["margin"]) * (
            P.output_factor(w.get("margin_unit") or "", self.axis_unit) or 1.0)
        self.dip = w["dip"]
        self.tracked = self.pred.tracked_for(self.options)
        self.track = bool(w["track"]) and bool(self.tracked)
        self.full_every = int(w["full_every"])
        self.registry = registry
        self.f = np.asarray(grid, dtype=float)
        self.n = len(self.f)
        self.group_dets = list(group_dets)      # array detectors filled the same way
        self.min_bins = max(1, min(int(min_bins or 1), self.n))
        self.arg = arg or "window"
        decl_p = {p.name: p for p in self.pred.params_for(w)}
        self.tracked_param = decl_p.get(self.tracked) if self.tracked else None
        # committed state
        self.value = self.params.get(self.tracked, math.nan) if self.tracked else math.nan
        self.estimates: list[float] = []
        self.points = 0
        self.last_failed = False
        self.baseline: dict = {}                # det -> full-length baseline
        self.last = None                        # the last committed Outcome (GUI)
        self.bins_measured = 0
        self.bins_total = 0

    # ---- inputs ----------------------------------------------------------
    def _read(self, pid, current: dict, family) -> float:
        if pid in current:                      # an axis or a condition: the setpoint
            v = current[pid]
        else:                                   # anything else: read it NOW
            v = self.registry.get(pid).get()
        try:
            v = float(v)
        except (TypeError, ValueError):
            return math.nan
        if not family:
            return v
        unit = (getattr(self.registry.get(pid), "unit", "") or "").strip().lower()
        return v * (P.INPUT_FAMILIES[family].get(unit) or 1.0)

    def read_inputs(self, current: dict) -> dict:
        """role -> value (in the unit the predictor wants) for this point."""
        x = {}
        for inp in self.input_decl:
            v = self.input_map.get(inp.role)
            if v is None:
                x[inp.role] = float(inp.default)
            elif isinstance(v, str):
                x[inp.role] = self._read(v, current, inp.family)
            else:
                x[inp.role] = float(v)
        return x

    def predict(self, x: dict, value=None) -> float:
        """The predicted peak in AXIS units, with the tracked parameter at
        `value` (default: the current estimate). NaN = not valid here."""
        p = dict(self.params)
        if self.tracked:
            p[self.tracked] = self.value if value is None else value
        if any(not math.isfinite(v) for v in x.values()):
            return math.nan
        try:
            f = float(self.pred.predict(x, p, self.options))
        except Exception:                       # a lab predictor may raise anything
            return math.nan
        return f * self.out_factor if math.isfinite(f) else math.nan

    def in_band(self, f) -> bool:
        return (math.isfinite(f) and float(np.nanmin(self.f)) <= f
                <= float(np.nanmax(self.f)))

    # ---- planning ----------------------------------------------------------
    def _bins(self, lo, hi) -> tuple[int, int]:
        idx = np.nonzero((self.f >= lo) & (self.f <= hi))[0]
        if len(idx):
            i0, i1 = int(idx.min()), int(idx.max())
        else:                                   # narrower than one bin
            c = int(np.argmin(np.abs(self.f - 0.5 * (lo + hi))))
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
        x = self.read_inputs(current)
        f_pred = self.predict(x)
        margin = self.margin * (2 ** attempt)
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
            return Plan(True, 0, self.n - 1, f_pred, x, margin, attempt, full_reason)
        fmin, fmax = float(np.nanmin(self.f)), float(np.nanmax(self.f))
        if f_pred + self.margin < fmin or f_pred - self.margin > fmax:
            # The model puts the line OUTSIDE the band: there is nothing to
            # look for. A minimal window at the nearest edge keeps the trace
            # measured where it joins the band; the periodic full sweeps would
            # show a line the model misplaced.
            edge = fmin if f_pred < fmin else fmax
            i0, i1 = self._bins(edge, edge)
            return Plan(False, i0, i1, f_pred, x, margin, attempt,
                        "line outside the band", out_of_band=True)
        i0, i1 = self._bins(f_pred - margin, f_pred + margin)
        if i0 == 0 and i1 == self.n - 1:
            return Plan(True, 0, self.n - 1, f_pred, x, margin, attempt,
                        "window covers the band")
        return Plan(False, i0, i1, f_pred, x, margin, attempt)

    # ---- after the read ----------------------------------------------------
    def assess(self, plan: Plan, values: dict) -> Outcome:
        """Look for the line in what was measured; decide widen / fill."""
        raw = {d: np.asarray(values[d]) for d in self.group_dets if d in values}
        y = raw[self.det]
        sl = slice(plan.i0, plan.i1 + 1)
        f_fit = math.nan
        if not plan.out_of_band:
            f_fit, _info = find_dip(self.f[sl], y[sl], self.dip)
        out = Outcome(plan, f_fit, self.value, raw)
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
        # where the model is not valid (FMR below saturation) is structure
        # in the baseline, not the line.
        if found and self.track and math.isfinite(plan.f_pred):
            p = dict(self.params, **{self.tracked: self.value})
            try:
                est = float(self.pred.track(out.f_fit / self.out_factor, plan.inputs,
                                            p, self.options))
            except Exception:
                est = math.nan
            if math.isfinite(est) and not self._outlier(est):
                self.estimates.append(est)
                if len(self.estimates) == 1:
                    self.value = est
                else:
                    self.value += SMOOTHING * (est - self.value)
        if plan.full:
            # The new baseline: this sweep with its peak region bridged.
            # Region = around the fitted line, else around where the model
            # (now corrected) puts it, else nothing to cut.
            centre = out.f_fit if found else self.predict(plan.inputs)
            for d, trace in out.raw.items():
                if math.isfinite(centre):
                    i0, i1 = self._bins(centre - self.margin, centre + self.margin)
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
        floor = getattr(self.tracked_param, "floor", None)
        if floor is None:
            floor = max(OUTLIER_REL * abs(med), 1e-12)
        return abs(est - med) > max(OUTLIER_MAD * mad, floor)

    # ---- what the GUI and the file show -----------------------------------
    def tracked_label(self) -> str:
        """'μ0Meff' / 'a' / ... and its unit, for the readout ('' = none)."""
        if not self.track:
            return ""
        par = self.tracked_param
        label = par.label if par is not None else self.tracked
        unit = P.unit_text(par.unit if par is not None else "", self.axis_unit,
                           {i.role: self._input_unit(i) for i in self.input_decl})
        return f"{label} [{unit}]" if unit else label

    def _input_unit(self, inp) -> str:
        if inp.family:
            return inp.unit
        v = self.input_map.get(inp.role)
        p = self.registry.get(v) if isinstance(v, str) else None
        return getattr(p, "unit", "") or ""

    def tracked_unit(self) -> str:
        par = self.tracked_param
        if par is None:
            return ""
        return P.unit_text(par.unit, self.axis_unit,
                           {i.role: self._input_unit(i) for i in self.input_decl})

    def state(self) -> dict:
        """The live readout: what the last KEPT point did (GUI, logs)."""
        out = self.last
        d = {"points": self.points, "tracked": self.value,
             "tracked_name": self.tracked if self.track else "",
             "tracked_label": self.tracked_label(),
             "axis_unit": self.axis_unit, "predictor": self.pred.name,
             "fraction_measured": (self.bins_measured / self.bins_total
                                   if self.bins_total else math.nan)}
        if out is not None:
            d.update(self.record(out))
            d["tracked_used"] = d.pop("tracked")
            d["tracked"] = self.value
            d["reason"] = out.plan.reason
            d["inputs"] = dict(out.plan.inputs)
        return d

    # ---- per-point record ----------------------------------------------------
    def record(self, out: Outcome) -> dict:
        """The per-point scalars stored in the dataset (names without the det)."""
        plan = out.plan
        return {
            "peak_pred": plan.f_pred,
            "peak_fit": out.f_fit,
            "window_lo": float(self.f[plan.i0]),
            "window_hi": float(self.f[plan.i1]),
            "tracked": out.tracked_used,
            "full_sweep": bool(plan.full),
        }

    def record_vars(self) -> dict:
        """suffix -> (dtype, units, long name) of every per-point variable."""
        u = self.axis_unit
        tl = (self.tracked_param.label if self.tracked_param is not None
              else (self.tracked or "tracked parameter"))
        return {
            "peak_pred": (float, u, f"peak predicted by the {self.pred.name} model"),
            "peak_fit": (float, u, "peak fitted in the measured bins (NaN = none)"),
            "window_lo": (float, u, "lowest measured bin"),
            "window_hi": (float, u, "highest measured bin"),
            "tracked": (float, self.tracked_unit(),
                        f"{tl}: the value the prediction used"),
            "full_sweep": (bool, "", "this point was a full-band sweep"),
        }


#: the per-point variable suffixes (see WindowRunner.record_vars)
RECORD_KEYS = ("peak_pred", "peak_fit", "window_lo", "window_hi", "tracked", "full_sweep")


def var_names(det: str) -> dict:
    """Every dataset variable the window adds for detector `det`."""
    out = {"mask": f"{det}_measured"}
    out.update({k: f"{det}_{k}" for k in RECORD_KEYS})
    return out
