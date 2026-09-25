"""
engine.py — run a recipe over a registry into an N-dimensional dataset.

The core is an ODOMETER over the compiled dims (dims[0] = outermost/slowest).
At each grid point it sets only the parameters of dims whose index changed
(outer dims change rarely, so we don't re-command them every point), fires the
matching hooks, reads every detector, and stores the values at the multi-index.
Result: an xarray.Dataset with one named/units-carrying coordinate per dim and
one data variable per detector — self-describing, arbitrary-N-D, netCDF-ready.

There is no loop-count limit: 1-D, 2-D, 5-D are all the same code path. XY
imaging is just two of the dims (from a raster axis).
"""

from __future__ import annotations

import time

import numpy as np
import xarray as xr

from .errors import RoutineError, ScanAborted
from .hooks import run_hooks


def _unravel(flat: int, shape: tuple[int, ...]) -> tuple[int, ...]:
    idx = []
    for s in reversed(shape):
        idx.append(flat % s)
        flat //= s
    return tuple(reversed(idx))


def _zigzag(idx: tuple[int, ...], shape: tuple[int, ...]) -> tuple[int, ...]:
    """Serpentine order: reverse a dim on every other pass of the dims outside it.

    The ORDER of visiting changes; the index does not. Each point is still
    stored at its own coordinate, so the dataset is identical to a normal
    scan's -- only the path between points is shorter, by one fly-back per row.
    """
    out = list(idx)
    for k in range(1, len(shape)):
        if sum(idx[:k]) % 2:
            out[k] = shape[k] - 1 - idx[k]
    return tuple(out)


def run(recipe, registry, on_progress=None, should_abort=None,
        created_iso: str | None = None, on_point=None,
        on_log=None) -> xr.Dataset:
    """Execute `recipe` against `registry`. Returns an xarray.Dataset.

    on_progress(done, total, eta_s) : optional callback for a GUI/CLI.
    should_abort() -> bool          : optional cooperative stop.
    created_iso                     : timestamp string for metadata (time is
                                      injected so runs are reproducible/testable).
    on_point(done, total, snapshot) : called after every point. `snapshot()`
                                      BUILDS the dataset as it stands (points
                                      not measured yet are NaN) -- a factory,
                                      not a dataset, so a caller that is only
                                      redrawing a few times a second does not
                                      pay for one per point. A long scan is
                                      unwatchable if its data only appears at
                                      the end.
    on_log(message)                 : what the ROUTINES are doing ("before_scan:
                                      set field = 150 mT ... done"). A routine
                                      can take minutes (a magnet ramp and a
                                      reference sweep) before the first point,
                                      and a progress bar sitting at 0 % says
                                      nothing about why.

    Routines: hooks at `before_scan` fire after the conditions are applied and
    before the first point; hooks at `after_scan` fire after the last point --
    and ALSO after an Abort, because "put the magnet back to 0" is exactly what
    you want when you stop a scan early. NOT after an exception: something is
    broken then, the error must surface unchanged, and driving more hardware
    from an unknown state is how a small fault becomes a bigger one.
    """
    errs = recipe.validate(registry)
    if errs:
        raise ValueError("invalid recipe:\n  - " + "\n  - ".join(errs))

    t_start = time.monotonic()
    # ctx is what hooks see. `current` = every value the ENGINE has set so far
    # ({param_id: value}), kept up to date as it goes: a routine that moves a
    # parameter mid-scan uses it to put that parameter back (hooks.py, `call`).
    current: dict = {}
    ctx = {"registry": registry, "recipe": recipe, "current": current,
           "log_fn": on_log or (lambda msg: None), "aborted": False}

    compiled = recipe.compile(registry)

    def after_scan(aborted: bool):
        ctx["aborted"] = aborted
        run_hooks(compiled.hooks, "after_scan", ctx)

    def after_abort():
        # An abort pressed while something was settling (a condition, a routine,
        # a point). Still an ABORT, so the after-scan routine runs; the caller
        # then re-raises and reports "aborted" exactly as before.
        try:
            after_scan(aborted=True)
        except Exception as exc:          # say it, but do not hide the abort
            ctx["log_fn"](f"after_scan routine failed after abort: {exc}")

    # establish the constant context before sweeping (rf power, unswept freq, …)
    try:
        for pid, val in recipe.fixed.items():
            current[pid] = registry.get(pid).set(float(val))
    except ScanAborted:
        after_abort()
        raise

    dims = compiled.dims
    shape = compiled.shape
    total = compiled.n_points
    dets = compiled.detectors

    # Allocate one array per detector. A scalar detector gets the scan's shape;
    # an ARRAY detector (a VNA trace, say) gets the scan's shape PLUS its own
    # inner dimensions, because the instrument sweeps those itself in hardware.
    # Its coordinate arrays are read ONCE here, not per point -- pulling 1601
    # frequencies over the wire at every grid point would dominate the run.
    det_axes = {}          # det id -> [AxisSpec, ...]
    det_coords = {}        # axis name -> coordinate array
    data = {}
    for d in dets:
        g = registry.get(d)
        axes = list(getattr(g, "axes", ()) or ())
        det_axes[d] = axes
        inner = []
        for ax in axes:
            vals = np.asarray(ax.values())
            if ax.name in det_coords:
                if len(det_coords[ax.name]) != len(vals):
                    raise ValueError(
                        f"detectors disagree about axis '{ax.name}': "
                        f"{len(det_coords[ax.name])} vs {len(vals)} points. "
                        f"Two detectors sharing an axis name must share its "
                        f"coordinate.")
            else:
                det_coords[ax.name] = vals
            inner.append(len(vals))
        dtype = np.complex128 if getattr(g, "dtype", "float") == "complex" else float
        fill = (np.nan + 1j * np.nan) if dtype is np.complex128 else np.nan
        data[d] = np.full(tuple(shape) + tuple(inner), fill, dtype=dtype)

    # One AcquireSpec per distinct group among the selected detectors.
    acquire_groups = []
    seen_groups = set()
    for d in dets:
        spec = getattr(registry.get(d), "acquire", None)
        if spec is not None and spec.group not in seen_groups:
            seen_groups.add(spec.group)
            acquire_groups.append(spec)

    prev = [None] * len(dims)
    ctx["shape"] = shape
    ctx["dim_names"] = [d.name for d in dims]     # each_sweep hooks find their axis here

    try:
        if should_abort and should_abort():
            aborted = True                # pressed before anything started
        else:
            run_hooks(compiled.hooks, "before_scan", ctx)
            # The ETA clock starts AFTER the before-scan routine: a two-minute
            # magnet ramp and reference sweep would otherwise be spread over
            # the points as if every one of them were that slow.
            t0 = time.monotonic()
            aborted = _sweep(recipe, registry, compiled, dims, shape, total,
                             dets, det_axes, det_coords, data, acquire_groups,
                             prev, ctx, t0, on_progress, should_abort, on_point,
                             created_iso)
    except ScanAborted:
        after_abort()
        raise

    ds = _to_dataset(recipe, compiled, registry, data, created_iso,
                     time.monotonic() - t_start, det_axes, det_coords)
    try:
        after_scan(aborted=aborted)
    except Exception as exc:
        # The points are measured and the dataset is built; a failing
        # "field -> 0" must not throw a finished map away with it.
        raise RoutineError(f"after_scan routine failed: {exc}", dataset=ds) from exc
    return ds


def _sweep(recipe, registry, compiled, dims, shape, total, dets, det_axes,
           det_coords, data, acquire_groups, prev, ctx, t0, on_progress,
           should_abort, on_point, created_iso) -> bool:
    """The odometer itself. Returns True if it stopped on an Abort."""
    current = ctx["current"]
    for flat in range(total):
        if should_abort and should_abort():
            return True
        idx = _unravel(flat, shape)
        if getattr(recipe, "zigzag", False):
            idx = _zigzag(idx, shape)
        ctx["flat"] = flat
        ctx["index"] = idx

        # set params for every dim whose index changed (outer→inner)
        for k, d in enumerate(dims):
            if idx[k] != prev[k]:
                if prev[k] is not None:
                    run_hooks(compiled.hooks, "after_axis", ctx, axis_name=d.name)
                for pid, values in d.params:
                    current[pid] = registry.get(pid).set(float(values[idx[k]]))
                run_hooks(compiled.hooks, "before_axis", ctx, axis_name=d.name)
        prev = list(idx)

        run_hooks(compiled.hooks, "before_point", ctx)

        # Slow detectors must be TRIGGERED and WAITED ON before they are read.
        # Trigger every group first and only then wait for them, so several
        # instruments acquire concurrently instead of one after another -- and
        # so detectors sharing a group (s11/s21/s12/s22 off one sweep) cost one
        # acquisition, not four.
        #
        # Without this a VNA hands back whatever is still in its buffer: the
        # PREVIOUS sweep, taken at the previous point. Nothing raises; the map
        # is simply one step behind and looks clean.
        for spec in acquire_groups:
            spec.trigger()
        for spec in acquire_groups:
            spec.wait()

        for det in dets:
            value = registry.get(det).get()
            if det_axes[det]:
                arr = np.asarray(value)
                expected = data[det].shape[len(shape):]
                if arr.shape != expected:
                    # Ragged data has nowhere sensible to go, and padding it
                    # would hand back a file that looks fine and is wrong. Stop
                    # instead, and say exactly where.
                    raise ValueError(
                        f"detector '{det}' returned shape {arr.shape} at grid "
                        f"index {idx}, but the scan was allocated for "
                        f"{expected}. The instrument's sweep changed mid-scan "
                        f"(a span or point-count change will do it). Re-run "
                        f"without reconfiguring it, or scan it as its own axis.")
                data[det][idx] = arr
            else:
                data[det][idx] = value
        run_hooks(compiled.hooks, "after_point", ctx)

        done = flat + 1
        if on_progress:
            elapsed = time.monotonic() - t0
            eta = elapsed / done * (total - done)
            on_progress(done, total, eta)
        if on_point:
            # COPY the buffers: an xarray.Dataset wraps the arrays it is given,
            # so a snapshot sharing them would keep changing under whoever holds
            # it -- a live plot that redraws later, or a partial file someone
            # saves. The copy costs one array per emission, and the caller
            # controls how often that is by only calling the factory when it
            # actually wants a picture.
            on_point(done, total,
                     lambda: _to_dataset(recipe, compiled, registry,
                                         {k: v.copy() for k, v in data.items()},
                                         created_iso, time.monotonic() - t0,
                                         det_axes, det_coords))
    return False


def _units(registry, pid: str) -> str:
    p = registry.get(pid)
    return getattr(p, "unit", "") if p else ""


def _to_dataset(recipe, compiled, registry, data, created_iso, seconds,
                det_axes=None, det_coords=None) -> xr.Dataset:
    dims = compiled.dims
    dim_names = [d.name for d in dims]
    det_axes = det_axes or {}
    det_coords = det_coords or {}

    # index coordinates (one per dim), carrying the driving parameter's units
    coords = {d.name: (d.name, d.coord, {"units": _units(registry, d.params[0][0]),
                                         "param": d.params[0][0]}) for d in dims}
    # zip / raster secondary members ride along as extra (non-index) coords
    for d in dims:
        for pid, vals in d.params[1:]:
            coords[pid] = (d.name, vals, {"units": _units(registry, pid)})

    # a detector's inner axes become real dimensions, shared by name
    for name, vals in det_coords.items():
        axis = next((a for axes in det_axes.values() for a in axes
                     if a.name == name), None)
        coords[name] = (name, vals,
                        {"units": getattr(axis, "unit", ""),
                         "label": getattr(axis, "label", name)})

    data_vars = {}
    for det, arr in data.items():
        g = registry.get(det)
        names = dim_names + [a.name for a in det_axes.get(det, ())]
        attrs = {"units": _units(registry, det),
                 "label": getattr(g, "label", det)}
        if np.iscomplexobj(arr):
            # Split complex into two real variables so the file stays
            # CONFORMING netCDF-4. h5netcdf will happily write complex as an
            # HDF5 compound type, but then warns the file "might not be
            # readable by other netcdf tools" -- and lab data gets opened in
            # MATLAB and Igor, not only in Python.
            # scan_core.data.as_complex(ds, det) puts it back together.
            data_vars[f"{det}_real"] = (names, arr.real,
                                        {**attrs, "complex_part": "real",
                                         "complex_pair": det})
            data_vars[f"{det}_imag"] = (names, arr.imag,
                                        {**attrs, "complex_part": "imag",
                                         "complex_pair": det})
        else:
            data_vars[det] = (names, arr, attrs)

    # The CONDITIONS the measurement was taken under, as scalar coordinates:
    # rf power, the field a frequency sweep sat in, the wavelength. They are in
    # `recipe_json` too, but a JSON blob in an attribute is not something you
    # notice in ncdump, in MATLAB, or in the viewer's header pane -- and "what
    # was the power?" is the first question asked of a file six months old.
    for pid, value in (getattr(recipe, "fixed", None) or {}).items():
        if pid in coords or pid in data_vars:
            continue
        try:
            coords[pid] = ((), float(value),
                           {"units": _units(registry, pid), "fixed": "true"})
        except (TypeError, ValueError):
            continue

    ds = xr.Dataset(data_vars=data_vars, coords=coords)
    ds.attrs.update(
        name=recipe.name,
        comment=recipe.comment,
        recipe_json=recipe.to_json(),
        created=created_iso or "",
        n_points=int(compiled.n_points),
        seconds=float(seconds),
        dims=",".join(dim_names),
    )
    return ds
