"""repeat.py -- the REPEAT axis: measure the same thing N times (2026-10-04).

Lukas: "averaging by repeating axis". A repeat axis sets NOTHING; it only says
"do everything inside me N times". Where it sits in the axis stack decides
what is repeated, exactly as for any other axis:

    axes: [repeat, field, freq]   outermost: N whole scans ("runs")
    axes: [field, repeat, freq]   in between: every frequency sweep N times
    axes: [field, freq, repeat]   innermost: every POINT N times in a row

    {type: repeat, num: 5}                          mode keep (the default)
    {type: repeat, num: 5, mode: average}
    {type: repeat, num: 10, interval_s: 60}         start a run at most once a minute
    {type: repeat, num: 3, name: run}               own dimension name

MODE `keep` (the default): the repeat is a real dimension of the data,
called `repeat` (`repeat_1`, `repeat_2`, ... when a scan has several), with
coordinate 0..N-1. Every run is in the file; the viewer shows one run (hold
the repeat slider) or the average of all of them (average over it). Nothing is
thrown away, so this is the right choice whenever you are not sure: drift
between runs, a run spoiled by a bump in the lab, a slowly changing sample --
all still visible afterwards.

MODE `average`: the repeat dimension is collapsed. For every detector the
file holds
    <det>        the MEAN over the repeats   (float64; NaN ignored)
    <det>_std    their SAMPLE standard deviation (ddof=1; NaN with < 2 values)
    <det>_n      how many repeats gave a finite value (uint32)
-- the same three a fly scan stores per pixel. Use it for a long averaging
run where the individual repeats are of no interest and would only make the
file N times bigger. The mean of a bool is the fraction of True; of an int a
float (the mean of 3 and 4 is 3.5), so the mean is float64 whatever the
detector's declared type, and the declared type is kept in `declared_type`.
A COMPLEX detector (a VNA trace) is averaged coherently -- the mean of the
complex values, as the instrument's own averaging does -- and its `_std` is
the spread of |z|: one real number a reader can put an error bar on. A trace
is averaged element-wise. Enum and string detectors are refused: there is no
mean of "IDLE" and "BUSY" (use keep). An aborted average scan keeps the mean
over the repeats done so far, and `_n` says how many that was.

How `average` is implemented: in memory the engine records every repeat
exactly as in `keep` (the odometer, the fault pause, the redo of a point --
all unchanged) and the dimension is collapsed whenever a dataset is BUILT:
for the live plot (which therefore shows the running mean), for an abort,
and for the file. Memory costs N times the averaged size; the file does not.

INTERVAL (`interval_s`, optional): repeat k (counting from 0) of a pass does
not START before t0 + k * interval, where t0 is when repeat 0 of that pass
started. A pass taking longer than the interval simply starts the next one at
once (no catching up). The wait can be aborted. Useful for a time series:
"a field sweep every 10 minutes for 2 hours".

Limits (refused by validate, with the reason): N must be a whole number
>= 1; only ONE average repeat per scan (several averages would multiply into
one anyway); a repeat cannot sit INSIDE a fly axis (a fly row is one
continuous move); `average` cannot be combined with a fly axis or with the
resonance window (both add per-point variables of their own whose average
would mean something else) -- use `keep` there and average in the viewer.
"""

from __future__ import annotations

import math
import time

import numpy as np

#: the two modes; the first is the default
MODES = ("keep", "average")

#: the default dimension name of a repeat axis (several: repeat_1, repeat_2, ...)
DEFAULT_NAME = "repeat"

#: seconds between abort checks while waiting for the interval
_WAIT_POLL_S = 0.05


def mode_of(ax: dict) -> str:
    return (ax.get("mode") or "keep") if isinstance(ax, dict) else "keep"


def is_repeat(ax) -> bool:
    return isinstance(ax, dict) and ax.get("type") == "repeat"


def num_of(ax: dict) -> int:
    """N of a repeat axis; raises ValueError for anything that is not a whole
    number >= 1 (2.5 repeats is a typo, not a request to round)."""
    n = ax.get("num")
    if isinstance(n, bool):
        raise ValueError(f"repeat num must be a whole number >= 1, not {n!r}")
    try:
        f = float(n)
    except (TypeError, ValueError):
        raise ValueError(f"repeat num must be a whole number >= 1, not {n!r}") from None
    if not math.isfinite(f) or f != int(f) or f < 1:
        raise ValueError(f"repeat num must be a whole number >= 1, not {n!r}")
    return int(f)


def interval_of(ax: dict) -> float | None:
    v = ax.get("interval_s")
    if v in (None, "", 0):
        return None
    return float(v)


# ─────────────────────────────── validation ──────────────────────────────────

def validate(recipe, registry) -> list[str]:
    """Problems with the repeat axes of `recipe` ([] when it has none)."""
    axes = list(getattr(recipe, "axes", None) or [])
    reps = [i for i, ax in enumerate(axes) if is_repeat(ax)]
    if not reps:
        return []
    errs: list[str] = []
    for i in reps:
        ax = axes[i]
        try:
            num_of(ax)
        except ValueError as exc:
            errs.append(str(exc))
        if mode_of(ax) not in MODES:
            errs.append(f"repeat mode must be 'keep' or 'average', not {ax.get('mode')!r}")
        v = ax.get("interval_s")
        if v not in (None, ""):
            try:
                ok = math.isfinite(float(v)) and float(v) >= 0
            except (TypeError, ValueError):
                ok = False
            if not ok:
                errs.append(f"repeat interval_s must be a number of seconds >= 0, "
                            f"not {v!r}")
    averages = [i for i in reps if mode_of(axes[i]) == "average"]
    if len(averages) > 1:
        errs.append("only one repeat axis per scan can average; make the others "
                    "mode 'keep' (or use one repeat with N = the product)")
    flies = [i for i, ax in enumerate(axes) if isinstance(ax, dict)
             and ax.get("type") == "fly"]
    if flies and any(i > flies[0] for i in reps):
        errs.append("a repeat axis cannot sit inside a fly axis: a fly row is one "
                    "continuous move. Put the repeat OUTSIDE (above) the fly axis")
    if averages and flies:
        errs.append("a repeat in mode 'average' cannot be combined with a fly axis "
                    "(a fly pixel is already a mean with its own _n/_std): use "
                    "mode 'keep' and average the runs in the viewer")
    if averages and getattr(recipe, "window", None):
        errs.append("a repeat in mode 'average' cannot be combined with the "
                    "resonance window (its mask and record are per measurement): "
                    "use mode 'keep'")
    if averages:
        for det in getattr(recipe, "detectors", None) or []:
            p = registry.get(det)
            if p is None:
                continue                     # the generic check reports it
            kind = getattr(getattr(p, "storage", None), "kind", None) \
                or getattr(p, "dtype", "float")
            if kind in ("enum", "string", "text"):
                errs.append(f"detector '{det}' is {kind}: a repeat in mode 'average' "
                            f"needs numbers (there is no mean of two states). "
                            f"Use mode 'keep', or untick the detector")
    return errs


# ─────────────────────────────── compile ─────────────────────────────────────

def name_dims(dims) -> None:
    """Name the repeat dims that were not given a `name`: `repeat` when the
    scan has one, `repeat_1`, `repeat_2`, ... (outer first) when it has
    several. In place."""
    unnamed = [d for d in dims if d.kind == "repeat" and not d.name]
    for k, d in enumerate(unnamed, start=1):
        d.name = DEFAULT_NAME if len(unnamed) == 1 else f"{DEFAULT_NAME}_{k}"


def average_index(dims) -> int | None:
    """Position of the (one) averaged repeat dim among `dims`, or None."""
    for k, d in enumerate(dims):
        if d.kind == "repeat" and d.mode == "average":
            return k
    return None


# ─────────────────────────────── pacing ──────────────────────────────────────

def pace(ctx, k: int, d, i: int, new_pass: bool) -> None:
    """Called when repeat dim `k` moves to index `i`. Keeps the time repeat 0
    of this pass started, and with an interval WAITS until repeat number
    `count` may start (t0 + count * interval).

    `new_pass` = a dim outside this one changed at the same point (or this is
    the first point): the repeats start counting again. A retried point that
    lands here twice at the same index counts once.
    """
    if getattr(d, "kind", "") != "repeat":
        return
    clocks = ctx.setdefault("repeat_clock", {})
    now = time.monotonic()
    clock = clocks.get(k)
    if new_pass or clock is None:
        clocks[k] = {"t0": now, "count": 0, "i": i}
        return
    if clock["i"] == i:
        return                               # the same visit, retried
    clock["count"] += 1
    clock["i"] = i
    interval = getattr(d, "interval_s", None)
    if not interval:
        return
    due = clock["t0"] + clock["count"] * float(interval)
    wait = due - now
    if wait <= 0:
        return                               # the pass took longer: go at once
    guard = ctx.get("guard")
    should_abort = getattr(guard, "should_abort", None)
    if wait > 1.0:
        (ctx.get("log_fn") or (lambda m: None))(
            f"{d.name} {clock['count'] + 1}/{d.size}: waiting {wait:.1f} s "
            f"for the {float(interval):g} s interval")
    from .errors import ScanAborted
    while True:
        left = due - time.monotonic()
        if left <= 0:
            return
        if should_abort and should_abort():
            raise ScanAborted("aborted while waiting for the repeat interval")
        time.sleep(min(_WAIT_POLL_S, left))


def min_seconds(compiled) -> float:
    """The least time the repeat intervals alone force on a scan (for the
    builder's ETA): each pass of a repeat dim with an interval lasts at least
    (N - 1) * interval, and there are prod(outer sizes) passes."""
    total, outer = 0.0, 1
    for d in compiled.dims:
        if d.kind == "repeat" and getattr(d, "interval_s", None):
            total = max(total, outer * (d.size - 1) * float(d.interval_s))
        outer *= d.size
    return total


# ─────────────────────────────── collapse ────────────────────────────────────

def collapse(data: dict, axis: int, dets, det_axes: dict, registry):
    """Average the repeat dim `axis` out of every detector buffer.

    Returns (data', det_axes', var_attrs') with <det> = mean, <det>_std and
    <det>_n for every detector in `dets`; anything else in `data` is passed
    through (validation keeps window/fly variables out of an average scan).
    """
    out, axes_out, attrs = {}, dict(det_axes), {}
    for name, arr in data.items():
        if name not in dets:
            out[name] = arr
            continue
        x = np.asarray(arr)
        cplx = np.iscomplexobj(x)
        ok = np.isfinite(x)                   # complex: both parts finite
        n = ok.sum(axis=axis)
        filled = np.where(ok, x, 0)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = filled.sum(axis=axis) / n
            mean = np.where(n > 0, mean, np.nan + (1j * np.nan if cplx else 0))
            # the spread: of |z| for a complex value (see the module docstring)
            mag = np.abs(x) if cplx else x
            mag_ok = np.where(ok, mag, 0.0)
            m_mean = mag_ok.sum(axis=axis) / n
            dev = np.where(ok, mag - np.expand_dims(m_mean, axis), 0.0)
            var = (dev ** 2).sum(axis=axis) / (n - 1)
            std = np.where(n >= 2, np.sqrt(var), np.nan)
        out[name] = mean.astype(np.complex128 if cplx else np.float64)
        out[f"{name}_std"] = std.astype(np.float64)
        # a count is never NaN; float in memory like every other number
        out[f"{name}_n"] = n.astype(np.float64)
        axes_out[f"{name}_std"] = det_axes.get(name, [])
        axes_out[f"{name}_n"] = det_axes.get(name, [])
        p = registry.get(name)
        label = getattr(p, "label", name)
        attrs[name] = {"repeat_stat": "mean"}
        attrs[f"{name}_std"] = {
            "units": getattr(p, "unit", "") or "",
            "label": f"{label}: spread over the repeats"
                     + (" (std of |z|)" if cplx else ""),
            "repeat_stat": "std"}
        attrs[f"{name}_n"] = {"units": "", "label": f"{label}: repeats averaged",
                              "repeat_stat": "count"}
    return out, axes_out, attrs
