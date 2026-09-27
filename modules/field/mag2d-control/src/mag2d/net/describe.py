"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" generically enough that a
client can build a control panel -- or scan-core a registry -- for a module it
has never heard of (INSTRUMENT_MODULE_GUIDE.md section 6b).

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every bound and every timeout is LOOKED UP from cfg when the
manifest is built. Change limits.field_max_mT and the field, Bx and By sliders
follow with no second edit, and `revision` changes so clients know to re-fetch.

The ids, verbs and settle blocks below are a CONTRACT with vna-control and
scan-core (the VNA-FMR split, 2026-09-16). Renaming one breaks saved recipes.

SETTLING. Every field-like control uses adopt_then_flag: first the status must
show the commanded setpoint (the service stores it exactly as sent), then
field_stable must be True. The controller resets field_stable in the same
critical section that stores a new setpoint, so the pair cannot be satisfied by
a status frame left over from the previous point.
"""

from __future__ import annotations

import json
import zlib

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, set=None, settle=None,
       timeout_s=None, args=None, danger=False, help=""):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract."""
    d = {
        "id": id, "label": label, "kind": kind, "type": type,
        "unit": unit, "group": group, "order": order,
        "writable": (kind == "control") if writable is None else writable,
        "plottable": plottable,
        "read_path": read_path,      # list of keys (and list indices) into status
    }
    for k, v in (("value", value), ("min", min), ("max", max), ("step", step),
                 ("decimals", decimals), ("options", options), ("set", set),
                 ("settle", settle), ("timeout_s", timeout_s), ("args", args),
                 ("help", help)):
        if v is not None and v != "":
            d[k] = v
    if danger:
        d["danger"] = True
    return d


def _stable_after(setpoint_key: str) -> dict:
    return {"policy": "adopt_then_flag", "setpoint_key": setpoint_key,
            "flag_key": "field_stable"}


def build_manifest(ctrl) -> dict:
    """The full manifest, with every bound read live from cfg."""
    cfg = ctrl.cfg
    lim = cfg.limits
    c = cfg.control
    fmax = abs(lim.field_max_mT)
    settle_t = c.settle_timeout_s
    # Switching the output off waits for the ramp: worst case full scale.
    ramp_t = abs(lim.ao_limit_V) / max(c.slew_V_per_s, 1e-3) + 10.0

    params = [
        # ---- controls ------------------------------------------------------
        _p("field", "Field magnitude", "control", "float", unit="mT",
           group="Field", order=10, min=-fmax, max=fmax, step=1.0, decimals=3,
           plottable=True, read_path=["measured_field_mT"],
           set={"verb": "set_field", "arg": "field_mT"},
           settle=_stable_after("setpoint_field_mT"), timeout_s=settle_t,
           help="Signed magnitude along the setpoint angle (the angle is kept). "
                "The read-back is the measured component along that direction."),
        _p("angle", "Field angle", "control", "float", unit="deg",
           group="Field", order=20, min=lim.angle_min_deg, max=lim.angle_max_deg,
           step=1.0, decimals=2, plottable=True, read_path=["measured_angle_deg"],
           set={"verb": "set_angle", "arg": "angle_deg"},
           settle=_stable_after("setpoint_angle_deg"), timeout_s=settle_t,
           help="Rotates the field, keeping its magnitude. 0 deg = +X, 90 deg = +Y."),
        _p("bx", "Field X", "control", "float", unit="mT",
           group="Vector", order=30, min=-fmax, max=fmax, step=1.0, decimals=3,
           plottable=True, read_path=["measured_bx_mT"],
           set={"verb": "set_bx", "arg": "bx_mT"},
           settle=_stable_after("setpoint_bx_mT"), timeout_s=settle_t,
           help="Sets Bx and keeps the By setpoint; magnitude and angle then "
                "follow as hypot / atan2."),
        _p("by", "Field Y", "control", "float", unit="mT",
           group="Vector", order=40, min=-fmax, max=fmax, step=1.0, decimals=3,
           plottable=True, read_path=["measured_by_mT"],
           set={"verb": "set_by", "arg": "by_mT"},
           settle=_stable_after("setpoint_by_mT"), timeout_s=settle_t,
           help="Sets By and keeps the Bx setpoint."),
        _p("output", "Output energized", "control", "bool",
           group="Output", order=50, read_path=["energized"],
           set={"verb": "set_output", "arg": "enabled"},
           settle={"policy": "echoes", "key": "energized"}, timeout_s=ramp_t,
           help="On: the PI loop drives the coils. Off: ramp to 0 V at the slew "
                "rate, then release the enable line."),
        _p("water_bypass", "Bypass water interlock", "control", "bool",
           group="Interlock", order=60, read_path=["water_bypass"],
           set={"verb": "set_water_bypass", "arg": "enabled"},
           settle={"policy": "echoes", "key": "water_bypass"}, danger=True,
           help="Run without the cooling-water flow switch. The coils are then "
                "NOT protected against overheating."),

        # ---- indicators ----------------------------------------------------
        _p("state", "State", "indicator", "string", group="Status", order=1,
           read_path=["state"],
           options=["OFF", "REGULATING", "STABLE", "RAMP_DOWN", "FAULT"]),
        _p("field_stable", "Field stable", "indicator", "bool", group="Status",
           order=2, read_path=["field_stable"]),
        _p("measured_bx", "Measured Bx", "indicator", "float", unit="mT",
           group="Measured", order=100, decimals=3, plottable=True,
           read_path=["measured_bx_mT"]),
        _p("measured_by", "Measured By", "indicator", "float", unit="mT",
           group="Measured", order=110, decimals=3, plottable=True,
           read_path=["measured_by_mT"]),
        _p("measured_magnitude", "Measured |B|", "indicator", "float", unit="mT",
           group="Measured", order=120, decimals=3, plottable=True,
           read_path=["measured_magnitude_mT"]),
        _p("measured_angle", "Measured angle", "indicator", "float", unit="deg",
           group="Measured", order=130, decimals=2, plottable=True,
           read_path=["measured_angle_deg"]),
        _p("error", "Vector error", "indicator", "float", unit="mT",
           group="Measured", order=140, decimals=3, plottable=True,
           read_path=["error_mT"],
           help="|setpoint - measured| as a vector."),
        _p("output_x", "Drive X", "indicator", "float", unit="V",
           group="Output", order=150, decimals=3, plottable=True,
           read_path=["output_V", 0]),
        _p("output_y", "Drive Y", "indicator", "float", unit="V",
           group="Output", order=160, decimals=3, plottable=True,
           read_path=["output_V", 1]),
        _p("hall_x", "Hall X voltage", "indicator", "float", unit="V",
           group="Raw", order=170, decimals=4, read_path=["hall_V", 0]),
        _p("hall_y", "Hall Y voltage", "indicator", "float", unit="V",
           group="Raw", order=180, decimals=4, read_path=["hall_V", 1]),
        _p("temp1", "Temperature 1", "indicator", "float", unit="C",
           group="Interlock", order=190, decimals=1, plottable=True,
           read_path=["temp_C", 0]),
        _p("temp2", "Temperature 2", "indicator", "float", unit="C",
           group="Interlock", order=200, decimals=1, plottable=True,
           read_path=["temp_C", 1]),
        _p("water_ok", "Cooling water", "indicator", "bool",
           group="Interlock", order=210, read_path=["water_ok"]),
        _p("fault", "Fault", "indicator", "string", group="Status", order=3,
           read_path=["fault"]),

        # ---- actions -------------------------------------------------------
        # No `wait` block: they are control-panel buttons, not scan routines.
        _p("zero", "Zero field", "action", "action", group="Field", order=5,
           help="Field setpoint 0 mT, angle kept. The loop keeps regulating."),
        _p("clear_fault", "Clear fault", "action", "action", group="Interlock",
           order=220,
           help="Refused while the cause is still present. The output stays off."),
    ]

    manifest = {
        "schema": SCHEMA_VERSION,
        "module": "mag2d",
        "label": "2D vector magnet",
        "parameters": params,
    }
    manifest["revision"] = manifest_revision(manifest)
    return manifest


def manifest_revision(manifest: dict) -> int:
    """A checksum over the parts of the manifest a client must react to.

    Derived, not hand-bumped. `value` is excluded on purpose -- it changes many
    times a second and is delivered by the status stream anyway.
    """
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` (keys, and ints for list entries)."""
    if not path:
        return None
    cur = status
    for key in path:
        if isinstance(key, int) and isinstance(cur, (list, tuple)):
            if not -len(cur) <= key < len(cur):
                return None
            cur = cur[key]
        elif isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur
