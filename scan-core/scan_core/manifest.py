"""manifest.py -- turn a service's `describe` manifest into registry Parameters.

This is where the self-description pays off. `lab.py` used to carry a
hand-written `_build_clMag` / `_build_smb` function naming every knob, its verb,
its status key and its settle rule. All of that already lives inside the module
that owns the hardware, so keeping a second copy here meant two places to edit
and one of them silently wrong whenever a limit moved.

A module that answers `describe` needs no code here at all: walk the manifest,
make a Settable for each control and a Gettable for each indicator. Adding an
instrument to a scan becomes "teach the module to describe itself", which is one
edit in the place that already knows the answer.

The settle policies named in a manifest are resolved through `instrument.py`, so
a blocking `set` still blocks correctly -- the module states its own rule for
"has it arrived" and the coordinator obeys it, rather than guessing.
"""

from __future__ import annotations

from .instrument import (Instrument, InstrumentError, adopt_then_flag, echoes,
                         flag_only, immediate)
from .registry import (AcquireSpec, Action, AxisSpec, Gettable, Registry,
                       Settable)

#: How a manifest's `settle` block maps onto a policy factory. Anything not
#: listed falls back to `immediate()` with a warning, so an unknown policy from
#: a newer module degrades to "don't wait" instead of crashing the scan -- but
#: it is reported, because silently not waiting is how you get a grid of points
#: measured one step behind.
def _k(c: dict, name: str):
    """The status key a settle block names, as a path when it has an `index`.

    Multi-axis and multi-channel services publish per-axis values as LISTS
    (kim's `moving`, hf2's `tc_set_s`), so the block says which entry:
    {"key": "moving", "index": 0} -> ["moving", 0].
    """
    key = c[name]
    return [key, int(c["index"])] if "index" in c else key


_POLICIES = {
    "adopt_then_flag": lambda c: adopt_then_flag(
        _k(c, "setpoint_key"), _k(c, "flag_key"), c.get("invert", False)),
    "echoes": lambda c: echoes(_k(c, "key"), c.get("tol", 1e-6)),
    "flag_only": lambda c: flag_only(_k(c, "key"), c.get("invert", False)),
    "immediate": lambda c: immediate(),
    "state_in": lambda c: _state_in(_k(c, "key"), c["states"]),
}


def _state_in(key, states):
    """Settled when a state-machine field reaches one of `states`.

    The set-and-forget half of the suite has no per-knob done-flag; what it has
    is a state machine that returns to IDLE. clMag's direct current control and
    its demag routine both settle this way.
    """
    from .instrument import _lookup
    wanted = set(states)

    def make(target):
        return lambda st: _lookup(st, key) in wanted
    return make


def resolve_settle(spec: dict | None, on_warn=None):
    """Build a settle-policy factory from a manifest `settle` block."""
    if not spec:
        return immediate()
    name = spec.get("policy", "immediate")
    factory = _POLICIES.get(name)
    if factory is None:
        if on_warn:
            on_warn(f"unknown settle policy {name!r}; not waiting for this knob")
        return immediate()
    try:
        return factory(spec)
    except KeyError as exc:
        if on_warn:
            on_warn(f"settle policy {name!r} missing field {exc}; not waiting")
        return immediate()


def register_manifest(reg: Registry, inst: Instrument, manifest: dict, *,
                      prefix: bool = False, on_warn=None,
                      module_name: str | None = None) -> list[str]:
    """Add every parameter in `manifest` to `reg`. Returns the ids added.

    `prefix=True` namespaces ids as "<module>.<id>", which you want as soon as
    two modules are connected: several of them have a knob called `position`.

    ACTIONS ARE NOT PARAMETERS. A registry Parameter is a value you can sweep
    or record; "Home" and "Demagnetise" are neither. There are two consumers of
    a manifest and they want different projections of it:

      * a **scan** wants Settables and Gettables -- that is this function
      * a **control panel** wants everything, including buttons, their argument
        lists, their `danger` flag and the `group`/`order` layout hints

    So a UI reads the manifest DIRECTLY rather than through the registry. Same
    single source of truth, two views of it. Bending the registry to carry
    buttons would make it worse at the one job it has.

    The one exception (2026-09-16): an action whose descriptor carries a `wait`
    block is registered as a registry `Action` -- in its own list, never as a
    Parameter -- so a scan ROUTINE can run it ("take a VNA reference before the
    field sweep"). The `wait` block is the module saying two things at once:
    this is safe to run unattended from a scan, and here is how to tell it has
    FINISHED. An action without one stays a control-panel button, because a
    routine that fires it could not know when to carry on.
    """
    # `module_name` = a unique alias for a remote copy of a module ("hf2_lab2"),
    # so it does not share ids -- or acquire groups -- with the local one.
    module = module_name or manifest.get("module", inst.name)
    added = []

    for d in manifest.get("parameters", []):
        kind = d.get("kind")
        if kind == "action":
            if d.get("wait"):
                aid = f"{module}.{d['id']}" if prefix else d["id"]
                reg.add_action(_action_from(d, inst, aid, on_warn))
                added.append(aid)
            continue
        pid = f"{module}.{d['id']}" if prefix else d["id"]
        label = d.get("label", d["id"])
        unit = d.get("unit", "")
        path = d.get("read_path")

        # wire_value = display_value * scale. Lives on the descriptor, not just
        # in the `set` block, so reading and setting cannot disagree: RF
        # frequency is scanned in MHz and published in Hz, and a scale applied
        # to only one direction would be a factor-of-a-million bug that looks
        # like a broken instrument.
        scale = float(d.get("scale", 1.0))

        def getter(_p=path, _inst=inst, _scale=scale):
            v = _inst.status()
            for key in (_p or []):
                # a key may be an int index into a per-axis list
                if isinstance(key, int) and isinstance(v, (list, tuple)):
                    if not -len(v) <= key < len(v):
                        return float("nan")
                    v = v[key]
                    continue
                if not isinstance(v, dict) or key not in v:
                    return float("nan")
                v = v[key]
            if not _p:
                return float("nan")
            if _scale != 1.0 and isinstance(v, (int, float)) and not isinstance(v, bool):
                return v / _scale
            return v

        dtype = d.get("type")

        if kind == "control" and d.get("set"):
            # scan-core's Settable is numeric by design: it clamps with
            # max/min and the engine builds coordinate arrays from it. bool
            # maps on cleanly (a two-point sweep of "RF on" is a real scan);
            # enum and string do not, so they are registered read-only rather
            # than silently crashing on float() at the first setpoint. The
            # control screen reads the manifest directly and can still drive
            # them.
            if dtype in ("enum", "string"):
                if on_warn:
                    on_warn(f"{pid}: {dtype} controls are not scannable; "
                            f"registered as an indicator")
                reg.add(Gettable(pid, label, unit, getter))
                added.append(pid)
                continue

            spec = d["set"]
            lo, hi = d.get("min"), d.get("max")
            if dtype == "bool":
                lo, hi = 0, 1           # not [-inf, inf]: a bool has two states
            else:
                # No advertised bound. Settable clamps to its limits, so an
                # infinite one is the honest choice -- the SERVICE still clamps
                # to its own envelope, which is the real safety boundary.
                lo = float("-inf") if lo is None else lo
                hi = float("inf") if hi is None else hi
            settle = resolve_settle(d.get("settle"), on_warn)
            timeout = float(d.get("timeout_s", 60.0))

            def setter(value, _s=spec, _inst=inst, _settle=settle, _t=timeout,
                       _id=pid, _u=unit, _bool=(dtype == "bool"), _scale=scale,
                       _int=(dtype == "int")):
                extra = dict(_s.get("extra") or {})
                scale = _scale
                # Send a bool as a bool. Settable hands us 0.0/1.0 after its
                # numeric clamp, and a service that does `bool(msg[arg])` would
                # cope -- but putting a float on the wire where the contract
                # says bool is the kind of sloppiness that bites a later reader.
                #
                # ROUND an INT control, and settle on the rounded value. An axis
                # of 21 points across indices 0..19 asks for 0.95, the service
                # stores 1, and a settle policy comparing to 1e-6 then waits out
                # its whole timeout on a point that actually arrived -- with
                # Abort ignored meanwhile. (Found on the rig, 2026-09-16.)
                wire = bool(value) if _bool else value * scale
                if _int and not _bool:
                    wire = int(round(wire))
                _inst.command(_s["verb"], **{_s["arg"]: wire}, **extra)
                shown = wire if _bool else f"{value:g} {_u}".strip()
                _inst.wait_until(_settle(wire), timeout_s=_t,
                                 what=f"{_id} = {shown}")

            param = reg.add(Settable(pid, label, unit, (lo, hi),
                                     set_fn=setter, get_fn=getter))
            # An INT control only has whole-number settings (a scan-array index,
            # a filter order). The builder reads this to offer whole points
            # rather than 21 samples across 0..19.
            param.integer = bool(_int_dtype(dtype))
            added.append(pid)

        elif kind == "indicator" or (kind == "control" and not d.get("set")):
            # An ARRAY indicator brings its own inner dimensions: a VNA returns
            # a whole trace per scan point, because the frequency sweep happens
            # in the instrument. Each declared axis becomes a real dataset
            # dimension, and its coordinate is fetched ONCE per scan -- pulling
            # 1601 frequencies at every grid point would dominate the run.
            axes = _axes_from(d, inst, prefix, module, on_warn)
            if d.get("read"):
                getter = _command_reader(d, inst)
            reg.add(Gettable(pid, label, unit, getter, axes=axes,
                             dtype=d.get("dtype", "float"),
                             acquire=_acquire_from(d, inst, module, on_warn)))
            added.append(pid)

    return added


def _int_dtype(dtype: str) -> bool:
    return dtype == "int"


def _command_reader(d: dict, inst: Instrument):
    """A getter that FETCHES the value with a command instead of reading status.

        "read": {"verb": "get_trace", "key": "s21", "args": {"which": "sample"}}

    A trace does not belong in the status stream: 1601 complex points ten times
    a second is ~60 kB/s of mostly repeated data, to every subscriber. So the
    module serves it on request, and this is the request.

    JSON has no complex numbers; a complex value travels as {"re": [...],
    "im": [...]} and is rebuilt here. null (a module's NaN) becomes nan.
    """
    spec = d["read"]
    verb, key = spec["verb"], spec.get("key", "value")
    args = dict(spec.get("args") or {})
    complex_ = d.get("dtype") == "complex"

    def getter():
        reply = inst.command(verb, **args)
        if key not in reply:
            raise InstrumentError(f"{d.get('id')}: the {verb!r} reply has no {key!r}")
        return decode_wire_value(reply[key], complex_)
    return getter


def decode_wire_value(value, complex_: bool = False):
    """A JSON value from a module -> what the engine stores.

    {"re", "im"} -> complex array; a list -> float array (null -> nan); a
    scalar passes through. `complex_` also accepts a plain real list for a
    complex detector (an instrument with no imaginary part to report)."""
    import numpy as np

    def arr(xs):
        return np.array([np.nan if v is None else v for v in xs], dtype=float)

    if isinstance(value, dict) and "re" in value and "im" in value:
        return arr(value["re"]) + 1j * arr(value["im"])
    if isinstance(value, (list, tuple)):
        out = arr(value)
        return out.astype(complex) if complex_ else out
    return value


def _axes_from(d: dict, inst: Instrument, prefix: bool, module: str, on_warn):
    """Build AxisSpecs from a descriptor's `dims` block.

    A dim's coordinate comes from one of two places, in this order:
      * `coord_verb` -- a command returning the array. Right for anything the
        instrument computes (a VNA's frequency grid follows start/stop/points).
      * `values` -- the array inline in the manifest. Fine for a short fixed
        axis; wasteful for 1601 floats fetched on every describe.
    """
    out = []
    for dim in d.get("dims") or []:
        name = dim.get("name")
        if not name:
            if on_warn:
                on_warn(f"{d.get('id')}: a dim with no name was ignored")
            continue
        # The axis name is namespaced with the parameter ids, or two modules
        # each publishing a "freq" axis would silently share one coordinate.
        axis_name = f"{module}.{name}" if prefix else name

        verb = dim.get("coord_verb")
        inline = dim.get("values")
        if verb:
            key = dim.get("coord_key", "values")

            def values_fn(_v=verb, _k=key, _inst=inst):
                return _inst.command(_v).get(_k, [])
        elif inline is not None:
            def values_fn(_vals=list(inline)):
                return _vals
        else:
            values_fn = None
            if on_warn and not dim.get("length"):
                on_warn(f"{d.get('id')}: dim '{name}' declares neither "
                        f"coord_verb, values nor length; using indices")

        out.append(AxisSpec(axis_name, dim.get("label", name),
                            dim.get("unit", ""), values_fn=values_fn,
                            length=dim.get("length")))
    return out


def _acquire_from(d: dict, inst: Instrument, module: str, on_warn):
    """Build an AcquireSpec from a descriptor's `acquire` block, or None.

    No block means a plain read is already fresh -- an NI sample, a status
    field. A block means the detector is SLOW and must be triggered and waited
    on, because reading it cold returns the previous acquisition.

        "acquire": {
            "group":         "sweep",     # detectors sharing this = one trigger
            "trigger_verb":  "sweep",     # fire-and-forget: start it
            "ready":  {"policy": "flag_only", "key": "sweeping", "invert": true},
            "timeout_s":     60
        }

    `target_key` (optional) closes the stale-status hole that `flag_only` has
    here too. Right after the trigger, the cached status can still be the
    frame from BEFORE it, saying "not busy" -- so the wait returns at once and
    the read gets the previous acquisition. A service that numbers its
    acquisitions returns the new number in the trigger reply; naming that
    field as `target_key` makes it the TARGET of the ready policy, so
    `adopt_then_flag` on the id waits for this acquisition and no other:

        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": true}
    """
    spec = d.get("acquire")
    if not spec:
        return None

    verb = spec.get("trigger_verb")
    ready = spec.get("ready")
    if not verb and not ready:
        if on_warn:
            on_warn(f"{d.get('id')}: `acquire` names neither trigger_verb nor "
                    f"ready; ignoring it")
        return None

    # Group names are namespaced per module, or a VNA and a lock-in both
    # calling their group "sweep" would be triggered as if they were one.
    group = f"{module}.{spec.get('group', d.get('id'))}"
    timeout = float(spec.get("timeout_s", 60.0))
    target_key = spec.get("target_key") if verb else None

    # The trigger's reply is handed to the wait through this one-slot box.
    # The engine always calls trigger() then wait() for a group, in that order.
    last = {"target": None}

    trigger_fn = None
    if verb:
        def trigger_fn(_v=verb, _i=inst, _key=target_key, _last=last, _g=group):
            reply = _i.command(_v)
            if _key is not None:
                if _key not in reply:
                    raise InstrumentError(
                        f"acquisition '{_g}': the {_v!r} reply has no "
                        f"{_key!r} field to wait on")
                _last["target"] = reply[_key]

    wait_fn = None
    if ready:
        policy = resolve_settle(ready, on_warn)
        wait_fn = (lambda _i=inst, _p=policy, _t=timeout, _g=group, _last=last:
                   _i.wait_until(_p(_last["target"]), timeout_s=_t,
                                 what=f"acquisition '{_g}' to finish"))

    return AcquireSpec(group, trigger_fn=trigger_fn, wait_fn=wait_fn)


def _action_from(d: dict, inst: Instrument, aid: str, on_warn) -> Action:
    """A registry Action from an action descriptor that has a `wait` block.

        "wait": {"target_key": "acq_id",
                 "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                           "flag_key": "acquiring", "invert": true},
                 "timeout_s": 60}

    Running it = send the verb (the descriptor's id, as the control panel does)
    with the DEFAULTS of its declared args -- a routine has no dialog to ask
    for them -- then block until `ready` holds.

    `target_key` is the same guard as in `_acquire_from`, for the same reason:
    fire-and-forget means the status right after the command can still describe
    the PREVIOUS reference ("not acquiring", old id). Waiting on the id the
    command replied with makes the wait mean "THIS one has finished". Without
    it a before-scan reference would return at once and the first points would
    be divided by a reference that is not there yet.

    The wait goes through `Instrument.wait_until`, so the operator's Abort
    reaches it and raises ScanAborted -- a long reference sweep never makes the
    Abort button look dead.
    """
    wait = d.get("wait") or {}
    verb = d["id"]
    args = {a["name"]: a["default"] for a in (d.get("args") or [])
            if isinstance(a, dict) and "name" in a and "default" in a}
    target_key = wait.get("target_key")
    # Optional OUTCOME check (2026-09-24, camera autofocus): "finished" is not
    # "succeeded". {"key": "af_error", "equals": "OK"} -> after the wait the
    # status must say so, else the action RAISES. A scan that quietly carries
    # on after a failed autofocus measures out of focus; raising hands the
    # decision to the routine's on_error (stop / continue + logged).
    check = wait.get("check") or None
    policy = resolve_settle(wait.get("ready"), on_warn)
    timeout = float(wait.get("timeout_s", 60.0))
    label = d.get("label", d["id"])

    def run_fn(_i=inst, _v=verb, _a=args, _key=target_key, _p=policy,
               _t=timeout, _id=aid, _check=check):
        reply = _i.command(_v, **_a)
        target = None
        if _key is not None:
            if _key not in reply:
                raise InstrumentError(
                    f"{_id}: the {_v!r} reply has no {_key!r} field to wait on")
            target = reply[_key]
        st = _i.wait_until(_p(target), timeout_s=_t, what=f"{_id} to finish")
        if _check:
            got = (st or {}).get(_check.get("key"))
            if got != _check.get("equals"):
                raise InstrumentError(f"{_id} finished but {_check.get('key')} = "
                                      f"{got!r} (expected {_check.get('equals')!r})")
        return reply

    return Action(aid, label, run_fn, help=d.get("help", "") or d.get("description", ""))


def describe_or_none(inst: Instrument):
    """Fetch a manifest, or None if this service does not speak `describe` yet.

    Not every module has been taught to describe itself, and a coordinator that
    refused to talk to the ones that haven't would be useless during the
    rollout. Callers fall back to a hand-written builder.
    """
    try:
        return inst.command("describe").get("describe") or None
    except Exception:
        return None
