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

Limits come from the config travel envelope. `revision` travels in every
status frame as `describe_rev`, so a client re-fetches only when it moves.
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
    """Three coarse stepper axes, expanded FLAT: position_x, position_y, ...

    One descriptor per axis rather than one taking an axis argument, so a
    control screen can place a single axis on a panel and scan-core can sweep
    one. The axis travels in the `set` block's `extra`, which is what carries
    a verb's non-value arguments.
    """
    cfg = brain.cfg
    lim = cfg.limits
    bounds = {
        "X": (lim.min_x, lim.max_x),
        "Y": (lim.min_y, lim.max_y),
        "Z": (lim.min_z, lim.max_z),
    }
    params = []
    for i, ax in enumerate(AXES):
        lo, hi = bounds[ax]
        low = ax.lower()
        params += [
            _p(f"position_{low}", f"Position {ax}", "control", "float",
               unit="mm", group="Position", order=10 + i, min=lo, max=hi,
               step=cfg.motion.jog_step, decimals=4, plottable=True,
               read_path=["position", i],
               set={"verb": "move_axis", "arg": "position",
                    "extra": {"axis": ax}},
               # No per-knob done-flag: the stage reports `moving` per axis and
               # does not echo a target, so "arrived" means "stopped".
               settle={"policy": "flag_only", "key": "moving", "invert": True,
                       "index": i}),

            _p(f"velocity_{low}", f"Velocity {ax}", "control", "float",
               unit="mm/s", group="Motion", order=40 + i, decimals=3,
               min=0.0, max=lim.max_velocity, read_path=["velocity", i],
               set={"verb": "set_velocity", "arg": "value",
                    "extra": {"axis": ax}},
               settle={"policy": "immediate"}),

            _p(f"moving_{low}", f"Moving {ax}", "indicator", "bool",
               group="Status", order=20 + i, read_path=["moving", i]),
            _p(f"relative_{low}", f"Relative {ax}", "indicator", "float",
               unit="mm", group="Position", order=70 + i, decimals=4,
               read_path=["relative", i]),
            _p(f"home_{low}", f"Home {ax}", "action", "action",
               group="Routines", order=100 + i, danger=True,
               settle={"policy": "flag_only", "key": "moving", "invert": True,
                       "index": i},
               help="Drives to the limit switch and re-references the axis."),
        ]

    params += [
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("stop", "STOP", "action", "action", group="Routines", order=90,
           danger=True, help="Halts every axis immediately."),
        _p("set_zero", "Zero here", "action", "action", group="Routines",
           order=110,
           help="Captures the current position as the relative origin. NOTE: "
                "this lives in the `relative` config group, so a set_config "
                "push from a coordinator overwrites it."),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "stage",
                "label": "3D coarse stage", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
