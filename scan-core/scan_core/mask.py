"""
mask.py -- measure only the interesting part of an XY map (2026-10-07).

Lukas: in an XY scan of a patterned sample, much of the area is nonmagnetic
substrate, and measuring it as carefully as the magnetic elements wastes most
of the night -- "but we still need to make the scan", i.e. the result must
still be a complete 2-D matrix. His recipe for it:

  1. a QUICK pass that looks only at the REFLECTIVITY, at every 3rd point of
     the final grid (1/9 of the points);
  2. a MASK from it -- the reflectivity interpolated onto the fine grid, then
     a threshold (elements reflect differently from the substrate), then grown
     by a margin so the edges of every element are measured too;
  3. the real scan, measuring only the points inside the mask.

The points left out are never VISITED: the stage goes straight from one
masked-in point to the next, so neither the travel nor the settle is paid for
them (a `skip_if` routine decides only after the axes have moved). In the file
they are "not measured" (NaN), the grid keeps its full shape, and `scan_mask`
says which points are which -- a NaN the mask left out is never confused with
an aborted scan.

The recipe block (all keys optional except `detector` for a measured pass):

    mask:
      detector: pm16.power   # what pass 1 records (a scalar number)
      axes: [pos_x, pos_y]   # the X and the Y axis; default = the raster's
      step: 3                # pass 1 at every 3rd point of the fine grid
      keep: above            # the elements are ABOVE the threshold (or below)
      threshold: auto        # auto (Otsu) | a number | {fraction: 0.5}
      margin: auto           # grow the mask by this much, in the axes' unit;
                             # auto = half the pitch of pass 1 (or of the file)
      from: mask.png         # use a file instead of measuring pass 1 (below)
      extent: {x: [-50, 50], y: [-40, 40]}   # where a picture lies

A MASK YOU MAKE YOURSELF (Lukas: "the format needs to be transparent so it
can be created from anything, e.g. a grayscale image"). `from` takes:

  * an IMAGE -- .png .tif .tiff .bmp .jpg .jpeg -- grayscale, or colour (read
    as its brightness; transparency is ignored); 8- or 16-bit.
  * a plain number MATRIX -- .csv (comma-separated) or .txt / .dat (spaces or
    tabs), one row per line. 1 / 0 works, so does any reading.
  * .npy, a numpy 2-D array.
  * .nc, an earlier AaltoFlow scan (pass 1 of another run, a reflectivity map);
    `detector` then names the variable to read.

  For an image or a matrix: COLUMNS run along X and ROWS along Y. Bright
  (a large number) = MEASURE by default (`keep: above`; `keep: below` turns it
  round). The threshold is `auto` unless given, which for a black-and-white
  picture lands half way between the two. The picture does not need the scan's
  resolution: it is interpolated onto the scan's grid exactly like pass 1.

  WHERE it lies: `extent: {x: [x0, x1], y: [y0, y1]}` = the X of the centre of
  the FIRST and LAST column, the Y of the first and last ROW (row 0 = the top
  of an image), in the axes' own unit. Give y as [y1, y0] to flip it. Without
  `extent` the picture spans exactly the scan's area, first row at the start
  of the Y axis -- so a map exported from a scan (row index = Y index) lines
  up without a number typed. Points of the scan outside the picture are
  MEASURED.

COORDINATES. The mask is matched point to point by the VALUE of the axis
parameters, never by array index, so it works in whatever frame the scan is
in: the camera's (camera.laser_x/y, the sample measured from the template --
immune to an open-loop stage's drift) or the stage's absolute um
(kim.position_x/y). A mask from a scan file must have been measured in the
SAME parameters: a mask in stage um laid over a scan in camera coordinates
would be off by the stage's drift, and there is no honest way to convert one
into the other, so that is refused. (A picture has no parameters; its
`extent` says where it is, in whatever frame the scan uses.)

WHY INTERPOLATE BEFORE THE THRESHOLD. Thresholding the coarse map first and
then upsampling the yes/no answer gives a mask made of 3x3 blocks, with every
edge on the coarse grid. Interpolating the reflectivity first puts the edge
where the signal actually crosses the threshold, between the coarse points.
The margin then covers what interpolation cannot know: an element smaller than
about one coarse pitch can fall between the coarse points entirely -- use a
finer pass 1 (a smaller step) for a sample like that.

CONSERVATIVE BY DESIGN. Whenever the mask cannot tell -- a coarse point that
read NaN, a scan point outside the area a file covers -- the point is
MEASURED. Measuring a point too many costs a few seconds; leaving out a
magnetic one costs the measurement.

Pure numpy here except `measure_pass`, which drives the registry the same way
the engine does (fault check, Abort, the operator's Pause).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

KEYS = ("detector", "axes", "step", "keep", "threshold", "margin", "from",
        "extent")
KEEP = ("above", "below")
DEFAULT_STEP = 3
#: files `from` reads as a picture or a matrix (anything else: a .nc scan)
IMAGE_EXT = (".png", ".tif", ".tiff", ".bmp", ".jpg", ".jpeg")
MATRIX_EXT = (".csv", ".txt", ".dat", ".npy")


# ───────────────────────────── the block, read ──────────────────────────────

def spec_of(block: dict) -> dict:
    """The block with every default filled in."""
    b = dict(block or {})
    return {"detector": b.get("detector"),
            "axes": b.get("axes"),
            "step": b.get("step", DEFAULT_STEP),
            "keep": b.get("keep") or "above",
            "threshold": b.get("threshold", "auto"),
            "margin": b.get("margin", "auto"),
            "from": b.get("from") or None,
            "extent": b.get("extent") or None}


def is_picture(path) -> bool:
    """An image or a plain matrix (not a scan file)."""
    return bool(path) and Path(str(path)).suffix.lower() in IMAGE_EXT + MATRIX_EXT


def mask_dims(recipe, dims) -> tuple[int, int, int, int]:
    """(outer, inner, X, Y): indices into `dims` of the two masked dims.

    `axes` is written [X, Y] (a picture's columns run along X, its rows along
    Y). Default: the recipe's raster axis. Raises ValueError with a message
    for the operator otherwise.
    """
    spec = spec_of(recipe.mask)
    names = [d.name for d in dims]
    axes = spec["axes"]
    if axes is None:
        rasters = [ax for ax in recipe.axes or []
                   if isinstance(ax, dict) and ax.get("type") == "raster"]
        if len(rasters) != 1:
            raise ValueError("mask: say which axes are X and Y (`axes: [x, y]`); "
                             "only a scan with exactly one raster axis finds them "
                             "on its own")
        r = rasters[0]
        axes = [r["x"].get("name") or r["x"]["param"],
                r["y"].get("name") or r["y"]["param"]]
    if not isinstance(axes, (list, tuple)) or len(axes) != 2 or axes[0] == axes[1]:
        raise ValueError("mask: `axes` must name two different axes, [x, y]")
    for a in axes:
        if a not in names:
            raise ValueError(f"mask: there is no axis {a!r} in this scan "
                             f"(axes: {', '.join(names)})")
    kx, ky = names.index(axes[0]), names.index(axes[1])
    ka, kb = sorted((kx, ky))
    for k in (ka, kb):
        d = dims[k]
        if not d.params or d.kind in ("repeat", "fly"):
            raise ValueError(f"mask: axis {d.name!r} is a {d.kind} axis; the mask "
                             f"needs two axes that MOVE something (X and Y)")
    return ka, kb, kx, ky


def coarse_indices(n: int, step: int) -> np.ndarray:
    """Every `step`-th index of 0..n-1, and always the last one -- so pass 1
    spans the whole fine grid and nothing has to be extrapolated."""
    idx = list(range(0, n, max(1, int(step))))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return np.asarray(idx, dtype=int)


# ───────────────────────────── validation ───────────────────────────────────

def validate(recipe, registry) -> list[str]:
    """Problems in recipe.mask ([] = fine, or no mask)."""
    block = getattr(recipe, "mask", None)
    if not block:
        return []
    if not isinstance(block, dict):
        return ["mask must be a mapping"]
    errs = []
    unknown = sorted(set(block) - set(KEYS))
    if unknown:
        errs.append(f"mask: unknown key(s) {', '.join(unknown)} "
                    f"(known: {', '.join(KEYS)})")
    spec = spec_of(block)
    if any(isinstance(ax, dict) and ax.get("type") == "fly" for ax in recipe.axes or []):
        errs.append("mask cannot be used in a fly scan: a row is one continuous "
                    "move and cannot step over the points it leaves out")
    det = spec["detector"]
    if is_picture(spec["from"]):
        pass                              # an image / a matrix: no detector involved
    elif not isinstance(det, str) or not det:
        errs.append("mask needs `detector`: what pass 1 records (e.g. the power "
                    "meter), or the variable to read from the .nc in `from`")
    elif not spec["from"]:
        g = registry.get(det)
        if g is None:
            errs.append(f"mask detector '{det}' is not known")
        elif not hasattr(g, "get"):
            errs.append(f"mask detector '{det}' cannot be read")
        elif getattr(g, "axes", None):
            errs.append(f"mask detector '{det}' returns an array; the mask needs "
                        f"ONE number per point (a reflectivity, a power)")
        elif getattr(g, "dtype", "float") not in ("float", "int"):
            errs.append(f"mask detector '{det}' is {g.dtype}, not a number")
    step = spec["step"]
    if isinstance(step, bool) or not isinstance(step, (int, float)) \
            or int(step) != step or step < 1:
        errs.append("mask: `step` must be a whole number >= 1 (3 = every 3rd point)")
    if spec["keep"] not in KEEP:
        errs.append("mask: `keep` must be above or below")
    errs += _threshold_problems(spec["threshold"])
    errs += _extent_problems(spec["extent"])
    m = spec["margin"]
    if m != "auto" and (isinstance(m, bool) or not isinstance(m, (int, float))
                        or not math.isfinite(m) or m < 0):
        errs.append("mask: `margin` must be auto or a number >= 0 (in the axes' unit)")
    try:
        dims = recipe.compile(registry).dims
        ks = mask_dims(recipe, dims)
    except ValueError as exc:
        errs.append(str(exc))
        return errs
    except Exception:
        return errs                       # the compile check reports it
    if spec["from"]:
        if not errs:
            try:
                load_source(spec, dims, *ks)
            except (OSError, ValueError, KeyError) as exc:
                errs.append(f"mask file: {exc}")
    elif spec["extent"]:
        errs.append("mask: `extent` places a picture given in `from`; "
                    "without one it has no meaning")
    return errs


def _threshold_problems(t) -> list[str]:
    if t == "auto":
        return []
    if isinstance(t, dict):
        f = t.get("fraction")
        if set(t) != {"fraction"} or isinstance(f, bool) \
                or not isinstance(f, (int, float)) or not 0 <= f <= 1:
            return ["mask: threshold {fraction: f} needs 0 <= f <= 1 "
                    "(0 = the lowest reading, 1 = the highest)"]
        return []
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t):
        return ["mask: threshold must be auto, a number, or {fraction: f}"]
    return []


def _extent_problems(e) -> list[str]:
    if e is None:
        return []
    msg = ["mask: extent must be {x: [x0, x1], y: [y0, y1]} (numbers, x0 != x1)"]
    if not isinstance(e, dict) or set(e) - {"x", "y"}:
        return msg
    for v in e.values():
        if not isinstance(v, (list, tuple)) or len(v) != 2 or any(
                isinstance(t, bool) or not isinstance(t, (int, float))
                or not math.isfinite(t) for t in v) or v[0] == v[1]:
            return msg
    return []


# ───────────────────────────── the mask, computed ───────────────────────────

def otsu(values) -> float:
    """Otsu's threshold: the level that splits the readings into the two
    groups that are each as tight as possible. Made for exactly this picture --
    a reflectivity map is two populations, substrate and elements -- and it
    needs no number from the operator. 256 bins over the finite readings."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        raise ValueError("mask: there is no finite reading to set a threshold from")
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
    # Every cut inside an EMPTY gap separates the two groups equally well, and
    # argmax alone would take the first of them -- right at the top of the
    # lower group's noise, where its next reading crosses. Take the MIDDLE of
    # the tied cuts: half way across the gap.
    best = np.flatnonzero(between >= between.max() * (1 - 1e-12))
    return float(0.5 * (edges[best[0] + 1] + edges[best[-1] + 1]))


def threshold_of(spec: dict, values) -> float:
    t = spec["threshold"]
    if t == "auto":
        return otsu(values)
    if isinstance(t, dict):
        v = np.asarray(values, dtype=float)
        v = v[np.isfinite(v)]
        if v.size == 0:
            raise ValueError("mask: there is no finite reading")
        lo, hi = float(v.min()), float(v.max())
        return lo + float(t["fraction"]) * (hi - lo)
    return float(t)


def _interp_along(xp, V, x, axis):
    """np.interp of V over coordinate xp onto x, along `axis`. Also returns
    a bool array: True where x is outside the range xp covers."""
    xp = np.asarray(xp, dtype=float)
    x = np.asarray(x, dtype=float)
    order = np.argsort(xp)
    xp = xp[order]
    V = np.take(V, order, axis=axis)
    Vm = np.moveaxis(V, axis, -1)
    out = np.empty(Vm.shape[:-1] + (len(x),))
    for i in np.ndindex(Vm.shape[:-1]):
        out[i] = np.interp(x, xp, Vm[i])
    tol = 1e-9 * max(1.0, float(np.ptp(xp)))
    outside = (x < xp[0] - tol) | (x > xp[-1] + tol)
    return np.moveaxis(out, -1, axis), outside


def _along(c, V, f, axis):
    """Interpolate along one axis; a single coarse point is just repeated."""
    c = np.asarray(c, dtype=float)
    f = np.asarray(f, dtype=float)
    if len(c) == 1:
        outside = np.abs(f - c[0]) > 1e-9 * max(1.0, abs(c[0]))
        return np.repeat(V, len(f), axis), outside
    return _interp_along(c, V, f, axis)


def interpolate(ca, cb, V, fa, fb):
    """Bilinear interpolation of the coarse map V[a, b] onto the fine grid.

    Returns (Vf, unknown): `unknown` is True where the fine value cannot be
    trusted -- it leans on a NaN coarse reading, or lies outside the area the
    coarse map covers. Those points are always measured.
    """
    V = np.asarray(V, dtype=float)
    nan = ~np.isfinite(V)
    V0 = np.where(nan, 0.0, V)
    W = nan.astype(float)          # interpolated alongside: > 0 = touches a NaN
    V0, out_a = _along(ca, V0, fa, 0)
    W, _ = _along(ca, W, fa, 0)
    Vf, out_b = _along(cb, V0, fb, 1)
    Wf, _ = _along(cb, W, fb, 1)
    unknown = (Wf > 0) | out_a[:, None] | out_b[None, :]
    return Vf, unknown


def grow(keep, fa, fb, margin: float):
    """Grow the True area of keep[a, b] by `margin` (in the axes' unit): every
    point within that distance of a kept point is kept too. A disc, not a
    square, using each axis's own pitch (X and Y may be stepped differently)."""
    keep = np.asarray(keep, dtype=bool)
    if margin <= 0 or not keep.any():
        return keep.copy()

    def radius(c):
        c = np.asarray(c, dtype=float)
        if c.size < 2:
            return 0, 0.0
        p = float(np.median(np.abs(np.diff(c))))
        return (min(int(margin / p + 1e-9), c.size), p) if p > 0 else (0, 0.0)

    ra, pa = radius(fa)
    rb, pb = radius(fb)
    out = keep.copy()
    na, nb = keep.shape
    for i in range(-ra, ra + 1):
        for j in range(-rb, rb + 1):
            if (i == 0 and j == 0) or (i * pa) ** 2 + (j * pb) ** 2 > margin ** 2 * (1 + 1e-9):
                continue
            # out[a + i, b + j] |= keep[a, b], clipped to the grid
            a0, a1 = max(0, i), min(na, na + i)
            b0, b1 = max(0, j), min(nb, nb + j)
            if a1 > a0 and b1 > b0:
                out[a0:a1, b0:b1] |= keep[a0 - i:a1 - i, b0 - j:b1 - j]
    return out


@dataclass
class MaskResult:
    keep: np.ndarray          # bool [a, b] on the FINE grid: True = measure
    threshold: float
    fine_values: np.ndarray   # the interpolated source on the fine grid (NaN = unknown)
    unknown: np.ndarray       # bool [a, b]: kept because the mask could not tell


def auto_margin(ca, cb) -> float:
    """Half the pitch of the mask SOURCE (pass 1, or the file's pixels).

    The interpolated edge is only as good as the source grid: between two
    source points the real rim can be anywhere, and on a convex element
    (a disc) straight-line interpolation puts it INSIDE the element. Measured
    in the simulator, 2026-10-07: pass 1 at 6.75 um and a 2 um margin lost 15
    island points of 1700; half a source pitch (3.4 um) lost none. Smaller
    than one FINE step it grows nothing, which is the honest answer then.
    """
    def pitch(c):
        c = np.asarray(c, dtype=float)
        return float(np.median(np.abs(np.diff(c)))) if c.size > 1 else 0.0
    return 0.5 * max(pitch(ca), pitch(cb))


def margin_of(spec, ca, cb) -> float:
    m = spec["margin"]
    return auto_margin(ca, cb) if m == "auto" else float(m or 0.0)


def build(spec, ca, cb, V, fa, fb) -> MaskResult:
    """The whole recipe: interpolate, threshold, grow, keep the unknown."""
    thr = threshold_of(spec, V)
    Vf, unknown = interpolate(ca, cb, V, fa, fb)
    inside = Vf > thr if spec["keep"] == "above" else Vf < thr
    inside &= ~unknown
    keep = grow(inside, fa, fb, margin_of(spec, ca, cb)) | unknown
    return MaskResult(keep=keep, threshold=thr,
                      fine_values=np.where(unknown, np.nan, Vf), unknown=unknown)


# ───────────────────────────── from a file ──────────────────────────────────

def load_pass(path, detector, dim_a, dim_b):
    """(ca, cb, V[a, b]) from an earlier scan's .nc.

    The file's two axes are found by the PARAMETER they swept (the `param`
    attribute every coordinate carries), so the mask can come from any XY scan
    of the same axes -- coarse or fine, a different range, a different name.
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
        want = {}
        for d, k in ((dim_a, "a"), (dim_b, "b")):
            pid = d.params[0][0]
            hit = [n for n in da.dims if n in ds.coords
                   and (ds[n].attrs.get("param") == pid or n == d.name)]
            if not hit:
                swept = [f"{n} ({ds[n].attrs.get('param', '?')})" for n in da.dims
                         if n in ds.coords]
                raise ValueError(
                    f"{p.name} was not measured in '{pid}' (its axes: "
                    f"{', '.join(swept) or 'none'}). A mask can only be used in the "
                    f"coordinates it was measured in -- camera and stage "
                    f"coordinates differ by the stage's drift.")
            want[k] = hit[0]
        extra = [n for n in da.dims if n not in want.values() and da.sizes[n] > 1]
        if extra:
            raise ValueError(f"'{name}' in {p.name} has more dimensions than X and "
                             f"Y ({', '.join(extra)}); use a plain XY map")
        da = da.squeeze(drop=True).transpose(want["a"], want["b"])
        return (np.asarray(ds[want["a"]].values, dtype=float),
                np.asarray(ds[want["b"]].values, dtype=float),
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


def load_source(spec, dims, ka, kb, kx, ky):
    """(ca, cb, V[a, b]) from the file in spec["from"], in the scan's
    (outer, inner) order: a scan file through load_pass, a picture placed by
    `extent` (default: exactly over the scan's area)."""
    src = spec["from"]
    if not is_picture(src):
        return load_pass(src, spec["detector"], dims[ka], dims[kb])
    img = read_picture(src)                        # [row = Y, column = X]
    ext = spec.get("extent") or {}
    cx = np.asarray(dims[kx].coord, dtype=float)
    cy = np.asarray(dims[ky].coord, dtype=float)
    x0, x1 = ext.get("x") or (cx[0], cx[-1])
    y0, y1 = ext.get("y") or (cy[0], cy[-1])
    ny, nx = img.shape
    xs = np.linspace(float(x0), float(x1), nx) if nx > 1 else np.array([float(x0)])
    ys = np.linspace(float(y0), float(y1), ny) if ny > 1 else np.array([float(y0)])
    if kx == ka:                                   # X is the OUTER dim
        return xs, ys, img.T
    return ys, xs, img


# ───────────────────────────── pass 1, measured ─────────────────────────────

def measure_pass(recipe, registry, dims, ka, kb, ctx, should_abort):
    """Measure pass 1: the mask detector at every `step`-th point of the two
    XY axes. Returns (ia, ib, V[a, b]) -- the coarse INDICES into the fine
    axes and the readings. Raises ScanAborted on Abort."""
    from .engine import _hold_for_operator
    from .errors import ScanAborted
    spec = spec_of(recipe.mask)
    da, db = dims[ka], dims[kb]
    ia = coarse_indices(da.size, spec["step"])
    ib = coarse_indices(db.size, spec["step"])
    g = registry.get(spec["detector"])
    acq = getattr(g, "acquire", None)
    guard = ctx["guard"]
    current = ctx["current"]
    log = ctx["log_fn"]
    V = np.full((len(ia), len(ib)), np.nan)
    log(f"mask pass 1: {spec['detector']} at {len(ia)} x {len(ib)} points "
        f"(every {spec['step']}. of {da.size} x {db.size})")
    last = [None, None]
    for r, i in enumerate(ia):
        cols = list(range(len(ib)))
        if getattr(recipe, "zigzag", False) and r % 2:
            cols.reverse()                 # the same serpentine as the scan
        for c in cols:
            j = ib[c]
            if should_abort and should_abort():
                raise ScanAborted("aborted during the mask pass")
            if _hold_for_operator(ctx, should_abort, f"mask point {r},{c}"):
                raise ScanAborted("aborted during the mask pass")
            redo = False
            while True:
                for k, (d, n) in enumerate(((da, i), (db, j))):
                    if redo or last[k] != n:
                        for pid, values in d.params:
                            current[pid] = registry.get(pid).set(float(values[n]))
                        last[k] = n
                if acq is not None:
                    acq.trigger()          # a FRESH reading, as in the scan
                    acq.wait()
                # the fault check before and after the read, as for a point
                faults = guard.faults()
                if not faults:
                    value = g.get()
                    faults = guard.faults()
                if faults:
                    guard.hold(faults, f"mask point {r},{c}")
                    redo = True
                    continue
                break
            try:
                V[r, c] = float(value)
            except (TypeError, ValueError):
                V[r, c] = np.nan          # unknown -> measured in pass 2
        log(f"mask pass 1: row {r + 1}/{len(ia)} done")
    return ia, ib, V


# ───────────────────────────── the engine's view ────────────────────────────

@dataclass
class Visit:
    """Which points of the scan are measured, in VISITING order.

    measured[flat] -- True = this point is measured (flat = the n-th point
                      visited, zig-zag already applied)
    before[flat]   -- how many measured points come before it
    The hooks use it so that "at the start of each row" means the first
    MEASURED point of the row, and "every n points" counts measured points.
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


def visit_of(keep, shape, ka, kb, zigzag: bool) -> Visit:
    """Broadcast the 2-D mask over the whole scan, in visiting order."""
    shape = tuple(int(s) for s in shape)
    total = int(np.prod(shape)) if shape else 0
    idx = np.array(np.unravel_index(np.arange(total), shape))   # [ndim, total]
    if zigzag:
        # the same rule as engine._zigzag, for every point at once
        out = idx.copy()
        for k in range(1, len(shape)):
            flip = idx[:k].sum(axis=0) % 2 == 1
            out[k] = np.where(flip, shape[k] - 1 - idx[k], idx[k])
        idx = out
    measured = np.asarray(keep, dtype=bool)[idx[ka], idx[kb]]
    before = np.concatenate([[0], np.cumsum(measured)[:-1]]).astype(int)
    return Visit(measured=measured, before=before)


def prepare(recipe, registry, compiled, ctx, should_abort) -> None:
    """Pass 1 (measured or loaded), the mask, and everything the engine and
    the file need from it -- put into ctx. Called after the before-scan
    routines, before the first point of the real scan."""
    spec = spec_of(recipe.mask)
    dims = compiled.dims
    ka, kb, kx, ky = mask_dims(recipe, dims)
    da, db = dims[ka], dims[kb]
    fa, fb = np.asarray(da.coord, float), np.asarray(db.coord, float)
    log = ctx["log_fn"]
    picture = is_picture(spec["from"])
    det = "file" if picture else spec["detector"]
    if spec["from"]:
        ca, cb, V = load_source(spec, dims, ka, kb, kx, ky)
        log(f"mask from {Path(str(spec['from'])).name}: "
            f"{V.shape[0]} x {V.shape[1]} points")
    else:
        ia, ib, V = measure_pass(recipe, registry, dims, ka, kb, ctx, should_abort)
        ca, cb = fa[ia], fb[ib]
    g = None if picture else registry.get(det)
    unit = getattr(g, "unit", "") if g is not None else ""
    # the source (pass 1, or the file) goes into the data file too, on its
    # own axes, so a mask that came out wrong can be looked at afterwards
    pa, pb = f"mask_{da.name}", f"mask_{db.name}"

    def unit_of(d):
        p = registry.get(d.params[0][0])
        return getattr(p, "unit", "") if p is not None else ""

    extra = ctx.setdefault("ds_extra", {})
    extra["coords"] = {
        pa: (pa, ca, {"units": unit_of(da), "param": da.params[0][0],
                      "long_name": f"{da.name}, mask source"}),
        pb: (pb, cb, {"units": unit_of(db), "param": db.params[0][0],
                      "long_name": f"{db.name}, mask source"}),
    }
    source = (f"the mask file {Path(str(spec['from'])).name}" if spec["from"]
              else "the mask pass (pass 1)")
    extra["vars"] = {f"mask_{det}": ((pa, pb), V, {
        "units": unit, "long_name": f"{det}, {source}"})}
    res = build(spec, ca, cb, V, fa, fb)
    n_keep, n_all = int(res.keep.sum()), int(res.keep.size)
    extra["vars"]["scan_mask"] = ((da.name, db.name), res.keep.astype(np.int8), {
        "long_name": "1 = measured, 0 = left out by the mask (stored as NaN)",
        "aaltoflow_type": "bool"})
    ctx["ds_attrs"].update(
        mask_json=json.dumps(recipe.mask), mask_threshold=float(res.threshold),
        mask_points=f"{n_keep} of {n_all}",
        mask_source=str(spec["from"]) if spec["from"] else "measured (pass 1)")
    margin = margin_of(spec, ca, cb)
    ctx["ds_attrs"]["mask_margin"] = float(margin)
    log(f"mask: threshold {res.threshold:.6g} {unit}, keep {spec['keep']}, margin "
        f"{margin:.4g}{' (auto)' if spec['margin'] == 'auto' else ''} -> {n_keep} of {n_all} XY points measured "
        f"({100.0 * n_keep / max(1, n_all):.0f} %)")
    if n_keep == 0:
        raise ValueError("the mask left no point to measure: check `keep` (above / "
                         "below) and the threshold against the mask source in the file")
    ctx["visit"] = visit_of(res.keep, compiled.shape, ka, kb,
                            bool(getattr(recipe, "zigzag", False)))
