"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of.
Two consumers: the reconfigurable control screen reads this manifest directly,
scan-core projects it into Settables / Gettables / Actions. Full contract:
INSTRUMENT_MODULE_GUIDE.md section 6b.

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every limit is looked up from cfg (or computed by the brain)
when the manifest is built. `revision` is a checksum of the manifest without
its values and travels in every status frame as `describe_rev`, so a client
re-fetches only when a limit moved.

Settle policies used here, and why:

* position -> adopt_then_flag(target_mm, moving, invert). The status echoes
  the target it was given, so a waiting scan first sees ITS target adopted and
  only then trusts "not moving" -- never the "not moving" of the previous point
  (gotcha #2).
* velocity -> echoes(velocity_mm_s): the service reports the speed it applied.
* find_reference -> an action with a `wait` block keyed on the reply's
  `ref_id` (gotcha #17), and a `check` that the axis really IS referenced
  afterwards: a search that ended at an end stop is "finished", not "succeeded".
"""

from __future__ import annotations

import json
import zlib

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, stream=None, wait=None, help=""):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract."""
    d = {
        "id": id, "label": label, "kind": kind, "type": type,
        "unit": unit, "group": group, "order": order,
        "writable": (kind == "control") if writable is None else writable,
        "plottable": plottable,
        "read_path": read_path,      # keys into the status dict, or None
    }
    for k, v in (("value", value), ("min", min), ("max", max), ("step", step),
                 ("decimals", decimals), ("options", options), ("scale", scale),
                 ("set", set), ("settle", settle), ("args", args),
                 ("stream", stream), ("wait", wait), ("help", help)):
        if v is not None and v != "":
            d[k] = v
    if danger:
        d["danger"] = True
    return d


def manifest_revision(manifest: dict) -> int:
    """A checksum over the parts of the manifest a client must react to.

    Derived, not hand-bumped. `value` is excluded on purpose: it changes many
    times a second and travels in the status stream anyway.
    """
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` against a status dict (None if absent)."""
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
    """One linear axis: its position, speed, hold time, state, and routines."""
    cfg = brain.cfg
    lim, mot = cfg.limits, cfg.motion
    vlo, vhi = brain.velocity_range()
    ref_note = (" Refused until the axis is referenced (motion.require_reference)."
                if mot.require_reference else "")
    params = [
        # -- position ---------------------------------------------------- #
        _p("position", "Position", "control", "float", unit="mm",
           group="Position", order=10, min=lim.min_mm, max=lim.max_mm,
           step=mot.jog_step_mm, decimals=4, plottable=True,
           read_path=["position_mm"],
           set={"verb": "move_to", "arg": "position"},
           settle={"policy": "adopt_then_flag", "setpoint_key": "target_mm",
                   "flag_key": "moving", "invert": True},
           # A fly scan records the encoder position continuously and bins its
           # detectors by it (stream_start / stream_read / stream_stop).
           stream={"group": "position", "channel": "position"},
           help="Absolute encoder position on the referenced scale; the "
                "readback is the MEASURED position." + ref_note),
        _p("target", "Target", "indicator", "float", unit="mm",
           group="Position", order=11, decimals=4, read_path=["target_mm"]),
        _p("relative", "Relative", "indicator", "float", unit="mm",
           group="Position", order=12, decimals=4, plottable=True,
           read_path=["relative_mm"],
           help="Position measured from the 'zero here' origin."),
        _p("on_target", "On target", "indicator", "bool", group="Position",
           order=13, read_path=["on_target"],
           help=f"Stopped within {mot.on_target_tol_um:g} um of the target."),

        # -- motion ------------------------------------------------------ #
        _p("velocity", "Velocity", "control", "float", unit="mm/s",
           group="Motion", order=20, min=vlo, max=vhi, decimals=3,
           read_path=["velocity_mm_s"],
           set={"verb": "set_velocity", "arg": "value"},
           settle={"policy": "echoes", "key": "velocity_mm_s", "tol": 1e-9},
           help="Nominal travel speed, set as the closed-loop max step "
                "frequency (speed / hardware.um_per_step)."),
        _p("hold_time", "Hold time", "control", "int", unit="ms",
           group="Motion", order=21, min=0, max=60000, step=100,
           read_path=["hold_time_ms"],
           set={"verb": "set_hold_time", "arg": "value"},
           settle={"policy": "echoes", "key": "hold_time_ms"},
           help="How long the controller actively holds a reached target "
                "(0 = let go at once). Applies from the next move."),
        _p("speed", "Measured speed", "indicator", "float", unit="mm/s",
           group="Motion", order=22, decimals=3, plottable=True,
           read_path=["speed_mm_s"]),
        _p("max_frequency", "Step frequency limit", "indicator", "int",
           unit="Hz", group="Motion", order=23, read_path=["max_frequency_hz"]),

        # -- state -------------------------------------------------------- #
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("referenced", "Referenced", "indicator", "bool", group="Status",
           order=2, read_path=["referenced"],
           help="The controller knows the absolute position (reference marks found)."),
        _p("moving", "Moving", "indicator", "bool", group="Status", order=3,
           read_path=["moving"]),
        _p("channel_state", "Controller state", "indicator", "string",
           group="Status", order=4, read_path=["channel_state"]),
        _p("hw_error", "Hardware error", "indicator", "string",
           group="Status", order=5, read_path=["hw_error"]),

        # -- routines ----------------------------------------------------- #
        _p("find_reference", "Find reference", "action", "action",
           group="Routines", order=90, danger=True,
           wait={"target_key": "ref_id",
                 "ready": {"policy": "adopt_then_flag", "setpoint_key": "ref_id",
                           "flag_key": "referencing", "invert": True},
                 "check": {"key": "referenced", "equals": True},
                 # worst case: the whole rail at the slowest allowed speed
                 "timeout_s": round(max(60.0, (lim.max_mm - lim.min_mm) / vlo * 1.5), 1)},
           help="Drives the carriage over two distance-coded reference marks "
                "(a few mm, possibly backwards) so the absolute scale is known."),
        _p("stop", "STOP", "action", "action", group="Routines", order=91,
           wait={"ready": {"policy": "immediate"}},
           help="Halts the carriage where it is. Always safe."),
        _p("set_zero", "Zero here", "action", "action", group="Routines",
           order=92, wait={"ready": {"policy": "immediate"}},
           help="Makes the current position the relative origin. It lives in "
                "the `relative` config group, so a set_config push overwrites it."),
        _p("clear_zero", "Clear zero", "action", "action", group="Routines",
           order=93, wait={"ready": {"policy": "immediate"}}),
        _p("move_by", "Step", "action", "action", group="Routines", order=94,
           args=[{"name": "delta", "label": "Step", "type": "float", "unit": "mm",
                  "default": mot.jog_step_mm,
                  "min": -(lim.max_mm - lim.min_mm), "max": lim.max_mm - lim.min_mm}],
           help="Relative move from the current target. Works before "
                f"referencing, limited to +-{mot.max_unreferenced_step_mm:g} mm then."),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "smaract",
                "label": "SmarAct linear stage", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
