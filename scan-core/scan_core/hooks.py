"""
hooks.py — actions bound to a level/cadence of the scan.

This is where the old "AF after inner loop", "AF every n iterations",
"wait before measure", "phase-lock at f change" checkboxes go — but as a small
open registry of named actions instead of hardcoded flags. A hook in a recipe:

    {when: "every_n_points", n: 25, action: "autofocus"}
    {when: "before_point",           action: "wait_ms", args: {ms: 200}}
    {when: "after_axis", axis: "field", action: "autofocus"}

and a ROUTINE (the `call` action), which drives the registry itself:

    {when: "before_scan", action: "call",
     args: {set: {mag2d.field: 150, mag2d.angle: 45}, action: vna.take_reference}}
    {when: "after_scan",  action: "call", args: {set: {mag2d.field: 0}}}

and a routine THROUGHOUT the scan (2026-09-24), e.g. autofocus once per row:

    {when: each_sweep, axis: x, edge: start, every: 1, on_error: continue,
     action: call, args: {action: camera.autofocus}}
    {when: every_n_points, n: 100, action: call, args: {action: vna.take_reference}}

`when` values: before_scan, after_scan, before_point, after_point,
every_n_points (needs n), each_sweep (needs axis), before_axis/after_axis
(needs axis name; kept for old recipes -- each_sweep is what the builder
writes). The engine calls run_hooks() at each of those moments. Add a new
capability = register one function here; recipes can use it immediately.

EACH SWEEP, and why it is not after_axis. One sweep of axis A = A runs from its
first value to its last while every axis OUTSIDE it stands still. In the
odometer that is a block of prod(shape[a:]) consecutive points, so

    a sweep of A starts at point `flat`  <=>  flat % block == 0

-- the same one-line rule at any depth (5-D is no different from 2-D), and it
counts VISITS, not index changes. after_axis(A) fires when A's own index
changes, which is wrong three ways: on the inner axis that is every point; with
zig-zag the inner index does NOT change at a turnaround (..8, 9, 9, 8..), so the
row boundary is missed; and when an outer axis steps, every inner axis resets
at once, so hooks keyed on each of them fire together.

`edge: start` (default) fires before the first point of every sweep, after the
new values are SET -- autofocus at the position the row is measured at -- and
includes the very first sweep. `edge: end` fires after the last point of every
sweep except the scan's last one, which is after_scan's moment. `every: m` fires
on every m-th sweep only (the 1st, m+1-th, ... at start; the m-th, 2m-th, ... at
end).

`on_error: continue` (any hook): a failure is logged and the scan carries on --
an autofocus that finds no peak should not throw away a night's map. The
default is to stop, as before. An Abort is never swallowed.
"""

from __future__ import annotations

import math
import time

from .errors import ScanAborted

ACTIONS = {}

#: Every moment the engine fires. validate() uses it to catch a typo such as
#: "befor_scan", which would otherwise be a routine that silently never runs.
MOMENTS = ("before_scan", "after_scan", "before_point", "after_point",
           "every_n_points", "each_sweep", "before_axis", "after_axis")

#: The edges of a sweep an each_sweep hook can fire at.
EDGES = ("start", "end")

#: What a failing hook does to the scan.
ON_ERROR = ("stop", "continue")


def action(name):
    def deco(fn):
        ACTIONS[name] = fn
        return fn
    return deco


@action("wait_ms")
def _wait_ms(ctx, ms: float = 100.0):
    time.sleep(float(ms) / 1000.0)


def find_autofocus(registry) -> str | None:
    """The id of the registry's autofocus ACTION, or None.

    `camera.autofocus` first (the camera module, 2026-09-24: it waits for its
    own numbered run and raises if the run failed), then any action named
    autofocus under another prefix (a second camera, the simulator's
    `sim_autofocus`)."""
    get_action = getattr(registry, "get_action", None)
    actions = getattr(registry, "actions", None)
    if get_action is None or actions is None:
        return None
    if get_action("camera.autofocus") is not None:
        return "camera.autofocus"
    for a in actions():
        if a.id == "autofocus" or a.id.endswith((".autofocus", "_autofocus")):
            return a.id
    return None


@action("autofocus")
def _autofocus(ctx, **kw):
    """{action: autofocus} = run the registry's autofocus action and WAIT for it.

    This was a stub that only logged, so an older recipe asking for autofocus
    every 200 points silently never focused. It is now the same thing as
    {action: call, args: {action: camera.autofocus}}, which is what the Scan
    Builder writes; validate() refuses it when nothing offers an autofocus.
    """
    aid = find_autofocus(ctx["registry"])
    if aid is None:
        raise KeyError("autofocus: no autofocus action here (is the camera connected?)")
    _call(ctx, action=aid)


@action("settle_extra")
def _settle_extra(ctx, seconds: float = 0.5):
    time.sleep(float(seconds))


def _fmt(p, value) -> str:
    unit = getattr(p, "unit", "") or ""
    return f"{p.id} = {float(value):g}{(' ' + unit) if unit else ''}"


@action("call")
def _call(ctx, **args):
    """A ROUTINE: set some parameters, then run one action, then put things back.

    args: {"set": {param_id: value, ...}, "action": action_id}, both optional.

    Order is fixed -- every set first, in the order written, then the action --
    because the case this exists for is "go to the reference field, THEN take
    the reference". Each set is the Settable's BLOCKING set, so the magnet has
    settled before the VNA sweeps; the action blocks until it has finished.

    THE RESTORE. Afterwards, every parameter this routine touched that the scan
    had ALREADY set (a condition, or an axis sitting at a value; the engine
    keeps them in ctx["current"]) is set back. A reference taken at 150 mT in
    the middle of a scan must not leave the scan at 150 mT: the engine only
    re-sets an axis when its INDEX changes, so the next point -- and with
    zig-zag the whole next row -- would be measured at 150 mT under coordinates
    that say otherwise. Parameters the scan has not set yet (the axis at
    before_scan) are left alone; the first point sets them anyway.

    Not at after_scan: nothing is measured afterwards, and "field -> 0 at the
    end" is exactly the thing that must NOT be undone.

    After an ABORT (ctx["aborted"]), the after-scan routine still sends every
    command, but the operator's Abort also cuts each WAIT short: they pressed
    Abort to stop waiting, and a wait that can never finish (a magnet that will
    not stabilise) must not hold the window for its whole timeout. The field
    still goes to 0; the routine just does not watch it arrive.
    """
    registry = ctx["registry"]
    current = ctx.setdefault("current", {})
    moment = ctx.get("moment", "")
    # What the log calls this routine: "start of each sweep of x" reads better
    # than the engine moment it fires at ("before_point").
    label = ctx.get("hook_label") or moment
    say = ctx.get("log_fn") or (lambda msg: None)
    sets = dict(args.get("set") or {})
    act_id = args.get("action") or None

    # Check EVERY id before moving anything. Driving the magnet to 150 mT and
    # only then finding the action misspelled leaves the sample somewhere odd
    # with nothing measured. (validate() normally catches this before a run;
    # this is for a caller that did not validate.)
    params = {}
    for pid in sets:
        p = registry.get(pid)
        if p is None or getattr(p, "kind", "") != "settable":
            raise KeyError(f"{label} routine: '{pid}' is not a settable parameter "
                           f"here (is its module connected?)")
        params[pid] = p
    act = None
    if act_id:
        get_action = getattr(registry, "get_action", None)
        act = get_action(act_id) if get_action else None
        if act is None:
            raise KeyError(f"{label} routine: no action '{act_id}' here "
                           f"(is its module connected?)")

    carry_on = ctx.get("on_error") == "continue"

    def step(what, fn):
        say(f"{label}: {what} ...")
        try:
            fn()
        except ScanAborted:
            if not ctx.get("aborted"):
                raise                         # Abort pressed DURING the routine
            say(f"{label}: {what} sent, not waited for (aborted)")
            return
        except Exception as exc:
            # on_error: continue -- say it and go on. The REST of the routine
            # still runs, the restore above all: a routine that moved the
            # magnet and then failed its action must still put the field back.
            if not carry_on:
                raise
            say(f"{label}: {what} FAILED ({exc}); carrying on")
            return
        say(f"{label}: {what} done")

    applied = {}
    for pid, value in sets.items():
        p = params[pid]
        step(f"set {_fmt(p, value)}", lambda p=p, v=value: p.set(float(v)))
        applied[pid] = float(value)
    if act is not None:
        step(f"run {act.id}", lambda: act.run(context=action_context(ctx)))

    if moment == "after_scan":
        return
    for pid, value in applied.items():
        if pid not in current:
            continue
        back = current[pid]
        # Skip a restore that changes nothing: a condition the routine set to
        # its own value (angle 45 while the scan holds 45) costs a settle wait
        # for no reason.
        if math.isclose(float(back), value, rel_tol=0.0, abs_tol=1e-12):
            continue
        p = params[pid]
        step(f"restore {_fmt(p, back)}", lambda p=p, v=back: p.set(float(v)))


def action_context(ctx) -> dict:
    """Where the scan is, for actions that save something next to the data.

    data_dir / data_stem = the folder and file name (without .nc) the
    measurement is written to, or "" when the run is not saved; moment =
    "before" / "after" at before_scan / after_scan, else "p00042" (the point,
    counting from 1), so several saves in one scan do not overwrite each other.
    """
    from pathlib import Path
    p = ctx.get("data_path")
    moment = ctx.get("moment", "")
    tag = {"before_scan": "before", "after_scan": "after"}.get(
        moment, f"p{int(ctx.get('flat', 0)) + 1:05d}")
    return {"data_dir": str(Path(p).parent) if p else "",
            "data_stem": Path(p).stem if p else "",
            "moment": tag}


def sweep_block(shape, dim_names, axis) -> int | None:
    """Points in one sweep of `axis`: prod(shape[a:]). None if not a dim."""
    if axis not in (dim_names or []):
        return None
    return int(math.prod(shape[list(dim_names).index(axis):]))


def _each_sweep_fires(h, moment, flat, total, block) -> bool:
    every = max(1, int(h.get("every") or 1))
    if (h.get("edge") or "start") == "start":
        return (moment == "before_point" and flat % block == 0
                and (flat // block) % every == 0)
    done = flat + 1                      # points finished, this one included
    return (moment == "after_point" and done % block == 0 and done < total
            and (done // block) % every == 0)


def firings(hook, shape, dim_names) -> int | None:
    """How many times `hook` fires in a scan of this shape (None: can't say).

    For the builder's summary ("fires 12x"), so the cost of a routine that runs
    throughout is visible before Run. Counted from the same rules run_hooks
    applies; a test checks the two agree.
    """
    total = int(math.prod(shape)) if shape else 0
    when = hook.get("when")
    if when == "every_n_points":
        n = int(hook.get("n") or 0)
        return -(-total // n) if n > 0 else None
    if when == "each_sweep":
        block = sweep_block(shape, dim_names, hook.get("axis"))
        if not block:
            return None
        sweeps = total // block
        every = max(1, int(hook.get("every") or 1))
        if (hook.get("edge") or "start") == "start":
            return -(-sweeps // every)
        return (sweeps - 1) // every
    if when in ("before_scan", "after_scan"):
        return 1
    if when in ("before_point", "after_point"):
        return total
    return None


def describe_trigger(hook) -> str:
    """'start of each sweep of x', 'every 100 points' -- for logs and summaries."""
    when = hook.get("when")
    if when == "every_n_points":
        return f"every {hook.get('n')} points"
    if when == "each_sweep":
        every = max(1, int(hook.get("every") or 1))
        which = "each sweep" if every == 1 else f"every {every}. sweep"
        return f"{hook.get('edge') or 'start'} of {which} of {hook.get('axis')}"
    return str(when)


def run_hooks(hooks, moment, ctx, axis_name=None):
    """Fire every hook whose trigger matches this moment."""
    flat = ctx.get("flat", 0)
    shape = ctx.get("shape") or ()
    total = int(math.prod(shape)) if shape else 0
    for h in hooks:
        when = h.get("when")
        axis_bound = when in ("before_axis", "after_axis")
        block = (sweep_block(shape, ctx.get("dim_names"), h.get("axis"))
                 if when == "each_sweep" else None)
        fire = (
            # An axis hook must NOT fire on the bare `when == moment` match:
            # that made {when: before_axis, axis: field} fire whenever ANY axis
            # changed, i.e. at every point of the inner loop. Harmless for a
            # stub autofocus; a routine that drives the magnet would run on
            # every point. (Fixed 2026-09-16.)
            (when == moment and not axis_bound)
            or (when == "every_n_points" and moment == "before_point"
                and h.get("n") and flat % int(h["n"]) == 0)
            or (block and _each_sweep_fires(h, moment, flat, total, block))
            or (axis_bound and moment == when and h.get("axis") == axis_name)
        )
        if not fire:
            continue
        fn = ACTIONS.get(h.get("action"))
        if fn:
            # The moment a hook fires AT, which is not always its `when`
            # (every_n_points fires at before_point): `call` needs the real one
            # to know whether a restore is wanted.
            ctx["moment"] = moment
            ctx["hook_label"] = (describe_trigger(h)
                                 if when in ("each_sweep", "every_n_points") else moment)
            ctx["on_error"] = h.get("on_error") or "stop"
            try:
                fn(ctx, **(h.get("args") or {}))
            except ScanAborted:
                raise
            except Exception as exc:
                # `call` handles its own steps; this catches the rest (a plain
                # action that failed, or a routine that could not even start).
                if ctx["on_error"] != "continue":
                    raise
                say = ctx.get("log_fn") or (lambda msg: None)
                say(f"{ctx['hook_label']}: {h.get('action')} FAILED ({exc}); "
                    f"carrying on")
            finally:
                ctx.pop("hook_label", None)
                ctx.pop("on_error", None)
