"""describe.py -- the PM400's self-description: what can be shown and driven.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. The rule that keeps it honest:
nothing here restates a value that lives somewhere else. Every bound is looked
up from the brain when the manifest is built -- the config envelope narrowed by
what the CONSOLE reports for the head that is plugged in.

What is dynamic (each change moves `revision`, and every status frame carries
it as `describe_rev`, so clients re-fetch):
  * the HEAD. A power head (photodiode, thermal) gives detectors `power` /
    `power_std` in mW and the controls auto range, range (mW) and averaging
    time. A pyroelectric head gives `energy` / `energy_std` in mJ, an energy
    range (mJ), the pulse rate, and no auto range, averaging or zero. With no
    head the sensor controls disappear.
  * `range` is a control on manual range and an indicator on auto range.
  * every limit follows the head (a Si photodiode stops at 1100 nm, a thermal
    head reaches 25 um).

Values are commanded and published in SI (W, J, s) and offered in mW / mJ / ms
through `scale`, because "0.00335 mW" reads better than 3.35e-06.

The scan detectors read the LATCHED sample and carry an `acquire` block:
trigger `acquire`, then wait until status shows that acquisition's id with
`acquiring` false (gotcha #17). `live_*` are for panels only. `zero` is a
routine-usable action with a `wait` block keyed on its own run number.
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
       settle=None, args=None, danger=False, acquire=None, wait=None, help=""):
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
                 ("acquire", acquire), ("wait", wait), ("help", help)):
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
    st = meter.status()
    q = meter.quantity                       # "power", "energy" or "none"
    energy = q == "energy"
    wlo, whi = meter.wavelength_limits()
    rlo, rhi = meter.range_limits()
    alo, ahi = meter.avg_time_limits()
    su = "mJ" if energy else "mW"            # the unit offered to people (scale 1e-3)

    acquire = {
        "group": "sample",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        # derived from settle + readings x reading time, never below the config
        "timeout_s": round(meter.acquire_timeout_s(), 1),
    }

    params = []
    # -- settings (only with a usable head) ---------------------------------------
    if q != "none":
        # The settle check compares against what we ASKED for (echoed exactly);
        # the readback is what the console applied and may be rounded.
        if st.wavelength_settable:
            params.append(_p(
                "wavelength", "Wavelength", "control", "float", unit="nm",
                group="Sensor", order=10, decimals=1, step=1.0,
                min=wlo, max=whi, read_path=["wavelength_nm"],
                set={"verb": "set_wavelength", "arg": "wavelength_nm"},
                settle={"policy": "echoes", "key": "wavelength_set_nm", "tol": 1e-6},
                help="The correction wavelength. A photodiode's responsivity changes "
                     "a lot with it; a thermal or pyro head's only a little. A wrong "
                     "wavelength gives a wrong reading, not an error."))
        else:
            params.append(_p("wavelength", "Wavelength", "indicator", "float",
                             unit="nm", group="Sensor", order=10, decimals=1,
                             read_path=["wavelength_nm"],
                             help="This head has a fixed wavelength."))

        if not energy:
            params.append(_p(
                "auto_range", "Auto range", "control", "bool", group="Sensor", order=20,
                read_path=["auto_range"],
                set={"verb": "set_auto_range", "arg": "on"},
                settle={"policy": "echoes", "key": "auto_range"},
                help="Right for CW light. Switch off for modulated light, or the "
                     "console keeps switching ranges."))

        if not energy and cfg.sensor.auto_range:
            params.append(_p(
                "range", "Range", "indicator", "float", unit=su, group="Sensor",
                order=30, decimals=4, scale=1e-3, read_path=["range"],
                help="Chosen by auto-range. Switch auto-range off to set it."))
        else:
            params.append(_p(
                "range", "Energy range" if energy else "Range", "control", "float",
                unit=su, group="Sensor", order=30, decimals=4, scale=1e-3,
                min=rlo * 1e3, max=rhi * 1e3, read_path=["range"],
                set={"verb": "set_range", "arg": "range"},
                # tol: a thousandth of the smallest range. An ABSOLUTE 1e-15 was
                # tighter than float rounding at the top of the range (250 W
                # sent as 250000 mW x 1e-3 = 250.00000000000003 W, clamped to
                # 250 -> never "settled"), and it can never mistake one range
                # step for another.
                settle={"policy": "echoes", "key": "range_set",
                        "tol": (rlo if math.isfinite(rlo) and rlo > 0 else 1e-10) * 1e-3},
                help="The console snaps UP to its next range."
                     + (" Pick one above the largest pulse." if energy else "")))

        if energy:
            params.append(_p(
                "rep_rate", "Pulse rate", "indicator", "float", unit="Hz",
                group="Sensor", order=40, decimals=2, plottable=True,
                read_path=["rep_rate_Hz"],
                help="Repetition rate the pyroelectric head sees."))
        else:
            params.append(_p(
                "avg_time", "Averaging time", "control", "float", unit="ms",
                group="Sensor", order=40, decimals=1, scale=1e-3,
                min=alo * 1e3, max=ahi * 1e3, read_path=["avg_time_s"],
                set={"verb": "set_avg_time", "arg": "avg_time_s"},
                settle={"policy": "echoes", "key": "avg_time_set_s", "tol": 1e-9},
                help="Each reading averages the console's samples over this time. "
                     "Longer = less noise, fewer readings per second."))

    # -- measurement: latched (scan) and live (panel) -----------------------------
    word = "energy" if energy else "power"
    Word = word.capitalize()
    params += [
        _p("acq_readings", "Readings per acquisition", "control", "int",
           group="Measurement", order=5, step=1,
           min=lim.readings_min, max=lim.readings_max, read_path=["acq_readings"],
           set={"verb": "set_acquisition", "arg": "readings"},
           settle={"policy": "echoes", "key": "acq_readings", "tol": 0.5},
           help="An acquisition averages this many fresh readings"
                + (" (pulses)." if energy else ".")),
        _p("acq_settle", "Settle before acquiring", "control", "float", unit="s",
           group="Measurement", order=6, decimals=2, step=0.1,
           min=0.0, max=lim.settle_max_s, read_path=["acq_settle_s"],
           set={"verb": "set_settle", "arg": "settle_s"},
           settle={"policy": "echoes", "key": "acq_settle_s", "tol": 1e-9},
           help="Readings that start earlier than this after the trigger are "
                "ignored. ~5 s for a thermal head; 0 for a photodiode."),

        _p(word, Word, "indicator", "float", unit=su, group="Measurement",
           order=10, decimals=6, scale=1e-3, read_path=["sample", "value"],
           acquire=acquire,
           help=f"Mean {word} of fresh readings latched by `acquire`: "
                "safe to record in a scan."),
        _p(f"{word}_std", f"{Word} std. dev.", "indicator", "float", unit=su,
           group="Measurement", order=11, decimals=6, scale=1e-3,
           read_path=["sample", "std"], acquire=acquire,
           help="Standard deviation of the readings in that acquisition."),
        _p(f"live_{word}", f"{Word} (live)", "indicator", "float", unit=su,
           group="Live", order=20, decimals=6, scale=1e-3, plottable=True,
           read_path=["value"]),
        _p("flag", "Reading flag", "indicator", "string", group="Live", order=21,
           read_path=["flag"], help="'overrange' when the manual range is too small."),

        _p("acquire", "Acquire sample", "action", "action", group="Measurement",
           order=1, help="Average the next fresh readings and latch the result."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=2, read_path=["acquiring"]),
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=3, read_path=["acq_id"]),
    ]

    # -- zero (photodiode and thermal heads only) ----------------------------------
    if st.zero_supported:
        params += [
            # Not flagged danger: it changes no hardware state that cannot be
            # redone, it only has to be run with the head covered.
            _p("zero", "Zero (dark adjust)", "action", "action", group="Zero", order=1,
               wait={"target_key": "zero_id",
                     "ready": {"policy": "adopt_then_flag", "setpoint_key": "zero_id",
                               "flag_key": "zeroing", "invert": True},
                     "check": {"key": "zero_error", "equals": "OK"},
                     "timeout_s": 120.0},
               help="Cover the head first: whatever light reaches it becomes the "
                    "new zero. Usable in a scan routine (e.g. behind a closed shutter)."),
            _p("cancel_zero", "Cancel zero", "action", "action", group="Zero", order=2,
               wait={"ready": {"policy": "immediate"}}),
            _p("zeroing", "Zeroing", "indicator", "bool", group="Zero", order=3,
               read_path=["zeroing"]),
            _p("zero_id", "Zero #", "indicator", "int", group="Zero", order=4,
               read_path=["zero_id"]),
            _p("dark_offset", "Zero offset", "indicator", "float",
               unit=st.dark_unit, group="Zero", order=5, read_path=["dark_offset"]),
        ]

    # -- status -------------------------------------------------------------------
    params += [
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("head", "Head type", "indicator", "string", group="Status", order=3,
           read_path=["head"]),
        _p("sensor", "Sensor head", "indicator", "string", group="Status", order=4,
           read_path=["sensor"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=5, read_path=["hw_error"]),
    ]

    # Bounds that are not known (no device yet) must not appear as NaN.
    for d in params:
        for k in ("min", "max"):
            if k in d and _finite_or_none(d[k]) is None:
                del d[k]

    manifest = {"schema": SCHEMA_VERSION, "module": "pm400",
                "label": "Optical power / energy meter (PM400)", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
