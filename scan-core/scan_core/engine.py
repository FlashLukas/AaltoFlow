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

from .errors import RoutineError, ScanAborted, ScanFault, format_faults
from .hooks import find_autofocus, routine_steps, run_hooks


# ─────────────────────────── faults and the PAUSE ────────────────────────────
#
# Lukas, 2026-09-28: a failed hardware read must be loud (A), and when the
# camera loses its pattern the measurement must stop or pause for the operator
# (B). So before the detectors of a point are read, and again after, the engine
# asks the registry's `fault_check` whether every instrument the scan uses can
# be trusted. If not:
#   * with a pause handler (`on_fault`, the GUI's): PAUSED -- report the faults,
#     wait until they are gone (or Abort), then measure the point AGAIN, from
#     setting its parameters on. The reading taken during the fault is thrown
#     away: it is exactly the kind of number that looks fine and is wrong.
#   * without one (a script): raise ScanFault, with the points measured so far.

#: Seconds between fault checks while PAUSED.
PAUSE_POLL_S = 0.5


class _Guard:
    """The engine's fault check and pause, one object for _sweep and fly_sweep."""

    def __init__(self, check, ids, on_fault, should_abort, log, poll_s):
        self._check, self._ids = check, ids
        self.on_fault, self.should_abort = on_fault, should_abort
        self.log, self.poll_s = log, poll_s
        self.pauses = 0

    def faults(self) -> list:
        if self._check is None:
            return []
        try:
            return list(self._check(self._ids) or [])
        except Exception as exc:          # a broken check is itself a fault
            return [("scan", f"fault check failed ({exc})")]

    def hold(self, faults, where: str, cause=None):
        """PAUSE until `faults` are gone, or raise (no handler / Abort).

        Returns normally when the faults have cleared -- the caller then
        measures the point again. Raises ScanAborted when Abort is pressed
        while paused (the ordinary abort path: after-scan routine, partial
        data kept), ScanFault when there is nobody to pause for.
        """
        text = format_faults(faults)
        if cause is not None and str(cause) not in text:
            text += f" [{cause}]"
        if self.on_fault is None:
            raise ScanFault(f"scan stopped at {where}: {text}", faults)
        self.pauses += 1
        self.log(f"PAUSED at {where}: {text}")
        last = list(faults)
        self.on_fault(last)
        try:
            while True:
                if self.should_abort and self.should_abort():
                    raise ScanAborted(f"aborted while paused ({text})")
                time.sleep(self.poll_s)
                now = self.faults()
                if not now:
                    self.log(f"faults cleared -- resuming; measuring {where} again")
                    return
                if now != last:
                    last = now
                    self.on_fault(now)
        finally:
            self.on_fault([])             # the banner goes, whatever happened


def _used_ids(recipe, compiled, registry) -> set:
    """Every parameter / action id this scan touches, for the fault check.

    Only the instruments a scan USES are asked about: a module that is merely
    connected must not pause somebody else's measurement.
    """
    ids = set(recipe.fixed or {}) | set(compiled.detectors)
    for d in compiled.dims:
        ids |= {pid for pid, _ in d.params}
    for ax in recipe.axes or []:
        if isinstance(ax, dict):
            for key in ("readback", "move", "speed_param"):
                if ax.get(key):
                    ids.add(ax[key])
    for h in recipe.hooks or []:
        if h.get("action") == "call":
            try:
                for step in routine_steps(h.get("args") or {}):
                    ids.add(step[1])
            except ValueError:
                pass
        elif h.get("action") == "autofocus":
            aid = find_autofocus(registry)
            if aid:
                ids.add(aid)
    w = getattr(recipe, "window", None)
    if isinstance(w, dict):
        # the resonance window READS the field (and angle) at every point: a
        # faulted magnet would put the window in the wrong place
        for key in ("field", "angle"):
            if isinstance(w.get(key), str):
                ids.add(w[key])
    return ids


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
        on_log=None, data_path=None, on_fault=None, fault_check=None,
        pause_poll_s: float = PAUSE_POLL_S, on_window=None) -> xr.Dataset:
    """Execute `recipe` against `registry` -- ONE scan per instrument at a time.

    Before anything moves, every instrument the scan uses is claimed for it
    (`registry.scan_claim`, suite_common/control.py): Lukas, 2026-09-29, "make
    sure there is no more than one scanning core running the same
    instruments". A second suite -- on this PC or another -- gets ScanBusy
    naming the scan that holds it, and nothing of this scan has been sent. The
    claim is given back at the end, also after an abort or an error. The rest
    is `_run` (below).
    """
    claim = getattr(registry, "scan_claim", None)
    if claim is None:
        return _run(recipe, registry, on_progress, should_abort, created_iso,
                    on_point, on_log, data_path, on_fault, fault_check,
                    pause_poll_s, on_window)
    errs = recipe.validate(registry)
    if errs:
        raise ValueError("invalid recipe:\n  - " + "\n  - ".join(errs))
    ids = _used_ids(recipe, recipe.compile(registry), registry)
    release = claim(ids, getattr(recipe, "name", "") or "scan", on_log)
    try:
        return _run(recipe, registry, on_progress, should_abort, created_iso,
                    on_point, on_log, data_path, on_fault, fault_check,
                    pause_poll_s, on_window)
    finally:
        release()


def _run(recipe, registry, on_progress=None, should_abort=None,
         created_iso: str | None = None, on_point=None,
         on_log=None, data_path=None, on_fault=None, fault_check=None,
         pause_poll_s: float = PAUSE_POLL_S, on_window=None) -> xr.Dataset:
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
    on_fault(faults)               : the PAUSE handler. Called with the list
                                      of Fault(name, message) when the scan
                                      pauses (and again when that list
                                      changes), and with [] when it resumes or
                                      is aborted. Without it a fault STOPS the
                                      scan with ScanFault (a script has nobody
                                      to pause for).
    on_window(state)                : RESONANCE WINDOW only (recipe.window):
                                      after every point, a dict with the
                                      predicted and fitted f_res, the window,
                                      the Meff in use -- for a live readout.
    fault_check(ids) -> [Fault]     : default `registry.fault_check` (set by
                                      build_lab_registry; the simulator has
                                      none, so nothing is checked).
    on_log(message)                 : what the ROUTINES are doing ("before_scan:
                                      set field = 150 mT ... done"). A routine
                                      can take minutes (a magnet ramp and a
                                      reference sweep) before the first point,
                                      and a progress bar sitting at 0 % says
                                      nothing about why.

    Faults (2026-09-28): before the detectors of a point are read, and again
    after, every instrument the scan uses is checked (dead/silent service,
    `hw_error`, `fault`); on a fault the scan pauses and the point is measured
    AGAIN once it clears (see _Guard). A fly row is checked when it is done
    and flown again.

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
           "log_fn": on_log or (lambda msg: None), "aborted": False,
           # where the measurement is written: actions that save something of
           # their own (a camera picture, the pattern) put it next to it
           "data_path": str(data_path) if data_path else None}

    compiled = recipe.compile(registry)
    check = fault_check if fault_check is not None else getattr(registry, "fault_check", None)
    ctx["guard"] = _Guard(check, _used_ids(recipe, compiled, registry), on_fault,
                          should_abort, ctx["log_fn"], pause_poll_s)

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

    # The RESONANCE WINDOW (opt-in, window.py): its runner keeps the model
    # state between points; its extra variables (mask + per-point record) are
    # allocated next to the detectors so every path that builds a dataset --
    # the live snapshot, an abort, a fault, the end -- carries them.
    if getattr(recipe, "window", None):
        _setup_window(recipe, registry, dets, det_axes, det_coords, data,
                      shape, ctx)
        ctx["on_window"] = on_window

    prev = [None] * len(dims)
    ctx["shape"] = shape
    ctx["dim_names"] = [d.name for d in dims]     # each_sweep hooks find their axis here

    sweeping = False          # True once the points have started
    try:
        if should_abort and should_abort():
            aborted = True                # pressed before anything started
        else:
            run_hooks(compiled.hooks, "before_scan", ctx)
            # The ETA clock starts AFTER the before-scan routine: a two-minute
            # magnet ramp and reference sweep would otherwise be spread over
            # the points as if every one of them were that slow.
            t0 = time.monotonic()
            sweeping = True
            # A FLY axis (innermost, flyscan.py) is one continuous move per row
            # instead of a point-by-point odometer. Every other scan takes
            # _sweep, unchanged.
            if dims and dims[-1].kind == "fly":
                from .flyscan import fly_sweep as sweep
            else:
                sweep = _sweep
            aborted = sweep(recipe, registry, compiled, dims, shape, total,
                            dets, det_axes, det_coords, data, acquire_groups,
                            prev, ctx, t0, on_progress, should_abort, on_point,
                            created_iso)
    except ScanFault as exc:
        # A fault with nobody to pause for. It is an ERROR, so -- as for any
        # other error -- the after-scan routine does NOT run (an instrument is
        # in a state nobody has looked at). The points measured before it are
        # handed over, exactly as an Abort's are.
        if sweeping and exc.dataset is None:
            try:
                exc.dataset = _to_dataset(recipe, compiled, registry, data,
                                          created_iso, time.monotonic() - t_start,
                                          det_axes, det_coords,
                                          var_attrs=ctx.get("var_attrs"))
            except Exception as build_exc:
                ctx["log_fn"](f"could not keep the measured points: {build_exc}")
        raise
    except ScanAborted as exc:
        after_abort()
        if sweeping:
            # Abort pressed while an instrument was SETTLING -- which is where a
            # real scan spends most of its time, so this is the usual abort.
            # Hand the points measured so far to the caller (unmeasured ones are
            # NaN), exactly as an abort between two points does; before
            # 2026-09-28 they were silently thrown away here.
            try:
                exc.dataset = _to_dataset(recipe, compiled, registry, data,
                                          created_iso, time.monotonic() - t_start,
                                          det_axes, det_coords,
                                          var_attrs=ctx.get("var_attrs"))
            except Exception as build_exc:     # say it, but do not hide the abort
                ctx["log_fn"](f"could not keep the measured points: {build_exc}")
        raise

    ds = _to_dataset(recipe, compiled, registry, data, created_iso,
                     time.monotonic() - t_start, det_axes, det_coords,
                     var_attrs=ctx.get("var_attrs"))
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
    guard = ctx.get("guard") or _Guard(None, (), None, should_abort,
                                       lambda m: None, PAUSE_POLL_S)
    for flat in range(total):
        if should_abort and should_abort():
            return True
        idx = _unravel(flat, shape)
        if getattr(recipe, "zigzag", False):
            idx = _zigzag(idx, shape)
        ctx["flat"] = flat
        ctx["index"] = idx

        redo = False
        while True:
            try:
                values = _measure_point(registry, compiled, dims, shape, dets,
                                        det_axes, data, acquire_groups, prev,
                                        ctx, idx, current, guard, redo)
                break
            except _Redo as r:
                guard.hold(r.faults, f"point {flat + 1} {idx}")
            except (ScanAborted, ScanFault):
                raise
            except Exception as exc:
                # A settle timeout, a refused command, a dead service ... If an
                # instrument the scan uses is ALSO reporting a fault, that fault
                # is the likely cause (a camera that lost its pattern never
                # settles its point): pause for it. Otherwise it is an ordinary
                # error and ends the scan exactly as before.
                faults = guard.faults()
                if not faults and getattr(exc, "is_fault", False):
                    # an InstrumentFault from a settle wait: the status said
                    # hw_error/fault for over a second, even if it has just
                    # cleared -- the point was never measured cleanly
                    faults = [(getattr(exc, "instrument", "") or "instrument", str(exc))]
                if not faults:
                    raise
                guard.hold(faults, f"point {flat + 1} {idx}", cause=exc)
            redo = True
        for det, value in values.items():
            data[det][idx] = value
        pending = ctx.pop("window_pending", None)
        if pending is not None:
            # only now, with the point kept, does the window learn from it
            # (Meff, baseline, counters) -- a paused and redone point must not
            # teach it twice
            runner = ctx["window"]
            runner.commit(pending)
            if ctx.get("on_window"):
                ctx["on_window"](runner.state())
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


class _Redo(Exception):
    """Internal: the fault check failed inside a point -- pause, then redo it."""

    def __init__(self, faults):
        super().__init__(format_faults(faults))
        self.faults = faults


def _measure_point(registry, compiled, dims, shape, dets, det_axes, data,
                   acquire_groups, prev, ctx, idx, current, guard, redo):
    """Set one point's parameters, acquire, read. Returns {det: value}.

    Nothing is written into `data` here: a reading taken while an instrument
    reports a fault must never land in the file, so the caller commits the
    values only once both fault checks have passed.

    `redo` = this point is being measured AGAIN after a pause: every dim's
    parameters are set, not only the ones whose index changed -- whatever
    went wrong may have moved them (a camera that lost its pattern and was
    re-aimed by hand) -- but without re-firing the axis hooks, because no axis
    has moved on to a new value. The before_point hooks DO run again (an
    each_sweep autofocus at the start of a row is exactly what you want after
    the sample was re-found).
    """
    # `prev` is the caller's list and is updated IN PLACE, dim by dim, as each
    # dim's hooks run: if this attempt fails half-way (a settle that raises),
    # the next attempt must neither fire an axis hook twice nor skip one.
    for k, d in enumerate(dims):
        changed = idx[k] != prev[k]
        if not (changed or redo):
            continue
        if changed and prev[k] is not None:
            run_hooks(compiled.hooks, "after_axis", ctx, axis_name=d.name)
            prev[k] = None                    # fired; not again on a retry
        for pid, values in d.params:
            current[pid] = registry.get(pid).set(float(values[idx[k]]))
        if changed:
            run_hooks(compiled.hooks, "before_axis", ctx, axis_name=d.name)
        prev[k] = idx[k]

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
    runner = ctx.get("window")
    plan = runner.plan(current) if runner is not None else None
    wspec = ctx.get("window_spec")
    for spec in acquire_groups:
        args = plan.args(runner.arg) if (plan is not None and spec is wspec) else None
        if args:
            spec.trigger(args)
        else:
            spec.trigger()
    for spec in acquire_groups:
        spec.wait()

    out = _checked_read(registry, dets, det_axes, data, shape, idx, guard)
    if runner is not None:
        out = _window_point(runner, plan, wspec, out, registry, det_axes, data,
                            shape, idx, guard, current, ctx)
    return out


def _checked_read(registry, dets, det_axes, data, shape, idx, guard) -> dict:
    """Fault check, read `dets`, fault check again. {det: value}."""
    # CHECK 1: everything is set and acquired -- is anyone faulted before we
    # read? (A camera that lost its pattern, a meter whose read failed.)
    faults = guard.faults()
    if faults:
        raise _Redo(faults)

    out = {}
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
            out[det] = arr
        else:
            out[det] = value

    # CHECK 2: did something fail WHILE we read? The values are only committed
    # when this passes too.
    faults = guard.faults()
    if faults:
        raise _Redo(faults)
    return out


# ─────────────────────────── the resonance window ────────────────────────────

def _setup_window(recipe, registry, dets, det_axes, det_coords, data, shape, ctx):
    """Build the WindowRunner and allocate the window's own variables."""
    from .window import RECORD_VARS, WindowRunner, var_names
    block = recipe.window
    det = block["detector"]
    g = registry.get(det)
    axis = det_axes[det][0]
    wspec = g.acquire
    # Every ARRAY detector off the same acquisition and the same frequency
    # axis is windowed with it (a module's "raw" trace next to its
    # "transmission"): same bins measured, each filled from its OWN baseline.
    group = [det] + [d for d in dets if d != det
                     and getattr(registry.get(d), "acquire", None) is not None
                     and registry.get(d).acquire.group == wspec.group
                     and [a.name for a in det_axes[d]] == [axis.name]]
    decl = g.window or {}
    runner = WindowRunner(block, registry, det_coords[axis.name], axis.unit, group,
                          min_bins=decl.get("min_bins", 3),
                          arg=decl.get("arg", "window"))
    ctx["window"] = runner
    ctx["window_spec"] = wspec
    names = var_names(det)
    inner = data[det].shape[len(shape):]
    # bool mask: True = this bin was MEASURED at this point, False = filled
    # from the baseline (or not measured yet, in a partial file)
    data[names["mask"]] = np.zeros(tuple(shape) + tuple(inner), dtype=bool)
    det_axes[names["mask"]] = det_axes[det]
    attrs = {names["mask"]: {
        "long_name": f"bins of {det} actually measured (0 = baseline fill)",
        "window_mask_of": ",".join(group)}}
    for key, (dtype, unit, text) in RECORD_VARS.items():
        name = names[key]
        data[name] = (np.zeros(shape, dtype=bool) if dtype is bool
                      else np.full(shape, np.nan))
        det_axes[name] = []
        attrs[name] = {"units": unit, "long_name": text}
    for d in group:
        # a complex detector is stored as <d>_real / <d>_imag (see _to_dataset)
        split = getattr(registry.get(d), "dtype", "float") == "complex"
        for name in ((f"{d}_real", f"{d}_imag") if split else (d,)):
            attrs[name] = {"window_mask": names["mask"]}
    ctx["var_attrs"] = {**(ctx.get("var_attrs") or {}), **attrs}


def _window_point(runner, plan, wspec, out, registry, det_axes, data, shape,
                  idx, guard, current, ctx) -> dict:
    """Look for the line in what the window measured; WIDEN and measure this
    point again while it is not there; then return `out` with the windowed
    traces replaced by the FILLED ones plus the window's own variables.

    Nothing is committed here (see _sweep): a fault or an abort half way
    leaves the runner exactly as it was, and the redone point plans again.
    """
    from .window import var_names
    group = runner.group_dets
    while True:
        outcome = runner.assess(plan, out)
        if not outcome.retry:
            break
        if guard.should_abort and guard.should_abort():
            raise ScanAborted("aborted while widening the resonance window")
        nxt = runner.plan(current, attempt=plan.attempt + 1)
        ctx["log_fn"](
            f"window: no line in {runner.f[plan.i0] / 1e9:.4f}-"
            f"{runner.f[plan.i1] / 1e9:.4f} GHz at point {idx}; "
            + ("sweeping the full band" if nxt.full else
               f"widening to +-{nxt.margin_hz / 1e6:.0f} MHz"))
        plan = nxt
        args = plan.args(runner.arg)
        if args:
            wspec.trigger(args)
        else:
            wspec.trigger()
        wspec.wait()
        out.update(_checked_read(registry, group, det_axes, data, shape, idx, guard))
    names = var_names(runner.det)
    out.update(outcome.filled)
    out[names["mask"]] = outcome.mask
    for key, value in runner.record(outcome).items():
        out[names[key]] = value
    ctx["window_pending"] = outcome
    return out


def _units(registry, pid: str) -> str:
    p = registry.get(pid)
    return getattr(p, "unit", "") if p else ""


def _to_dataset(recipe, compiled, registry, data, created_iso, seconds,
                det_axes=None, det_coords=None, var_attrs=None) -> xr.Dataset:
    """Build the Dataset. `var_attrs` = {name: {attr: value}} merged into a
    variable's or coordinate's attributes (a fly scan's per-pixel count and
    spread are not registry parameters, so their units come from here)."""
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
            # A detector that IS an axis parameter (recording the MEASURED
            # camera y while stepping its setpoint) would share the axis
            # coordinate's name, and xarray refuses that -- at the END of the
            # scan, taking the data with it. Store it beside the coordinate.
            name = f"{det}_measured" if det in coords else det
            if name != det:
                attrs["measured_of"] = det
            data_vars[name] = (names, arr, attrs)

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

    for name, extra in (var_attrs or {}).items():
        target = (data_vars.get(name) or data_vars.get(f"{name}_measured")
                  or coords.get(name))
        if target is not None and len(target) == 3:
            target[2].update(extra)

    ds = xr.Dataset(data_vars=data_vars, coords=coords)
    w = getattr(recipe, "window", None)
    if w:
        # the window's settings in plain sight (they are in recipe_json too):
        # a file with baseline-filled bins must SAY so in its header
        import json as _json
        ds.attrs["window_json"] = _json.dumps(w)
        ds.attrs["window_detector"] = str(w.get("detector", ""))
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
