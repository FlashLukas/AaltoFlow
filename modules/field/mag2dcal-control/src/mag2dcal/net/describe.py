"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" generically enough that a
client can build a control panel -- or scan-core a registry -- for a module it
has never heard of (INSTRUMENT_MODULE_GUIDE.md section 6b).

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every bound and every timeout is LOOKED UP when the manifest is
built. Here that matters more than in mag2d-control, because the field bounds
come from the MEASURED CALIBRATION: loading or running one changes what the
magnet can promise, the min/max follow with no second edit, and `revision`
changes so every client re-fetches.

THE IDS ARE THE SAME AS mag2d-control's. This module is a drop-in alternative to
it -- same verbs, same status keys, same descriptor ids -- so a saved scan recipe
or a vna-control field source works against either. Renaming one breaks that.
What is new is additive: the `stabilizer` control, the `calibrate` action and
three indicators.

SETTLING. Every field-like control uses adopt_then_flag: first the status must
show the commanded setpoint (the service stores it exactly as sent), then
field_stable must be True. The controller resets field_stable in the same
critical section that stores a new setpoint, so the pair cannot be satisfied by
a status frame left over from the previous point.
"""

from __future__ import annotations

import json
import zlib

from .protocol import STATE_VALUES

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
    """The full manifest, with every bound read live from cfg / the calibration."""
    cfg = ctrl.cfg
    lim = cfg.limits
    c = cfg.control
    # NOT abs(lim.field_max_mT): the envelope is the narrower of the configured
    # limit and the measured calibration (Controller.field_envelope_mT).
    fmax = ctrl.field_envelope_mT()
    calibrated = ctrl.is_calibrated
    settle_t = c.settle_timeout_s
    # Switching the output off waits for the ramp: worst case full scale.
    ramp_t = abs(lim.ao_limit_V) / max(c.slew_V_per_s, 1e-3) + 10.0

    how = ("The setpoint is reached by a jump onto the measured calibration curve "
           "(on the hysteresis leg matching the approach), a short one-way PI "
           "trim, and then a FROZEN output."
           if calibrated else
           "NO CALIBRATION LOADED: the jump uses the straight line "
           f"B / {c.ff_mT_per_V:g} mT per volt. Run the `calibrate` action, or "
           "load a saved curve, before trusting these limits.")

    params = [
        # ---- controls ------------------------------------------------------
        _p("field", "Field magnitude", "control", "float", unit="mT",
           group="Field", order=10, min=-fmax, max=fmax, step=1.0, decimals=3,
           plottable=True, read_path=["measured_field_mT"],
           set={"verb": "set_field", "arg": "field_mT"},
           settle=_stable_after("setpoint_field_mT"), timeout_s=settle_t,
           help="Signed magnitude along the setpoint angle (the angle is kept). "
                "The read-back is the measured component along that direction. "
                + how),
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
           help="On: the seek drives the coils. Off: ramp to 0 V at the slew "
                "rate, then release the enable line."),
        _p("stabilizer", "Long-term stabilizer", "control", "bool",
           group="Field", order=55, read_path=["stabilizer"],
           set={"verb": "set_stabilizer", "arg": "enabled"},
           settle={"policy": "echoes", "key": "stabilizer"},
           help="A slow deadbanded trim that corrects drift while the field is "
                "held. It is the ONLY thing allowed to move a frozen output, at "
                f"most once every {cfg.stabilizer.period_s:g} s."),
        _p("water_bypass", "Bypass water interlock", "control", "bool",
           group="Interlock", order=60, read_path=["water_bypass"],
           set={"verb": "set_water_bypass", "arg": "enabled"},
           settle={"policy": "echoes", "key": "water_bypass"}, danger=True,
           help="Run without the cooling-water flow switch. The coils are then "
                "NOT protected against overheating."),

        # ---- indicators ----------------------------------------------------
        _p("state", "State", "indicator", "string", group="Status", order=1,
           read_path=["state"], options=list(STATE_VALUES)),
        _p("field_stable", "Field stable", "indicator", "bool", group="Status",
           order=2, read_path=["field_stable"]),
        _p("frozen", "Output frozen", "indicator", "bool", group="Status",
           order=4, read_path=["frozen"],
           help="Both axes are inside tolerance/2 and the drive is being held "
                "exactly still -- the state that stops hysteresis dither."),
        _p("calibrated", "Calibration loaded", "indicator", "bool",
           group="Calibration", order=230, read_path=["calibrated"]),
        _p("calibration_progress", "Calibration progress", "indicator", "float",
           group="Calibration", order=240, decimals=2, read_path=["calibration_progress"],
           min=0.0, max=1.0, help="0 .. 1 while a calibration sweep is running."),
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
        # (scan-core only picks up actions that declare how to wait for them.)
        _p("zero", "Zero field", "action", "action", group="Field", order=5,
           help="Field setpoint 0 mT, angle kept. Also aborts a calibration."),
        _p("calibrate", "Run calibration", "action", "action",
           group="Calibration", order=250, danger=True,
           args=[{"name": "n_per_leg", "label": "points per leg", "type": "int",
                  "default": cfg.calibration.n_per_leg, "min": 2, "max": 401},
                 {"name": "dwell_s", "label": "dwell", "type": "float", "unit": "s",
                  "default": cfg.calibration.dwell_s, "min": 0.0, "max": 60.0},
                 {"name": "v_max", "label": "sweep to", "type": "float", "unit": "V",
                  "default": cfg.calibration.v_max_V, "min": 0.1,
                  "max": abs(lim.ao_limit_V)}],
           help="Sweeps each axis over its full range (the other axis at 0 V) "
                "and measures both hysteresis legs. The magnet goes to full "
                "field while this runs. Abort with `zero`."),
        _p("clear_fault", "Clear fault", "action", "action", group="Interlock",
           order=220,
           help="Refused while the cause is still present. The output stays off."),
    ]

    manifest = {
        "schema": SCHEMA_VERSION,
        "module": "mag2dcal",
        "label": "2D vector magnet (calibrated)",
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
