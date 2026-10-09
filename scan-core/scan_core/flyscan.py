"""flyscan.py -- the FLY SCAN: move without stopping, bin by the MEASURED position.

A stepped scan visits every point: set, wait until settled, measure, next. For
an image that is mostly waiting -- a 200-pixel line on a slip-stick stage is
200 settle waits. A fly scan instead moves the stage slowly and continuously
from one end of the line to the other, while the detectors and the position
readback are RECORDED all the way (a "stream": every sample with the time it
was taken). Afterwards every detector sample is given the position the stage
had AT THAT TIME, and the samples are averaged per pixel. The result is the
same regular grid a stepped scan gives -- same coordinates, same file layout --
built from where the stage really was, not from where it was told to be.

How it plugs in (everything stepped stays exactly as it was):

  * A recipe axis of `type: fly`, allowed only as the LAST (innermost) axis.
    Outer axes are stepped as usual; the fly axis is one continuous move per
    row.

        {type: fly, param: kim.position_x, start: 0, stop: 50, num: 101,
         speed: 5, speed_param: kim.velocity_x,
         readback: kim.position_x,          # optional, default = param
         lag_correction: true}              # optional, default true

    `num` pixels, centred on the same coordinates a `linear` axis with the
    same start/stop/num would visit; each pixel extends half a pixel either
    side, so the move runs from half a pixel before `start` to half a pixel
    after `stop`. `speed` is set on `speed_param` for the fly move and the
    old speed is put back for the approach and at the end.

  * FLYING IN SOMEONE ELSE'S COORDINATES (`move`). The camera measures where
    the laser is ON THE SAMPLE (camera.laser_x/y, um from the template), which
    an open-loop stage's step counter cannot. With

        {type: fly, param: camera.laser_x, start: -20, stop: 20, num: 81,
         move: kim.position_y, speed: 3, speed_param: kim.velocity_y}

    the grid, the placement at each row start and the binning are all in the
    CAMERA's coordinates (param), while the stage named by `move` does the
    flying. How the two relate -- which way, how many um per um -- is not
    assumed: the stage is sent well past the end, the row ends when the camera
    SEES the far edge, and the direction is learned from the first row (a
    first guess that proves wrong is logged, and the row is flown again).

  * FLYING ANY KNOB THAT CAN SWEEP (2026-10-09, Lukas: "not just XY
    scanning ... magnetic field, RF frequency, RF power, phase"). A module
    that can sweep a control continuously declares a `ramp` block on it in
    describe (ramp.py has the contract). Then

        {type: fly, param: clMag.field, start: -50, stop: 50, num: 201,
         speed: 2}                          # mT/s; or row_time_s: 60

    asks the MODULE to sweep the field over each row (ramp verbs, not the
    ordinary set), and bins the samples by the ramp's readback: the MEASURED
    field when the module can report it while sweeping (clMag's Hall probe),
    the COMMANDED value with its time stamp when not (a generator's
    frequency) -- the coordinate's attribute `fly_binned_by` says which.
    The row ends when the module says its sweep is over (a numbered done,
    never a settle rule: gotcha #35); Abort stops the sweep. Without
    `speed`, `row_time_s` gives the pace, else the module's default rate.

    Why the stage keeps its own path and is not squeezed into the ramp
    contract: a stage has no "sweep to x at v" verb -- its speed is a
    persistent setting and the move is an ordinary set -- and that path
    carries rig-verified fixes (the approach checked on the measured
    position, flying in the camera's coordinates). A shim would re-route a
    proven path through new code for no gain; ramp_of() picks the path
    (`speed_param` or `move` named = stage, else a ramp block = ramp).

  * The recipe's `zigzag` flag means what it means for a stepped scan: every
    other row is flown BACKWARDS. That is the fly-back saved -- and also the
    best check that the lag correction is right: a forward and a backward row
    over the same edge must put it in the same place.

  * Every detector, and the readback, must be STREAMABLE (the module declares
    a `stream` block in describe; registry.StreamSpec). A detector that can
    only be read one value at a time cannot be binned by position.

  * WHOLE TRACES (2026-10-09, the VNA). A detector with one dimension of its
    own (a VNA sweep) can fly when its module streams every sweep as one
    sample: the traces whose time stamp falls in a pixel are averaged element
    by element, coherently for complex values, into (..., pixel, freq); the
    count `<det>_n` is per pixel (sweeps), the spread `<det>_std` per element.
    A trace is stamped at its sweep's middle, so its ends belong to the
    neighbouring moments (logged when a sweep spans more than a pixel); single
    frequency points of the same sweeps are scalar channels with their own
    stamps (the moment each point was measured) and bin like any scalar.

  * AVERAGING (2026-10-09): a `repeat` axis in mode 'average' above the fly
    axis flies every row N times; the pixels are pooled over the repeats when
    the dataset is built (repeat._pool_fly), weighted by their samples.

THE LAG. A lock-in's output is its input averaged over the last few time
constants, so the value recorded at time t belongs to where the stage was a
little EARLIER. Moving at speed v, that shifts the image by v * delay, in
opposite directions on forward and backward rows. Each module states the delay
of every channel it streams (for a lock-in, order * tau: the filter's group
delay, which is exactly how far a convolution moves the centroid of a feature),
and each sample is moved back by it before its position is looked up. The
correction removes the SHIFT; it cannot undo the SMEARING (features narrower
than about v * delay are blurred), so the run log warns when the pixel is
smaller than that.

Routines: a fly row has no "points" to hang a per-point routine on, so
before_point / after_point / every_n_points are refused with a fly axis.
Routines at the start or end of each sweep (each_sweep) work, on the fly axis
too: its sweep is one row.
"""

from __future__ import annotations

import math
import threading
import time

import numpy as np

from .errors import ScanAborted, ScanFault
from .hooks import run_hooks

#: Moments that cannot fire in a fly scan (there are no per-pixel stops).
_PER_POINT = ("before_point", "after_point", "every_n_points")

#: How often the engine collects the streams during a row (s): the live plot
#: and the progress bar move at this rate.
POLL_S = 0.25

#: Recording at rest before each row starts to move (s), long enough for every
#: stream to deliver a first sample -- and with it its declared delay.
LEAD_S = 0.05

#: A row also ends when the readback has not moved for this long after the
#: move command returned (the stage stopped short of the far end).
STALL_S = 1.0

#: ... and ends normally once the readback has sat at the far end, unchanged,
#: for this long: arrived and stopped, not merely passing close to the end.
SETTLED_S = 0.15

#: RAMP path: after the module says its sweep is over, how long a MEASURED
#: readback may take to come within half a pixel of the end (a coil lags
#: its setpoint) before the row ends anyway.
ARRIVE_S = 2.0


# ───────────────────────────── pure functions ────────────────────────────────

def pixel_grid(start: float, stop: float, num: int):
    """(centres, edges) of a fly axis, both in SCAN order (start -> stop).

    The centres are exactly a linear axis's points, so a fly scan and a stepped
    scan of the same line share coordinates and can be compared directly.
    """
    num = int(num)
    centres = np.linspace(float(start), float(stop), num)
    w = (float(stop) - float(start)) / (num - 1) if num > 1 else 0.0
    edges = np.linspace(float(start) - w / 2, float(stop) + w / 2, num + 1)
    return centres, edges


def bin_samples(t_pos, pos, t_det, values, edges, delay_s: float = 0.0,
                pos_delay_s: float = 0.0):
    """Average detector samples per pixel of MEASURED position.

    t_pos, pos    : the position stream (time stamps, positions)
    t_det, values : one detector's stream
    edges         : pixel edges, num+1 of them, in scan order (either direction)
    delay_s       : how late the detector is (its filter lag): a sample stamped
                    t is looked up at t - delay_s
    pos_delay_s   : the same for the position readback (normally 0)

    Returns (mean, n, std), one entry per pixel in the order of `edges`. A pixel
    no sample fell into is NaN with n = 0 -- never 0, which would be a value.

    Positions are INTERPOLATED onto the detector's time stamps, never the other
    way round: the stage moves smoothly between two position samples, a
    detector signal need not. A detector sample outside the time span of the
    position record is dropped (extrapolating a position is guessing).

    A sample may be COMPLEX (a VNA's S at one frequency) and/or a whole 1-D
    TRACE (a VNA sweep: `values` is (samples, points), 2026-10-09):
      * the mean is COHERENT: the mean of the complex values, never of |z| --
        averaging magnitudes would turn noise into a positive bias and wash
        out a resonance's phase roll, exactly what a VNA's own averaging
        avoids. A trace is averaged element by element: mean[pixel, k] is the
        mean of point k of every trace that fell into the pixel;
      * n counts SAMPLES (traces) per pixel, one number per pixel, not per
        frequency point: "how many sweeps went into this pixel" is the
        question it answers;
      * std is the spread of the samples around that mean, element by
        element: sqrt(mean |z - mean|^2), the population standard deviation
        (ddof = 0, as for real numbers here). For complex values it is the
        RMS distance from the mean in the complex plane -- one real number,
        the radius of the noise cloud, which is what an error bar on a
        complex quantity can honestly mean.
    """
    edges = np.asarray(edges, dtype=float)
    npix = len(edges) - 1
    vals = np.asarray(values)
    cplx = np.iscomplexobj(vals)
    if not cplx:
        vals = vals.astype(float)
    inner = tuple(vals.shape[1:]) if vals.ndim > 1 else ()
    mean = np.full((max(npix, 0),) + inner, np.nan, dtype=complex if cplx else float)
    if cplx:
        mean[...] = complex(np.nan, np.nan)
    std = np.full((max(npix, 0),) + inner, np.nan)
    n = np.zeros(max(npix, 0), dtype=int)
    t_pos = np.asarray(t_pos, dtype=float) - float(pos_delay_s)
    pos = np.asarray(pos, dtype=float)
    ok = np.isfinite(t_pos) & np.isfinite(pos)
    t_pos, pos = t_pos[ok], pos[ok]
    t_eff = np.asarray(t_det, dtype=float) - float(delay_s)
    if npix < 1 or len(t_pos) < 2 or len(t_eff) == 0:
        return mean, n, std
    order = np.argsort(t_pos, kind="stable")
    t_pos, pos = t_pos[order], pos[order]
    fin = np.isfinite(vals)                      # complex: both parts finite
    # a scalar sample counts if it is a number; a trace if ANY of its points
    # is (a trace with a few NaN points still says something about the rest)
    sample_ok = fin if not inner else fin.reshape(len(vals), -1).any(axis=1)
    inside = (t_eff >= t_pos[0]) & (t_eff <= t_pos[-1]) & sample_ok
    if not inside.any():
        return mean, n, std
    x = np.interp(t_eff[inside], t_pos, pos)
    v, f = vals[inside], fin[inside]

    # Bin on ASCENDING edges, then map back if the axis runs downwards.
    flip = edges[-1] < edges[0]
    asc = edges[::-1] if flip else edges
    idx = np.searchsorted(asc, x, side="right") - 1
    idx[x == asc[-1]] = npix - 1                 # the far edge belongs to the last pixel
    keep = (idx >= 0) & (idx < npix)
    idx, v, f = idx[keep], v[keep], f[keep]
    cnt = np.bincount(idx, minlength=npix)       # samples (traces) per pixel
    if not inner and not cplx:
        # the plain case, kept exactly as it always was
        s1 = np.bincount(idx, weights=v, minlength=npix)
        s2 = np.bincount(idx, weights=v * v, minlength=npix)
        has = cnt > 0
        m = np.full(npix, np.nan)
        m[has] = s1[has] / cnt[has]
        var = np.full(npix, np.nan)
        var[has] = np.maximum(s2[has] / cnt[has] - m[has] ** 2, 0.0)
        if flip:
            m, var, cnt = m[::-1], var[::-1], cnt[::-1]
        return m, cnt.astype(int), np.sqrt(var)

    # complex and/or traces: sum per pixel, element by element, leaving out
    # the elements that are NaN (their own count, `ce`)
    v2 = v.reshape(len(v), -1)
    f2 = f.reshape(len(f), -1)
    vz = np.where(f2, v2, 0)
    ce = _sum_by_pixel(idx, f2.astype(float), npix)
    s1 = _sum_by_pixel(idx, vz, npix)
    s2 = _sum_by_pixel(idx, np.abs(vz) ** 2, npix)
    has = ce > 0
    with np.errstate(invalid="ignore", divide="ignore"):
        m = np.where(has, s1 / np.where(has, ce, 1), np.nan)
        var = np.where(has, np.maximum(s2 / np.where(has, ce, 1) - np.abs(m) ** 2, 0.0),
                       np.nan)
    if cplx:
        m = np.where(has, m, complex(np.nan, np.nan))
    m = m.reshape((npix,) + inner)
    var = var.reshape((npix,) + inner)
    if flip:
        m, var, cnt = m[::-1], var[::-1], cnt[::-1]
    return m, cnt.astype(int), np.sqrt(var)


def _sum_by_pixel(idx, arr, npix):
    """Row sums of `arr` (samples x elements) grouped by pixel index `idx`:
    (npix x elements). Sorting once and summing each run with add.reduceat is
    O(samples x elements) -- this runs on every live refresh of a row, over a
    few hundred 1601-point traces."""
    out = np.zeros((npix, arr.shape[1]), dtype=arr.dtype)
    if len(idx) == 0:
        return out
    order = np.argsort(idx, kind="stable")
    si, sa = idx[order], arr[order]
    starts = np.flatnonzero(np.r_[True, si[1:] != si[:-1]])
    out[si[starts]] = np.add.reduceat(sa, starts, axis=0)
    return out


def find_speed_param(registry, pid: str) -> str | None:
    """The settable that sets the speed of position `pid`, if one is obvious.

    Matched by the module and axis of the position and a unit of "<unit>/s":
    kim.position_x (um) -> kim.velocity_x (um/s); the simulator's pos_x ->
    stage_speed. Used by the Scan Builder to fill in `speed_param`; a recipe
    always names it explicitly, so a guess never drives anything unseen.
    """
    p = registry.get(pid)
    if p is None:
        return None
    unit = getattr(p, "unit", "") or ""
    module = pid.rsplit(".", 1)[0] if "." in pid else ""
    tail = pid.rsplit("_", 1)[-1] if "_" in pid else ""
    best = None
    for q in registry.settables():
        if q.id == pid or (getattr(q, "unit", "") or "") != f"{unit}/s":
            continue
        qmod = q.id.rsplit(".", 1)[0] if "." in q.id else ""
        if qmod != module:
            continue
        qtail = q.id.rsplit("_", 1)[-1] if "_" in q.id else ""
        if tail and qtail == tail:
            return q.id                      # same axis letter: the one
        if best is None:
            best = q.id
    return best


def ramp_of(ax: dict, registry):
    """The RampSpec a fly axis flies with, or None for the STAGE path.

    Two ways to fly, chosen by what the axis names (2026-10-09):
      * the STAGE path (unchanged since 2026-09-27): a position knob that
        moves at a speed of its own -- `speed_param` sets that speed and the
        ordinary set is the move; or `move` flies another stage in this
        parameter's coordinates (the camera's);
      * the RAMP path: any knob whose module declares a `ramp` block (field,
        frequency, ...): the module sweeps it at the row's rate.
    A knob with a ramp block flown with `speed_param` or `move` still takes
    the stage path: the recipe asked for that mechanism by name.
    """
    if ax.get("speed_param") or ax.get("move"):
        return None
    p = registry.get(ax.get("param")) if registry is not None else None
    return getattr(p, "ramp", None) if p is not None else None


def fly_rate(ax: dict, registry=None) -> float:
    """The pace of a fly row, in the knob's unit per second.

    `speed` when the axis gives one (as it always has); else `row_time_s`, the
    time one row should take -- the pace is then what covers the run-in to
    run-out distance in that time; else, for a knob with a ramp block, the
    module's default rate. NaN when none of these is known.
    """
    def num(key):
        try:
            v = float(ax.get(key))
        except (TypeError, ValueError):
            return float("nan")
        return v if math.isfinite(v) else float("nan")

    sp = num("speed")
    if math.isfinite(sp):
        return sp
    rt = num("row_time_s")
    if math.isfinite(rt) and rt > 0:
        try:
            n = int(ax.get("num") or 0)
            span = abs(float(ax["stop"]) - float(ax["start"]))
        except (KeyError, TypeError, ValueError):
            return float("nan")
        w = span / (n - 1) if n > 1 else 0.0
        return (span + w) / rt
    ramp = ramp_of(ax, registry) if registry is not None else None
    if ramp is not None and ramp.rate_default is not None:
        return float(ramp.rate_default)
    return float("nan")


def row_seconds(ax: dict, registry=None) -> float:
    """How long one fly row takes, from the recipe alone (for the ETA)."""
    try:
        num = int(ax.get("num") or 0)
        span = abs(float(ax["stop"]) - float(ax["start"]))
        w = span / (num - 1) if num > 1 else 0.0
        return (span + w) / fly_rate(ax, registry)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return float("nan")


def fly_axis(recipe) -> dict | None:
    """The recipe's fly axis, or None for an ordinary stepped scan."""
    for ax in getattr(recipe, "axes", None) or []:
        if isinstance(ax, dict) and ax.get("type") == "fly":
            return ax
    return None


def validate_fly(recipe, registry) -> list[str]:
    """Problems that only exist for a fly scan ([] for a stepped one)."""
    axes = list(getattr(recipe, "axes", None) or [])
    flies = [i for i, ax in enumerate(axes) if isinstance(ax, dict)
             and ax.get("type") == "fly"]
    if not flies:
        return []
    errs: list[str] = []
    if len(flies) > 1:
        errs.append("a scan can have only one fly axis")
    if flies[-1] != len(axes) - 1:
        errs.append("the fly axis must be the innermost (last) axis: it is one "
                    "continuous move per row, and the axes outside it step")
    ax = axes[flies[-1]]
    pid = ax.get("param")
    try:
        num = int(ax.get("num") or 0)
    except (TypeError, ValueError):
        num = 0
    if num < 2:
        errs.append(f"fly axis '{pid}' needs at least 2 pixels (num)")
    ramp = ramp_of(ax, registry)
    speed = fly_rate(ax, registry)
    if not (math.isfinite(speed) and speed > 0):
        errs.append(f"fly axis '{pid}' needs a speed > 0 (or row_time_s > 0)")
    elif ramp is not None:
        lo, hi = ramp.rate_limits
        if not lo <= speed <= hi:
            errs.append(f"fly rate {speed:g} {ramp.rate_unit} is outside what "
                        f"'{pid}' can sweep [{lo:g},{hi:g}]")
    sp = ax.get("speed_param")
    if sp:
        q = registry.get(sp)
        if q is None:
            errs.append(f"fly axis speed parameter '{sp}' is not available")
        elif q.kind != "settable":
            errs.append(f"fly axis speed parameter '{sp}' is not settable")
        elif math.isfinite(speed):
            lo, hi = q.limits
            if not (lo <= speed <= hi) or speed <= 0:
                errs.append(f"fly speed {speed:g} is outside the limits of "
                            f"'{sp}' [{lo:g},{hi:g}]")
    mv = ax.get("move")
    if mv:
        q = registry.get(mv)
        if q is None:
            errs.append(f"fly axis `move` parameter '{mv}' is not available")
        elif q.kind != "settable":
            errs.append(f"fly axis `move` parameter '{mv}' is not settable")
        elif mv == pid:
            errs.append("fly axis `move` names the axis parameter itself; leave it out")
    rb = ax.get("readback") or pid
    q = registry.get(rb)
    if ramp is not None and not ax.get("readback"):
        pass            # the ramp brings its own readback (or the command)
    elif q is None:
        errs.append(f"fly axis readback '{rb}' is not available")
    elif getattr(q, "stream", None) is None:
        if rb == pid and getattr(registry.get(pid), "kind", "") == "settable":
            errs.append(f"'{rb}' cannot be recorded continuously and cannot "
                        f"sweep: its module neither streams it (as a stage "
                        f"streams its position) nor declares a `ramp` block "
                        f"for it (a knob it can sweep at a set pace), so it "
                        f"cannot be flown -- step it with a linear axis")
        else:
            errs.append(f"'{rb}' cannot be recorded continuously (its module does "
                        f"not stream it), so there is no measured position to bin by")
    for det in getattr(recipe, "detectors", None) or []:
        q = registry.get(det)
        if q is None:
            continue                         # the generic check reports it
        kind = getattr(getattr(q, "storage", None), "kind", "float")
        axes_q = getattr(q, "axes", None) or []
        if len(axes_q) > 1:
            errs.append(f"detector '{det}' returns a {len(axes_q)}-D array per "
                        f"reading; a fly scan bins single values and 1-D traces "
                        f"(a VNA sweep), nothing with more dimensions")
        elif axes_q and getattr(q, "stream", None) is None:
            # since 2026-10-09 a whole TRACE can fly, when its module streams
            # every completed sweep (vna); what cannot is a trace that can
            # only be fetched one acquisition at a time
            errs.append(f"detector '{det}' returns a whole trace and its module "
                        f"does not stream traces. A fly scan records what a "
                        f"module streams: single values (a lock-in, a power "
                        f"meter, single VNA frequency points) and whole 1-D "
                        f"traces from a module that streams every sweep (vna)")
        elif kind in ("enum", "string"):
            # a pixel of a fly row is the MEAN of the samples that fell in it,
            # and there is no mean of "IDLE" and "BUSY"
            errs.append(f"detector '{det}' is {kind}; a fly scan averages the "
                        f"samples of each pixel, which needs numbers")
        elif getattr(q, "stream", None) is None:
            errs.append(f"detector '{det}' cannot be recorded continuously "
                        f"(its module does not stream it); a fly scan can only "
                        f"record streamed detectors")
    fly_name = ax.get("name") or pid
    for h in getattr(recipe, "hooks", None) or []:
        if not isinstance(h, dict):
            continue
        when = h.get("when")
        if when in _PER_POINT:
            errs.append(f"a {when} routine cannot run in a fly scan: the stage "
                        f"does not stop at points. Use 'start/end of each "
                        f"sweep' instead (a sweep of the fly axis is one row)")
        if when in ("before_axis", "after_axis") and h.get("axis") == fly_name:
            errs.append(f"a {when} routine on the fly axis cannot run: it "
                        f"changes continuously. Use each_sweep instead")
    return errs


# ────────────────────────────── the engine part ──────────────────────────────

def fly_sweep(recipe, registry, compiled, dims, shape, total, dets, det_axes,
              det_coords, data, acquire_groups, prev, ctx, t0, on_progress,
              should_abort, on_point, created_iso) -> bool:
    """The fly counterpart of engine._sweep. Returns True on Abort.

    Same signature, same `data` buffers: the outer dims are stepped exactly as
    the odometer steps them, and each row of the innermost (fly) dim is filled
    from one continuous move. Adds `<det>_n` (samples per pixel) and
    `<det>_std` (their spread) next to every detector.
    """
    from .engine import (PAUSE_POLL_S, _Guard, _hold_for_operator, _to_dataset,
                         _unravel, _zigzag, where_of)

    # the engine's fault check / pause (engine._Guard): checked after every
    # row, and a faulted row is flown again once the fault is gone
    guard = ctx.get("guard") or _Guard(None, (), None, should_abort,
                                       lambda m: None, PAUSE_POLL_S)

    ax = fly_axis(recipe)
    fly = dims[-1]
    outer = dims[:-1]
    outer_shape = tuple(shape[:-1])
    npix = fly.size
    n_rows = int(np.prod(outer_shape)) if outer_shape else 1
    current = ctx["current"]
    log = ctx.get("log_fn") or (lambda msg: None)

    pos_p = registry.get(fly.params[0][0])
    # THE RAMP PATH (any knob with a `ramp` block: field, frequency, ...) or
    # the STAGE path (ramp_of explains the choice). In ramp mode the module
    # sweeps the knob from one end of the row to the other at the row's rate,
    # and the row is binned by the ramp's READBACK: the measured value when
    # the module can report it while sweeping, else the commanded one.
    ramp = ramp_of(ax, registry)
    track = None
    if ramp is not None:
        if ax.get("readback"):
            rb = registry.get(ax["readback"])
        elif ramp.readback is not None:
            rb = ramp.readback
        else:
            # nothing streamed at all: compute the commanded value from the
            # start time and the rate (ramp.CommandTrack)
            from .ramp import CommandTrack, Readback
            track = CommandTrack(f"{pos_p.id}.command")
            rb = Readback(f"{pos_p.id}#command", f"{pos_p.label} (commanded)",
                          pos_p.unit, track.spec(), track.channel)
    else:
        rb = registry.get(ax.get("readback") or pos_p.id)
    speed = fly_rate(ax, registry)
    speed_p = registry.get(ax["speed_param"]) if ax.get("speed_param") else None
    # `move`: another stage flies the row, the grid stays in param's coordinates
    move_p = registry.get(ax["move"]) if ax.get("move") else None
    drive = {"k": None, "reversed": 0} if move_p is not None else None
    lag = ax.get("lag_correction", True) is not False
    _, edges = pixel_grid(ax["start"], ax["stop"], npix)
    width = abs(edges[1] - edges[0]) if npix else 0.0
    lo, hi = pos_p.limits
    row_timeout = float(ax.get("timeout_s") or
                        (3.0 * (abs(edges[-1] - edges[0]) / speed) + 30.0))

    # One stream per group, however many parameters share it.
    groups: dict = {}
    # (the readback by object: a ramp's readback is not a registry parameter)
    for spec in [rb.stream] + [registry.get(pid).stream for pid in dets]:
        groups.setdefault(id(spec), spec)
    for spec in groups.values():
        # a stream whose chunks carry `settings` (the VNA's grid) must keep
        # them for the whole scan, across rows (registry.StreamSpec.pin_reset)
        reset = getattr(spec, "pin_reset", None)
        if reset is not None:
            reset()

    params = {det: registry.get(det) for det in dets}
    for det in dets:
        # NaN = row not flown yet (in memory). On disk the count is a uint32
        # and the mean a float64 even for an int/bool detector -- the mean of
        # 3 and 4 is 3.5 (engine._storage_for).
        # A TRACE detector (a VNA sweep): the mean and the spread are traces
        # per pixel, (..., pixel, freq); the count is one number per pixel --
        # how many sweeps went into it.
        inner = tuple(data[det].shape[len(shape):])
        data[f"{det}_n"] = np.full(shape, np.nan)
        data[f"{det}_std"] = np.full(tuple(shape) + inner, np.nan)
        if det_axes.get(det):
            det_axes[f"{det}_std"] = det_axes[det]
    binned_by = ("measurement" if (ramp is None or ax.get("readback")) else ramp.binned_by)
    ctx["var_attrs"] = _var_attrs(params, fly, ax, rb.id, speed)
    if ramp is not None:
        log(f"fly: {pos_p.id} SWEPT by its module ({ramp.kind} ramp) at "
            f"{speed:g} {ramp.rate_unit}; binned by "
            + ("the MEASURED value" if binned_by == "measurement" else
               "the COMMANDED value and its time stamp (the module cannot "
               "report the real one while sweeping)"))

    # The speed the stage had before: the approach to each row runs at it (a
    # fly speed is usually slow, and crawling back across the sample to the
    # start of the next row at it would double the scan), and it is put back
    # at the end, abort or not.
    orig_speed = float("nan")
    if speed_p is not None:
        try:
            orig_speed = float(speed_p.get())
        except Exception:
            orig_speed = float("nan")
    state = {"fly_speed": False}

    def use_speed(value):
        # 0 is a legitimate speed to go BACK to (the simulator's "instant");
        # only a speed that could not be read is skipped
        if speed_p is None or not math.isfinite(value):
            return
        speed_p.set(value)
        state["fly_speed"] = (value == speed)

    warned = {"lag": False}

    def approach(target):
        """Go to a row's run-in and be THERE before the row starts.

        The Settable's blocking set is not enough on its own: a module whose
        settle rule is a bare "moving" flag can answer from a status frame
        from BEFORE the move (gotcha #2) -- on the rig (2026-09-28) the
        approach to row 0 returned at once, the fly speed was set while the
        stage was still on its way, and the fly move turned it round: the
        first 8 pixels of the row stayed empty. So, as at the row's end, the
        MEASURED position decides. With `move` the placement's own settle
        (the camera's laser_settled, checked on every frame) is already a
        measurement, and a camera coordinate is never still enough to wait
        for rest -- so this extra wait is for a stage flown in its own
        coordinates.
        """
        value = pos_p.set(target)
        if move_p is None and ramp is None:
            _await_position(rb, value, 0.5 * width, row_timeout, log,
                            should_abort=should_abort)
        # (a ramp knob: its own settle rule has said "arrived" -- a field
        # seek's field_stable, a generator's echo -- and a measured readback
        # is noisy, so waiting for it to rest would wait for nothing)
        return value

    def snapshot():
        return _to_dataset(recipe, compiled, registry,
                           {k: v.copy() for k, v in data.items()},
                           created_iso, time.monotonic() - t0,
                           det_axes, det_coords, var_attrs=ctx.get("var_attrs"),
                           ds_attrs=ctx.get("ds_attrs"))

    def fly_row(row, redo):
        """Fly ONE row (approach, outer dims, the move, binning).

        Returns (aborted, oidx, backwards). `redo` = this row is flown AGAIN
        after a fault: the outer dims are set again even though their
        index did not change (the fault may have moved them -- a camera
        placement that lost its pattern), without re-firing axis hooks.
        """
        raw = _unravel(row, outer_shape) if outer_shape else ()
        oidx = _zigzag(raw, outer_shape) if (recipe.zigzag and outer_shape) else raw
        backwards = bool(recipe.zigzag) and sum(raw) % 2 == 1

        # -- back to the ORDINARY speed, and to this row's run-in, FIRST.
        # Before the outer axes move, not after: (1) anything that moves
        # the stage between rows must not crawl at the fly speed; (2) when
        # the outer axis is a PLACEMENT that keeps the other coordinate
        # (camera.laser_y keeps the camera's x target), it would otherwise
        # keep the PREVIOUS row's start and drag the laser back across the
        # whole row -- seen on the rig, 2026-09-27: slow returns, and with
        # zig-zag a pointless trip to the far side before every backward row.
        a, b = (edges[-1], edges[0]) if backwards else (edges[0], edges[-1])
        a, b = min(max(a, lo), hi), min(max(b, lo), hi)
        # Already there? With zig-zag a row starts where the last one
        # ended: switching to the approach speed, "approaching" and
        # switching back cost ~0.9 s a row on the rig for nothing.
        # Judged on where the last row's STREAM saw the stage stop, not on
        # rb.get(): a status cache (kim: 8 Hz) can still show the stage a
        # tenth of a second back along the row -- 0.25 um at 2 um/s, just
        # outside half a pixel, so the round-trip came back (rig, 2026-09-28).
        end = state.get("rb_end")
        at_runin = (move_p is None and (state["fly_speed"] or ramp is not None)
                    and end is not None and abs(end - a) <= 0.5 * width)
        if not at_runin:
            if state["fly_speed"]:
                use_speed(orig_speed)
            current[pos_p.id] = approach(a)       # blocking: AT the run-in, at rest
        else:
            current[pos_p.id] = a

        # -- the outer (stepped) dims, exactly as the odometer does them
        first_idx = tuple(oidx) + ((npix - 1) if backwards else 0,)
        ctx["flat"] = row * npix
        ctx["index"] = first_idx
        outer_moved = False
        outer_changed = False
        for k, d in enumerate(outer):
            changed = oidx[k] != prev[k]
            if changed and d.kind == "repeat":
                # a repeat outside the fly axis: may wait for its interval
                from .repeat import pace
                pace(ctx, k, d, int(oidx[k]), outer_changed)
            outer_changed = outer_changed or changed
            if changed or redo:
                outer_moved = True
                if changed and prev[k] is not None:
                    run_hooks(compiled.hooks, "after_axis", ctx, axis_name=d.name)
                    prev[k] = None            # fired; not again if this row is redone
                for pid, values in d.params:
                    current[pid] = registry.get(pid).set(float(values[oidx[k]]))
                if changed:
                    run_hooks(compiled.hooks, "before_axis", ctx, axis_name=d.name)
                prev[k] = oidx[k]

        # each_sweep routines at the START of a sweep fire here (validation
        # has refused every per-point one)
        run_hooks(compiled.hooks, "before_point", ctx)

        # (an outer axis in stage coordinates is another axis: it leaves the
        # run-in where it was, so "already there" still holds)
        if outer_moved and not at_runin:
            # an outer axis that moves the same stage may have moved the
            # run-in too: make sure (a no-op if it did not)
            if state["fly_speed"]:
                use_speed(orig_speed)
            current[pos_p.id] = approach(a)
        if not state["fly_speed"]:
            use_speed(speed)

        if ramp is not None:
            # THE RAMP PATH: the module sweeps the knob over the row
            aborted, chunks = _ramp_one_row(
                ramp, a, b, speed, row_timeout, groups, should_abort, row, npix,
                total, t0 + ctx.get("user_paused_s", 0.0), on_progress, rb,
                params, edges, lag, data, oidx, snapshot, on_point, log, track)
        else:
            while True:
                aborted, chunks, again = _fly_one_row(
                    pos_p, b, row_timeout, groups, should_abort, row, npix, total,
                    # the ETA clock leaves out the time the operator held the scan
                    t0 + ctx.get("user_paused_s", 0.0), on_progress, rb, params,
                    edges, lag, data, oidx, snapshot,
                    on_point, log, a=a, move_p=move_p, drive=drive, speed=speed)
                if not again:
                    break
                # the first guess of the direction was wrong: back to the start
                # of the row (at the approach speed) and fly it again
                if state["fly_speed"]:
                    use_speed(orig_speed)
                current[pos_p.id] = approach(a)
                use_speed(speed)
        current[pos_p.id] = b
        state["rb_end"] = _last_value(chunks, rb)   # where the stream saw it stop
        _bin_into(chunks, rb, params, edges, lag, data, oidx)
        if not warned["lag"]:
            warned["lag"] = True
            _warn_quality(chunks, rb, params, data, oidx, speed, width, log)
        if any(c.get("overflow") for cs in chunks.values() for c in cs):
            log("fly: a stream buffer overflowed -- samples were lost on this "
                "row (slow the stream or shorten the row)")
        return aborted, oidx, backwards

    def blank_row(oidx):
        # a row flown while something was faulted must not stay in the
        # data (or on the live plot while paused): NaN it, the redo refills it
        for name, arr in data.items():
            if arr.ndim >= len(shape):
                arr[tuple(oidx)] = np.nan

    def stop_streams():
        for spec in groups.values():
            try:
                spec.stop()
            except Exception:
                pass

    try:
        for row in range(n_rows):
            if should_abort and should_abort():
                return True
            # the operator's Pause (engine._hold_for_operator): between rows,
            # where Abort is checked -- a row in flight is never cut short
            if _hold_for_operator(ctx, should_abort, f"row {row + 1} of {n_rows}"):
                return True
            redo = False
            while True:
                where = f"row {row + 1} of {n_rows}"
                raw = _unravel(row, outer_shape) if outer_shape else ()
                oidx = _zigzag(raw, outer_shape) if (recipe.zigzag and outer_shape) else raw
                try:
                    aborted, oidx, backwards = fly_row(row, redo)
                except (ScanAborted, ScanFault):
                    raise
                except Exception as exc:
                    # as in the stepped odometer: an error while something the
                    # scan uses reports a fault is that fault -- pause for it
                    faults = guard.faults()
                    if not faults and getattr(exc, "is_fault", False):
                        faults = [(getattr(exc, "instrument", "") or "instrument", str(exc))]
                    if not faults:
                        raise
                    stop_streams()
                    blank_row(oidx)
                    guard.hold(faults, where, cause=exc)
                    redo = True
                    continue
                if aborted:
                    return True
                # the row is binned: was everything trustworthy while it flew?
                faults = guard.faults()
                if not faults:
                    break
                blank_row(oidx)
                guard.hold(faults, where)
                redo = True

            done = (row + 1) * npix
            if on_progress:
                elapsed = time.monotonic() - t0 - ctx.get("user_paused_s", 0.0)
                # WHERE, once per row: the outer index of the row just flown
                # (zig-zag applied); the fly axis itself has no single value
                on_progress(done, total, elapsed / done * (total - done),
                            where=where_of(dims, tuple(oidx), row * npix,
                                           registry, row=(row + 1, n_rows)))
            if on_point:
                on_point(done, total, snapshot)
            # each_sweep routines at the END of a sweep
            ctx["flat"] = row * npix + npix - 1
            ctx["index"] = tuple(oidx) + ((0 if backwards else npix - 1),)
            run_hooks(compiled.hooks, "after_point", ctx)
        return False
    finally:
        for spec in groups.values():
            try:
                spec.stop()
            except Exception:
                pass
        if state["fly_speed"]:
            try:
                use_speed(orig_speed)
            except ScanAborted:
                pass                           # sent; not waited for (aborted)
            except Exception as exc:
                log(f"fly: could not put the stage speed back ({exc})")


def _fly_one_row(pos_p, target, timeout, groups, should_abort, row, npix,
                 total, t0, on_progress, rb, params, edges, lag, data, oidx,
                 snapshot, on_point, log, a=None, move_p=None, drive=None,
                 speed=None):
    """Start the streams, fly to `target`, collect as it goes.

    The move is the position Settable's ordinary BLOCKING set, run in a helper
    thread with a long timeout; this thread meanwhile drains the streams every
    POLL_S, so the row fills in on the live plot and Abort is seen at once.
    With `move_p` (flying in another parameter's coordinates) the stage
    `move_p` is sent well past the end instead, and the row ends when the
    READBACK crosses `target`; see _drive_check. Returns (aborted, {group id:
    [chunks]}, again) -- again = the direction guess was wrong, fly it again.
    """
    chunks = {g: [] for g in groups}
    for spec in groups.values():
        spec.start()
    # THE LEAD-IN, the mirror image of the tail below. A channel `delay`
    # seconds late needs the position from `delay` seconds BEFORE each of its
    # samples; if the stage left the moment recording began, the first part
    # of the row would have no position to look up and its pixels would stay
    # empty. So record at rest for one delay first (the delay is learned from
    # the first read: only the module knows it).
    time.sleep(LEAD_S)
    for g, spec in groups.items():
        chunks[g].append(spec.read())
    lead = max((d for cs in chunks.values() for c in cs
                for d in c["delay_s"].values()), default=0.0)
    if lead > 0:
        time.sleep(min(lead, 5.0))
    result: dict = {}
    mover, goal = pos_p, target
    if move_p is not None:
        # Send the stage FAR past the end, in the direction learned so far
        # (first row: a guess). The camera, not this number, ends the row.
        k = drive["k"] or 1.0
        width = abs(edges[1] - edges[0]) if len(edges) > 1 else 0.0
        way = 1.0 if target >= a else -1.0
        p0 = float(move_p.get())
        lo, hi = move_p.limits
        goal = min(max(p0 + k * way * (1.5 * abs(target - a) + 2 * width), lo), hi)
        mover = move_p
        drive.update(r0=None, t_go=time.monotonic(), way=way, p0=p0,
                     width=width, sent=False, t_sent=0.0, goal=goal)

    def move():
        try:
            mover.set(goal, timeout_s=timeout)
        except BaseException as exc:          # handed back to the scan thread
            result["error"] = exc

    th = threading.Thread(target=move, name="fly-move", daemon=True)
    th.start()
    aborted = False
    last_live = 0.0
    # WHEN IS THE ROW OVER? Not when the blocking set returns: a module whose
    # settle rule is a bare "moving" flag can report "not moving" from the
    # status frame BEFORE the move began (the fire-and-forget window, gotcha
    # #2), and the row would end before the stage has left. So the MEASURED
    # position decides: the set has returned AND the readback has come to
    # REST at the far end (within half a pixel, and unchanged for SETTLED_S:
    # "near the end but still moving" would cut the last pixel short) -- or
    # it has stopped short of it for STALL_S, which is logged, since the end
    # of the row then has no data.
    tol = 0.5 * abs(edges[1] - edges[0]) if len(edges) > 1 else 0.0
    t_deadline = time.monotonic() + timeout
    track = {"pos": None, "since": time.monotonic()}

    def row_over():
        here = _last_value(chunks, rb)
        now = time.monotonic()
        if here is not None and (track["pos"] is None or abs(here - track["pos"]) > 1e-9):
            track["pos"], track["since"] = here, now
        if move_p is not None:
            verdict = _drive_check(drive, here, target, move_p, rb, speed, th,
                                   track, now, log)
            if verdict == "wait":
                if now >= t_deadline:
                    raise TimeoutError(f"fly: {rb.id} did not reach {target:g} "
                                       f"within {timeout:g} s")
                return False
            result.setdefault("verdict", verdict)
            return True
        if th.is_alive():
            return False
        if "error" in result:
            return True
        resting = now - track["since"]
        if here is not None and abs(here - target) <= tol and resting >= SETTLED_S:
            return True
        if resting >= STALL_S:
            log(f"fly: the stage stopped at {here if here is not None else '?'} "
                f"{getattr(pos_p, 'unit', '')}, short of {target:g}")
            return True
        if now >= t_deadline:
            raise TimeoutError(f"fly: {pos_p.id} did not reach {target:g} within "
                               f"{timeout:g} s")
        return False

    while not row_over():
        th.join(POLL_S)
        for g, spec in groups.items():
            chunks[g].append(spec.read())
        if should_abort and should_abort():
            aborted = True
            if move_p is not None:
                _stop_mover(move_p, drive, speed, log)
            else:
                _stop_stage(pos_p, chunks, rb, log)
            th.join(5.0)
            break
        _bin_into(chunks, rb, params, edges, lag, data, oidx)
        first = next(iter(params), None)
        n_done = (int(np.count_nonzero(data[f"{first}_n"][tuple(oidx)] > 0))
                  if first else 0)
        done = row * npix + min(n_done, npix - 1)
        if on_progress and done > 0:
            elapsed = time.monotonic() - t0
            on_progress(done, total, elapsed / done * (total - done))
        now = time.monotonic()
        if on_point and now - last_live >= POLL_S:
            last_live = now
            on_point(done, total, snapshot)
    if move_p is not None and not aborted:
        th.join(15.0)                         # the stop has been sent: let it land
    again = result.get("verdict") == "reverse"
    if not aborted and "error" not in result and not again:
        # THE TAIL. A lagging channel's last samples belong to the end of the
        # row but are only RECORDED up to its delay after the stage stops, so
        # keep recording that long -- otherwise the last pixel of every row
        # comes out empty. (Plus one sample's own duration -- a VNA sweep
        # under way when the stage stopped: _sample_span.)
        tail = max((d for cs in chunks.values() for c in cs
                    for d in c["delay_s"].values()), default=0.0) + _sample_span(chunks)
        if tail > 0:
            time.sleep(min(tail + 0.02, 5.0))
    for g, spec in groups.items():
        try:
            chunks[g].append(spec.stop())
        except Exception as exc:
            log(f"fly: stopping stream {spec.group} failed ({exc})")
    err = result.get("error")
    if isinstance(err, ScanAborted):
        aborted = True
        if move_p is not None:
            _stop_mover(move_p, drive, speed, log)
        else:
            _stop_stage(pos_p, chunks, rb, log)
    elif err is not None and not aborted and not (move_p is not None and drive.get("sent")):
        # (a mover stopped on purpose may report the stop as an error -- a
        # settle wait whose target moved; the row itself is fine)
        raise err
    return aborted, chunks, again


def _ramp_one_row(ramp, a, b, rate, timeout, groups, should_abort, row, npix,
                  total, t0, on_progress, rb, params, edges, lag, data, oidx,
                  snapshot, on_point, log, track=None):
    """One row of the RAMP path: the module sweeps the knob from a to b.

    Same shape as _fly_one_row: start the streams, record at rest for the
    lead-in, start the ramp, drain the streams every POLL_S (live plot,
    progress, Abort), then the tail. Returns (aborted, {group id: [chunks]}).

    WHEN IS THE ROW OVER? When the MODULE says its ramp is over (the numbered
    done check of ramp.RampSpec -- never a settle rule, gotcha #35), and, for
    a measured readback, once the reading has also come within half a pixel
    of the end (a coil lags its setpoint) or ARRIVE_S has passed since the
    module said done (a noisy reading may never sit inside half a small
    pixel; the pixels it did not reach stay empty and are logged).
    """
    chunks = {g: [] for g in groups}
    if track is not None:
        track.rest(a)
    for spec in groups.values():
        spec.start()
    handle = None
    try:
        time.sleep(LEAD_S)
        for g, spec in groups.items():
            chunks[g].append(spec.read())
        lead = max((d for cs in chunks.values() for c in cs
                    for d in c["delay_s"].values()), default=0.0)
        if lead > 0:
            time.sleep(min(lead, 5.0))
        handle = ramp.start(b, rate)
        if track is not None:
            track.go(a, b, rate)
        tol = 0.5 * abs(edges[1] - edges[0]) if len(edges) > 1 else 0.0
        t_deadline = time.monotonic() + timeout
        done_at = None
        aborted = False
        last_live = 0.0
        while True:
            # sleep POLL_S in short slices: Abort is seen within ~50 ms
            end = time.monotonic() + POLL_S
            while time.monotonic() < end:
                if should_abort and should_abort():
                    break
                time.sleep(0.05)
            for g, spec in groups.items():
                chunks[g].append(spec.read())
            if should_abort and should_abort():
                aborted = True
                _stop_ramp(ramp, track, log, "aborted mid-row")
                break
            _bin_into(chunks, rb, params, edges, lag, data, oidx)
            first = next(iter(params), None)
            n_done = (int(np.count_nonzero(data[f"{first}_n"][tuple(oidx)] > 0))
                      if first else 0)
            done = row * npix + min(n_done, npix - 1)
            now = time.monotonic()
            if on_progress and done > 0:
                on_progress(done, total, (now - t0) / done * (total - done))
            if on_point and now - last_live >= POLL_S:
                last_live = now
                on_point(done, total, snapshot)
            if ramp.done(handle):
                if done_at is None:
                    done_at = now
                here = _last_value(chunks, rb)
                if (not ramp.measured or rb is not ramp.readback or here is None
                        or abs(here - b) <= tol or now - done_at >= ARRIVE_S):
                    if (ramp.measured and here is not None and abs(here - b) > tol):
                        log(f"fly: the sweep ended with the reading at {here:g}, "
                            f"short of {b:g} (more than half a pixel)")
                    break
            if now >= t_deadline:
                _stop_ramp(ramp, track, log, "row timed out")
                raise TimeoutError(f"fly: the sweep to {b:g} did not end within "
                                   f"{timeout:g} s")
        if not aborted:
            # THE TAIL: a lagging detector records the end of the row late,
            # and a sweep under way at the end finishes one sweep later
            tail = max((d for cs in chunks.values() for c in cs
                        for d in c["delay_s"].values()), default=0.0) + _sample_span(chunks)
            if tail > 0:
                time.sleep(min(tail + 0.02, 5.0))
    except BaseException:
        # whatever went wrong (a fault, a refused command, Ctrl-C), the knob
        # must not keep sweeping on its own after the scan has let go of it
        if handle is not None:
            _stop_ramp(ramp, track, log, "error mid-row")
        for spec in groups.values():
            try:
                spec.stop()
            except Exception:
                pass
        raise
    for g, spec in groups.items():
        try:
            chunks[g].append(spec.stop())
        except Exception as exc:
            log(f"fly: stopping stream {spec.group} failed ({exc})")
    return aborted, chunks


def _stop_ramp(ramp, track, log, why):
    """Stop a sweep where it is (Abort, a timeout, an error)."""
    try:
        ramp.stop()
        log(f"fly: {why}, sweep stopped")
    except Exception as exc:                  # noqa: BLE001
        log(f"fly: {why}, could not stop the sweep ({exc})")
    if track is not None:
        track.stopped()


def _drive_check(drive, here, target, move_p, rb, speed, th, track, now, log):
    """Where is a row flown by ANOTHER stage (fly axis with `move`)?

    Returns "wait", "done" (the readback crossed the far edge; the stage has
    been told to stop), "reverse" (the first guess of direction was wrong;
    the stage has been stopped), or "stall" (the stage stopped without the
    readback reaching the end -- logged).
    """
    way = drive["way"]
    if here is not None and drive["r0"] is None:
        drive["r0"] = here
    # "At rest" is judged on the STAGE here, never on the readback: a camera
    # coordinate is never still (pixel noise, drift), so waiting for it to stop
    # changing would wait forever.
    if drive["sent"]:
        # the stop has been sent (and waited for by _stop_mover): give the
        # lagging channels their tail and finish
        if not th.is_alive() and now - drive["t_sent"] >= SETTLED_S:
            return drive["sent"]
        return "wait"
    arrived = False
    if not th.is_alive():
        # the move returned -- which a stale "not moving" frame can make it do
        # before the stage has even left (gotcha #35): believe it only when
        # the stage REPORTS being at the far goal
        try:
            arrived = abs(float(move_p.get()) - drive["goal"]) <= max(drive["width"], 0.3)
        except Exception:
            arrived = True
    if here is None or drive["r0"] is None:
        return "stall" if arrived else "wait"
    moved = here - drive["r0"]
    need = max(drive["width"], 0.3)
    if drive["k"] is None and abs(moved) >= need:
        if moved * way < 0:
            if drive["reversed"]:
                raise RuntimeError(f"fly: {rb.id} runs AGAINST {move_p.id} in both "
                                   f"directions -- is the pattern still tracked?")
            drive["reversed"] += 1
            drive["k"] = -1.0
            log(f"fly: moving {move_p.id} up moves {rb.id} DOWN -- learned; "
                f"flying this row again")
            _stop_mover(move_p, drive, speed, log, quiet=True)
            drive["sent"], drive["t_sent"] = "reverse", time.monotonic()
            return "wait"
        drive["k"] = 1.0 if drive["k"] is None else drive["k"]
    if (here - target) * way >= 0:
        _stop_mover(move_p, drive, speed, log, quiet=True)
        drive["sent"], drive["t_sent"] = "done", time.monotonic()
        return "wait"
    if arrived:
        log(f"fly: {move_p.id} stopped with {rb.id} at {here:g}, short of "
            f"{target:g} -- out of travel, or its um are much smaller than "
            f"{rb.id}'s")
        return "stall"
    if now - drive["t_go"] > max(3.0, 20 * need / max(speed or 1.0, 1e-9)) \
            and abs(moved) < 0.5 * need:
        _stop_mover(move_p, drive, speed, log, quiet=True)
        raise RuntimeError(f"fly: moving {move_p.id} does not move {rb.id} -- the "
                           f"other axis? (change `move` on the fly axis)")
    return "wait"


def _stop_mover(move_p, drive, speed, log, quiet=False):
    """Stop the flying stage: a setpoint just AHEAD of where it last reported
    being. Its status can be a frame old, and a setpoint behind it would make
    an open-loop stage walk BACK over the row; a little further on is harmless."""
    try:
        here = float(move_p.get())
        way = (drive.get("k") or 1.0) * drive.get("way", 1.0)
        move_p.set(here + way * float(speed or 0.0) * 0.15, timeout_s=10.0)
    except ScanAborted:
        pass
    except Exception as exc:
        if not quiet:
            log(f"fly: could not stop {move_p.id} ({exc})")


def _await_position(rb, target, tol, timeout, log, rest_s=0.2, poll_s=0.05,
                    should_abort=None):
    """Block until the readback `rb` is within `tol` of `target` AND at rest.

    At rest = has not changed by more than a quarter of `tol` for `rest_s`
    (a step counter is exactly still; a sensor jitters a little). A stage
    that has not started yet is simply waited for -- it will: the command
    was accepted. Raises TimeoutError after `timeout` s, naming both numbers.

    `should_abort` is checked on every poll and raises ScanAborted, like every
    other wait in a scan: this one can last the whole row timeout (a slow
    stage crawling back across the sample), and until 2026-09-28 Abort did
    nothing for that long.
    """
    deadline = time.monotonic() + timeout
    last, since, v = None, time.monotonic(), float("nan")
    while True:
        if should_abort is not None and should_abort():
            raise ScanAborted(f"fly: aborted while {rb.id} was on its way to "
                              f"the run-in {target:g}")
        try:
            v = float(rb.get())
        except Exception:
            v = float("nan")
        now = time.monotonic()
        if math.isfinite(v):
            if last is None or abs(v - last) > 0.25 * max(tol, 1e-12):
                last, since = v, now
            elif abs(v - target) <= tol and now - since >= rest_s:
                return v
        if now >= deadline:
            raise TimeoutError(f"fly: {rb.id} did not arrive at the run-in {target:g} "
                               f"within {timeout:g} s (at {v:g})")
        time.sleep(poll_s)


def _last_value(chunks, rb):
    """The most recent finite readback position recorded, or None."""
    scale = float(getattr(rb, "stream_scale", 1.0) or 1.0)
    for c in reversed(chunks.get(id(rb.stream), [])):
        v = c["values"].get(rb.stream_channel)
        if v is not None and len(v):
            fin = v[np.isfinite(v)]
            if len(fin):
                return float(fin[-1]) / scale
    return None


def _stop_stage(pos_p, chunks, rb, log):
    """Abort mid-row: ask the stage to stay where it is.

    Nothing generic says "stop" to every module, but every position knob takes
    a setpoint, and a setpoint at the current position IS a stop. The command
    goes out; the wait for it is cut short by the same Abort, which is fine --
    the operator pressed Abort to stop waiting.
    """
    here = _last_value(chunks, rb)
    if here is None:
        try:
            here = float(pos_p.get())
        except Exception:
            return
    try:
        pos_p.set(here, timeout_s=10.0)
        log(f"fly: aborted mid-row, stage stopped at {here:g} {pos_p.unit}")
    except ScanAborted:
        log(f"fly: aborted mid-row, stop sent at {here:g} {pos_p.unit}")
    except Exception as exc:
        log(f"fly: aborted mid-row, could not stop the stage ({exc})")


def _joined(chunks, p):
    """(t, values, delay) of parameter p's channel over all chunks read so far.

    Values come back in the PARAMETER's unit: a stream carries wire units, and
    `stream_scale` (the descriptor's `scale`, wire = display x scale) converts
    them exactly as the parameter's one-value getter does -- pm16 streams watts
    and offers milliwatts.
    """
    spec, channel = p.stream, p.stream_channel
    scale = float(getattr(p, "stream_scale", 1.0) or 1.0)
    ts, vs, delay = [], [], 0.0
    for c in chunks.get(id(spec), []):
        why = (c.get("errors") or {}).get(channel)
        if why:
            # the module says this channel cannot be streamed now (a VNA's u
            # with no reference): stop, with ITS reason -- never a silent
            # row of NaN
            raise RuntimeError(f"{getattr(p, 'id', channel)}: {why}")
        v = c["values"].get(channel)
        if v is None or len(v) == 0:
            continue
        ts.append((c.get("t_ch") or {}).get(channel, c["t"]))
        vs.append(v)
        delay = c["delay_s"].get(channel, delay)
    if not ts:
        return np.array([]), np.array([]), 0.0
    try:
        vals = np.concatenate(vs)
    except ValueError:
        raise ValueError(f"{getattr(p, 'id', channel)}: the streamed traces changed "
                         f"length during the row") from None
    return np.concatenate(ts), (vals / scale if scale != 1.0 else vals), float(delay)


def _sample_span(chunks) -> float:
    """The longest time one streamed sample took to measure (t_end - t_start;
    a VNA sweep), 0 when no stream says. A row must keep recording that long
    after the knob stops, or the sweep under way at the end -- whose time
    stamp may still lie inside the row -- is lost."""
    span = 0.0
    for cs in chunks.values():
        for c in cs:
            a, b = c.get("t_start"), c.get("t_end")
            if a is not None and b is not None and len(a) and len(a) == len(b):
                d = np.asarray(b, dtype=float) - np.asarray(a, dtype=float)
                d = d[np.isfinite(d)]
                if d.size:
                    span = max(span, float(d.max()))
    return span


def _bin_into(chunks, rb, params, edges, lag, data, oidx):
    """Re-bin everything recorded on this row so far into its data row."""
    t_pos, pos, pos_delay = _joined(chunks, rb)
    for det, p in params.items():
        t, v, delay = _joined(chunks, p)
        want = data[det].shape[len(oidx) + 1:]
        if len(v) and tuple(v.shape[1:]) != tuple(want):
            # the trace no longer matches the coordinate read at the start of
            # the scan (points changed) -- refuse rather than misfile it
            raise ValueError(f"{det}: the streamed trace has {v.shape[1:] or 'no'} "
                             f"points, the scan's coordinate {want or 'none'}: the "
                             f"instrument's sweep changed")
        m, n, s = bin_samples(t_pos, pos, t, v, edges,
                              delay_s=delay if lag else 0.0,
                              pos_delay_s=pos_delay if lag else 0.0)
        data[det][tuple(oidx)] = m
        data[f"{det}_n"][tuple(oidx)] = n
        data[f"{det}_std"][tuple(oidx)] = s


def _var_attrs(params, fly, ax, rb_id, speed) -> dict:
    """Attributes for the dataset: what each variable is, and how it was flown.
    (How the AXIS was flown -- speed, mode, `fly_binned_by` -- is written by
    recipe.axis_attrs, one mechanism for every advanced axis setting.)"""
    out = {}
    for det, p in params.items():
        label = getattr(p, "label", det)
        trace = bool(getattr(p, "axes", None))
        cplx = getattr(p, "dtype", "") == "complex"
        out[det] = {"fly_stat": "mean"}
        out[f"{det}_n"] = {"units": "",
                           "label": f"{label}: {'traces' if trace else 'samples'} per pixel",
                           "fly_stat": "count"}
        out[f"{det}_std"] = {"units": getattr(p, "unit", ""),
                             "label": f"{label}: spread within the pixel"
                                      + (" (rms |z - mean|)" if cplx else ""),
                             "fly_stat": "std"}
    out[fly.name] = {"fly": "true", "readback": rb_id,
                     "speed": float(speed),
                     "lag_correction": "true" if ax.get("lag_correction", True)
                     is not False else "false"}
    return out


def _warn_quality(chunks, rb, params, data, oidx, speed, width, log):
    """After the first row: say so if the numbers make the image unreliable."""
    for det, p in params.items():
        _, _, delay = _joined(chunks, p)
        smear = speed * delay
        if width > 0 and smear > width:
            log(f"fly: {det} lags {delay * 1e3:.3g} ms = {smear:.3g} "
                f"{getattr(rb, 'unit', '')} at this speed ({smear / width:.1f} "
                f"pixels). The shift is corrected, but detail finer than that "
                f"is smeared: fly slower or use a shorter time constant.")
    span = _sample_span(chunks)
    if width > 0 and speed * span > width:
        # a whole trace is filed at the MIDDLE of its sweep: point 0 was
        # measured half a sweep earlier, the last point half a sweep later
        log(f"fly: one sweep takes {span * 1e3:.3g} ms = {speed * span:.3g} "
            f"{getattr(rb, 'unit', '')} at this speed ({speed * span / width:.1f} "
            f"pixels): each trace is filed at its sweep's middle, so its ends "
            f"belong to neighbouring pixels. Fly slower, sweep fewer points, or "
            f"record single frequency points (each stamped at its own moment).")
    for det, p in params.items():
        n = data[f"{det}_n"][tuple(oidx)]
        med = float(np.nanmedian(n)) if np.size(n) else 0.0
        if med < 3:
            log(f"fly: {det} has only ~{med:.0f} samples per pixel; the "
                f"average is thin -- fly slower or use fewer pixels.")
        if np.any(n == 0):
            log(f"fly: {int(np.sum(n == 0))} pixel(s) of {det} got no sample "
                f"on the first row (NaN in the data).")
