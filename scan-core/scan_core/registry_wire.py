"""registry_wire.py -- a scan registry as JSON, and a PROXY registry built back
from it.

Why (Lukas, 2026-10-11): a measurement suite WATCHING a scan server on
another PC defines scans for the server to run. Its Scan tab -- the
parameter palette, the limits each axis is clamped to, the validation, the
ETA, the advanced axis settings (fly, ramp, window) -- has to show the
instruments THE SERVER drives, not the ones the watching PC happens to be
connected to. Until now the watcher could only connect to the lab's modules
itself (when the server reports them), and a server with a FIXED registry
(`--sim`, a test rig) reported nothing at all. The server now answers
`get_registry` with the manifest built here, and the watcher builds a PROXY
registry from it.

A proxy is a real scan_core Registry: the same Settable / Gettable / Action
classes, with the same limits, units, labels, integer flags, resolutions,
storage, array axes, acquire groups, stream and ramp capabilities and action
arguments -- everything `Recipe.validate`, `Recipe.compile` and the Scan
Builder read. What it can NOT do is move or read anything: setting, reading
and running raise RemoteOnly, because the scan runs on the server. (A watcher
may pass `read_from`, a registry it is connected to directly, so the few
places that ask "what is it now?" -- the default fly speed -- still get an
answer.)

Pure data, no Qt, no sockets: tested offline.
"""

from __future__ import annotations

import json
import math
import zlib

from .registry import (AcquireSpec, Action, AxisSpec, Gettable, Registry, Settable,
                       StreamSpec)

#: the wire format's version (a later one may add keys; readers ignore the
#: keys they do not know)
SCHEMA = 1


class RemoteOnly(RuntimeError):
    """A proxy parameter was asked to move, read or run: that happens on the
    scan server, not here."""


# ─────────────────────────────── to the wire ──────────────────────────────────

def _num(v):
    """A float for JSON: inf / nan -> None (JSON has neither; None = unbounded)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _storage(st) -> dict | None:
    if st is None:
        return None
    return {"kind": st.kind, "lo": st.lo, "hi": st.hi, "bits": st.bits,
            "options": list(st.options), "store": st.store}


def _stream(p) -> dict | None:
    s = getattr(p, "stream", None)
    if s is None:
        return None
    return {"group": str(getattr(s, "group", "") or ""),
            "channel": getattr(p, "stream_channel", None),
            "scale": _num(getattr(p, "stream_scale", 1.0)) or 1.0}


def _ramp(r) -> dict | None:
    if r is None:
        return None
    lo, hi = r.rate_limits
    return {"kind": r.kind, "rate_unit": r.rate_unit,
            "rate_limits": [_num(lo), _num(hi)],
            "rate_default": _num(r.rate_default),
            "binned_by": r.binned_by}


def _settable(p) -> dict:
    lo, hi = tuple(getattr(p, "limits", (None, None)))
    return {"id": p.id, "label": p.label, "unit": p.unit,
            "limits": [_num(lo), _num(hi)],
            "integer": bool(getattr(p, "integer", False)),
            "resolution": _num(getattr(p, "resolution", None)),
            "can_send": bool(getattr(p, "can_send", False)),
            "storage": _storage(getattr(p, "storage", None)),
            "stream": _stream(p),
            "ramp": _ramp(getattr(p, "ramp", None))}


def _gettable(p) -> dict:
    acq = getattr(p, "acquire", None)
    return {"id": p.id, "label": p.label, "unit": p.unit,
            "dtype": getattr(p, "dtype", "float"),
            "storage": _storage(getattr(p, "storage", None)),
            # an array detector's own dimensions (a VNA trace, a camera frame):
            # names, units and declared lengths -- NOT the coordinate values,
            # which the module computes and would cost a request per detector
            "axes": [{"name": a.name, "label": a.label, "unit": a.unit,
                      "length": a.length, "attrs": _plain(a.attrs)}
                     for a in (getattr(p, "axes", None) or [])],
            "acquire": {"group": acq.group} if acq is not None else None,
            "window": _plain(getattr(p, "window", None)),
            "stream": _stream(p)}


def _action(a) -> dict:
    return {"id": a.id, "label": a.label, "help": a.help,
            "args": _plain(getattr(a, "arg_specs", [])),
            "takes_args": bool(getattr(a, "takes_args", False)),
            "takes_context": bool(getattr(a, "_takes_context", False)),
            # the module's `wait` block (how a routine waits for it to finish)
            "wait": _plain(getattr(a, "wait", None))}


def _plain(obj):
    """Anything JSON can carry (numpy numbers, tuples, inf) as plain JSON."""
    try:
        import numpy as np
        if isinstance(obj, np.generic):
            obj = obj.item()
    except ImportError:                       # pragma: no cover
        pass
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    return str(obj)


def registry_to_wire(reg) -> dict:
    """The manifest of a registry: {schema, settables, gettables, actions}.

    Ordered as the registry holds them (the palette keeps that order)."""
    return {"schema": SCHEMA,
            "settables": [_settable(p) for p in reg.settables()],
            "gettables": [_gettable(p) for p in reg.gettables()],
            "actions": [_action(a) for a in reg.actions()]}


def wire_revision(wire: dict) -> int:
    """A number that changes whenever the manifest does (CRC-32 of its
    canonical JSON). Stable across restarts: a server restarted with the same
    instruments gives the same number, so a watcher fetches nothing again."""
    blob = json.dumps(wire, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


# ─────────────────────────────── from the wire ────────────────────────────────

def _bound(v, default):
    return default if v is None else float(v)


def _nope(what):
    def fn(*_a, **_k):
        raise RemoteOnly(f"{what} runs on the scan server, not on this PC")
    return fn


class _ReadbackStub:
    """Stands for a ramp's measured readback (only `binned_by` looks at it)."""
    stream = None
    stream_channel = None
    stream_scale = 1.0


def _storage_from(d):
    from .storage import Storage
    if not isinstance(d, dict):
        return None
    try:
        return Storage(d.get("kind") or "float", lo=d.get("lo"), hi=d.get("hi"),
                       bits=d.get("bits"), options=d.get("options"), store=d.get("store"))
    except (TypeError, ValueError):
        return None


def _attach_stream(p, d, streams: dict):
    s = d.get("stream")
    if not isinstance(s, dict):
        return
    group = str(s.get("group") or "")
    # one StreamSpec per group, shared like the real ones (the fly engine
    # starts a group once; validation only asks "is there a stream?")
    spec = streams.get(group)
    if spec is None:
        spec = streams[group] = StreamSpec(group, _nope(f"stream {group}"),
                                           _nope(f"stream {group}"))
    p.stream = spec
    p.stream_channel = s.get("channel")
    p.stream_scale = float(s.get("scale") or 1.0)


def _ramp_from(d):
    if not isinstance(d, dict):
        return None
    from .ramp import RampSpec
    lo, hi = (list(d.get("rate_limits") or []) + [None, None])[:2]
    measured = d.get("binned_by") == "measurement"
    return RampSpec(_nope("a ramp"), _nope("a ramp"), _nope("a ramp"),
                    kind=d.get("kind") or "software", rate_unit=d.get("rate_unit") or "",
                    rate_limits=(_bound(lo, 0.0), _bound(hi, math.inf)),
                    rate_default=d.get("rate_default"),
                    measured=measured, readback=_ReadbackStub() if measured else None)


def _reader(pid, read_from):
    def get():
        # read_from may be a callable that returns the registry of the moment
        # (the suite reconnects its direct instruments now and then)
        src = read_from() if callable(read_from) else read_from
        q = src.get(pid) if src is not None else None
        if q is None or not hasattr(q, "get"):
            raise RemoteOnly(f"{pid} is read on the scan server, not on this PC")
        return q.get()
    return get


def registry_from_wire(wire: dict, read_from=None) -> Registry:
    """A PROXY registry from a manifest (registry_to_wire's output).

    Limits, units and capabilities are the server's; set / run raise
    RemoteOnly. `read_from`: a registry this PC is connected to directly --
    a parameter with the same id is READ through it (never set)."""
    if not isinstance(wire, dict):
        raise ValueError("a registry manifest must be a JSON object")
    reg = Registry()
    #: marks a proxy: the scan tab says the scan server's instruments
    reg.proxy = True
    streams: dict = {}
    for d in wire.get("settables") or []:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        pid = str(d["id"])
        lo, hi = (list(d.get("limits") or []) + [None, None])[:2]
        send = _nope(f"setting {pid}")
        p = Settable(pid, d.get("label") or pid, d.get("unit") or "",
                     (_bound(lo, -math.inf), _bound(hi, math.inf)),
                     set_fn=_nope(f"setting {pid}"), get_fn=_reader(pid, read_from),
                     send_fn=(lambda v, _s=send: _s(v)) if d.get("can_send") else None)
        p.integer = bool(d.get("integer"))
        p.resolution = d.get("resolution")
        p.storage = _storage_from(d.get("storage"))
        p.ramp = _ramp_from(d.get("ramp"))
        _attach_stream(p, d, streams)
        reg.add(p)
    for d in wire.get("gettables") or []:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        pid = str(d["id"])
        axes = [AxisSpec(str(a.get("name")), a.get("label") or "", a.get("unit") or "",
                         values_fn=None, length=a.get("length"), attrs=a.get("attrs"))
                for a in (d.get("axes") or []) if isinstance(a, dict) and a.get("name")]
        acq = d.get("acquire")
        g = Gettable(pid, d.get("label") or pid, d.get("unit") or "",
                     _reader(pid, read_from), axes=axes, dtype=d.get("dtype") or "float",
                     acquire=AcquireSpec(str(acq.get("group") or pid))
                     if isinstance(acq, dict) else None,
                     window=d.get("window") if isinstance(d.get("window"), dict) else None,
                     storage=_storage_from(d.get("storage")))
        _attach_stream(g, d, streams)
        reg.add(g)
    for d in wire.get("actions") or []:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        aid = str(d["id"])
        fail = _nope(f"action {aid}")

        def run(context=None, args=None, _f=fail):
            _f()
        a = Action(aid, d.get("label") or aid, run, help=d.get("help") or "",
                   arg_specs=d.get("args") or [])
        # what the REAL action takes (the signature above takes both)
        a.takes_args = bool(d.get("takes_args"))
        a._takes_context = bool(d.get("takes_context"))
        a.wait = d.get("wait") if isinstance(d.get("wait"), dict) else None
        reg.add_action(a)
    return reg


#: what update_proxy copies onto a proxy parameter that is still there
_PARAM_ATTRS = ("label", "unit", "limits", "integer", "resolution", "storage", "ramp",
                "stream", "stream_channel", "stream_scale", "axes", "dtype", "acquire",
                "window", "_send")
_ACTION_ATTRS = ("label", "help", "arg_specs", "takes_args", "_takes_context", "wait")


def _shape(reg) -> list:
    return ([("s", p.id) for p in reg.settables()] + [("g", p.id) for p in reg.gettables()]
            + [("a", a.id) for a in reg.actions()])


def update_proxy(reg, wire: dict, read_from=None) -> bool:
    """Bring proxy `reg` up to date with `wire` IN PLACE when only values
    moved (a limit, a unit, a ramp's rate) and the parameters are the same.
    True when done; False when parameters came or went -- the caller then
    builds a new proxy (registry_from_wire).

    In place because the Scan tab's rows hold these Parameter objects: a
    limit that moved on the lab must re-clamp the axis rows, not throw the
    whole axis stack away (lab.py _apply_limits does the same for a direct
    connection)."""
    fresh = registry_from_wire(wire, read_from)
    if not getattr(reg, "proxy", False) or _shape(reg) != _shape(fresh):
        return False
    for new in [*fresh.settables(), *fresh.gettables()]:
        old = reg.get(new.id)
        for attr in _PARAM_ATTRS:
            if hasattr(new, attr) or hasattr(old, attr):
                setattr(old, attr, getattr(new, attr, None))
    for new in fresh.actions():
        old = reg.get_action(new.id)
        for attr in _ACTION_ATTRS:
            setattr(old, attr, getattr(new, attr, None))
    return True


__all__ = ["RemoteOnly", "registry_to_wire", "registry_from_wire", "update_proxy",
           "wire_revision", "SCHEMA"]
