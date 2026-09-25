"""describe.py -- the power meter's self-description: what can be shown and driven.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. The rule that keeps it honest:
nothing here restates a value that lives somewhere else. Every bound is looked
up from the meter when the manifest is built -- and on the real PM16 those
bounds come from the DEVICE (wavelength range of the head, the meter's own
range steps), narrowed by the config envelope.

What is dynamic:
  * `range` is a control on manual range and an indicator on auto range (same
    id both ways). Toggling auto-range changes `revision`, and every status
    frame carries it as `describe_rev`, so clients re-fetch.

Power is offered in mW (scale 1e-3), commanded and published in W, because a
column of 3.3e-06 in a data file is hard to read and "0.00335 mW" is not.

The scan detectors (`power`, `power_std`) read the LATCHED sample and carry an
`acquire` block: trigger `acquire`, then wait until status shows that
acquisition's id with `acquiring` false. `live_power` is for panels only.
"""

from __future__ import annotations

import json
import math
import zlib

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, acquire=None, help=""):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract."""
    d = {
        "id": id, "label": label, "kind": kind, "type": type,
        "unit": unit, "group": group, "order": order,
        "writable": (kind == "control") if writable is None else writable,
        "plottable": plottable,
        "read_path": read_path,
    }
    for k, v in (("value", value), ("min", min), ("max", max), ("step", step),
                 ("decimals", decimals), ("options", options), ("scale", scale),
                 ("set", set), ("settle", settle), ("args", args),
                 ("acquire", acquire), ("help", help)):
        if v is not None and v != "":
            d[k] = v
    if danger:
        d["danger"] = True
    return d


def manifest_revision(manifest: dict) -> int:
    """CRC over the manifest with `value` stripped -- derived, never hand-bumped."""
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` (a list of keys/indices) in a status dict."""
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


def _finite_or_none(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def build_manifest(meter) -> dict:
    cfg = meter.cfg
    lim = cfg.limits
    wlo, whi = meter.wavelength_limits()
    rlo, rhi = meter.range_limits()

    acquire = {
        "group": "sample",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": cfg.acquisition.timeout_s,
    }

    params = [
        # -- settings ------------------------------------------------------------
        # The settle check compares against what we ASKED for (echoed exactly);
        # the readback is what the meter applied and may be rounded.
        _p("wavelength", "Wavelength", "control", "float", unit="nm",
           group="Sensor", order=10, decimals=1, step=1.0,
           min=wlo, max=whi, read_path=["wavelength_nm"],
           set={"verb": "set_wavelength", "arg": "wavelength_nm"},
           settle={"policy": "echoes", "key": "wavelength_set_nm", "tol": 1e-6},
           help="Selects the responsivity used to convert photocurrent to power. "
                "A wrong wavelength gives a wrong power, not an error."),

        _p("auto_range", "Auto range", "control", "bool", group="Sensor", order=20,
           read_path=["auto_range"],
           set={"verb": "set_auto_range", "arg": "on"},
           settle={"policy": "echoes", "key": "auto_range"},
           help="Right for CW light. Switch off for modulated or pulsed light, "
                "or the meter keeps switching ranges."),
    ]

    if cfg.sensor.auto_range:
        params.append(_p(
            "range", "Range", "indicator", "float", unit="mW", group="Sensor",
            order=30, decimals=4, scale=1e-3, read_path=["range_W"],
            help="Chosen by auto-range. Switch auto-range off to set it."))
    else:
        params.append(_p(
            "range", "Range", "control", "float", unit="mW", group="Sensor",
            order=30, decimals=4, scale=1e-3,
            min=rlo * 1e3, max=rhi * 1e3, read_path=["range_W"],
            set={"verb": "set_range", "arg": "range_W"},
            settle={"policy": "echoes", "key": "range_set_W", "tol": 1e-15},
            help="The meter snaps UP to its next range (100x steps on the PM16-121)."))

    params += [
        _p("average_time", "Average time", "indicator", "float", unit="ms",
           group="Sensor", order=40, decimals=1, scale=1e-3,
           read_path=["average_time_s"],
           help="Fixed on the PM16: every reading is a new average over this time."),

        _p("acq_readings", "Readings per acquisition", "control", "int",
           group="Measurement", order=5, step=1,
           min=lim.readings_min, max=lim.readings_max,
           read_path=["acq_readings"],
           set={"verb": "set_acquisition", "arg": "readings"},
           settle={"policy": "echoes", "key": "acq_readings", "tol": 0.5},
           help="An acquisition averages this many fresh readings."),

        # -- measurement: latched (scan) and live (panel) -------------------------
        _p("power", "Power", "indicator", "float", unit="mW", group="Measurement",
           order=10, decimals=6, scale=1e-3, read_path=["sample", "power_W"],
           acquire=acquire,
           help="Mean of fresh readings latched by `acquire`: safe to record in a scan."),
        _p("power_std", "Power std. dev.", "indicator", "float", unit="mW",
           group="Measurement", order=11, decimals=6, scale=1e-3,
           read_path=["sample", "std_W"], acquire=acquire,
           help="Standard deviation of the readings in that acquisition."),
        _p("live_power", "Power (live)", "indicator", "float", unit="mW",
           group="Live", order=20, decimals=6, scale=1e-3, plottable=True,
           read_path=["power_W"]),
        _p("flag", "Reading flag", "indicator", "string", group="Live", order=21,
           read_path=["flag"], help="'overrange' when the manual range is too small."),

        _p("acquire", "Acquire sample", "action", "action", group="Measurement",
           order=1, help="Average the next fresh readings and latch the result."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=2, read_path=["acquiring"]),
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=3, read_path=["acq_id"]),

        # -- zero ---------------------------------------------------------------------
        _p("zero", "Zero (dark adjust)", "action", "action", group="Zero", order=1,
           danger=True,
           help="COVER THE SENSOR FIRST: whatever light reaches it becomes the new zero."),
        _p("cancel_zero", "Cancel zero", "action", "action", group="Zero", order=2),
        _p("zeroing", "Zeroing", "indicator", "bool", group="Zero", order=3,
           read_path=["zeroing"]),
        _p("dark_offset", "Dark offset", "indicator", "float", unit="A",
           group="Zero", order=4, read_path=["dark_offset"]),

        # -- status -------------------------------------------------------------------
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("sensor", "Sensor", "indicator", "string", group="Status", order=3,
           read_path=["sensor"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=4, read_path=["hw_error"]),
    ]

    # Bounds that are not known (no device yet) must not appear as NaN.
    for d in params:
        for k in ("min", "max"):
            if k in d and _finite_or_none(d[k]) is None:
                del d[k]

    manifest = {"schema": SCHEMA_VERSION, "module": "pm16",
                "label": "Optical power meter", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
