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

LIMITS ARE DYNAMIC HERE. Travel depends on the loop mode -- ~200 um
open-loop, ~160 um closed-loop -- so the position bounds are read from LIVE
status, and toggling closed_loop changes `revision`. Every status frame
carries `describe_rev` so a control screen redraws its slider instead of
offering travel the stage no longer has.
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


AXES = ("X", "Y")


def build_manifest(brain) -> dict:
    """Two piezo flexure axes -- and the module where limits genuinely move.

    TRAVEL DEPENDS ON THE LOOP MODE: the PXY-200 gives ~200 um open-loop and
    ~160 um closed-loop, and switching to CL re-clamps a standing target. So the
    position bounds here are read from LIVE STATUS (`travel_max` per axis),
    not from a config constant -- and flipping a `closed_loop` toggle changes
    the manifest revision, which is exactly the signal a control screen needs to
    redraw its slider instead of offering travel the stage no longer has.
    """
    cfg = brain.cfg
    lim = cfg.limits
    st = brain.status()          # live: travel_max follows the CL/OL mode
    travel_max = list(getattr(st, "travel_max", [lim.travel_max_ol] * 2))
    closed = list(getattr(st, "closed_loop", [False] * 2))

    params = []
    for i, ax in enumerate(AXES):
        low = ax.lower()
        hi = travel_max[i] if i < len(travel_max) else lim.travel_max_ol
        params += [
            _p(f"position_{low}", f"Position {ax}", "control", "float",
               unit="um", group="Position", order=10 + i,
               min=lim.travel_min, max=hi, step=cfg.motion.jog_step,
               decimals=3, plottable=True, read_path=["position", i],
               set={"verb": "move_axis", "arg": "position",
                    "extra": {"axis": ax}},
               settle={"policy": "flag_only", "key": "moving", "invert": True,
                       "index": i},
               help=f"Travel ceiling is {hi:g} um in "
                    f"{'closed' if (i < len(closed) and closed[i]) else 'open'}"
                    f" loop; it changes when the loop mode changes."),

            _p(f"closed_loop_{low}", f"Closed loop {ax}", "control", "bool",
               group="Loop mode", order=30 + i, read_path=["closed_loop", i],
               set={"verb": "set_closed_loop", "arg": "enabled",
                    "extra": {"axis": ax}},
               settle={"policy": "immediate"},
               help="Switching to closed loop REDUCES the travel ceiling and "
                    "re-clamps a standing target."),

            _p(f"velocity_{low}", f"Velocity {ax}", "control", "float",
               unit="um/s", group="Motion", order=50 + i, decimals=2,
               min=0.0, max=lim.max_velocity, read_path=["velocity", i],
               set={"verb": "set_velocity", "arg": "value",
                    "extra": {"axis": ax}},
               settle={"policy": "immediate"}),

            _p(f"moving_{low}", f"Moving {ax}", "indicator", "bool",
               group="Status", order=20 + i, read_path=["moving", i]),
            _p(f"relative_{low}", f"Relative {ax}", "indicator", "float",
               unit="um", group="Position", order=70 + i, decimals=3,
               read_path=["relative", i]),
        ]

    params += [
        _p("ramp_mode", "Ramp mode", "control", "enum", group="Motion",
           order=60, options=["hardware", "software", "off"],
           read_path=["ramp_mode"],
           set={"verb": "set_ramp_mode", "arg": "mode"},
           settle={"policy": "echoes", "key": "ramp_mode"},
           help="In `software` mode the hardware slew rate must be 0, or the "
                "two limiters stack and the stage lags the ramp."),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("stop", "STOP", "action", "action", group="Routines", order=90,
           danger=True),
        _p("set_zero", "Zero here", "action", "action", group="Routines",
           order=110,
           help="Captures the current position as the relative origin."),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "piezo",
                "label": "2D piezo stage", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
