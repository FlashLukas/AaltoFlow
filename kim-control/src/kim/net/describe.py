"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of.

Two consumers, two projections of one source: the reconfigurable control screen
reads this manifest DIRECTLY (it wants buttons, their arguments, their danger
flags and the group/order layout hints), while scan-core projects it into
Settables and Gettables. Full contract: INSTRUMENT_MODULE_GUIDE.md section 6b.

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every limit is looked up from cfg or from live status when the
manifest is built, never copied into a literal. A manifest that restates a limit
is a limit with two homes, and the wrong one does not announce itself -- it just
draws a slider with the wrong range.

LIMITS ARE DYNAMIC HERE. An armed leash REPLACES the absolute travel clamp,
so the bounds are read from the brain's published effective limits
(`limit_lo`/`limit_hi`). Arming the leash changes `revision`, and every
status frame carries `describe_rev`.
"""

from __future__ import annotations

import json
import zlib

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, help=""):
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
                 ("help", help)):
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


AXES = ("X", "Y", "Z")


def build_manifest(brain) -> dict:
    """Three piezo-inertia axes -- the other module where limits genuinely move.

    THE ARMED LEASH REPLACES THE MIN/MAX CLAMP. The brain already publishes the
    EFFECTIVE bounds per axis as `limit_lo` / `limit_hi` (leash applied when it
    is armed, full travel when it is not), so this reads those rather than
    recomputing the rule -- there is exactly one implementation of "what can
    this axis reach", and it is the one that does the clamping.

    Positions are offered in MICROMETRES, the unit an operator thinks in, using
    the per-axis `um_per_step` calibration the brain publishes. Changing the
    drive voltage changes the physical step size and therefore invalidates that
    calibration.
    """
    cfg = brain.cfg
    lim = cfg.limits
    st = brain.status()
    # Limits per axis are named min_steps_x / max_steps_x / ... in config, but
    # the brain publishes the EFFECTIVE bounds (leash applied when armed) as
    # limit_lo / limit_hi, so prefer those and fall back to the config only if a
    # status is somehow missing them.
    cfg_lo = [lim.min_steps_x, lim.min_steps_y, lim.min_steps_z]
    cfg_hi = [lim.max_steps_x, lim.max_steps_y, lim.max_steps_z]
    lo_steps = list(getattr(st, "limit_lo", None) or cfg_lo)
    hi_steps = list(getattr(st, "limit_hi", None) or cfg_hi)
    um_per_step = list(getattr(st, "um_per_step", None)
                       or [cfg.calibration.um_per_step] * 3)
    leashed = bool(getattr(st, "leash", False))

    params = []
    for i, ax in enumerate(AXES):
        low = ax.lower()
        k = um_per_step[i] if i < len(um_per_step) and um_per_step[i] else 0.02
        params += [
            _p(f"position_{low}", f"Position {ax}", "control", "float",
               unit="um", group="Position", order=10 + i,
               min=lo_steps[i] * k, max=hi_steps[i] * k, decimals=3,
               plottable=True, read_path=["position_um", i],
               set={"verb": "move_to_um", "arg": "position",
                    "extra": {"axis": ax}},
               settle={"policy": "flag_only", "key": "moving", "invert": True,
                       "index": i},
               help=("Bounds are the LEASH box around the datum."
                     if leashed else
                     "Bounds are the full symmetric travel; arming the leash "
                     "narrows them.")),

            _p(f"velocity_{low}", f"Velocity {ax}", "control", "float",
               unit="um/s", group="Motion", order=40 + i, decimals=3,
               min=0.0, max=lim.max_step_rate * k, read_path=["velocity_um", i],
               set={"verb": "set_velocity_um", "arg": "value",
                    "extra": {"axis": ax}},
               settle={"policy": "immediate"}),

            _p(f"voltage_{low}", f"Drive voltage {ax}", "control", "float",
               unit="V", group="Drive", order=60 + i, decimals=1, step=1.0,
               min=lim.min_voltage, max=lim.max_voltage,
               read_path=["voltage", i],
               set={"verb": "set_voltage", "arg": "value",
                    "extra": {"axis": ax}},
               settle={"policy": "immediate"},
               help="Sets the physical step size. Changing it INVALIDATES the "
                    "um_per_step calibration for this axis."),

            _p(f"moving_{low}", f"Moving {ax}", "indicator", "bool",
               group="Status", order=20 + i, read_path=["moving", i]),
            _p(f"steps_{low}", f"Step counter {ax}", "indicator", "int",
               group="Position", order=80 + i, read_path=["position_steps", i]),
            _p(f"datum_{low}", f"Datum {ax}", "action", "action",
               group="Routines", order=100 + i, danger=True,
               help="HARDWARE reset of the step counter -- the closest thing "
                    "this open-loop stage has to a home. Not the same as "
                    "'Zero here', which only moves the display origin."),
        ]

    params += [
        _p("speed_fast", "Movement: fast", "control", "bool", group="Presets",
           order=200, read_path=["speed_fast"],
           set={"verb": "set_speed", "arg": "fast"},
           settle={"policy": "echoes", "key": "speed_fast"},
           help="Front-panel preset. Off = slow (300 steps/s), the safe default."),
        _p("step_large", "Steps: large", "control", "bool", group="Presets",
           order=210, read_path=["step_large"],
           set={"verb": "set_step_size", "arg": "large"},
           settle={"policy": "echoes", "key": "step_large"},
           help="Drives every axis to max or min voltage."),
        _p("leash", "Leash armed", "indicator", "bool", group="Limits",
           order=220, read_path=["leash"],
           help="When armed, a symmetric box around the datum REPLACES the "
                "absolute travel limits -- so the position bounds above change."),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("stop", "STOP", "action", "action", group="Routines", order=90,
           danger=True),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "kim",
                "label": "3D inertia stage", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
