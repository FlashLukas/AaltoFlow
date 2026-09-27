"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of.
Two consumers, two projections of one source: the reconfigurable control screen
reads this manifest DIRECTLY, scan-core projects it into Settables / Gettables /
Actions. Full contract: INSTRUMENT_MODULE_GUIDE.md section 6b.

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every bound is looked up from cfg (or the brain's constants)
when the manifest is built.

What is DYNAMIC here: the angle's range depends on the wrap policy. In literal
mode it is the configured travel box [min_deg, max_deg]; in the modulo-360
modes it is [0, 360] (any number is accepted and reduced, but a scan should
sweep one turn). Switching the policy therefore changes `revision`, and every
status frame carries it as `describe_rev`, so clients re-fetch.

How a scan knows the stage has ARRIVED: `adopt_then_flag` on the echoed
target. `target_deg` is the angle exactly as commanded, and the brain sets it
in the same critical section as its "move pending" latch, so "adopted and not
moving" cannot be read off a frame from before the move (gotchas #2, #28).
"""

from __future__ import annotations

import json
import math
import zlib

from ..config import WRAP_POLICIES, wrap_policy
from ..rotator import MIN_ACCELERATION, MIN_VELOCITY

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
    """A CRC over the manifest without its `value` fields (derived, never
    hand-bumped; `value` changes every frame and would mean nothing)."""
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


def _timeout(span_deg: float, velocity: float) -> float:
    """A generous wait for a move of `span_deg` at `velocity`, rounded UP to
    30 s so a small velocity change does not change `revision` every time."""
    v = velocity if (velocity and math.isfinite(velocity) and velocity > 0) else MIN_VELOCITY
    raw = 1.5 * abs(span_deg) / v + 10.0
    return float(math.ceil(raw / 30.0) * 30.0)


def angle_bounds(cfg) -> tuple[float, float]:
    """The range a client should offer for `angle`, by the wrap policy."""
    if wrap_policy(cfg) == "literal":
        return float(cfg.limits.min_deg), float(cfg.limits.max_deg)
    return 0.0, 360.0


def build_manifest(brain) -> dict:
    cfg = brain.cfg
    lim = cfg.limits
    policy = wrap_policy(cfg)
    lo, hi = angle_bounds(cfg)
    move_settle = {"policy": "adopt_then_flag", "setpoint_key": "target_deg",
                   "flag_key": "moving", "invert": True}
    # The move ACTIONS wait on the move NUMBER, not on the target angle: in a
    # modulo mode `move_by 360` keeps the same target number, and a frame from
    # before the command would pass for the arrival (see rotator.py).
    move_done = {"policy": "adopt_then_flag", "setpoint_key": "move_id",
                 "flag_key": "moving", "invert": True}
    move_timeout = _timeout(hi - lo, cfg.motion.velocity)

    params = [
        # -- position ------------------------------------------------------ #
        _p("angle", "Angle", "control", "float", unit="deg", group="Position",
           order=10, min=lo, max=hi, step=cfg.motion.jog_step, decimals=4,
           plottable=True, read_path=["angle_deg"],
           set={"verb": "move_to", "arg": "angle"},
           settle=move_settle,
           # a fly scan records the ENCODER angle continuously and bins its
           # detectors by it (stream_start / _read / _stop)
           stream={"group": "position", "channel": "angle"},
           help=(f"Wrap policy '{policy}'. " +
                 ("A linear coordinate inside the travel box; 350 -> 10 turns "
                  "back 340 deg." if policy == "literal" else
                  "Taken modulo 360; the fly-scan angle jumps at the 0/360 seam, "
                  "so fly across it only in literal mode.") +
                 " Refused until the stage is homed.")),
        _p("raw", "Controller position", "indicator", "float", unit="deg",
           group="Position", order=20, decimals=4, plottable=True,
           read_path=["raw_deg"],
           help="The K-Cube's own count, continuous over turns; 0 = encoder index."),
        _p("target", "Target", "indicator", "float", unit="deg", group="Position",
           order=30, decimals=4, read_path=["target_deg"]),
        _p("zero", "Display zero", "indicator", "float", unit="deg",
           group="Position", order=40, decimals=4, read_path=["zero_deg"]),

        # -- motion profile ------------------------------------------------ #
        _p("velocity", "Velocity", "control", "float", unit="deg/s",
           group="Motion", order=10, min=MIN_VELOCITY, max=lim.max_velocity,
           decimals=3, read_path=["velocity"],
           set={"verb": "set_velocity", "arg": "value"},
           # the brain reads the value back from the controller before replying
           settle={"policy": "echoes", "key": "velocity", "tol": 1e-3}),
        _p("acceleration", "Acceleration", "control", "float", unit="deg/s^2",
           group="Motion", order=20, min=MIN_ACCELERATION, max=lim.max_acceleration,
           decimals=2, read_path=["acceleration"],
           set={"verb": "set_acceleration", "arg": "value"},
           # tol 0.5: the K-Cube stores acceleration as an integer whose unit
           # is ~0.36 deg/s^2 with the DDR25 scale (pylablib's KBD101 time
           # base), so the value read back is rounded to that grid -- a
           # tighter tolerance would never be met on the real controller.
           settle={"policy": "echoes", "key": "acceleration", "tol": 0.5}),
        _p("wrap", "Wrap policy", "control", "enum", group="Motion", order=30,
           options=list(WRAP_POLICIES), read_path=["wrap"],
           set={"verb": "set_wrap", "arg": "wrap"},
           settle={"policy": "immediate"},
           help="How an absolute angle becomes a controller position: literal "
                "(linear), shortest, positive or negative (modulo 360)."),

        # -- status -------------------------------------------------------- #
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("moving", "Moving", "indicator", "bool", group="Status", order=2,
           read_path=["moving"]),
        _p("homed", "Homed", "indicator", "bool", group="Status", order=3,
           read_path=["homed"]),
        _p("homing", "Homing", "indicator", "bool", group="Status", order=4,
           read_path=["homing"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=5, read_path=["hw_error"]),

        # -- actions (id = verb) ------------------------------------------- #
        _p("home", "Home", "action", "action", group="Routines", order=10,
           danger=True,
           wait={"target_key": "home_id",
                 "ready": {"policy": "adopt_then_flag", "setpoint_key": "home_id",
                           "flag_key": "homing", "invert": True},
                 "check": {"key": "homed", "equals": True},
                 "timeout_s": _timeout(360.0, cfg.motion.velocity)},
           help="Turns (up to a full revolution) to the encoder index and "
                "makes it controller 0. Needed once after power-up before any "
                "absolute move."),
        _p("stop", "STOP", "action", "action", group="Routines", order=1,
           danger=True,
           args=[{"name": "immediate", "label": "Immediate", "type": "bool",
                  "default": False}],
           help="Decelerates to a stop (immediate: halts on the spot). The "
                "target is forgotten."),
        _p("move_by", "Move by", "action", "action", group="Routines", order=20,
           args=[{"name": "delta", "label": "Step", "type": "float", "unit": "deg",
                  "default": cfg.motion.jog_step}],
           wait={"target_key": "move_id", "ready": move_done,
                 "timeout_s": move_timeout},
           help="Relative move; allowed before homing."),
        _p("goto_angle", "Go to stored angle", "action", "action", group="Routines",
           order=30,
           args=[{"name": "slot", "label": "Slot", "type": "int", "default": 0,
                  "min": 0, "max": len(brain.angles.slots) - 1}],
           wait={"target_key": "move_id", "ready": move_done,
                 "timeout_s": move_timeout}),
        _p("set_zero", "Zero here", "action", "action", group="Routines", order=40,
           wait={"ready": {"policy": "immediate"}},
           help="The current orientation reads 0 deg from now on. Lives in the "
                "`frame` config group, so a set_config push of that group "
                "overwrites it."),
        _p("clear_zero", "Clear zero", "action", "action", group="Routines",
           order=41, wait={"ready": {"policy": "immediate"}}),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "ddr25",
                "label": "DDR25 rotation stage", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
