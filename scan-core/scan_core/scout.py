"""
scout.py -- the SCOUT PASS: take a quick look first, then measure in detail
only where something is happening (2026-10-08; it grew out of the XY mask of
2026-10-07).

Lukas, for an XY map of a patterned sample: much of the area is nonmagnetic
substrate, and measuring it as carefully as the magnetic elements wastes most
of the night -- "but we still need to make the scan", i.e. the result must
still be the complete matrix. And then: "it should work for ANY
multidimensional scan once it is given something to measure" -- an FMR line in
field x frequency is the same situation as an island on a substrate: most of
the map is background, and the background can be recognised quickly.

THE IDEA, for any number of axes:

  1. the SCOUT: one quick, scalar detector at every k-th point of the axes
     ticked "scout" (k per axis, default 3: a 2-D scout costs 1/9 of the
     points). Optionally with its own `settings` -- a short lock-in time
     constant, one scope average -- that hold ONLY during the scout and are put
     back for the real scan, also after an Abort or an error;
  2. a MASK from it: the scout's readings interpolated onto the full grid
     (linear, one axis after the other), then a decision -- `above` / `below` a
     threshold, or `deviates` from the background by more than k x noise --
     then grown by a margin in GRID POINTS so the edges are measured too;
  3. the REAL scan, which VISITS only the points inside the mask.

The points left out are never visited: no move, no settle, no routine. In the
file they are "not measured" (NaN), the grid keeps its full shape, and
`scan_mask` says which points are which -- a NaN the mask left out is never
confused with an aborted scan.

THE RECIPE BLOCK (key `scout`; every key optional except `axes`, and
`detector` for a measured scout):

    scout:
      axes: {pos_y: 3, pos_x: 3}   # the scouted axes, each with its coarse step
      detector: pm16.power         # what the scout reads (ONE number per point)
      keep: above                  # above | below | deviates
      threshold: auto              # above/below: auto (Otsu) | a number | {fraction: f}
      k: 4                         # deviates: |value - median| > k x noise
      margin: auto                 # grid points: auto (= half the coarse step) |
                                   # n | {axis: n}
      per_outer: once              # once | each (see OUTER AXES)
      settings: {hf2.tc1: 0.001}   # held during the scout only
      from: mask.png               # an image instead of measuring (2 axes only)
      from: {file: run1.nc, detector: lockin_r}   # ... or an earlier scan
      extent: {x: [-50, 50], y: [-40, 40]}        # where an image lies

The old `mask:` block (2026-10-07, XY only, margin in the axes' unit) is still
read: `from_mask` translates it into this form when a recipe is loaded, so
every .yaml and every .nc written before keeps working. Only `scout` is
written.

WHICH AXES, AND WHAT THE OTHERS DO. The scout's mask lies on the SCOUTED axes
only; every other axis just carries it along:

  * OUTER axes -- the unscouted axes OUTSIDE (above) every scouted one -- are
    governed by `per_outer`:
      once : one scout, at their FIRST values, and the same mask for all of
             their values (a patterned sample does not move with the field);
      each : a fresh scout at every step of the outer axes, right before that
             block is measured -- for a feature that MOVES with them (an FMR
             line drifting with the field angle).
  * INNER axes -- the unscouted axes inside (below) a scouted one -- are held
    at their FIRST value during the scout, and the mask's decision then holds
    for all of their values: an XY scout on reflectivity with a field sweep at
    every point measures the whole field sweep on the elements and none of it
    on the substrate. (The rule is the same wherever such an axis sits; it is
    what "the scout looks at the scouted axes" means. If the feature depends
    on an inner axis, scout that axis too.)
  * A FLY axis cannot be combined with a scout: a fly row is one continuous
    move and cannot step over the points it leaves out. A REPEAT axis cannot
    be scouted (it moves nothing); outside the scouted axes it is an ordinary
    outer or inner axis, except that `per_outer: each` cannot sit inside an
    AVERAGING repeat (the mask would change between the runs it averages).

WHERE TO MEASURE (`keep`):
  * above / below a threshold: `auto` is Otsu's method -- the level that best
    splits the readings into two groups, made for a two-level sample
    (substrate and elements); a number; or {fraction: f} of the range.
  * deviates: |value - median| > k x noise, noise = 1.4826 x the median
    absolute deviation (MAD) of the scout's readings -- a ROBUST estimate of
    the background's scatter that the few points on a feature hardly move. It
    finds peaks AND dips without being told the sign, and a sample with
    several levels or a gradient where Otsu's two groups do not exist. (It
    assumes the background is most of the scout: if the feature covers more
    than about half of the points, the median IS the feature -- use
    above/below then.)

WHY INTERPOLATE BEFORE THE DECISION. Thresholding the coarse map first and
then upsampling the yes/no answer gives a mask of k x k blocks with every
edge on the coarse grid. Interpolating the reading first puts the edge where
the signal actually crosses, between the coarse points. The margin then
covers what interpolation cannot know: between two scout points the real rim
can be anywhere, and on a convex element straight-line interpolation puts it
INSIDE. Auto = half the coarse step, measured in the simulator (2026-10-07: a
smaller margin lost island points, half a step lost none) and confirmed on the
rig (2026-10-08: 1-2 of 39 boundary points above the threshold, none lost; it
costs ~28 % of the second pass). An element smaller than about one coarse step
can fall between the scout's points entirely -- use a smaller step for that.

CONSERVATIVE BY DESIGN. Whenever the mask cannot tell -- a scout reading that
is NaN, a point outside the area a file covers, a block not yet scouted --
the point is MEASURED. Measuring a point too many costs seconds; leaving out a
magnetic one costs the measurement.

THE COARSE GRID always includes the LAST index of each axis (0, 3, ..., 27,
29 for 30 points: the last gap is 1), so the scout spans the whole grid and
nothing has to be extrapolated.

COORDINATES. A mask from a file is matched point to point by the VALUE of the
axis parameters, never by index. An earlier scan must have swept the same
parameters (found by the coordinate's `param` attribute, or its NAME): a mask
in stage um laid over a scan in camera coordinates would be off by the
stage's drift, and there is no honest way to convert one into the other, so
that is refused. Points outside its range are measured. (A picture has no
parameters; its `extent` says where it is, in whatever frame the scan uses.)

IN THE FILE (the names of 2026-10-07 are kept, so files written then and now
read the same way): `scan_mask` (int8, 1 = measured, 0 = left out) on the
scouted axes -- with `per_outer: each` also on the outer ones; `mask_<det>` =
the scout's own readings on coarse axes `mask_<axis>` (`mask_file` for a
picture); attributes `mask_json` (the block; `scout_json` is the same text
under the new name), `mask_threshold`, `mask_points`, `mask_source`,
`mask_margin` (in the axes' unit, when the margin is one length on every
scouted axis) and `mask_margin_points` (per axis, in grid points).

Pure numpy except `ScoutRunner`, which drives the registry the way the engine
does (fault check, Abort, the operator's Pause).
"""

from __future__ import annotations

import itertools
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

KEYS = ("axes", "step", "detector", "keep", "threshold", "k", "margin",
        "per_outer", "settings", "from", "extent")
KEEP = ("above", "below", "deviates")
PER_OUTER = ("once", "each")
DEFAULT_STEP = 3
DEFAULT_K = 4.0
#: MAD -> standard deviation for normally distributed noise
MAD_TO_SIGMA = 1.4826
#: files `from` reads as a picture or a matrix (anything else: a .nc scan)
IMAGE_EXT = (".png", ".tif", ".tiff", ".bmp", ".jpg", ".jpeg")
MATRIX_EXT = (".csv", ".txt", ".dat", ".npy")


# ───────────────────────────── the block, read ──────────────────────────────

def _as_axes(axes, step):
    """`axes` as {name: step}: the mapping as it is, or a list of names with
    one common `step` (the old mask's form). Anything else comes back as it
    was, for validate() to complain about."""
    if isinstance(axes, dict):
        return dict(axes)
    if isinstance(axes, (list, tuple)) and all(isinstance(a, str) for a in axes) \
            and len(set(axes)) == len(axes):
        return {a: step for a in axes}
    return axes


def spec_of(block: dict) -> dict:
    """The block with every default filled in, `axes` as {name: step} and
    `from` split into the file and (for a scan file) the variable to read."""
    b = dict(block or {})
    src = b.get("from") or None
    src_file, src_det = src, b.get("detector")
    if isinstance(src, dict):
        src_file = src.get("file")
        src_det = src.get("detector") or b.get("detector")
    return {"axes": _as_axes(b.get("axes"), b.get("step", DEFAULT_STEP)),
            "detector": b.get("detector"),
            "keep": b.get("keep") or "above",
            "threshold": b.get("threshold", "auto"),
            "k": b.get("k", DEFAULT_K),
            "margin": b.get("margin", "auto"),
            "per_outer": b.get("per_outer") or "once",
            "settings": dict(b.get("settings") or {}),
            "from": src,
            "from_file": str(src_file) if src_file else None,
            "from_detector": src_det,
            "extent": b.get("extent") or None}


def is_picture(path) -> bool:
    """An image or a plain matrix (not a scan file)."""
    return bool(path) and Path(str(path)).suffix.lower() in IMAGE_EXT + MATRIX_EXT


def _raster_xy(axes_list) -> list[str] | None:
    rasters = [ax for ax in axes_list or []
               if isinstance(ax, dict) and ax.get("type") == "raster"]
    if len(rasters) != 1:
        return None
    r = rasters[0]
    try:
        return [r["x"].get("name") or r["x"]["param"],
                r["y"].get("name") or r["y"]["param"]]
    except (KeyError, AttributeError, TypeError):
        return None


def from_mask(mask, axes_list) -> dict:
    """Translate an old `mask:` block (2026-10-07) into a `scout:` block.

    The mask was XY only, so its axes are [X, Y] (default: the raster's) with
    one `step`; its margin was in the AXES' UNIT and is now in grid points --
    a number is converted with each axis's pitch (margin / pitch, exactly the
    same disc as before), `auto` stays auto (half the coarse step, which is
    what the old auto -- half the pass-1 pitch -- amounted to). Nothing else
    changes meaning.
    """
    if not isinstance(mask, dict):
        return mask                          # validate() says what is wrong
    m = dict(mask)
    out: dict = {}
    step = m.pop("step", DEFAULT_STEP)
    axes = m.pop("axes", None)
    if axes is None:
        axes = _raster_xy(axes_list)
    if axes is not None:
        out["axes"] = _as_axes(axes, step)
    elif step != DEFAULT_STEP:
        out["step"] = step                   # kept for validate() to check
    margin = m.pop("margin", "auto")
    if isinstance(margin, (int, float)) and not isinstance(margin, bool) \
            and isinstance(out.get("axes"), dict):
        pts = _margin_in_points(margin, list(out["axes"]), axes_list)
        out["margin"] = pts if pts is not None else "auto"
    elif margin != "auto":
        out["margin"] = margin               # a bad one: validate() reports it
    for key, value in m.items():
        out[key] = value                     # detector, keep, threshold, from, extent
    return out


def _margin_in_points(margin, names, axes_list):
    """{axis: margin / pitch} for the old um margin; None if the axes cannot
    be compiled here (a file axis whose file is elsewhere)."""
    try:
        from .recipe import _compile_axis
        dims = [d for ax in axes_list or [] for d in _compile_axis(ax)]
    except Exception:
        return None
    out = {}
    for name in names:
        d = next((d for d in dims if d.name == name), None)
        if d is None or not d.params:
            return None
        p = _pitch(d.coord)
        out[name] = round(float(margin) / p, 6) if p > 0 else 0.0
    return out


def scout_dims(spec, dims) -> list[int]:
    """Indices into `dims` of the scouted axes, in the scan's order (outer
    first). Raises ValueError with a message for the operator."""
    axes = spec["axes"]
    names = [d.name for d in dims]
    if not isinstance(axes, dict) or not axes:
        if isinstance(axes, (list, tuple)) and len(set(axes)) != len(axes):
            raise ValueError("scout: `axes` must name different axes")
        raise ValueError("scout: say which axes to scout -- tick 'scout' on them "
                         "(`axes: {name: step}`); a recipe from the old XY mask "
                         "finds them on its own only with exactly one raster axis")
    out = []
    for a in axes:
        if a not in names:
            raise ValueError(f"scout: there is no axis {a!r} in this scan "
                             f"(axes: {', '.join(names)})")
        d = dims[names.index(a)]
        if d.kind == "repeat" or not d.params:
            raise ValueError(f"scout: {a!r} is a repeat axis, which moves "
                             f"nothing -- there is nothing to scout along it")
        if d.kind == "fly":
            raise ValueError(f"scout: {a!r} is a fly axis; a fly row is one "
                             f"continuous move and cannot skip points")
        out.append(names.index(a))
    return sorted(out)


def outer_dims(ks) -> list[int]:
    """The unscouted axes OUTSIDE every scouted one (they come first)."""
    return list(range(min(ks))) if ks else []


def coarse_indices(n: int, step: int) -> np.ndarray:
    """Every `step`-th index of 0..n-1, and ALWAYS the last one -- so the
    scout spans the whole grid and nothing has to be extrapolated
    (0, 3, ..., 27, 29 for 30 points: the last gap is 1)."""
    idx = list(range(0, n, max(1, int(step))))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return np.asarray(idx, dtype=int)


def _pitch(c) -> float:
    c = np.asarray(c, dtype=float)
    return float(np.median(np.abs(np.diff(c)))) if c.size > 1 else 0.0


# ───────────────────────────── validation ───────────────────────────────────

def validate(recipe, registry) -> list[str]:
    """Problems in recipe.scout ([] = fine, or no scout)."""
    block = getattr(recipe, "scout", None)
    if not block:
        return []
    if not isinstance(block, dict):
        return ["scout must be a mapping"]
    errs = []
    unknown = sorted(set(block) - set(KEYS))
    if unknown:
        errs.append(f"scout: unknown key(s) {', '.join(unknown)} "
                    f"(known: {', '.join(KEYS)})")
    spec = spec_of(block)
    if any(isinstance(ax, dict) and ax.get("type") == "fly" for ax in recipe.axes or []):
        errs.append("the scout pass cannot be used in a fly scan: a fly row is one "
                    "continuous move and cannot step over the points it leaves out")
    src = spec["from"]
    picture = is_picture(spec["from_file"])
    if src is not None and not (isinstance(src, str)
                                or (isinstance(src, dict) and isinstance(src.get("file"), str)
                                    and set(src) <= {"file", "detector"})):
        errs.append("scout: `from` is a file name, or {file: scan.nc, detector: name}")
    det = spec["from_detector"] if src else spec["detector"]
    if picture:
        pass                              # an image / a matrix: no detector involved
    elif not isinstance(det, str) or not det:
        errs.append("scout needs `detector`: what the scout reads (e.g. the power "
                    "meter), or the variable to read from the scan file in `from`")
    elif not src:
        errs += detector_problems(registry, det)
    axes = spec["axes"]
    if isinstance(axes, dict):
        for name, step in axes.items():
            if isinstance(step, bool) or not isinstance(step, (int, float)) \
                    or int(step) != step or step < 1:
                errs.append(f"scout: the step of {name!r} must be a whole number "
                            f">= 1 (3 = every 3rd point)")
    elif "step" in block:
        st = block["step"]
        if isinstance(st, bool) or not isinstance(st, (int, float)) \
                or int(st) != st or st < 1:
            errs.append("scout: `step` must be a whole number >= 1 (3 = every 3rd point)")
    if spec["keep"] not in KEEP:
        errs.append("scout: `keep` must be above, below or deviates")
    if spec["keep"] != "deviates":
        errs += _threshold_problems(spec["threshold"])
    k = spec["k"]
    if isinstance(k, bool) or not isinstance(k, (int, float)) or not math.isfinite(k) \
            or k <= 0:
        errs.append("scout: `k` (deviates: how many noise widths) must be a number > 0")
    errs += _margin_problems(spec["margin"], axes)
    if spec["per_outer"] not in PER_OUTER:
        errs.append("scout: `per_outer` must be once or each")
    elif spec["per_outer"] == "each" and src:
        errs.append("scout: `per_outer: each` re-scouts at every step of the outer "
                    "axes, which needs a MEASURED scout -- a file is the same at "
                    "every step (use once)")
    errs += _extent_problems(spec["extent"])
    try:
        compiled = recipe.compile(registry)
        dims = compiled.dims
        ks = scout_dims(spec, dims)
    except ValueError as exc:
        errs.append(str(exc))
        return errs
    except Exception:
        return errs                       # the compile check reports it
    errs += _settings_problems(recipe, registry, spec["settings"], dims)
    if spec["per_outer"] == "each":
        from .repeat import average_index
        avg = average_index(dims)
        if avg is not None and avg in outer_dims(ks):
            errs.append("scout: `per_outer: each` cannot sit inside a repeat that "
                        "AVERAGES: every run would have its own mask, and the "
                        "average would mix them (use mode keep, or per_outer once)")
    if picture and len(ks) != 2:
        errs.append(f"scout: an image or a matrix is 2-D; it needs exactly two "
                    f"scouted axes (X = the first in `axes`, Y = the second), "
                    f"not {len(ks)}")
    elif src:
        if not errs:
            try:
                load_source(spec, dims, ks)
            except (OSError, ValueError, KeyError) as exc:
                errs.append(f"scout file: {exc}")
    if spec["extent"] and not picture:
        errs.append("scout: `extent` places a picture given in `from`; "
                    "without one it has no meaning")
    return errs


def detector_problems(registry, det) -> list[str]:
    """Why `det` cannot be the scout's detector ([] = it can)."""
    g = registry.get(det)
    if g is None:
        return [f"scout detector '{det}' is not known"]
    if not hasattr(g, "get"):
        return [f"scout detector '{det}' cannot be read"]
    if getattr(g, "axes", None):
        return [f"scout detector '{det}' returns an array; the scout needs ONE "
                f"number per point (a reflectivity, a power, a lock-in R)"]
    if getattr(g, "dtype", "float") not in ("float", "int"):
        return [f"scout detector '{det}' is {g.dtype}, not a number"]
    return []


def _threshold_problems(t) -> list[str]:
    if t == "auto":
        return []
    if isinstance(t, dict):
        f = t.get("fraction")
        if set(t) != {"fraction"} or isinstance(f, bool) \
                or not isinstance(f, (int, float)) or not 0 <= f <= 1:
            return ["scout: threshold {fraction: f} needs 0 <= f <= 1 "
                    "(0 = the lowest reading, 1 = the highest)"]
        return []
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t):
        return ["scout: threshold must be auto, a number, or {fraction: f}"]
    return []


def _margin_problems(m, axes) -> list[str]:
    def bad(v):
        return (isinstance(v, bool) or not isinstance(v, (int, float))
                or not math.isfinite(v) or v < 0)
    msg = ["scout: `margin` must be auto, a number of grid points >= 0, or "
           "{axis: points}"]
    if m == "auto":
        return []
    if isinstance(m, dict):
        if any(v != "auto" and bad(v) for v in m.values()):
            return msg
        if isinstance(axes, dict):
            extra = sorted(set(m) - set(axes))
            if extra:
                return [f"scout: margin names {', '.join(extra)}, which is not a "
                        f"scouted axis"]
        return []
    return msg if bad(m) else []


def _extent_problems(e) -> list[str]:
    if e is None:
        return []
    msg = ["scout: extent must be {x: [x0, x1], y: [y0, y1]} (numbers, x0 != x1)"]
    if not isinstance(e, dict) or set(e) - {"x", "y"}:
        return msg
    for v in e.values():
        if not isinstance(v, (list, tuple)) or len(v) != 2 or any(
                isinstance(t, bool) or not isinstance(t, (int, float))
                or not math.isfinite(t) for t in v) or v[0] == v[1]:
            return msg
    return []


def _settings_problems(recipe, registry, settings, dims) -> list[str]:
    """`settings`: settable, numeric, inside the limits -- and NOT an axis.
    An axis is already moved by the scout itself; a scout-only value for it
    would be overwritten at once, or would overwrite the scout's own grid."""
    if not isinstance(settings, dict):
        return ["scout: `settings` must be {parameter: value}"]
    errs = []
    swept = {pid for d in dims for pid, _ in d.params}
    for pid, value in settings.items():
        p = registry.get(pid)
        if p is None:
            errs.append(f"scout setting '{pid}' is not known")
            continue
        if getattr(p, "kind", "") != "settable":
            errs.append(f"scout setting '{pid}' is not settable")
            continue
        if pid in swept:
            errs.append(f"scout setting '{pid}' is an axis of the scan; the scout "
                        f"moves the axes itself")
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            errs.append(f"scout setting '{pid}' = {value!r} is not a number")
            continue
        if not math.isfinite(v):
            errs.append(f"scout setting '{pid}' is not a finite number")
            continue
        lo, hi = getattr(p, "limits", (None, None))
        if lo is not None and not (lo <= v <= hi):
            errs.append(f"scout setting '{pid}' = {v:g} is outside its limits "
                        f"[{lo:g},{hi:g}]")
    return errs


# ───────────────────────────── the mask, computed ───────────────────────────

def otsu(values) -> float:
    """Otsu's threshold: the level that splits the readings into the two
    groups that are each as tight as possible. Made for a two-level picture --
    substrate and elements -- and it needs no number from the operator. 256
    bins over the finite readings."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        raise ValueError("scout: there is no finite reading to set a threshold from")
    lo, hi = float(v.min()), float(v.max())
    if hi <= lo:
        return lo
    hist, edges = np.histogram(v, bins=256, range=(lo, hi))
    centres = 0.5 * (edges[:-1] + edges[1:])
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    m0 = np.cumsum(hist * centres)
    with np.errstate(invalid="ignore", divide="ignore"):
        mu0 = m0 / w0
        mu1 = (m0[-1] - m0) / w1
        between = w0 * w1 * (mu0 - mu1) ** 2
    between = np.nan_to_num(between, nan=-1.0)[:-1]
    # Every cut inside the GAP between the two groups separates them almost
    # equally well: the score is FLAT there. argmax alone picks one of those
    # cuts by the noise of this particular scout, so two scouts of the same
    # area gave 0.0073 and 0.0084 mW and the mask moved by ~20 rim points
    # (lab rig, 2026-10-09). Take the MIDDLE of every cut within OTSU_FLAT of
    # the best score instead: half way across the gap, and the same answer
    # for two scouts that agree to a percent.
    best = np.flatnonzero(between >= between.max() * (1 - OTSU_FLAT))
    return float(0.5 * (edges[best[0] + 1] + edges[best[-1] + 1]))


#: cuts scoring within this fraction of Otsu's best count as "as good" (see otsu)
OTSU_FLAT = 0.01


def background_fraction(values, median, half_width) -> float:
    """The share of the readings that `deviates` calls BACKGROUND
    (|value - median| <= half_width)."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return 1.0
    return float(np.mean(np.abs(v - median) <= half_width))


def threshold_of(spec: dict, values) -> float:
    t = spec["threshold"]
    if t == "auto":
        return otsu(values)
    if isinstance(t, dict):
        v = np.asarray(values, dtype=float)
        v = v[np.isfinite(v)]
        if v.size == 0:
            raise ValueError("scout: there is no finite reading")
        lo, hi = float(v.min()), float(v.max())
        return lo + float(t["fraction"]) * (hi - lo)
    return float(t)


def robust_noise(values) -> tuple[float, float]:
    """(median, noise) of the readings; noise = 1.4826 x MAD. The median and
    the MAD barely notice the few points on a feature, which is what makes
    them the BACKGROUND's level and scatter."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        raise ValueError("scout: there is no finite reading to estimate the noise from")
    med = float(np.median(v))
    return med, MAD_TO_SIGMA * float(np.median(np.abs(v - med)))


def _along(c, V, f, axis):
    """Linear interpolation of V over coordinate c onto f, along `axis`.

    Also returns a bool array (len f): True where f lies outside the range c
    covers -- such a point is not known, and is measured. A single source point
    is just repeated (known only exactly at it).
    """
    c = np.asarray(c, dtype=float)
    f = np.asarray(f, dtype=float)
    # "the same coordinate" up to rounding: a file's 3.0000000001 is 3
    tol = 1e-9 * max(1.0, float(np.ptp(c)), float(np.max(np.abs(c))))
    if c.size == 1:
        outside = np.abs(f - c[0]) > tol
        return np.repeat(V, len(f), axis), outside
    order = np.argsort(c)
    c = c[order]
    V = np.take(V, order, axis=axis)
    fc = np.clip(f, c[0], c[-1])
    j = np.clip(np.searchsorted(c, fc, side="right") - 1, 0, c.size - 2)
    span = c[j + 1] - c[j]
    with np.errstate(invalid="ignore", divide="ignore"):
        w = np.where(span > 0, (fc - c[j]) / np.where(span > 0, span, 1.0), 0.0)
    shape = [1] * V.ndim
    shape[axis] = len(f)
    w = w.reshape(shape)
    out = np.take(V, j, axis=axis) * (1 - w) + np.take(V, j + 1, axis=axis) * w
    # on a source point w is exactly 0, so its value comes through exactly
    outside = (f < c[0] - tol) | (f > c[-1] + tol)
    return out, outside


def interpolate(coarse, V, fine):
    """Multilinear interpolation of the source V[a, b, ...] (on the coordinate
    lists `coarse`) onto the full grid `fine`, one axis after the other.

    Returns (Vf, unknown): `unknown` is True where the value cannot be trusted
    -- it leans on a NaN source reading, or lies outside the area the source
    covers. Those points are always measured.
    """
    V = np.asarray(V, dtype=float)
    nan = ~np.isfinite(V)
    V0 = np.where(nan, 0.0, V)
    W = nan.astype(float)          # interpolated alongside: > 0 = touches a NaN
    outside = np.zeros(tuple(len(f) for f in fine), dtype=bool)
    for ax, (c, f) in enumerate(zip(coarse, fine)):
        V0, out = _along(c, V0, f, ax)
        W, _ = _along(c, W, f, ax)
        shape = [1] * len(fine)
        shape[ax] = len(f)
        outside |= np.asarray(out, bool).reshape(shape)
    unknown = (W > 1e-12) | outside
    return V0, unknown


def grow(keep, radii):
    """Grow the True area of keep[a, b, ...] by `radii` GRID POINTS per axis:
    every point inside the ellipse (sum (offset_k / r_k)^2 <= 1) around a kept
    point is kept too. A radius of 0 does not grow along that axis.

    In grid points, not in the axes' unit: the scouted axes may be anything
    (field in mT and frequency in MHz have no common length), and the
    question the margin answers -- how far between two scout points can the
    real edge be -- is counted in points anyway.
    """
    keep = np.asarray(keep, dtype=bool)
    radii = [max(0.0, float(r)) for r in radii]
    if not keep.any() or all(r <= 0 for r in radii):
        return keep.copy()
    reach = [int(math.floor(r + 1e-9)) for r in radii]
    out = keep.copy()
    shape = keep.shape
    for off in itertools.product(*[range(-q, q + 1) for q in reach]):
        if not any(off):
            continue
        s = sum((o / r) ** 2 for o, r in zip(off, radii) if r > 0)
        if s > 1 + 1e-9:
            continue
        dst, src = [], []
        ok = True
        for o, n in zip(off, shape):
            a0, a1 = max(0, o), min(n, n + o)
            if a1 <= a0:
                ok = False
                break
            dst.append(slice(a0, a1))
            src.append(slice(a0 - o, a1 - o))
        if ok:
            out[tuple(dst)] |= keep[tuple(src)]
    return out


def margin_radii(spec, names, source_steps) -> list[float]:
    """The margin per scouted axis, in grid points. `source_steps` = how many
    fine grid points one step of the SOURCE spans on each axis (the scout's
    step, or a file's pixel pitch over the scan's): auto = half of it."""
    m = spec["margin"]
    out = []
    for name, s in zip(names, source_steps):
        v = m.get(name, "auto") if isinstance(m, dict) else m
        out.append(0.5 * float(s) if v == "auto" else float(v or 0.0))
    return out


@dataclass
class MaskResult:
    keep: np.ndarray          # bool on the FINE grid of the scouted axes: True = measure
    threshold: float          # above/below: the level; deviates: k x noise
    fine_values: np.ndarray   # the interpolated source (NaN = unknown)
    unknown: np.ndarray       # bool: kept because the mask could not tell
    info: dict = field(default_factory=dict)   # deviates: median, noise


def decide(spec, source, fine_values):
    """(inside, threshold, info): where the interpolated reading says
    "something is happening"."""
    keep = spec["keep"]
    if keep == "deviates":
        med, noise = robust_noise(source)
        thr = float(spec["k"]) * noise
        with np.errstate(invalid="ignore"):
            inside = np.abs(fine_values - med) > thr
        # `deviates` assumes the BACKGROUND is most of what the scout saw: the
        # median sits in it and its scatter sets the noise. On the rig
        # (2026-10-09) dark film was just over half the scout -- it worked,
        # but a bit more bright area would have flipped the median and the
        # scan would have measured the dark film. Say so when it is close.
        return inside, thr, {"median": med, "noise": noise,
                             "background": background_fraction(source, med, thr)}
    thr = threshold_of(spec, source)
    with np.errstate(invalid="ignore"):
        inside = fine_values > thr if keep == "above" else fine_values < thr
    return inside, thr, {}


def build(spec, coarse, V, fine, radii) -> MaskResult:
    """The whole recipe: interpolate, decide, grow, keep the unknown."""
    Vf, unknown = interpolate(coarse, V, fine)
    inside, thr, info = decide(spec, V, Vf)
    inside &= ~unknown
    keep = grow(inside, radii) | unknown
    return MaskResult(keep=keep, threshold=thr,
                      fine_values=np.where(unknown, np.nan, Vf), unknown=unknown,
                      info=info)


# ───────────────────────────── from a file ──────────────────────────────────

def load_scan(path, detector, dims):
    """(coarse coordinate list, V) from an earlier scan's .nc, on `dims` (the
    scouted ones, in the scan's order).

    Each axis is found in the file by the PARAMETER it swept (the `param`
    attribute every coordinate carries) or by its NAME, so the source can be
    any scan of the same axes -- coarse or fine, a different range.
    """
    import xarray as xr
    p = Path(str(path))
    if not p.is_file():
        raise ValueError(f"{p} does not exist")
    with xr.open_dataset(p, engine="h5netcdf") as ds:
        name = detector if detector in ds.data_vars else f"{detector}_measured"
        if name not in ds.data_vars:
            raise ValueError(f"{p.name} has no variable '{detector}'")
        da = ds[name]
        want = []
        for d in dims:
            pid = d.params[0][0]
            hit = [n for n in da.dims if n in ds.coords
                   and (ds[n].attrs.get("param") == pid or n == d.name)]
            if not hit:
                swept = [f"{n} ({ds[n].attrs.get('param', '?')})" for n in da.dims
                         if n in ds.coords]
                raise ValueError(
                    f"{p.name} was not measured along '{d.name}' / '{pid}' (its axes: "
                    f"{', '.join(swept) or 'none'}). A mask can only be used in the "
                    f"coordinates it was measured in -- camera and stage "
                    f"coordinates differ by the stage's drift.")
            want.append(hit[0])
        extra = [n for n in da.dims if n not in want and da.sizes[n] > 1]
        if extra:
            raise ValueError(f"'{name}' in {p.name} has more dimensions than the "
                             f"scouted axes ({', '.join(extra)}); use a file with "
                             f"just those")
        da = da.squeeze(drop=True).transpose(*want)
        return ([np.asarray(ds[w].values, dtype=float) for w in want],
                np.asarray(da.values, dtype=float))


def read_picture(path) -> np.ndarray:
    """An image or a matrix file as a float array [row, column]."""
    p = Path(str(path))
    if not p.is_file():
        raise ValueError(f"{p} does not exist")
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        try:
            from PIL import Image
        except ImportError:                       # pragma: no cover
            raise ValueError("reading an image needs Pillow "
                             "(it comes with: uv sync --extra gui)")
        with Image.open(p) as im:
            if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
                arr = np.asarray(im, dtype=float)              # 16-bit / float
            else:
                arr = np.asarray(im.convert("L"), dtype=float)  # brightness 0..255
    elif ext == ".npy":
        arr = np.asarray(np.load(p, allow_pickle=False), dtype=float)
    else:
        arr = np.loadtxt(p, delimiter="," if ext == ".csv" else None,
                         dtype=float, ndmin=2, encoding="utf-8")
    if arr.ndim != 2 or min(arr.shape) < 1:
        raise ValueError(f"{p.name} is not a 2-D picture or matrix "
                         f"(shape {arr.shape})")
    return arr


def picture_xy(spec, dims) -> tuple[int, int]:
    """(kx, ky): the dims a picture's columns (X) and rows (Y) run along --
    the first and the second axis named in `axes`."""
    names = [d.name for d in dims]
    a = list(spec["axes"])
    return names.index(a[0]), names.index(a[1])


def load_source(spec, dims, ks):
    """(coarse coordinate list, V) from the file in `from`, in the order of
    `ks`: a scan file through load_scan, a picture placed by `extent`
    (default: exactly over the scan's area, its first row at the START of Y)."""
    src = spec["from_file"]
    if not is_picture(src):
        return load_scan(src, spec["from_detector"], [dims[k] for k in ks])
    img = read_picture(src)                        # [row = Y, column = X]
    kx, ky = picture_xy(spec, dims)
    ext = spec.get("extent") or {}
    cx = np.asarray(dims[kx].coord, dtype=float)
    cy = np.asarray(dims[ky].coord, dtype=float)
    x0, x1 = ext.get("x") or (cx[0], cx[-1])
    y0, y1 = ext.get("y") or (cy[0], cy[-1])
    ny, nx = img.shape
    xs = np.linspace(float(x0), float(x1), nx) if nx > 1 else np.array([float(x0)])
    ys = np.linspace(float(y0), float(y1), ny) if ny > 1 else np.array([float(y0)])
    if kx < ky:                                    # X is the OUTER dim
        return [xs, ys], img.T
    return [ys, xs], img


# ───────────────────────────── the visiting record ──────────────────────────

@dataclass
class Visit:
    """Which points of the scan are measured, in VISITING order.

    measured[flat] -- True = this point is measured (flat = the n-th point
                      visited, zig-zag already applied)
    before[flat]   -- how many measured points come before it
    The hooks use it so that "at the start of each row" means the first
    MEASURED point of the row, and "every n points" counts measured points.
    With `per_outer: each` the blocks not scouted yet count as measured
    (unknown = measured) until their scout has run.
    """
    measured: np.ndarray
    before: np.ndarray

    @property
    def n_measured(self) -> int:
        return int(self.measured.sum())

    def any_in(self, lo: int, hi: int) -> bool:
        """Is any point in [lo, hi) of the visiting order measured?"""
        lo, hi = max(0, lo), min(len(self.measured), hi)
        if hi <= lo:
            return False
        end = self.before[hi - 1] + int(self.measured[hi - 1])
        return bool(end - self.before[lo] > 0)


def visiting_order(shape, zigzag: bool) -> np.ndarray:
    """The flat (C-order) index of the n-th point VISITED -- the engine's
    odometer, zig-zag included, for every point at once."""
    shape = tuple(int(s) for s in shape)
    total = int(np.prod(shape)) if shape else 0
    idx = np.array(np.unravel_index(np.arange(total), shape))   # [ndim, total]
    if zigzag:
        out = idx.copy()
        for k in range(1, len(shape)):
            flip = idx[:k].sum(axis=0) % 2 == 1
            out[k] = np.where(flip, shape[k] - 1 - idx[k], idx[k])
        idx = out
    return np.ravel_multi_index(tuple(idx), shape) if total else np.zeros(0, int)


def visit_of(keep_full, order) -> Visit:
    """The visiting record of a mask given on the FULL scan grid."""
    measured = np.asarray(keep_full, dtype=bool).ravel()[order]
    before = np.concatenate([[0], np.cumsum(measured)[:-1]]).astype(int) \
        if measured.size else np.zeros(0, int)
    return Visit(measured=measured, before=before)


# ───────────────────────────── the engine's runner ──────────────────────────

class ScoutRunner:
    """The scout pass inside a running scan (engine._sweep calls it).

    One per run. `due(idx)` says whether the point `idx` starts a block that
    has not been scouted yet (the whole scan with `per_outer: once`; one block
    per value of the outer axes with `each`); `run_block` then scouts it --
    or loads the file -- builds the mask, and updates `ctx["visit"]` and the
    file's variables. The engine has already moved the OUTER axes to the
    block's values (with their routines) when it calls run_block.
    """

    def __init__(self, recipe, registry, compiled, ctx, should_abort):
        self.recipe, self.registry, self.ctx = recipe, registry, ctx
        self.should_abort = should_abort
        self.spec = spec = spec_of(recipe.scout)
        self.dims = dims = compiled.dims
        self.shape = tuple(compiled.shape)
        self.ks = scout_dims(spec, dims)
        self.names = [dims[k].name for k in self.ks]
        self.outer = outer_dims(self.ks)
        self.each = spec["per_outer"] == "each" and bool(self.outer)
        self.picture = is_picture(spec["from_file"])
        self.det = ("file" if self.picture
                    else (spec["from_detector"] if spec["from"] else spec["detector"]))
        self.steps = [int(spec["axes"][n]) for n in self.names]
        self.fine = [np.asarray(dims[k].coord, float) for k in self.ks]
        self.coarse_idx = [coarse_indices(dims[k].size, s)
                           for k, s in zip(self.ks, self.steps)]
        self.order = visiting_order(self.shape, bool(getattr(recipe, "zigzag", False)))
        self.done_blocks: set = set()
        self.scout_s = 0.0               # seconds spent scouting (not in the per-point ETA)
        self.kept = 0                    # measured points decided so far (scouted blocks)
        self.decided = 0                 # points decided so far
        # Every point is measured until a scout says otherwise (conservative;
        # with `each` the blocks not scouted yet stay that way until theirs).
        self.keep_full = np.ones(self.shape, dtype=bool)
        ctx["visit"] = visit_of(self.keep_full, self.order)
        self._source = None              # a file's (coarse, V), read once
        self._setup_file_vars()

    # ---- the file's variables -------------------------------------------------
    def _unit(self, pid) -> str:
        p = self.registry.get(pid)
        return getattr(p, "unit", "") if p is not None else ""

    def _setup_file_vars(self):
        """Allocate `mask_<det>` (+ its coarse axes) and `scan_mask` now, so
        every dataset built from here on -- the live snapshots during the
        scout, an abort, the end -- carries them."""
        ctx, dims = self.ctx, self.dims
        spec = self.spec
        if spec["from"]:
            coarse, V = self._load()
            self.coarse_coords = coarse
        else:
            self.coarse_coords = [f[i] for f, i in zip(self.fine, self.coarse_idx)]
        cshape = tuple(len(c) for c in self.coarse_coords)
        lead = [dims[k] for k in self.outer] if self.each else []
        lead_shape = tuple(d.size for d in lead)
        self.source = np.full(lead_shape + cshape, np.nan)
        self.mask_store = np.ones(lead_shape + tuple(len(f) for f in self.fine),
                                  dtype=np.int8)
        self.thr_store = np.full(lead_shape, np.nan) if self.each else None
        cnames = [f"mask_{n}" for n in self.names]
        extra = ctx.setdefault("ds_extra", {})
        coords = extra.setdefault("coords", {})
        for k, cn, c in zip(self.ks, cnames, self.coarse_coords):
            d = dims[k]
            coords[cn] = (cn, np.asarray(c, float),
                          {"units": self._unit(d.params[0][0]), "param": d.params[0][0],
                           "long_name": f"{d.name}, mask source"})
        g = None if self.picture else self.registry.get(self.det)
        unit = getattr(g, "unit", "") if g is not None else ""
        src_text = (f"the mask file {Path(spec['from_file']).name}" if spec["from"]
                    else "the scout pass")
        lead_names = [d.name for d in lead]
        vars_ = extra.setdefault("vars", {})
        vars_[f"mask_{self.det}"] = (lead_names + cnames, self.source, {
            "units": unit, "long_name": f"{self.det}, {src_text}"})
        vars_["scan_mask"] = (lead_names + self.names, self.mask_store, {
            "long_name": "1 = measured, 0 = left out by the scout's mask (stored as NaN)",
            "aaltoflow_type": "bool"})
        if self.each:
            vars_["mask_threshold"] = (lead_names, self.thr_store, {
                "long_name": "the scout's threshold at each step of the outer axes "
                             "(deviates: k x noise)", "units": unit})
        text = json.dumps(self.recipe.scout)
        ctx["ds_attrs"].update(
            mask_json=text, scout_json=text,
            mask_source=str(spec["from"]) if spec["from"] else "measured (scout pass)",
            scout_per_outer=spec["per_outer"])

    def _load(self):
        if self._source is None:
            self._source = load_source(self.spec, self.dims, self.ks)
        return self._source

    # ---- blocks ----------------------------------------------------------------
    def block_of(self, idx) -> tuple:
        return tuple(int(idx[k]) for k in self.outer) if self.each else ()

    def due(self, idx) -> bool:
        return self.block_of(idx) not in self.done_blocks

    def n_blocks(self) -> int:
        return int(np.prod([self.dims[k].size for k in self.outer])) if self.each else 1

    def points_per_scout(self) -> int:
        return 0 if self.spec["from"] else int(np.prod([len(i) for i in self.coarse_idx]))

    def run_block(self, idx, current, guard, live=None, on_scout=None):
        """Scout the block that `idx` starts (or load the file), make its mask,
        update the visit record. Returns the dims the scout MOVED (they must
        be set again before the next point is measured)."""
        block = self.block_of(idx)
        self.done_blocks.add(block)
        log = self.ctx["log_fn"]
        t0 = time.monotonic()
        moved: list[int] = []
        where = ("" if not self.each else " at " + ", ".join(
            f"{self.dims[k].name} = {self.dims[k].coord[idx[k]]:g}" for k in self.outer))
        if self.spec["from"]:
            coarse, V = self._load()
            log(f"scout from {Path(self.spec['from_file']).name}: "
                f"{' x '.join(str(s) for s in V.shape)} points")
        else:
            coarse = self.coarse_coords
            target = self.source[block] if self.each else self.source
            moved = self._measure(idx, current, guard, target, live, on_scout, where)
            V = target
        res = build(self.spec, coarse, V, self.fine,
                    margin_radii(self.spec, self.names, self._source_steps(coarse)))
        self.scout_s += time.monotonic() - t0
        self._commit(block, res, coarse, where, on_scout)
        return moved

    def _source_steps(self, coarse) -> list[float]:
        """How many fine points one step of the source spans, per axis."""
        if not self.spec["from"]:
            return [float(s) for s in self.steps]
        out = []
        for c, f in zip(coarse, self.fine):
            pf = _pitch(f)
            out.append(_pitch(c) / pf if pf > 0 else 0.0)
        return out

    def _commit(self, block, res, coarse, where, on_scout):
        ctx, spec = self.ctx, self.spec
        keep = res.keep
        n_keep, n_all = int(keep.sum()), int(keep.size)
        if self.each:
            self.mask_store[block] = keep.astype(np.int8)
            self.thr_store[block] = res.threshold
        else:
            self.mask_store[...] = keep.astype(np.int8)
        # broadcast the block's mask over the full grid: the outer axes at
        # this block's values (all of them with once), every other unscouted
        # axis along for the ride
        full_idx = [slice(None)] * len(self.shape)
        for k, b in zip(self.outer if self.each else [], block):
            full_idx[k] = b
        sub_shape = [1] * len(self.shape)
        for k, n in zip(self.ks, keep.shape):
            sub_shape[k] = n
        view = self.keep_full[tuple(full_idx)]
        # `view` has the shape of the dims left after indexing the outer ones
        rest = [k for k in range(len(self.shape))
                if not (self.each and k in self.outer)]
        bshape = [sub_shape[k] for k in rest]
        view[...] = keep.reshape(bshape)
        ctx["visit"] = visit_of(self.keep_full, self.order)
        self.kept += n_keep
        self.decided += n_all
        unit = ""
        if not self.picture:
            unit = self._unit_of_det()
        how = (f"|value - {res.info['median']:.6g}| > {res.threshold:.6g} {unit}"
               f" ({spec['k']:g} x noise {res.info['noise']:.3g})"
               if spec["keep"] == "deviates"
               else f"threshold {res.threshold:.6g}{(' ' + unit) if unit else ''}, keep {spec['keep']}")
        bg = res.info.get("background")
        if bg is not None and bg < 0.6:
            ctx["log_fn"](f"scout{where}: WARNING -- only {100 * bg:.0f} % of the scout looks "
                          f"like background; `deviates` assumes it is most of it. If the "
                          f"mask picked the wrong side, use keep above or below")
        radii = margin_radii(spec, self.names, self._source_steps(coarse))
        ctx["log_fn"](f"scout{where}: {how}, margin "
                      f"{', '.join(f'{r:g}' for r in radii)} pts -> {n_keep} of {n_all} "
                      f"points measured ({100.0 * n_keep / max(1, n_all):.0f} %)")
        attrs = ctx["ds_attrs"]
        attrs["mask_points"] = f"{self.kept} of {self.decided}"
        if not self.each:
            attrs["mask_threshold"] = float(res.threshold)
        # what mask_threshold MEANS: a level (above / below) or, for deviates,
        # the half-width around the median -- a reader must not compare the
        # two (lab, 2026-10-09)
        attrs["mask_threshold_kind"] = "deviation" if spec["keep"] == "deviates" else "level"
        if res.info:
            attrs["mask_median"] = float(res.info["median"])
            attrs["mask_noise"] = float(res.info["noise"])
            if not self.each:
                attrs["mask_deviation"] = float(res.threshold)
        attrs["mask_margin_points"] = json.dumps(dict(zip(self.names, radii)))
        length = self._margin_length(radii)
        if length is not None:
            attrs["mask_margin"] = length
        if on_scout is not None:
            on_scout({"phase": "made", "where": where.strip(),
                      "variable": f"mask_{self.det}",
                      "threshold": float(res.threshold), "keep": spec["keep"],
                      "unit": unit, "kept": n_keep, "of": n_all,
                      "measured": int(ctx["visit"].n_measured),
                      "total": int(len(self.order))})
        if n_keep == 0:
            if not self.each:
                raise ValueError(
                    "the scout's mask left no point to measure: check `keep` (above / "
                    "below / deviates) and the threshold against the scout's readings "
                    "in the file")
            ctx["log_fn"](f"scout{where}: nothing to measure here -- this block is "
                          f"skipped")

    def _unit_of_det(self) -> str:
        g = self.registry.get(self.det)
        return getattr(g, "unit", "") if g is not None else ""

    def _margin_length(self, radii):
        """The margin as ONE length in the axes' unit (the old `mask_margin`),
        when every scouted axis shares the unit and the length; else None."""
        units = {self._unit(self.dims[k].params[0][0]) for k in self.ks}
        lengths = [r * _pitch(f) for r, f in zip(radii, self.fine)]
        if len(units) == 1 and lengths and np.allclose(lengths, lengths[0], rtol=1e-6):
            return float(lengths[0])
        return None

    # ---- the scout, measured -----------------------------------------------------
    def _measure(self, idx, current, guard, V, live, on_scout, where):
        """Read the scout's detector at every coarse point of the scouted axes,
        into V (shape = the coarse grid). Outer axes stay where the engine put
        them; inner unscouted axes go to their FIRST value. The scout-only
        `settings` hold during the pass and are put back afterwards -- also
        when it is aborted or fails. Returns the dims it moved."""
        from .engine import _hold_for_operator, _zigzag
        from .errors import ScanAborted
        registry, recipe, dims = self.registry, self.recipe, self.dims
        log = self.ctx["log_fn"]
        g = registry.get(self.det)
        acq = getattr(g, "acquire", None)
        cshape = tuple(len(i) for i in self.coarse_idx)
        total = int(np.prod(cshape))
        log(f"scout{where}: {self.det} at {' x '.join(str(n) for n in cshape)} points "
            f"(every {', '.join(f'{s}.' for s in self.steps)} of "
            f"{' x '.join(str(dims[k].size) for k in self.ks)})")
        moved = []
        restore = self._apply_settings(current)
        ok = False
        try:
            # the inner unscouted axes: at their FIRST value for the scout
            for k, d in enumerate(dims):
                if k in self.ks or k in self.outer or not d.params:
                    continue
                for pid, values in d.params:
                    current[pid] = registry.get(pid).set(float(values[0]))
                moved.append(k)
            moved += self.ks
            last = [None] * len(self.ks)
            t_start = time.monotonic()
            for flat in range(total):
                ci = np.unravel_index(flat, cshape)
                if getattr(recipe, "zigzag", False):
                    ci = _zigzag(tuple(int(c) for c in ci), cshape)
                fi = [int(self.coarse_idx[j][c]) for j, c in enumerate(ci)]
                label = f"scout point {flat + 1}/{total}"
                if self.should_abort and self.should_abort():
                    raise ScanAborted("aborted during the scout pass")
                if _hold_for_operator(self.ctx, self.should_abort, label):
                    raise ScanAborted("aborted during the scout pass")
                redo = False
                while True:
                    self._move(fi, last, redo, current)
                    if acq is not None:
                        acq.trigger()          # a FRESH reading, as in the scan
                        acq.wait()
                    # the fault check before and after the read, as for a point
                    faults = guard.faults()
                    if not faults:
                        value = g.get()
                        faults = guard.faults()
                    if faults:
                        guard.hold(faults, label)
                        redo = True
                        continue
                    break
                try:
                    V[tuple(int(c) for c in ci)] = float(value)
                except (TypeError, ValueError):
                    V[tuple(int(c) for c in ci)] = np.nan   # unknown -> measured
                if live is not None:
                    live()
                if on_scout is not None:
                    el = time.monotonic() - t_start
                    on_scout({"phase": "scout", "where": where.strip(),
                              "variable": f"mask_{self.det}",
                              "done": flat + 1, "total": total,
                              "eta_s": el / (flat + 1) * (total - flat - 1)})
            ok = True
        finally:
            self._restore_settings(restore, ok)
        return sorted(set(moved))

    def _move(self, fi, last, redo, current):
        """Move the scouted axes to the fine indices `fi`, only those that
        changed (all of them on a redo). With the recipe's `diagonal` and
        knobs that can split send/wait, several changing axes move together
        (no settle at the corner of a row change)."""
        registry, dims = self.registry, self.dims
        todo = [(j, dims[k], fi[j]) for j, k in enumerate(self.ks)
                if redo or last[j] != fi[j]]
        knobs = [registry.get(pid) for _, d, _ in todo for pid, _ in d.params]
        if (getattr(self.recipe, "diagonal", False) and len(todo) >= 2
                and all(getattr(p, "can_send", False) for p in knobs)):
            waits = []
            for j, d, n in todo:
                for pid, values in d.params:
                    current[pid], w = registry.get(pid).send(float(values[n]))
                    waits.append(w)
            for w in waits:
                w()
        else:
            for j, d, n in todo:
                for pid, values in d.params:
                    current[pid] = registry.get(pid).set(float(values[n]))
        for j, _, n in todo:
            last[j] = n

    def _apply_settings(self, current) -> list:
        """Set the scout-only `settings`; return [(pid, old value)] to put back.
        The old value is what the engine set (a condition), else what the
        instrument reports now. Read BEFORE anything is changed: a value that
        cannot be read cannot be put back, so the scout refuses to start."""
        restore = []
        settings = self.spec["settings"]
        if not settings:
            return restore
        log = self.ctx["log_fn"]
        for pid in settings:
            if pid in current:
                restore.append((pid, current[pid]))
                continue
            try:
                restore.append((pid, float(self.registry.get(pid).get())))
            except Exception as exc:
                raise ValueError(f"scout setting '{pid}': cannot read its present "
                                 f"value, so it could not be put back after the "
                                 f"scout ({exc})") from exc
        done = []
        try:
            for pid, value in settings.items():
                self.registry.get(pid).set(float(value))
                done.append(pid)
                log(f"scout: {pid} = {float(value):g} for the scout only")
        except BaseException:
            self._restore_settings([r for r in restore if r[0] in done], False)
            raise
        return restore

    def _restore_settings(self, restore, ok: bool):
        """Put the scout-only settings back. After a clean scout a failure here
        is an ERROR (the real scan must not run with the scout's lock-in time
        constant). After an Abort or an error every value is still SENT, and a
        failing one is only logged, so the original problem surfaces."""
        log = self.ctx["log_fn"]
        for pid, old in restore:
            try:
                self.registry.get(pid).set(float(old))
                log(f"scout: {pid} back to {float(old):g}")
            except Exception as exc:
                if ok:
                    raise
                log(f"scout: {pid} back to {float(old):g} -- sent, not confirmed ({exc})")

    # ---- the ETA -------------------------------------------------------------------
    def remaining(self, flat) -> tuple[float, int]:
        """(measured points still to come -- an estimate for blocks not
        scouted yet, from the fraction kept so far --, scouts still to come)."""
        visit = self.ctx["visit"]
        left = visit.n_measured - (int(visit.before[flat]) + int(visit.measured[flat]))
        blocks_left = self.n_blocks() - len(self.done_blocks)
        if blocks_left <= 0 or not self.decided:
            return float(left), max(0, blocks_left)
        # the blocks not scouted yet are counted as all-measured in `visit`;
        # replace that by the fraction the scouts so far kept
        per_block = int(np.prod(self.shape)) // max(1, self.n_blocks())
        frac = self.kept / self.decided
        return float(left - blocks_left * per_block * (1 - frac)), blocks_left


# ───────────────────────────── for the builder ──────────────────────────────

def estimate(recipe) -> dict | None:
    """What the builder can say BEFORE the run: the scout's own points and
    how many scouts there will be. None without a scout (or when the recipe
    does not compile)."""
    block = getattr(recipe, "scout", None)
    if not block:
        return None
    try:
        spec = spec_of(block)
        dims = recipe.compile(None).dims
        ks = scout_dims(spec, dims)
    except Exception:
        return None
    names = [dims[k].name for k in ks]
    coarse = {n: coarse_indices(dims[k].size, int(spec["axes"][n])).tolist()
              for n, k in zip(names, ks)}
    outer = outer_dims(ks)
    blocks = (int(np.prod([dims[k].size for k in outer]))
              if spec["per_outer"] == "each" and outer else 1)
    per = 0 if spec["from"] else int(np.prod([len(c) for c in coarse.values()]))
    return {"names": names, "coarse": coarse, "sizes": [dims[k].size for k in ks],
            "points": per, "blocks": blocks, "from": spec["from_file"],
            "outer": [dims[k].name for k in outer],
            "inner": [d.name for k, d in enumerate(dims)
                      if k not in ks and k not in outer]}
