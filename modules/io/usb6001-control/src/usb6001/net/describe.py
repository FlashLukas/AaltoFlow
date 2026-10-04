"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel, or scan-core a scan registry, for a
module it has never heard of. Full contract: INSTRUMENT_MODULE_GUIDE.md 6b.

THE MANIFEST FOLLOWS THE CONFIGURATION. Only the analog inputs that are
enabled, and only the digital lines in the matching direction, appear: an
output line is a control, an input line a detector, an unused line nothing.
It follows the layout the service STARTED with (the tasks that really exist),
not a config edited since -- that one applies after the restart, and the
manifest changes then. The configured names are the labels; the ids stay
fixed (`ai2`, `do_p0_4`), so a saved scan recipe keeps working after a rename.

`revision` is a checksum over the manifest without its values, so every status
frame's `describe_rev` tells a client, for one integer compare, that its copy
went stale (a rename, a new AO limit, a new layout after a restart).

WHY THE AI DETECTORS HAVE AN `acquire` BLOCK: a scan must record a voltage
measured AFTER its scan point was set. The live values in status come from the
poll thread and may be up to one poll period old. So scan-core triggers
`acquire`, waits for THAT acquisition's number (gotcha #17), and reads the
latched `sample`. All AI and DI detectors share ONE acquire group: one trigger,
one hardware read, every channel from the same moment.
"""

from __future__ import annotations

import json
import math
import zlib

from ..config import AI_CHANNELS, AO_CHANNELS, DIO_LINES, line_id

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, help="", acquire=None, wait=None):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract."""
    d = {
        "id": id, "label": label, "kind": kind, "type": type,
        "unit": unit, "group": group, "order": order,
        "writable": (kind == "control") if writable is None else writable,
        "plottable": plottable,
        "read_path": read_path,      # keys/indices into the status dict, or None
    }
    for k, v in (("value", value), ("min", min), ("max", max), ("step", step),
                 ("decimals", decimals), ("options", options), ("scale", scale),
                 ("set", set), ("settle", settle), ("args", args),
                 ("help", help), ("acquire", acquire), ("wait", wait)):
        if v is not None and v != "":
            d[k] = v
    if danger:
        d["danger"] = True
    return d


def manifest_revision(manifest: dict) -> int:
    """A checksum over the parts of the manifest a client must react to.

    Derived, not hand-bumped -- a counter someone has to remember to increment
    is a counter that will eventually be wrong. `value` is excluded on purpose:
    it changes many times a second and travels in the status stream anyway, so
    including it would tell clients to re-fetch constantly and mean nothing.
    """
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` against a status dict.

    A list of keys rather than a dotted string, because channel names and ids
    can contain dots and a dotted path could not be split back apart. An int
    element indexes into a per-axis list.
    """
    if not path:
        return None
    cur = status
    for key in path:
        if isinstance(key, int) and isinstance(cur, (list, tuple)):
            if not -len(cur) <= key < len(cur):
                return None
            cur = cur[key]
            continue
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur




def _is_scaled(ch) -> bool:
    return ch.unit.strip() not in ("", "V") or ch.slope != 1.0 or ch.offset != 0.0


def build_manifest(daq) -> dict:
    """The manifest for the layout `daq` is running with (see the docstring)."""
    cfg = daq.cfg
    lay = daq.layout
    acquire = {
        "group": "sample",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": round(daq.acquire_timeout_s(), 3),
    }
    params = []

    # -- analog outputs: controls ----------------------------------------------------
    # `echoes` on the published AO value, which the brain stores only AFTER
    # the write succeeded (gotcha #40) -- so "echoed" really means "written".
    # Before the first set of this session the echo is NaN ("unknown": the
    # 6001 cannot read AO back), which matches no target, as it should.
    for i, ch in enumerate(cfg.ao.channels):
        lo, hi = daq.ao_limits(i)
        params.append(_p(
            AO_CHANNELS[i], ch.name or AO_CHANNELS[i].upper(), "control", "float",
            unit="V", group="Analog outputs", order=10 + i, decimals=4, step=0.01,
            plottable=True, min=lo, max=hi, read_path=["ao_V", i],
            set={"verb": "set_ao", "arg": "volts", "extra": {"channel": i}},
            settle={"policy": "echoes", "key": "ao_V", "index": i, "tol": 1e-9},
            help=f"{AO_CHANNELS[i]} of the USB-6001. Shows 'unknown' until set in this "
                 f"session: the card cannot read its outputs back."))

    # -- digital outputs: bool controls ----------------------------------------------------
    for line in lay.do:
        d = cfg.dio.lines[line]
        params.append(_p(
            f"do_{line_id(line)}", d.name or DIO_LINES[line].upper(), "control", "bool",
            group="Digital outputs", order=100 + line, read_path=["dio", line],
            set={"verb": "set_do", "arg": "state", "extra": {"line": DIO_LINES[line]}},
            settle={"policy": "echoes", "key": "dio", "index": line},
            help=f"Digital line {DIO_LINES[line]}, configured as OUTPUT."))

    # -- analog inputs: fresh detectors + live indicators ---------------------------------
    for i in lay.ai:
        ch = cfg.ai.channels[i]
        label = ch.name or AI_CHANNELS[i].upper()
        unit = ch.unit.strip() or "V"
        params.append(_p(
            AI_CHANNELS[i], label, "indicator", "float", unit=unit,
            group="Analog inputs", order=200 + i, decimals=5,
            read_path=["sample", "ai", i], acquire=acquire,
            help=f"{AI_CHANNELS[i]} ({ch.terminal}): mean of {cfg.ai.samples_per_read} "
                 f"samples taken AFTER the trigger -- safe to record in a scan."
                 + (f" Scaled: {ch.slope:g} {unit}/V x V + {ch.offset:g} {unit}."
                    if _is_scaled(ch) else "")))
        if _is_scaled(ch):
            params.append(_p(
                f"{AI_CHANNELS[i]}_V", f"{label} (V)", "indicator", "float", unit="V",
                group="Analog inputs", order=200 + i, decimals=5,
                read_path=["sample", "ai_V", i], acquire=acquire,
                help="The same fresh reading in volts, before the scale."))
        params.append(_p(
            f"{AI_CHANNELS[i]}_live", f"{label} (live)", "indicator", "float", unit=unit,
            group="Live", order=300 + i, decimals=4, plottable=True,
            read_path=["ai", i],
            help="Latest poll-thread reading: for a panel, not for a scan point."))

    # -- digital inputs -------------------------------------------------------------------------
    for line in lay.di:
        d = cfg.dio.lines[line]
        label = d.name or DIO_LINES[line].upper()
        params.append(_p(
            f"di_{line_id(line)}", label, "indicator", "bool",
            group="Digital inputs", order=400 + line,
            read_path=["sample", "dio", line], acquire=acquire,
            help=f"Line {DIO_LINES[line]} read AFTER the trigger (scan-safe)."))
        params.append(_p(
            f"di_{line_id(line)}_live", f"{label} (live)", "indicator", "bool",
            group="Live", order=500 + line, read_path=["dio", line]))

    # -- actions ---------------------------------------------------------------------------------
    params += [
        _p("acquire", "Acquire sample", "action", "action", group="Measurement",
           order=1,
           # a `wait` block makes it usable in scan routines (before/after scan)
           wait={"target_key": "acq_id", "ready": acquire["ready"],
                 "timeout_s": acquire["timeout_s"]},
           help="Read every enabled input once, fresh, and latch it as the sample."),
        _p("read_ai", "Read analog input", "action", "action", group="Measurement",
           order=2,
           args=[{"name": "channel", "label": "Channel (empty = all)", "type": "string",
                  "default": ""}],
           help="A fresh averaged reading; the reply carries volts and scaled values."),
        _p("read_di", "Read digital input", "action", "action", group="Measurement",
           order=3,
           args=[{"name": "line", "label": "Line (empty = all)", "type": "string",
                  "default": ""}],
           help="A fresh reading of the input lines."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=4, read_path=["acquiring"]),
        # a counter that starts at 0 and only counts up: min=0 is a promise
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=5, min=0, read_path=["acq_id"]),

        # -- status ------------------------------------------------------------------------------
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),
        _p("restart_pending", "Restart pending", "indicator", "bool", group="Status",
           order=4, read_path=["restart_pending"],
           help="The saved layout (AI channels, line directions) differs from the "
                "running one; it applies after a service restart."),
    ]

    # A bound that is not a finite number must not appear at all.
    for d in params:
        for k in ("min", "max"):
            if k in d and not (isinstance(d[k], (int, float)) and math.isfinite(d[k])):
                del d[k]

    manifest = {"schema": SCHEMA_VERSION, "module": "usb6001",
                "label": "General DAQ (NI USB-6001)", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
