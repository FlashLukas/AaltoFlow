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

or, when a routine does SEVERAL things in a given order (2026-09-25), the same
`call` with an ordered list of STEPS -- each step sets parameters or runs one
action, and they run exactly in the order written:

    {when: before_scan, action: call, args: {steps: [
        {action: camera.autofocus},
        {action: camera.save_scan_pattern},
        {action: camera.save_picture}]}}

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

FIVE GENERIC STEPS (2026-10-04). Lukas chose exactly these, and asked for them
to stay simple -- no loops, no if/else; a measurement on several dies is a
QUEUE of scans, not a program inside one. Each is a step of a `call` routine
(so the Scan Builder shows it in a routine's step list, in order with the sets
and actions), or -- the same thing written alone -- a hook of its own
({when: before_point, action: abort_if, args: {condition: ...}}):

    {wait_until: {condition: "abs(ppms.temperature - 10) < 0.05",
                  hold_s: 600, timeout_s: 7200, on_timeout: stop}}
    {abort_if:   {condition: "hf2.r1 > 0.9"}}
    {skip_if:    {condition: "camera.point_settled == False"}}
    {pause:      {message: "Insert the polariser, then Continue", headless: fail}}
    {comment:    {text: "sample rotated 90 deg; T = {ppms.temperature}"}}
    {compute_set: {set: {smb.frequency: "2.8e9 + 28e6 * clMag.field"}}}

* wait_until: polls (every WAIT_POLL_S) until the condition has been TRUE
  without a break for hold_s seconds (0 = true once). timeout_s is required;
  on_timeout: stop ends the scan like abort_if, continue logs and goes on.
  Abort works during the wait; progress is logged every WAIT_LOG_S.
* abort_if: the condition true -> the scan STOPS like an Abort (after-scan
  routine runs, data kept and saved), with the reason in the file's
  `stopped_by` attribute. Not at after_scan (there is nothing left to stop).
* skip_if: the condition true -> the CURRENT point is stored as not measured
  (NaN / the storage's fill value) and the scan goes on with the next one.
  At before_point (and every_n_points, each_sweep start) the point is not
  measured at all; at after_point (each_sweep end) the values just measured
  are thrown away. Only at those moments: before or after the scan there is no
  "current point". Not in a fly scan (a row is one move).
* pause: waits for the OPERATOR (the GUI shows the message with Continue /
  Abort scan); "Abort scan" stops like abort_if. In a run without a GUI
  (engine.run without on_pause) headless: fail (default) makes the step fail
  with a clear message, headless: continue logs the message and goes on.
* comment: appends {time, point, index, text} to the file's `comments`
  attribute (a JSON list); {param.id} in the text is filled with the value.
* compute_set: evaluates the formula and SETS the parameter (the blocking set,
  as `set` does), refusing a value outside its limits (the step fails, and
  on_error decides). Restored afterwards exactly like a `set`.

Conditions and formulas use the restricted evaluator in expr.py (no eval), and
read parameters from the STATUS CACHE -- never a new acquisition.
on_error (of the hook) applies to every step's FAILURE (a read that fails, a
value out of limits); a condition coming true is not a failure, so abort_if
and skip_if act whatever on_error says.
"""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime

from .errors import ScanAborted, ScanStopped, SkipPoint

ACTIONS = {}

#: Every moment the engine fires. validate() uses it to catch a typo such as
#: "befor_scan", which would otherwise be a routine that silently never runs.
MOMENTS = ("before_scan", "after_scan", "before_point", "after_point",
           "every_n_points", "each_sweep", "before_axis", "after_axis")

#: The edges of a sweep an each_sweep hook can fire at.
EDGES = ("start", "end")

#: What a failing hook does to the scan.
ON_ERROR = ("stop", "continue")

#: The five generic steps (2026-10-04) plus compute_set's sibling `set`.
STEP_KINDS = ("wait_until", "abort_if", "skip_if", "pause", "comment", "compute_set")

#: The keys each step's mapping may hold, and which of them are required.
STEP_KEYS = {
    "wait_until": ({"condition", "timeout_s", "hold_s", "on_timeout"},
                   {"condition", "timeout_s"}),
    "abort_if": ({"condition"}, {"condition"}),
    "skip_if": ({"condition"}, {"condition"}),
    "pause": ({"message", "headless"}, {"message"}),
    "comment": ({"text"}, {"text"}),
    "compute_set": ({"set"}, {"set"}),
}

#: Where a step makes sense. Not listed = every moment.
STEP_MOMENTS = {
    # nothing is left to stop once the scan is over
    "abort_if": tuple(m for m in MOMENTS if m != "after_scan"),
    # a "current point" exists only while the points run; before_axis /
    # after_axis are left out because they fire on the way INTO the next
    # point, which makes "this point" ambiguous
    "skip_if": ("before_point", "after_point", "every_n_points", "each_sweep"),
}

WAIT_ON_TIMEOUT = ("stop", "continue")
PAUSE_HEADLESS = ("fail", "continue")

#: wait_until: seconds between two looks at the condition (status-cache reads,
#: cheap), and between two progress lines in the log.
WAIT_POLL_S = 0.5
WAIT_LOG_S = 30.0
#: pause: how often the engine checks for an answer (or Abort) while waiting.
PAUSE_POLL_S = 0.2


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


def routine_steps(args) -> list[tuple]:
    """The steps of a `call` routine, flattened, in the order they run.

    Returns a list of ("set", param_id, value), ("action", action_id) and,
    for the five generic steps, (kind, spec) -- e.g. ("wait_until",
    {"condition": ..., "timeout_s": ...}); see step_problems().
    A routine can be written two ways, and both come out the same here:

    * the ORIGINAL form (2026-09-16): {set: {a: 1, b: 2}, action: X} -- every
      set in the order written, then the one action;
    * the ORDERED form (2026-09-25): {steps: [{set: {a: 1}}, {action: X},
      {set: {a: 0}}, {action: Y}]} -- for a routine that runs several actions,
      or sets something again after an action ("find focus, then save the
      pattern, then save a picture"). A step holding both `set` and `action`
      means "these sets, then that action", exactly like the original form.

    The Scan Builder writes the original form whenever it says the same thing
    (sets, then at most one action), so such files stay readable by an older
    scan-core; the ordered form only when it is needed.

    Raises ValueError on a malformed routine. recipe.validate() calls this too,
    so the same problem is reported before a run, with the routine named.
    """
    if not isinstance(args, dict):
        raise ValueError("args must be a mapping: {set, action} or {steps: [...]}")
    if "steps" in args:
        if "set" in args or "action" in args:
            raise ValueError("use either 'steps' or 'set'/'action', not both")
        raw = args.get("steps")
        if not isinstance(raw, list):
            raise ValueError("'steps' must be a list")
    else:
        raw = [args]                      # the original form is ONE step
    out: list[tuple] = []
    for k, st in enumerate(raw, 1):
        # The five generic steps (2026-10-04): {kind: {...}}, alone in their
        # step. They come back as (kind, spec); step_problems() checks the spec.
        kinds = set(st) & set(STEP_KINDS) if isinstance(st, dict) else set()
        if kinds:
            kind = next(iter(kinds))
            if len(st) != 1:
                raise ValueError(f"step {k}: a {kind} step holds only its own key "
                                 f"({{{kind}: {{...}}}})")
            if not isinstance(st[kind], dict):
                raise ValueError(f"step {k}: {kind} needs a mapping, e.g. "
                                 f"{{{kind}: {{{sorted(STEP_KEYS[kind][1])[0]}: ...}}}}")
            out.append((kind, st[kind]))
            continue
        if not isinstance(st, dict) or not set(st) <= {"set", "action"}:
            raise ValueError(f"step {k} must be {{set: {{id: value}}}}, {{action: id}} "
                             f"or one of {', '.join(STEP_KINDS)}")
        sets = st.get("set") or {}
        if not isinstance(sets, dict):
            raise ValueError(f"step {k}: 'set' must map parameter ids to values")
        out += [("set", pid, value) for pid, value in sets.items()]
        if st.get("action"):
            out.append(("action", st["action"]))
    return out


def _number(spec, key, default=None):
    v = spec.get(key, default)
    if isinstance(v, bool):
        raise ValueError
    v = float(v)
    if not math.isfinite(v):
        raise ValueError
    return v


def step_problems(kind: str, spec: dict, registry=None, moment: str | None = None,
                  fly: bool = False) -> list[str]:
    """Everything wrong with one generic step ([] = fine).

    The SAME check for recipe.validate() (before the run, with the routine
    named) and for _call() (a caller that did not validate). Expressions are
    parsed here, so a forbidden construct or an unknown parameter id is found
    before anything moves.
    """
    from . import expr as _expr
    errs: list[str] = []
    allowed, required = STEP_KEYS[kind]
    extra = set(spec) - allowed
    if extra:
        errs.append(f"{kind}: unknown key(s) {', '.join(sorted(map(str, extra)))} "
                    f"(allowed: {', '.join(sorted(allowed))})")
    missing = required - set(spec)
    if missing:
        hint = (" -- a wait needs a limit, or a sensor that never gets there "
                "holds the scan forever" if "timeout_s" in missing else "")
        errs.append(f"{kind}: needs {', '.join(sorted(missing))}{hint}")
    if moment is not None and moment in MOMENTS and kind in STEP_MOMENTS \
            and moment not in STEP_MOMENTS[kind]:
        why = {"abort_if": "after the scan there is nothing left to stop",
               "skip_if": "there is no current point to skip there "
                          "(use before_point or after_point)"}[kind]
        errs.append(f"{kind} cannot run at {moment}: {why}")
    if kind == "skip_if" and fly:
        errs.append("skip_if cannot run in a fly scan: a row is one continuous "
                    "move, there is no single point to leave out")

    def expression(text, what):
        for msg in _expr.check(text, registry):
            errs.append(f"{kind} {what}: {msg}")

    if kind in ("wait_until", "abort_if", "skip_if") and "condition" in spec:
        expression(spec["condition"], "condition")
    if kind == "wait_until":
        for key, positive in (("timeout_s", True), ("hold_s", False)):
            if key not in spec:
                continue
            try:
                v = _number(spec, key)
                ok = v > 0 if positive else v >= 0
            except (TypeError, ValueError):
                ok = False
            if not ok:
                errs.append(f"wait_until: {key} must be a number "
                            f"{'> 0' if positive else '>= 0'} (seconds)")
        if (spec.get("on_timeout") or "stop") not in WAIT_ON_TIMEOUT:
            errs.append(f"wait_until: on_timeout must be one of "
                        f"{', '.join(WAIT_ON_TIMEOUT)}")
    if kind == "pause":
        if "message" in spec and not isinstance(spec["message"], str):
            errs.append("pause: message must be text")
        if (spec.get("headless") or "fail") not in PAUSE_HEADLESS:
            errs.append(f"pause: headless must be one of {', '.join(PAUSE_HEADLESS)}")
    if kind == "comment" and "text" in spec and not isinstance(spec["text"], str):
        errs.append("comment: text must be text")
    if kind == "compute_set" and "set" in spec:
        sets = spec["set"]
        if not isinstance(sets, dict) or not sets:
            errs.append("compute_set: set must map parameter ids to formulas")
        else:
            for pid, text in sets.items():
                p = registry.get(pid) if registry is not None else None
                if registry is not None and (p is None or getattr(p, "kind", "") != "settable"):
                    errs.append(f"compute_set: '{pid}' is not a settable parameter here")
                if isinstance(text, bool) or not isinstance(text, (str, int, float)):
                    errs.append(f"compute_set {pid}: the formula must be text")
                    continue
                expression(str(text), pid)
    return errs


def _cond_text(spec) -> str:
    return str(spec.get("condition", "")).strip()


def _values_text(expr, registry) -> str:
    """'ppms.temperature = 10.03, ppms.field = 0' -- what a condition sees."""
    from . import expr as _expr
    parts = []
    for pid in expr.names:
        try:
            parts.append(f"{pid} = {_expr.format_value(_expr.read_value(registry, pid))}")
        except Exception as exc:
            parts.append(f"{pid} = ? ({exc})")
    return ", ".join(parts)


def wait_until(ctx, condition, timeout_s, hold_s=0.0, on_timeout="stop"):
    """Block until `condition` has been true for `hold_s` s without a break.

    Polled from the status cache every WAIT_POLL_S (no acquisitions). Abort
    ends the wait at once (ScanAborted). After timeout_s: on_timeout stop
    raises ScanStopped (a clean stop, the reason in the file), continue logs
    and returns.
    """
    from . import expr as _expr
    registry = ctx["registry"]
    say = ctx.get("log_fn") or (lambda msg: None)
    should_abort = ctx.get("should_abort")
    expr = _expr.parse(condition)
    timeout_s, hold_s = float(timeout_s), float(hold_s or 0.0)
    t0 = time.monotonic()
    true_since = None
    last_log = t0
    while True:
        if should_abort and should_abort():
            raise ScanAborted(f"aborted while waiting until {expr.text}")
        ok = bool(_expr.evaluate(expr, registry))
        now = time.monotonic()
        if ok:
            true_since = now if true_since is None else true_since
            if now - true_since >= hold_s:
                held = f", held {hold_s:g} s" if hold_s else ""
                say(f"wait_until: {expr.text} is true{held} "
                    f"(after {now - t0:.0f} s)")
                return
        else:
            true_since = None
        if now - t0 >= timeout_s:
            msg = (f"wait_until timed out after {timeout_s:g} s: {expr.text} "
                   f"({_values_text(expr, registry)})")
            if (on_timeout or "stop") == "continue":
                say(f"{msg}; carrying on (on_timeout: continue)")
                return
            raise ScanStopped(msg)
        if now - last_log >= WAIT_LOG_S:
            last_log = now
            held = 0.0 if true_since is None else now - true_since
            say(f"waiting: {_values_text(expr, registry)} (want {expr.text}), "
                f"held {held:.0f}/{hold_s:g} s, {now - t0:.0f}/{timeout_s:g} s")
        left = timeout_s - (now - t0)
        if true_since is not None:
            left = min(left, hold_s - (now - true_since))
        time.sleep(max(0.0, min(WAIT_POLL_S, left)))


def pause_for_operator(ctx, message, headless="fail"):
    """Wait until the operator answers Continue (return) or Abort scan
    (ScanStopped). `ctx["on_pause"](message, answer)` shows the question --
    `answer(True)` = Continue, `answer(False)` = Abort -- and is called with
    (None, None) when the question is gone. Abort pressed elsewhere ends the
    wait too. Without on_pause: headless fail -> RuntimeError, continue -> log.
    """
    say = ctx.get("log_fn") or (lambda msg: None)
    on_pause = ctx.get("on_pause")
    if on_pause is None:
        if (headless or "fail") == "continue":
            say(f"pause (no one to ask -- carrying on, headless: continue): {message}")
            return
        raise RuntimeError(
            f"pause step {message!r}: this run has no operator to answer it "
            f"(no GUI). Run it from the Scan Builder, or give the step "
            f"headless: continue to only log the message")
    answered = threading.Event()
    box = {"go_on": None}

    def answer(go_on: bool):
        box["go_on"] = bool(go_on)
        answered.set()

    say(f"PAUSED for the operator: {message}")
    should_abort = ctx.get("should_abort")
    on_pause(message, answer)
    try:
        while not answered.wait(PAUSE_POLL_S):
            if should_abort and should_abort():
                raise ScanAborted(f"aborted while paused ({message})")
    finally:
        try:
            on_pause(None, None)             # the question goes, whatever happened
        except Exception:
            pass
    if not box["go_on"]:
        raise ScanStopped(f"the operator chose Abort at the pause: {message}")
    say("operator: Continue")


def add_comment(ctx, text):
    """Append {time, point, index, text} to the run's comments (file attr)."""
    import json
    from . import expr as _expr
    say = ctx.get("log_fn") or (lambda msg: None)
    filled, bad = _expr.fill_placeholders(str(text), ctx["registry"])
    for pid in bad:
        say(f"comment: warning: {{{pid}}} is not a parameter that can be read "
            f"here -- left as text")
    inside = ctx.get("moment") not in ("before_scan", "after_scan", None)
    entry = {"time": datetime.now().isoformat(timespec="seconds"),
             "point": int(ctx.get("flat", 0)) + 1 if inside else None,
             "index": [int(i) for i in ctx.get("index", ())] if inside else None,
             "text": filled}
    comments = ctx.setdefault("comments", [])
    comments.append(entry)
    ctx.setdefault("ds_attrs", {})["comments"] = json.dumps(comments)
    where = f" (point {entry['point']})" if inside else ""
    say(f"comment{where}: {filled}")


def _computed(p, text, registry) -> float:
    """compute_set: the formula's value, REFUSED (not clamped) outside limits.

    Settable.set clamps silently -- right for a typed setpoint the builder has
    already clamped, wrong for a formula: "2.8e9 + 28e6 * field" landing on the
    instrument's upper limit is a measurement at the wrong frequency that looks
    fine. So the step fails, and on_error decides.
    """
    from . import expr as _expr
    v = _expr.evaluate(str(text), registry)
    if isinstance(v, str):
        raise ValueError(f"{p.id} = {text}: the formula gives text ({v!r}), not a number")
    v = float(v)
    if not math.isfinite(v):
        raise ValueError(f"{p.id} = {text}: the formula gives {v}")
    lo, hi = getattr(p, "limits", (None, None))
    if lo is not None and not (lo <= v <= hi):
        raise ValueError(f"{p.id} = {text} = {v:g} is outside its limits "
                         f"[{lo:g}, {hi:g}]")
    return v


@action("call")
def _call(ctx, **args):
    """A ROUTINE: sets and actions in the order written, then put things back.

    args: {"set": {param_id: value, ...}, "action": action_id} (both optional),
    or {"steps": [{"set": {...}}, {"action": id}, ...]} for several actions or
    any other order -- see routine_steps(). "Go to the reference field, THEN
    take the reference"; "find focus, THEN save the pattern, THEN save a
    picture". Each set is the Settable's BLOCKING set, so the magnet has
    settled before the VNA sweeps; each action blocks until it has finished.

    THE RESTORE. Afterwards -- ONCE, after the last step, not between steps --
    every parameter this routine touched that the scan had ALREADY set (a
    condition, or an axis sitting at a value; the engine keeps them in
    ctx["current"]) is set back. A reference taken at 150 mT in the middle of
    a scan must not leave the scan at 150 mT: the engine only re-sets an axis
    when its INDEX changes, so the next point -- and with zig-zag the whole
    next row -- would be measured at 150 mT under coordinates that say
    otherwise. Parameters the scan has not set yet (the axis at before_scan)
    are left alone; the first point sets them anyway.

    Why once, at the end: take [set field 190, run reference, set field 0] at
    before_scan. With the field a CONDITION of 50 mT it runs 190 -> reference
    -> 0 -> back to 50, because 50 is what the scan says it was measured at.
    (Putting things back after EVERY action would add a pointless 190 -> 50
    ramp in the middle.) With the field an AXIS it runs 190 -> reference -> 0,
    and the first point then sets the field. A parameter set twice is compared
    with its LAST value and restored once.

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
    try:
        steps = routine_steps(args)
    except ValueError as exc:
        raise KeyError(f"{label} routine: {exc}") from None

    # Check EVERY id before moving anything. Driving the magnet to 150 mT and
    # only then finding the action misspelled leaves the sample somewhere odd
    # with nothing measured. (validate() normally catches this before a run;
    # this is for a caller that did not validate.)
    params, acts = {}, {}
    get_action = getattr(registry, "get_action", None)
    for kind, ident, *_ in steps:
        if kind == "set":
            p = registry.get(ident)
            if p is None or getattr(p, "kind", "") != "settable":
                raise KeyError(f"{label} routine: '{ident}' is not a settable parameter "
                               f"here (is its module connected?)")
            params[ident] = p
        elif kind == "action":
            act = get_action(ident) if get_action else None
            if act is None:
                raise KeyError(f"{label} routine: no action '{ident}' here "
                               f"(is its module connected?)")
            acts[ident] = act
        else:
            # a generic step: `ident` is its spec
            probs = step_problems(kind, ident, registry, moment)
            if probs:
                raise KeyError(f"{label} routine: {probs[0]}")
            if kind == "compute_set":
                for pid in ident["set"]:
                    params[pid] = registry.get(pid)

    carry_on = ctx.get("on_error") == "continue"

    def step(what, fn, quiet=False):
        # `quiet`: the per-point checks (abort_if, skip_if, comment) say
        # nothing unless something HAPPENS -- "check ... done" at every point
        # of a 10 000-point map would bury the log.
        if not quiet:
            say(f"{label}: {what} ...")
        try:
            fn()
        except (SkipPoint, ScanStopped) as exc:
            if isinstance(exc, ScanStopped) and ctx.get("aborted"):
                say(f"{label}: {what} -- {exc} (already aborting)")
                return
            raise                             # a condition came true: not a failure
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
        if not quiet:
            say(f"{label}: {what} done")

    def condition(kind, spec):
        """abort_if / skip_if: evaluate; act if true."""
        from . import expr as _expr
        expr = _expr.parse(spec["condition"])
        point = int(ctx.get("flat", 0)) + 1

        def check():
            if not _expr.evaluate(expr, registry):
                return
            seen = _values_text(expr, registry)
            if kind == "abort_if":
                where = (f" at {moment}" if moment == "before_scan"
                         else f" at point {point}")
                raise ScanStopped(f"abort_if {expr.text}{where} ({seen})")
            say(f"{label}: point {point} skipped -- skip_if {expr.text} ({seen})")
            raise SkipPoint(expr.text)
        step(f"{kind} {expr.text}", check, quiet=True)

    applied = {}          # param -> the LAST value this routine set it to
    skipped = None
    try:
        for kind, ident, *value in steps:
            if kind == "set":
                p, v = params[ident], float(value[0])
                step(f"set {_fmt(p, v)}", lambda p=p, v=v: p.set(v))
                applied[ident] = v
            elif kind == "action":
                act = acts[ident]
                step(f"run {act.id}", lambda act=act: act.run(context=action_context(ctx)))
            elif kind == "compute_set":
                for pid, text in ident["set"].items():
                    p, box = params[pid], {}

                    def do(p=p, pid=pid, text=text, box=box):
                        v = _computed(p, text, registry)
                        say(f"{label}: {pid} = {text} -> {_fmt(p, v)}")
                        p.set(v)
                        box["v"] = v
                    step(f"set {pid} = {text}", do)
                    if "v" in box:
                        applied[pid] = box["v"]
            elif kind == "wait_until":
                step(f"wait until {_cond_text(ident)}",
                     lambda s=ident: wait_until(ctx, s["condition"], s["timeout_s"],
                                                s.get("hold_s", 0.0),
                                                s.get("on_timeout") or "stop"))
            elif kind in ("abort_if", "skip_if"):
                condition(kind, ident)
            elif kind == "pause":
                step(f"pause: {ident.get('message', '')}",
                     lambda s=ident: pause_for_operator(ctx, s.get("message", ""),
                                                        s.get("headless") or "fail"))
            elif kind == "comment":
                step("comment", lambda s=ident: add_comment(ctx, s.get("text", "")),
                     quiet=True)
    except SkipPoint as exc:
        # skip_if: the rest of the routine does not run, but the RESTORE does
        # -- a routine that moved the field and then skipped the point must not
        # leave the next point to be measured at the moved field.
        skipped = exc

    if moment != "after_scan":
        for pid, value in applied.items():
            if pid not in current:
                continue
            back = current[pid]
            # Skip a restore that changes nothing: a condition the routine set
            # to its own value (angle 45 while the scan holds 45) costs a
            # settle wait for no reason.
            if math.isclose(float(back), value, rel_tol=0.0, abs_tol=1e-12):
                continue
            p = params[pid]
            step(f"restore {_fmt(p, back)}", lambda p=p, v=back: p.set(float(v)))
    if skipped is not None:
        raise skipped


def _single_step(kind):
    """{when: ..., action: abort_if, args: {condition: ...}} = a routine of
    that ONE step: the same checks, the same logging, the same restore."""
    def run_one(ctx, **args):
        _call(ctx, steps=[{kind: args}])
    run_one.__name__ = f"_{kind}"
    return run_one


for _kind in STEP_KINDS:
    ACTIONS[_kind] = _single_step(_kind)


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
            except (ScanAborted, SkipPoint):
                raise                    # an Abort / a stop / a skip: not failures
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
