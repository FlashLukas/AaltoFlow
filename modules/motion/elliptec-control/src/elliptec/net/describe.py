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

Limits come from the config angle window and velocity window. `revision` travels in every
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
       settle=None, args=None, wait=None, danger=False, help="", ramp=None,
       stream=None):
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
                 ("set", set), ("settle", settle), ("args", args), ("wait", wait),
                 ("ramp", ramp), ("stream", stream), ("help", help)):
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


def build_manifest(brain) -> dict:
    """One rotary axis per configured bus address, expanded FLAT.

    Ids carry the ADDRESS, not the list position (``angle_0``, ``angle_a``),
    so a saved scan recipe keeps pointing at the same physical mount if the
    address list is reordered.  The axis index travels in the ``set`` block's
    ``extra``, which is what carries a verb's non-value arguments.
    """
    cfg = brain.cfg
    lim = cfg.limits
    if lim.enforce:
        a_lo, a_hi = float(lim.min_angle_deg), float(lim.max_angle_deg)
        v_lo, v_hi = int(lim.min_velocity_pct), min(100, int(lim.max_velocity_pct))
    else:
        a_lo, a_hi, v_lo, v_hi = 0.0, 360.0, 1, 100
    # The wrap to [0, 360) always applies, so no window can be wider than that.
    a_lo, a_hi = max(0.0, a_lo), min(360.0, a_hi)
    step = float(cfg.motion.jog_step_deg)
    r_lo, r_hi = brain.ramp_rate_limits()

    # An ACTION is invoked by its id (scan-core and the control screen send the
    # id as the verb, with no axis argument), so per-axis actions are named by
    # address -- home_0, set_zero_a -- and the service answers those verbs.
    params = []
    for i, (addr, name) in enumerate(zip(brain.addresses, brain.names)):
        s = addr.lower()
        who = f"{name} [{addr}]"
        params += [
            _p(f"angle_{s}", f"Angle {who}", "control", "float",
               unit="deg", group="Angle", order=10 + i, min=a_lo, max=a_hi,
               step=step, decimals=3, plottable=True,
               read_path=["angle_deg", i],
               set={"verb": "move_abs", "arg": "angle_deg", "extra": {"axis": i}},
               # Adopt first, then trust `moving`: right after the command the
               # status can still describe the previous point (gotcha #2).
               # target_deg is published exactly as commanded, never rounded.
               settle={"policy": "adopt_then_flag", "setpoint_key": "target_deg",
                       "flag_key": "moving", "invert": True, "index": i},
               # A CONTINUOUS SWEEP for fly scans (2026-10-10; guide 6b,
               # "Ramps"): a HARDWARE ramp -- the mount turns to the angle at
               # a set speed by itself (the rate in deg/s becomes its velocity
               # percent; the user's velocity comes back afterwards). Binned
               # by the MEASURED encoder angle the worker polls. The slowest
               # speed is ~30 % of ~430 deg/s: fly rows over an angle are fast.
               ramp={"kind": "hardware",
                     "start": {"verb": "ramp_angle",
                               "args": {"to": "angle_deg", "rate": "rate_deg_per_s"},
                               "extra": {"axis": i}},
                     "stop": {"verb": "ramp_stop"},
                     "rate": {"unit": "deg/s", "min": r_lo, "max": r_hi,
                              "default": r_lo},
                     "readback": {"stream": {"group": "angle", "channel": f"angle_{s}"},
                                  "measured": True},
                     "done": {"key": "ramping", "id_key": "ramp_id"}},
               stream={"group": "angle", "channel": f"angle_{s}"},
               help="User angle (device angle minus the offset), degrees. "
                    "360 is the same orientation as 0."),

            _p(f"velocity_{s}", f"Velocity {who}", "control", "int",
               unit="%", group="Motion", order=40 + i, min=v_lo, max=v_hi,
               step=5, read_path=["velocity_pct", i],
               set={"verb": "set_velocity", "arg": "value", "extra": {"axis": i}},
               settle={"policy": "echoes", "key": "velocity_pct", "index": i},
               help="Drive speed as a percentage of the mount's maximum."),

            _p(f"device_angle_{s}", f"Device angle {who}", "indicator", "float",
               unit="deg", group="Angle", order=20 + i, decimals=3,
               plottable=True, read_path=["device_deg", i],
               help="Encoder angle from the home mark, before the offset."),
            _p(f"offset_{s}", f"Offset {who}", "indicator", "float",
               unit="deg", group="Angle", order=30 + i, decimals=3,
               read_path=["offset_deg", i]),
            _p(f"moving_{s}", f"Moving {who}", "indicator", "bool",
               group="Status", order=50 + i, read_path=["moving", i]),
            _p(f"homed_{s}", f"Homed {who}", "indicator", "bool",
               group="Status", order=60 + i, read_path=["homed", i]),
            _p(f"error_{s}", f"Error {who}", "indicator", "string",
               group="Status", order=70 + i, read_path=["error", i]),

            _p(f"home_{s}", f"Home {who}", "action", "action",
               group="Routines", order=100 + i,
               # Numbered like a move, so a routine waits for THIS home and not
               # for a stale "not moving" frame from before it (gotcha #17).
               wait={"target_key": "move_id",
                     "ready": {"policy": "adopt_then_flag", "setpoint_key": "move_id",
                               "flag_key": "moving", "invert": True, "index": i},
                     "timeout_s": float(cfg.hardware.move_timeout_s) + 5.0},
               help="Turns the mount to its home mark (device 0 deg) and "
                    "re-references the encoder. The optic rotates."),
            _p(f"set_zero_{s}", f"Zero here {who}", "action", "action",
               group="Routines", order=120 + i,
               wait={"ready": {"policy": "immediate"}},
               help="Calls the current angle 0 deg (sets the offset). Lives in "
                    "the `offsets` config group, so a set_config push of that "
                    "group overwrites it."),
        ]

    params += [
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("ramping", "Sweeping", "indicator", "bool", group="Status", order=2,
           read_path=["ramping"],
           help="True while an angle sweep (ramp_angle) turns a mount."),
        _p("stop", "STOP", "action", "action", group="Routines", order=90,
           danger=True,
           wait={"ready": {"policy": "immediate"}},
           help="Halts every mount immediately."),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "elliptec",
                "label": "Elliptec rotation mount", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
