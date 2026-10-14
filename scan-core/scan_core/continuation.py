"""continuation.py -- CONTINUE a measurement from a point of an earlier file.

Lukas, 2026-10-14: "continue measuring a data file from a point ... in case of
a crash, lost template ... the new file would include the original data and
only the data from a selected point would be overwritten."

How it works:

* The earlier file is the definition: its `recipe_json` is the scan that is
  run again -- the same axes, the same order, the same routines. The scan is
  compiled against the CURRENT instruments and must come out with the same
  grid (and the same detector axes, e.g. the same VNA frequencies); a scan
  that would land its points somewhere else is refused, not merged.
* Every value of the earlier file is copied into the new run's buffers first.
  The engine then starts the odometer at the chosen point (counted in the
  order the points are MEASURED, zig-zag included) and overwrites from there
  on. A point it has not reached yet keeps the earlier value -- so stopping
  the continued scan early never loses data the earlier file had.
* The new file is a NEW file (the usual autosave name); the earlier one is
  never touched. Its header says where it came from: `continued_from`,
  `continued_at_point` (1 = first), `continued_kept_points`.
* The before-scan routines run as for any scan (a reference, a field ramp);
  a routine "at the start of each sweep" also runs AT the chosen point for
  every sweep already under way there -- an autofocus at the start of a row
  is exactly what a scan continued in the middle of a row needs.

Not offered (refused with the reason, `problems`): fly axes (the row is the
unit there), a scout pass or a resonance window (their state is not in the
file), an averaged repeat (the file holds the mean, not the repeats), and
camera-frame detectors.

No Qt in here: the suite's dialog and a script use the same functions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import xarray as xr

from . import framestore as FS
from .recipe import Recipe, dim_zigzag


def _visit_order(shape, flags) -> list[tuple[int, ...]]:
    """The grid index measured at each step, in order (engine.visit_index)."""
    from .engine import _unravel, visit_index
    total = int(np.prod(shape)) if len(shape) else 1
    return [visit_index(_unravel(f, shape), shape, flags) for f in range(total)]


def _file_values(ds: xr.Dataset, det: str, scan_dims: list[str]):
    """The earlier file's values of one detector, or None when it has none."""
    for name in (det, f"{det}_measured"):
        if name in ds.data_vars:
            return ds[name]
    if f"{det}_real" in ds.data_vars and f"{det}_imag" in ds.data_vars:
        re, im = ds[f"{det}_real"], ds[f"{det}_imag"]
        return re.copy(data=np.asarray(re.values) + 1j * np.asarray(im.values))
    return None


def measured_mask(ds: xr.Dataset, dets, shape) -> np.ndarray:
    """True at every grid point where at least one detector has a value."""
    have = np.zeros(tuple(shape), dtype=bool)
    nd = len(shape)
    for d in dets:
        v = _file_values(ds, d, [])
        if v is None:
            continue
        a = np.asarray(v.values)
        if a.shape[:nd] != tuple(shape):
            continue
        if a.dtype == object:
            ok = np.vectorize(lambda x: x not in ("", None), otypes=[bool])(a)
        else:
            ok = np.isfinite(a)
        if a.ndim > nd:
            ok = ok.reshape(a.shape[:nd] + (-1,)).any(axis=-1)
        have |= ok
    return have


def problems(recipe: Recipe, compiled, registry) -> list[str]:
    """Why this scan cannot be continued ([] = it can)."""
    from .repeat import average_index
    out = []
    dims = compiled.dims
    if dims and dims[-1].kind == "fly":
        out.append("a fly scan cannot be continued yet (its unit is a whole row)")
    if getattr(recipe, "scout", None):
        out.append("a scan with a scout pass cannot be continued yet "
                   "(the mask is decided while it runs)")
    if getattr(recipe, "window", None):
        out.append("a scan with a resonance window cannot be continued yet")
    if average_index(dims) is not None:
        out.append("an AVERAGED repeat cannot be continued: the file holds the "
                   "mean, not the single repeats")
    for d in compiled.detectors:
        try:
            if FS.is_image(registry.get(d)):
                out.append(f"camera frames ({d}) cannot be continued yet")
        except Exception:
            pass
    return out


@dataclass
class Progress:
    """What an earlier file holds, for the dialog (and a script)."""
    path: Path
    recipe: Recipe
    shape: tuple
    total: int
    #: per step of the visiting order: was that point measured?
    measured_in_order: np.ndarray
    order: list = field(repr=False)
    ds: xr.Dataset = field(repr=False)

    @property
    def n_measured(self) -> int:
        return int(self.measured_in_order.sum())

    @property
    def first_missing(self) -> int:
        """0-based step of the first point not measured (total if complete)."""
        miss = np.flatnonzero(~self.measured_in_order)
        return int(miss[0]) if miss.size else self.total

    def where_text(self, step: int) -> str:
        """'clMag.field = -46.55 mT (2/117)' -- the point measured at `step`."""
        if not 0 <= step < self.total:
            return "(the end of the scan)"
        idx = self.order[step]
        dims = [d for d in self.ds.attrs.get("dims", "").split(",") if d]
        parts = []
        for k, name in enumerate(dims):
            if name not in self.ds.coords:
                continue
            c = self.ds[name]
            v = c.values[idx[k]]
            unit = c.attrs.get("units", "")
            try:
                txt = f"{float(v):.6g}"
            except (TypeError, ValueError):
                txt = str(v)
            run = ""
            try:
                # an axis that goes up AND down: the same value is on two runs
                from aaltoview.segments import segments_of
                segs = segments_of(self.ds, name)
                if segs is not None:
                    run = f", {segs.name_at(idx[k])}"
            except Exception:
                pass
            parts.append(f"{name} = {txt}{(' ' + unit) if unit else ''} "
                         f"({idx[k] + 1}/{c.size}{run})")
        return ",  ".join(parts)


def read_progress(path) -> Progress:
    """Open an earlier measurement: its definition and which points it has."""
    path = Path(path)
    with xr.open_dataset(path, engine="h5netcdf") as f:
        ds = f.load()
    text = ds.attrs.get("recipe_json")
    if not text:
        raise ValueError(f"{path.name} holds no scan definition (recipe_json)")
    recipe = Recipe.from_dict(json.loads(text))
    dims = [d for d in str(ds.attrs.get("dims", "")).split(",") if d]
    if not dims or any(d not in ds.sizes for d in dims):
        raise ValueError(f"{path.name}: its scan dimensions are not in the file")
    shape = tuple(int(ds.sizes[d]) for d in dims)
    order = _visit_order(shape, dim_zigzag(recipe.axes)[:len(shape)])
    have = measured_mask(ds, recipe.detectors, shape)
    in_order = np.array([bool(have[i]) for i in order], dtype=bool)
    return Progress(path, recipe, shape, len(order), in_order, order, ds)


@dataclass
class Continuation:
    """What engine.run(continue_from=...) needs: the earlier data and the step
    to start at (0-based, in the visiting order)."""
    progress: Progress
    start: int

    def check(self, recipe, compiled, registry) -> list[str]:
        """The new scan must land on the SAME grid as the earlier file."""
        out = problems(recipe, compiled, registry)
        ds = self.progress.ds
        if tuple(compiled.shape) != tuple(self.progress.shape):
            out.append(f"the scan now has the shape {tuple(compiled.shape)}, the "
                       f"file {tuple(self.progress.shape)} -- not the same grid")
            return out
        for d in compiled.dims:
            if d.name in ds.coords and d.params:
                old, new = np.asarray(ds[d.name].values), np.asarray(d.coord)
                if old.dtype.kind in "fiu" and new.dtype.kind in "fiu":
                    if old.shape != new.shape or not np.allclose(old, new,
                                                                 rtol=1e-9, atol=1e-12):
                        out.append(f"axis {d.name}: its points differ from the file's")
        if not 0 <= self.start < compiled.n_points:
            out.append(f"point {self.start + 1} is not in the scan "
                       f"(1 .. {compiled.n_points})")
        return out

    def fill(self, data: dict, det_axes: dict, det_coords: dict, ndim: int) -> int:
        """Copy the earlier values into the engine's buffers. Returns how many
        points of the earlier file are kept (those BEFORE the start)."""
        ds = self.progress.ds
        for name, vals in det_coords.items():
            if name in ds.coords:
                old = np.asarray(ds[name].values)
                if old.shape != np.shape(vals) or (
                        old.dtype.kind in "fiu"
                        and not np.allclose(old, np.asarray(vals, dtype=float),
                                            rtol=1e-6)):
                    raise ValueError(
                        f"the detector axis {name} is not the one in the file "
                        f"(an instrument's sweep changed?) -- the earlier data "
                        f"would not line up")
        for det, buf in data.items():
            v = _file_values(ds, det, [])
            if v is None:
                continue                 # a detector the earlier file lacked
            a = np.asarray(v.values)
            if a.shape != np.shape(buf):
                raise ValueError(f"{det}: the file holds {a.shape}, the scan "
                                 f"{np.shape(buf)}")
            if buf.dtype == object:
                buf[...] = np.vectorize(
                    lambda x: "" if x is None or (isinstance(x, float) and np.isnan(x))
                    else str(x), otypes=[object])(a)
            else:
                buf[...] = a.astype(buf.dtype, copy=False)
        return int(self.progress.measured_in_order[:self.start].sum())

    def attrs(self, kept: int) -> dict:
        return {"continued_from": self.progress.path.name,
                "continued_at_point": int(self.start + 1),
                "continued_kept_points": int(kept)}
