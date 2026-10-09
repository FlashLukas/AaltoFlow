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

import threading
import time

from .instrument import (Instrument, InstrumentError, adopt_then_flag, echoes,
                         flag_only, immediate)
from .registry import (AcquireSpec, Action, AxisSpec, Gettable, Registry,
                       Settable, StreamSpec)
from .storage import Storage

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
    # (`index` may be None in a hand-written block: no index then)
    return [key, int(c["index"])] if c.get("index") is not None else key


_POLICIES = {
    # `index` applies to BOTH keys (per-axis target_um AND per-axis moving --
    # the motion modules' target echo, gotcha #40). `tol`: how far the echoed
    # target may sit from the request (a stepper rounds um to whole steps).
    "adopt_then_flag": lambda c: adopt_then_flag(
        _k(c, "setpoint_key"), _k(c, "flag_key"), c.get("invert", False),
        float(c.get("tol", 1e-6))),
    "echoes": lambda c: echoes(_k(c, "key"), c.get("tol", 1e-6)),
    "flag_only": lambda c: flag_only(_k(c, "key"), c.get("invert", False)),
    "immediate": lambda c: immediate(),
    "state_in": lambda c: _state_in(_k(c, "key"), c["states"]),
}


#: an echoed setpoint that sits on another value for this long is checked
#: against the module's CURRENT limits (see _clamp_guard)
CLAMP_CHECK_S = 2.0


def _clamp_guard(predicate, inst, desc: dict, wire, pid: str, is_bool: bool):
    """Fail FAST when the service adopted a DIFFERENT value than asked for.

    Found on the rig (2026-10-02): a scan asked the camera for scan point 48;
    the camera's array had 48 points (0..47) by then, so it clamped to 47,
    reported index 47 and "settled" -- and the adopt check, waiting for 48,
    sat out its whole 60 s timeout ("the measurement suite is stuck").

    For a settle that waits for an ECHO (adopt_then_flag's setpoint_key, or
    echoes' key): if the echo has stayed on another value for CLAMP_CHECK_S,
    re-read `describe` ONCE; if the request lies outside the module's current
    [min, max], raise at once, naming both values and the limits. Inside the
    limits it is just slow, and the normal wait goes on.
    """
    if is_bool:
        return predicate
    settle = desc.get("settle") or {}
    name = settle.get("setpoint_key") if settle.get("policy") == "adopt_then_flag" \
        else settle.get("key") if settle.get("policy") == "echoes" else None
    if not name:
        return predicate
    key = [name, int(settle["index"])] if settle.get("index") is not None else name
    tol = float(settle.get("tol", 1e-6))
    state = {"since": None, "seen": None, "checked": False}

    def guarded(st):
        if predicate(st):
            return True
        from .instrument import _lookup
        got = _lookup(st, key)
        if not isinstance(got, (int, float)) or isinstance(got, bool) \
                or abs(got - wire) <= tol:
            # forget BOTH: a stale frame can flap back to the old value after
            # the echo matched once (old, new, old, ... seen on the AFG's phase,
            # 2026-10-07); with `seen` kept, that old value looked "unchanged"
            # and the clock below was read while `since` was None (TypeError)
            state["since"] = state["seen"] = None
            return False
        now = time.monotonic()
        if state["seen"] != got:                       # still moving: restart the clock
            state["seen"], state["since"] = got, now
            return False
        if state["checked"] or now - state["since"] < CLAMP_CHECK_S:
            return False
        state["checked"] = True
        try:
            fresh = inst.command("describe").get("describe") or {}
        except Exception:
            return False                               # cannot tell: keep waiting
        inst.manifest = fresh
        mine = next((p for p in fresh.get("parameters", [])
                     if p.get("id") == desc.get("id")), {})
        lo, hi = mine.get("min"), mine.get("max")
        scale = float(desc.get("scale", 1.0)) or 1.0
        if (lo is not None and wire < lo) or (hi is not None and wire > hi):
            module = getattr(inst, "alias", None) or fresh.get("module") or inst.name
            raise InstrumentError(
                f"{module} clamped {pid} {wire / scale:g} -> {got / scale:g}: its limits "
                f"are now [{(lo if lo is not None else float('-inf')) / scale:g}, "
                f"{(hi if hi is not None else float('inf')) / scale:g}] -- the module "
                f"changed (e.g. a smaller scan array) since the scan was checked")
        return False

    return guarded


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
    # One StreamSpec per stream GROUP of this module, however many parameters
    # are recorded in it -- they are started, read and stopped once together.
    streams: dict = {}

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
                # Recordable since 2026-10-04 (storage.py): an enum as its
                # option's code, a string as text. Not an AXIS: it is a
                # Gettable, and recipe.validate wants a settable there.
                reg.add(Gettable(pid, label, unit, getter, dtype=dtype,
                                 storage=Storage.from_descriptor(d, use_bounds=False)))
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
            # SUPERSEDING. Every set of this knob takes a new number; a wait
            # still running for an OLDER number ends at once. Why: with a
            # target-echo settle (the motion modules since 2026-09-28, gotcha
            # #40) a set waits until the service echoes ITS target -- and a
            # later command ("stop here", a new move) replaces that target, so
            # the echo never comes. A fly row ends its move exactly like that,
            # and the move's own wait (in its helper thread) used to sit out
            # its whole timeout. A newer command on the same knob answers the
            # older wait: the knob is now the newer command's business.
            gen = [0, threading.Lock()]

            # RESOLUTION: the instrument realises only multiples of it (the
            # DS generator's 0.5 dB attenuator, which IGNORES an off-step
            # request -- lab PC 2026-10-06). Not `step`, which is only the
            # GUI's increment (clMag's field step is 2 mT and is no such
            # thing). In the module's own unit, i.e. on the WIRE value.
            res = _resolution(d)

            def sender(value, _s=spec, _inst=inst, _settle=settle, _t=timeout,
                       _id=pid, _u=unit, _bool=(dtype == "bool"), _scale=scale,
                       _int=(dtype == "int"), timeout_s=None, _gen=gen, _d=d, _res=res):
                """SEND the command now; return the function that WAITS for
                it to settle. setter() below is the two in a row; the engine's
                `diagonal` row change sends two knobs before waiting for
                either (2026-10-08)."""
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
                elif _res and not _bool:
                    # ... and settle on the ROUNDED value, as for an int: the
                    # echo will be -14.0, never the -13.75 that was asked
                    wire = round(round(wire / _res) * _res, 9)
                with _gen[1]:
                    _gen[0] += 1
                    mine = _gen[0]
                _inst.command(_s["verb"], **{_s["arg"]: wire}, **extra)
                shown = wire if _bool else f"{value:g} {_u}".strip()

                def wait():
                    # timeout_s: a fly scan's row is ONE long move, far slower
                    # than the ordinary step this timeout was declared for
                    _inst.wait_until(_clamp_guard(_settle(wire), _inst, _d, wire, _id, _bool),
                                     timeout_s=_t if timeout_s is None else timeout_s,
                                     what=f"{_id} = {shown}",
                                     cancel=lambda: _gen[0] != mine)
                return wait

            def setter(value, timeout_s=None, _send=sender):
                _send(value, timeout_s=timeout_s)()

            param = reg.add(Settable(pid, label, unit, (lo, hi),
                                     set_fn=setter, get_fn=getter, send_fn=sender))
            _attach_stream(param, d, inst, module, streams, on_warn)
            # A knob the module can SWEEP CONTINUOUSLY (its `ramp` block):
            # a fly scan can fly it (ramp.py). Its readback stream shares
            # this module's stream cache, so a group recorded for detectors
            # too is started and read once.
            from .ramp import ramp_from_descriptor
            param.ramp = ramp_from_descriptor(d, inst, module, streams, _stream_from,
                                              pid, on_warn)
            # An INT control only has whole-number settings (a scan-array index,
            # a filter order). The builder reads this to offer whole points
            # rather than 21 samples across 0..19.
            param.integer = bool(_int_dtype(dtype))
            # in SCAN units, for the axis preview (value * scale is the wire)
            param.resolution = (res / scale) if (res and scale) else None
            # Recorded as a detector (the readback of a knob being driven), a
            # bool is stored as 0/1 and an int as an integer. Its min/max are
            # SETTING limits, not a promise about the readback, so they do not
            # narrow the storage (storage.py, from_descriptor).
            param.storage = Storage.from_descriptor(d, use_bounds=False)
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
            # A string/enum indicator (a state name, a filter) is recordable
            # since 2026-10-04: dtype names it, and its STORAGE -- like every
            # indicator's -- comes from the descriptor (storage.py): type,
            # min/max (a promise: a value outside stops the scan), bits,
            # options, store.
            text = dtype in ("enum", "string") and not d.get("dtype")
            param = reg.add(Gettable(pid, label, unit, getter, axes=axes,
                                     dtype=dtype if text else d.get("dtype", "float"),
                                     acquire=_acquire_from(d, inst, module, on_warn),
                                     window=_window_from(d, axes, on_warn),
                                     storage=Storage.from_descriptor(d)))
            _attach_stream(param, d, inst, module, streams, on_warn)
            added.append(pid)

    return added


def _resolution(d: dict) -> float | None:
    """A descriptor's `resolution` (> 0), or None."""
    try:
        r = float(d.get("resolution") or 0.0)
    except (TypeError, ValueError):
        return None
    return r if r > 0 else None


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
    # The descriptor's `scale` (wire = display x scale) applies here exactly as
    # it does to the status reader: one descriptor must give ONE number however
    # the module serves it. (Missing until 2026-09-28; no module combined
    # `read` with `scale` yet, so nothing measured was affected.)
    scale = float(d.get("scale", 1.0) or 1.0)

    def getter():
        reply = inst.command(verb, **args)
        if key not in reply:
            raise InstrumentError(f"{d.get('id')}: the {verb!r} reply has no {key!r}")
        value = decode_wire_value(reply[key], complex_)
        if scale != 1.0 and not isinstance(value, bool):
            if isinstance(value, (int, float)):
                return value / scale
            if hasattr(value, "dtype") and value.dtype.kind in "fc":
                return value / scale
        return value
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
        def trigger_fn(_v=verb, _i=inst, _key=target_key, _last=last, _g=group,
                       **extra):
            # `extra` = the resonance window's bins ({"window": [i0, i1]}),
            # present only on a windowed point of a windowed scan
            reply = _i.command(_v, **extra)
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


def _window_from(d: dict, axes, on_warn):
    """The descriptor's `window` block (resonance window support), or None.

        "window": {"arg": "window", "unit": "bin", "min_bins": 5}

    = the acquire trigger accepts `window: [i0, i1]`, inclusive BIN INDICES of
    the detector's full frequency grid, sweeps only those bins, and the fetched
    trace comes back FULL LENGTH with null outside [i0, i1]. Bins, never Hz:
    the measured bins then sit exactly on the dataset's frequency axis.
    Only meaningful on a 1-D array detector with an acquire step.
    """
    w = d.get("window")
    if not w:
        return None
    if not isinstance(w, dict) or (w.get("unit") or "bin") != "bin":
        if on_warn:
            on_warn(f"{d.get('id')}: `window` must be {{\"unit\": \"bin\", ...}}; ignoring it")
        return None
    if len(axes or []) != 1 or not d.get("acquire"):
        if on_warn:
            on_warn(f"{d.get('id')}: `window` needs a 1-D array detector with an "
                    f"`acquire` block; ignoring it")
        return None
    try:
        min_bins = max(1, int(w.get("min_bins", 3)))
    except (TypeError, ValueError):
        min_bins = 3
    return {"arg": str(w.get("arg") or "window"), "unit": "bin", "min_bins": min_bins}


def _attach_stream(param, d: dict, inst: Instrument, module: str,
                   streams: dict, on_warn) -> None:
    """Give `param` the module's stream, if its descriptor declares one.

        "stream": {"group": "demod", "channel": "x1"}

    Optional "start_verb" / "read_verb" / "stop_verb" (default stream_start,
    stream_read, stream_stop). The read and stop replies carry

        {"ok": true, "stream": {"t": [...], "values": {"x1": [...], ...},
                                "delay_s": {"x1": 0.02, ...},
                                "overflow": false, "now": <module time.time()>}}

    `now` lets scan-core put the module's time stamps on THIS computer's
    clock: a module on another PC stamps with its own clock, and two clocks
    a few ms apart would shift one stream against the other -- the same error
    as an uncorrected filter lag. The offset is estimated NTP-style from the
    read with the shortest round trip (the one whose `now` is least blurred by
    network delay).
    """
    spec = d.get("stream")
    if not spec:
        return
    channel = spec.get("channel")
    if not channel:
        if on_warn:
            on_warn(f"{d.get('id')}: `stream` names no channel; ignoring it")
        return
    group = f"{module}.{spec.get('group', 'stream')}"
    key = (group, spec.get("start_verb"), spec.get("read_verb"), spec.get("stop_verb"))
    if key not in streams:
        streams[key] = _stream_from(group, spec, inst)
    param.stream = streams[key]
    param.stream_channel = channel
    # the stream carries WIRE units, like status: convert with the same scale
    param.stream_scale = float(d.get("scale", 1.0) or 1.0)


def _stream_from(group: str, spec: dict, inst: Instrument) -> StreamSpec:
    start_v = spec.get("start_verb", "stream_start")
    read_v = spec.get("read_verb", "stream_read")
    stop_v = spec.get("stop_verb", "stream_stop")
    samples: list = []          # recent (round trip, offset) pairs

    def fetch(verb):
        import time as _time
        t0 = _time.time()
        reply = inst.command(verb)
        t1 = _time.time()
        chunk = dict(reply.get("stream") or {})
        now = chunk.get("now")
        if isinstance(now, (int, float)):
            samples.append((t1 - t0, float(now) - 0.5 * (t0 + t1)))
            del samples[:-20]
        offset = min(samples)[1] if samples else 0.0
        if offset and chunk.get("t"):
            chunk["t"] = [None if t is None else t - offset for t in chunk["t"]]
        return chunk

    return StreamSpec(group,
                      start_fn=lambda: inst.command(start_v),
                      read_fn=lambda: fetch(read_v),
                      stop_fn=lambda: fetch(stop_v))


class _Blank(dict):
    """format_map helper: an unknown {name} becomes "" instead of a KeyError."""
    def __missing__(self, key):
        return ""


def fill_placeholders(args: dict, context: dict | None) -> dict:
    """Fill {data_dir}, {data_stem}, {moment} in an action's TEXT arguments.

    A module declares where a routine's output should go with placeholders in
    its argument defaults -- the camera's "save pattern" defaults to
    folder "{data_dir}", name "{data_stem}_{moment}_pattern" -- and scan-core,
    which alone knows where the measurement is being written, fills them in
    when the action runs as a routine. Outside a scan (no context) they become
    "" and the module falls back to its own folder.
    """
    ctx = _Blank(context or {})
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and "{" in v:
            try:
                v = v.format_map(ctx)
            except (ValueError, IndexError):      # a stray brace: send it as it is
                pass
        out[k] = v
    return out


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
    _declared = [a["name"] for a in (d.get("args") or [])
                 if isinstance(a, dict) and "name" in a]
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
               _t=timeout, _id=aid, _check=check, context=None, args=None):
        # `args` (a script, scan_core/api.py) replaces declared defaults; a
        # name the module did not declare is refused here, before anything is
        # sent -- a typo must not be silently dropped
        merged = dict(_a)
        if args:
            unknown = sorted(set(args) - set(_declared))
            if unknown:
                raise ValueError(f"{_id}: no argument called {', '.join(unknown)}"
                                 f" (it takes: {', '.join(_declared) or 'none'})")
            merged.update(args)
        reply = _i.command(_v, **fill_placeholders(merged, context))
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
