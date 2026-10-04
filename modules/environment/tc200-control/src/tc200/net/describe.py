"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of.

Two consumers, two projections of one source: the reconfigurable control screen
reads this manifest DIRECTLY (it wants buttons, their arguments, their danger
flags and the group/order layout hints), while scan-core projects it into
Settables and Gettables. Full contract: INSTRUMENT_MODULE_GUIDE.md section 6b.

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every limit is looked up from cfg or from the brain when the
manifest is built, never copied into a literal.

The setpoint ceiling is DYNAMIC: it is min(our limit, TMAX - margin, 200 C)
and TMAX lives in the box (it can be changed on the front panel). `revision`
travels in every status frame as `describe_rev`, so a client notices a moved
ceiling without polling the whole manifest.
"""

from __future__ import annotations

import json
import zlib

from ..config import (D_GAIN_RANGE, I_GAIN_RANGE, P_GAIN_RANGE, PMAX_MIN_W, SENSORS,
                      TMAX_MAX_C, TMAX_MIN_C)

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, timeout_s=None, args=None, wait=None, danger=False, help=""):
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
                 ("set", set), ("settle", settle), ("timeout_s", timeout_s),
                 ("args", args), ("wait", wait),
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
    it changes many times a second and travels in the status stream anyway.
    """
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` against a status dict (a list of keys,
    not a dotted string, so ids with dots stay unambiguous)."""
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


def settle_timeout(cfg, t_max_C: float) -> int:
    """How long a scan may wait for one temperature point, in seconds.

    scan-core's default is 60 s; a heated block can need far longer, above all
    when COOLING, which a heater cannot do: the block only drifts towards the
    room, slower and slower. So the timeout is DERIVED -- the whole setpoint
    range at the configured pessimistic rate, with a 50 % margin, plus the hold
    time and a fixed allowance for the final approach -- never typed in."""
    t = cfg.temperature
    rate = max(float(t.slowest_rate_C_per_min), 1e-3) / 60.0
    span = max(0.0, t_max_C - float(cfg.limits.temperature_min_C))
    return round(1.5 * span / rate + float(t.stable_time_s) + 300.0)


def build_manifest(heater) -> dict:
    """The TC200: one closed-loop setpoint (the box runs the loop), the output
    switch, the box's stored settings, and readbacks.

    The temperature settles with `adopt_then_flag`, the same rule as clMag's
    field: the status must first show OUR setpoint (so a frame from the
    previous point cannot pass), and only then is `temperature_stable`
    believed. The brain clears the flag in the same critical section in which
    it stores the setpoint, so the two can never be seen out of step.
    """
    cfg = heater.cfg
    lim, t = cfg.limits, cfg.temperature
    t_lo = float(lim.temperature_min_C)
    t_hi = heater.temperature_max()          # LIVE: follows the box's TMAX
    pmax_hi = max(PMAX_MIN_W, float(lim.pmax_max_W))
    params = [
        # ---- temperature -----------------------------------------------------
        _p("temperature", "Temperature setpoint", "control", "float", unit="C",
           group="Temperature", order=10, decimals=1, step=0.1, plottable=True,
           min=t_lo, max=t_hi, read_path=["setpoint_C"],
           set={"verb": "set_temperature", "arg": "temperature_C"},
           settle={"policy": "adopt_then_flag", "setpoint_key": "setpoint_C",
                   "flag_key": "temperature_stable"},
           timeout_s=settle_timeout(cfg, t_hi),
           help=f"Reached = heater on and within {t.tolerance_C:g} C for "
                f"{t.stable_time_s:g} s. Enable the heater first: the setpoint "
                "alone heats nothing. A heater cannot cool -- going down is slow."),
        _p("enabled", "Heater output", "control", "bool", group="Temperature",
           order=20, read_path=["enabled"],
           set={"verb": "set_enabled", "arg": "enabled"},
           settle={"policy": "echoes", "key": "enabled"},
           help="Refused while the box's sensor setting does not match the sensor "
                "wired to it, on a sensor alarm, or in CYCLE mode."),
        _p("measured_temperature", "Temperature", "indicator", "float", unit="C",
           group="Temperature", order=30, decimals=2, plottable=True,
           read_path=["temperature_C"]),
        _p("temperature_error", "Temperature error", "indicator", "float", unit="C",
           group="Temperature", order=40, decimals=2, plottable=True,
           read_path=["temperature_error_C"]),
        _p("temperature_stable", "Temperature reached", "indicator", "bool",
           group="Temperature", order=50, read_path=["temperature_stable"]),
        _p("heater_off", "Heater off", "action", "action", group="Temperature",
           order=60, wait={"ready": {"policy": "immediate"}},
           help="Switch the output off (a safe end to a scan: use it in an "
                "after-scan routine)."),

        # ---- settings stored in the box -----------------------------------------
        _p("p_gain", "P gain", "control", "int", group="Controller", order=10,
           min=P_GAIN_RANGE[0], max=P_GAIN_RANGE[1], step=1, read_path=["p_gain"],
           set={"verb": "set_p_gain", "arg": "p"},
           settle={"policy": "echoes", "key": "p_gain"},
           help="Unitless, as on the front panel. Changing a gain drops a TUNE offset."),
        _p("i_gain", "I gain", "control", "int", group="Controller", order=20,
           min=I_GAIN_RANGE[0], max=I_GAIN_RANGE[1], step=1, read_path=["i_gain"],
           set={"verb": "set_i_gain", "arg": "i"},
           settle={"policy": "echoes", "key": "i_gain"},
           help="Start low (< 10): too much I overshoots and rings."),
        _p("d_gain", "D gain", "control", "int", group="Controller", order=30,
           min=D_GAIN_RANGE[0], max=D_GAIN_RANGE[1], step=1, read_path=["d_gain"],
           set={"verb": "set_d_gain", "arg": "d"},
           settle={"policy": "echoes", "key": "d_gain"}),
        _p("pmax", "Power limit (PMAX)", "control", "float", unit="W",
           group="Controller", order=40, decimals=1, step=0.1,
           min=PMAX_MIN_W, max=pmax_hi, read_path=["pmax_W"],
           set={"verb": "set_pmax", "arg": "pmax_W"},
           settle={"policy": "echoes", "key": "pmax_W", "tol": 0.051},
           help="Set it to the heater's rating."),
        _p("tmax", "Trip temperature (TMAX)", "control", "float", unit="C",
           group="Controller", order=50, decimals=1, step=0.1,
           min=TMAX_MIN_C, max=TMAX_MAX_C, read_path=["tmax_C"],
           set={"verb": "set_tmax", "arg": "tmax_C"},
           settle={"policy": "echoes", "key": "tmax_C", "tol": 0.051},
           danger=True,
           help=f"The box's own over-temperature trip. The setpoint stays "
                f"{lim.tmax_margin_C:g} C below it. Raising it removes a safety net."),
        _p("sensor", "Sensor type", "control", "enum", group="Controller", order=60,
           options=list(SENSORS), read_path=["sensor"],
           set={"verb": "set_sensor", "arg": "sensor"},
           settle={"policy": "immediate"}, danger=True,
           help="Must match the sensor really wired (a PT100 here). A wrong type "
                "makes the controller misread the temperature. Refused while heating."),

        # ---- status ------------------------------------------------------------------
        _p("sensor_ok", "Sensor setting matches", "indicator", "bool", group="Status",
           order=1, read_path=["sensor_ok"]),
        _p("sensor_alarm", "Sensor alarm", "indicator", "bool", group="Status",
           order=2, read_path=["sensor_alarm"]),
        _p("tmax_alarm", "TMAX alarm", "indicator", "bool", group="Status",
           order=3, read_path=["tmax_alarm"]),
        # An ENUM (scan-core stores it as a code + the names, developer notes
        # 4b): heater.status() reports exactly these two, from the box's
        # cycle-mode bit.
        _p("mode", "Mode", "indicator", "enum", group="Status", order=4,
           options=["normal", "cycle"], read_path=["mode"]),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=5, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=6,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=7, read_path=["hw_error"]),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "tc200",
                "label": "TC200 heater", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
