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

Limits here are the config's safety envelope; `revision` travels in every
status frame as `describe_rev`, so a client notices an edited envelope (or a
changed ramp rate, which moves the settle timeouts) without polling the whole
manifest.
"""

from __future__ import annotations

import json
import zlib

from ..config import FIELD_APPROACHES, TEMPERATURE_APPROACHES

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, timeout_s=None, args=None, danger=False, help=""):
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
                 ("args", args),
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


def settle_timeouts(cfg) -> tuple[float, float]:
    """How long a scan may wait for a field / temperature point, in seconds.

    scan-core's default is 60 s, which a superconducting magnet cannot meet: a
    full -9 T -> +9 T sweep at 22 mT/s is ~14 minutes. So the timeout is DERIVED
    from what the point can cost at the current rate -- the whole range, with a
    50 % margin, plus the hold time and a fixed allowance -- never typed in.
    Temperature gets a generous fixed allowance on top, because the last kelvin
    near base temperature settles far slower than the sweep rate suggests."""
    lim, f, t = cfg.limits, cfg.field, cfg.temperature
    rate_f = max(float(f.rate_mT_per_s), 1e-6)
    field_s = 1.5 * (2.0 * lim.field_max_mT) / rate_f + f.stable_time_s + 120.0
    rate_t = max(float(t.rate_K_per_min), 1e-6) / 60.0
    temp_s = (1.5 * (lim.temperature_max_K - lim.temperature_min_K) / rate_t
              + t.stable_time_s + 1800.0)
    return round(field_s), round(temp_s)


def build_manifest(cryo) -> dict:
    """The DynaCool: two closed-loop setpoints (MultiVu runs the loops) plus
    settings and readbacks.

    Field and temperature settle with `adopt_then_flag`, the same rule as
    clMag: the status must first show OUR setpoint (so a frame from the
    previous point cannot pass), and only then is the stable flag believed. The
    brain additionally clears the flag in the same critical section in which
    it stores the setpoint, so the two can never be seen out of step.

    The approach modes settle `immediate`: the setter has finished before the
    reply is sent, and `echoes` compares numbers, not names.
    """
    cfg = cryo.cfg
    lim, f, t = cfg.limits, cfg.field, cfg.temperature
    field_t, temp_t = settle_timeouts(cfg)
    params = [
        # ---- field ---------------------------------------------------------
        _p("field", "Magnetic field", "control", "float", unit="mT", group="Field",
           order=10, decimals=2, plottable=True,
           min=-lim.field_max_mT, max=lim.field_max_mT,
           read_path=["setpoint_field_mT"],
           set={"verb": "set_field", "arg": "field_mT"},
           settle={"policy": "adopt_then_flag", "setpoint_key": "setpoint_field_mT",
                   "flag_key": "field_stable"},
           timeout_s=field_t,
           help=f"Setpoint. Reached = within {f.tolerance_mT:g} mT and MultiVu holding, "
                f"for {f.stable_time_s:g} s. Ramps at the field rate."),
        _p("field_rate", "Field rate", "control", "float", unit="mT/s", group="Field",
           order=20, decimals=2, min=lim.field_rate_min_mT_per_s,
           max=lim.field_rate_max_mT_per_s, read_path=["field_rate_mT_per_s"],
           set={"verb": "set_field_rate", "arg": "rate_mT_per_s"},
           settle={"policy": "echoes", "key": "field_rate_mT_per_s", "tol": 1e-6},
           help="Applies from the next field setpoint."),
        _p("field_approach", "Field approach", "control", "enum", group="Field",
           order=30, options=list(FIELD_APPROACHES), read_path=["field_approach"],
           set={"verb": "set_field_approach", "arg": "approach"},
           settle={"policy": "immediate"},
           help="linear is fastest; oscillate demagnetises on the way in. "
                "Applies from the next field setpoint."),
        _p("measured_field", "Measured field", "indicator", "float", unit="mT",
           group="Field", order=40, decimals=2, plottable=True,
           read_path=["measured_field_mT"]),
        _p("field_error", "Field error", "indicator", "float", unit="mT",
           group="Field", order=50, decimals=3, plottable=True,
           read_path=["field_error_mT"]),
        _p("field_status", "Magnet status", "indicator", "string", group="Field",
           order=60, read_path=["field_status"]),
        _p("field_stable", "Field reached", "indicator", "bool", group="Field",
           order=70, read_path=["field_stable"]),

        # ---- temperature ---------------------------------------------------
        _p("temperature", "Temperature", "control", "float", unit="K",
           group="Temperature", order=10, decimals=3, plottable=True,
           min=lim.temperature_min_K, max=lim.temperature_max_K,
           read_path=["setpoint_temperature_K"],
           set={"verb": "set_temperature", "arg": "temperature_K"},
           settle={"policy": "adopt_then_flag", "setpoint_key": "setpoint_temperature_K",
                   "flag_key": "temperature_stable"},
           timeout_s=temp_t,
           help=f"Setpoint. Reached = within {t.tolerance_K:g} K and MultiVu Stable, "
                f"for {t.stable_time_s:g} s."),
        _p("temperature_rate", "Temperature rate", "control", "float", unit="K/min",
           group="Temperature", order=20, decimals=2,
           min=lim.temperature_rate_min_K_per_min, max=lim.temperature_rate_max_K_per_min,
           read_path=["temperature_rate_K_per_min"],
           set={"verb": "set_temperature_rate", "arg": "rate_K_per_min"},
           settle={"policy": "echoes", "key": "temperature_rate_K_per_min", "tol": 1e-6},
           help="Applies from the next temperature setpoint."),
        _p("temperature_approach", "Temperature approach", "control", "enum",
           group="Temperature", order=30, options=list(TEMPERATURE_APPROACHES),
           read_path=["temperature_approach"],
           set={"verb": "set_temperature_approach", "arg": "approach"},
           settle={"policy": "immediate"},
           help="Applies from the next temperature setpoint."),
        _p("measured_temperature", "Measured temperature", "indicator", "float",
           unit="K", group="Temperature", order=40, decimals=3, plottable=True,
           read_path=["temperature_K"]),
        _p("temperature_error", "Temperature error", "indicator", "float", unit="K",
           group="Temperature", order=50, decimals=3, plottable=True,
           read_path=["temperature_error_K"]),
        _p("temperature_status", "Temperature status", "indicator", "string",
           group="Temperature", order=60, read_path=["temperature_status"]),
        _p("temperature_stable", "Temperature reached", "indicator", "bool",
           group="Temperature", order=70, read_path=["temperature_stable"]),

        # ---- system --------------------------------------------------------
        _p("chamber", "Chamber", "indicator", "string", group="Status", order=1,
           read_path=["chamber"]),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=2, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=3,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=4, read_path=["hw_error"]),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "ppms",
                "label": "PPMS DynaCool", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
