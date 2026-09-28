"""describe.py -- the SMU's self-description: what can be shown and driven.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. The rule that keeps it honest:
nothing here restates a value that lives somewhere else. Every bound is asked
of the brain (level_limits / limit_limits) when the manifest is built.

What is dynamic -- and why `revision` moves:
  * The SOURCE FUNCTION decides which level and which limit are live.
    Sourcing voltage: `source_voltage` and `current_limit` are controls,
    `source_current` and `voltage_limit` become read-only indicators (they are
    stored, but setting them would change nothing at the sample -- a scan over
    them would record a flat line, so they are not offered as scan axes).
  * The 2450's output boxes couple level and limit: a current limit above
    105 mA shrinks the voltage range to +-21 V, a voltage above 21 V shrinks the
    current limit to 105 mA. A fixed source range caps the level at 105 % of it.
  * Ranges are controls on fixed range and indicators on autorange (pm16's pattern).
Every status frame carries `describe_rev`, so clients re-fetch when it moves.

Settle policies:
  * Source levels: `adopt_then_flag` -- the service must have ADOPTED the new
    setpoint, then `settled` must be true (the level has been held
    source.settle_s with the output on). Echo alone would let a scan measure
    during the sample's own transient.
  * The source CURRENT is offered in uA on the wire. scan-core's
    adopt_then_flag compares with a fixed absolute tolerance of 1e-6 wire
    units; in amperes a 0.5 uA step would count as "already adopted" and the
    scan would record the previous point. In uA the tolerance is 1 pA.
  * Everything else is set-and-forget: `echoes`; ranges `immediate` (the
    instrument snaps a requested range UP, so the value asked for is never
    echoed exactly -- they are not meant to be scanned).

Detectors: `voltage`, `current`, `resistance` (+ `_std`) read the LATCHED
sample and carry an `acquire` block (pm16/hf2 pattern, gotcha #17). The
`live_*` indicators are for panels only.
"""

from __future__ import annotations

import json
import math
import zlib

from ..backends.base import other, range_table

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


def _acquire_timeout(cfg) -> float:
    """How long a client may wait for one acquisition: the configured timeout,
    or -- if that is too short for the settings -- three times the expected
    duration (N readings of NPLC / line frequency, ~50 ms of query overhead
    each on the real instrument, plus the settle time). Derived, so 1000
    readings at 10 NPLC (~250 s) cannot time out a scan that is working."""
    a, m = cfg.acquisition, cfg.measure
    per = m.nplc / max(1.0, float(cfg.hardware.line_freq_Hz)) + 0.05
    expected = int(a.readings) * per + cfg.source.settle_s
    return round(max(float(a.timeout_s), 3.0 * expected + 5.0), 1)


def build_manifest(smu) -> dict:
    cfg = smu.cfg
    src, m, lim = cfg.source, cfg.measure, cfg.limits
    fn = src.function
    mfn = other(fn)
    sv = fn == "voltage"
    u_src, u_meas = ("V", "A") if sv else ("A", "V")

    vlo, vhi = smu.level_limits("voltage")
    ilo, ihi = smu.level_limits("current")
    ilim_lo, ilim_hi = smu.limit_limits("voltage")
    vlim_lo, vlim_hi = smu.limit_limits("current")

    acquire = {
        "group": "sample",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": _acquire_timeout(cfg),
    }

    params = [
        # -- output ----------------------------------------------------------------
        _p("output", "Output", "control", "bool", group="Output", order=1,
           read_path=["output"], danger=True,
           set={"verb": "set_output", "arg": "on"},
           settle={"policy": "echoes", "key": "output"},
           help="Output relay. ON re-sends the compliance limit, then the level."),
        _p("output_off", "Output OFF", "action", "action", group="Output", order=2,
           wait={"ready": {"policy": "flag_only", "key": "output", "invert": True}},
           help="The safe state. Usable in a scan routine (e.g. after_scan)."),
        _p("settled", "Source settled", "indicator", "bool", group="Output",
           order=3, read_path=["settled"]),
        _p("tripped", "In compliance", "indicator", "bool", group="Output",
           order=4, read_path=["tripped"],
           help="The limit is reached: the SMU is now regulating the OTHER quantity."),

        # -- source ------------------------------------------------------------------
        _p("source_function", "Source function", "control", "enum", group="Source",
           order=10, options=["voltage", "current"], read_path=["source_function"],
           set={"verb": "set_source_function", "arg": "function"},
           settle={"policy": "echoes", "key": "source_function"},
           help="Switching turns the output OFF first."),
    ]

    # Voltage level + current limit: live when sourcing voltage.
    if sv:
        params += [
            _p("source_voltage", "Source voltage", "control", "float", unit="V",
               group="Source", order=20, decimals=6, step=0.01, plottable=True,
               min=vlo, max=vhi, read_path=["source_voltage_set_V"],
               set={"verb": "set_voltage", "arg": "voltage_V"},
               settle={"policy": "adopt_then_flag",
                       "setpoint_key": "source_voltage_set_V", "flag_key": "settled"},
               help="Limited to +-21 V while the current limit is above 105 mA."),
            _p("current_limit", "Current limit", "control", "float", unit="A",
               group="Source", order=30, decimals=9,
               min=ilim_lo, max=ilim_hi, read_path=["current_limit_A"],
               set={"verb": "set_current_limit", "arg": "current_limit_A"},
               settle={"policy": "echoes", "key": "current_limit_A", "tol": 1e-12},
               help="Compliance (ILIM). Limited to 105 mA while |V| > 21 V."),
            # uA, like the control it becomes when sourcing current: one id,
            # one unit, whatever the mode (a plot or a saved recipe must not
            # see the same parameter jump by a factor 1e6)
            _p("source_current", "Source current (stored)", "indicator", "float",
               unit="uA", group="Source", order=40, decimals=4,
               read_path=["source_current_set_uA"],
               help="Used when sourcing current."),
            _p("voltage_limit", "Voltage limit (stored)", "indicator", "float",
               unit="V", group="Source", order=50, decimals=4,
               read_path=["voltage_limit_V"], help="Used when sourcing current."),
        ]
    else:
        params += [
            _p("source_current", "Source current", "control", "float", unit="uA",
               group="Source", order=20, decimals=4, step=1.0, plottable=True,
               min=ilo * 1e6, max=ihi * 1e6, read_path=["source_current_set_uA"],
               set={"verb": "set_current", "arg": "current_uA"},
               settle={"policy": "adopt_then_flag",
                       "setpoint_key": "source_current_set_uA", "flag_key": "settled"},
               help="In uA so a scan's settle check resolves 1 pA (see describe.py). "
                    "Limited to +-105 mA while the voltage limit is above 21 V."),
            _p("voltage_limit", "Voltage limit", "control", "float", unit="V",
               group="Source", order=30, decimals=4,
               min=vlim_lo, max=vlim_hi, read_path=["voltage_limit_V"],
               set={"verb": "set_voltage_limit", "arg": "voltage_limit_V"},
               settle={"policy": "echoes", "key": "voltage_limit_V", "tol": 1e-9},
               help="Compliance (VLIM). Limited to 21 V while |I| > 105 mA."),
            _p("source_voltage", "Source voltage (stored)", "indicator", "float",
               unit="V", group="Source", order=40, decimals=6,
               read_path=["source_voltage_set_V"], help="Used when sourcing voltage."),
            _p("current_limit", "Current limit (stored)", "indicator", "float",
               unit="A", group="Source", order=50, decimals=9,
               read_path=["current_limit_A"], help="Used when sourcing voltage."),
        ]

    params.append(_p(
        "source_auto_range", "Source autorange", "control", "bool", group="Source",
        order=60, read_path=["source_auto_range"],
        set={"verb": "set_source_auto_range", "arg": "on"},
        settle={"policy": "echoes", "key": "source_auto_range"}))
    if src.auto_range:
        params.append(_p("source_range", "Source range", "indicator", "float",
                         unit=u_src, group="Source", order=61, read_path=["source_range"],
                         help="Chosen by autorange."))
    else:
        table = range_table(fn)
        params.append(_p("source_range", "Source range", "control", "float",
                         unit=u_src, group="Source", order=61,
                         min=table[0], max=table[-1], read_path=["source_range"],
                         set={"verb": "set_source_range", "arg": "range"},
                         settle={"policy": "immediate"},
                         help="Snaps UP to a real range; the level is capped at 105 % of it."))

    # -- measure ---------------------------------------------------------------------
    params.append(_p(
        "measure_auto_range", "Measure autorange", "control", "bool", group="Measure",
        order=10, read_path=["measure_auto_range"],
        set={"verb": "set_measure_auto_range", "arg": "on"},
        settle={"policy": "echoes", "key": "measure_auto_range"}))
    if m.auto_range:
        params.append(_p("measure_range", "Measure range", "indicator", "float",
                         unit=u_meas, group="Measure", order=11,
                         read_path=["measure_range"], help="Chosen by autorange."))
    else:
        table = range_table(mfn)
        params.append(_p("measure_range", "Measure range", "control", "float",
                         unit=u_meas, group="Measure", order=11,
                         min=table[0], max=table[-1], read_path=["measure_range"],
                         set={"verb": "set_measure_range", "arg": "range"},
                         settle={"policy": "immediate"},
                         help="Snaps UP; readings above 105 % of it overflow (NaN)."))

    params += [
        _p("nplc", "Integration (NPLC)", "control", "float", group="Measure",
           order=20, decimals=2, step=0.1, min=lim.nplc_min, max=lim.nplc_max,
           read_path=["nplc"], set={"verb": "set_nplc", "arg": "nplc"},
           settle={"policy": "echoes", "key": "nplc", "tol": 1e-9},
           help="Power-line cycles per reading: 1 NPLC = 20 ms at 50 Hz. "
                "Longer = quieter and slower."),
        _p("four_wire", "4-wire sense", "control", "bool", group="Measure", order=30,
           read_path=["four_wire"], set={"verb": "set_four_wire", "arg": "on"},
           settle={"policy": "echoes", "key": "four_wire"},
           help="Remote sense: lead and contact resistance drop out of V (and R)."),
        _p("acq_readings", "Readings per acquisition", "control", "int",
           group="Measure", order=40, step=1,
           min=lim.readings_min, max=lim.readings_max, read_path=["acq_readings"],
           set={"verb": "set_acquisition", "arg": "readings"},
           settle={"policy": "echoes", "key": "acq_readings", "tol": 0.5}),

        # -- the scan detectors: latched by `acquire` ----------------------------------
        _p("voltage", "Voltage", "indicator", "float", unit="V", group="Sample",
           order=10, decimals=7, read_path=["sample", "voltage_V"], acquire=acquire,
           help="Mean of fresh readings after the source settled. Sourcing voltage "
                "this is the READBACK: below the setpoint when in compliance."),
        _p("voltage_std", "Voltage std. dev.", "indicator", "float", unit="V",
           group="Sample", order=11, decimals=7,
           read_path=["sample", "voltage_std_V"], acquire=acquire),
        _p("current", "Current", "indicator", "float", unit="A", group="Sample",
           order=20, decimals=10, read_path=["sample", "current_A"], acquire=acquire),
        _p("current_std", "Current std. dev.", "indicator", "float", unit="A",
           group="Sample", order=21, decimals=10,
           read_path=["sample", "current_std_A"], acquire=acquire),
        _p("resistance", "Resistance V/I", "indicator", "float", unit="ohm",
           group="Sample", order=30, decimals=4,
           read_path=["sample", "resistance_ohm"], acquire=acquire,
           help="Mean V / mean I. In 2-wire it includes the leads."),
        _p("resistance_std", "Resistance std. dev.", "indicator", "float", unit="ohm",
           group="Sample", order=31, decimals=4,
           read_path=["sample", "resistance_std_ohm"], acquire=acquire),
        _p("sample_tripped", "In compliance (sample)", "indicator", "bool",
           group="Sample", order=40, read_path=["sample", "tripped"], acquire=acquire,
           help="True if ANY reading of the acquisition was in compliance."),
        _p("acquire", "Acquire sample", "action", "action", group="Sample", order=1,
           help="Average the next fresh readings and latch the result."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Sample", order=2,
           read_path=["acquiring"]),
        _p("acq_id", "Acquisition #", "indicator", "int", group="Sample", order=3,
           read_path=["acq_id"]),

        # -- live (panels, not scans) -----------------------------------------------------
        _p("live_voltage", "Voltage (live)", "indicator", "float", unit="V",
           group="Live", order=10, decimals=7, plottable=True, read_path=["voltage_V"]),
        _p("live_current", "Current (live)", "indicator", "float", unit="A",
           group="Live", order=20, decimals=10, plottable=True, read_path=["current_A"]),
        _p("live_resistance", "Resistance (live)", "indicator", "float", unit="ohm",
           group="Live", order=30, decimals=4, plottable=True,
           read_path=["resistance_ohm"]),
        _p("flag", "Reading flag", "indicator", "string", group="Live", order=40,
           read_path=["flag"], help="'compliance' or 'overflow'."),

        # -- status -------------------------------------------------------------------------
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),
    ]

    for d in params:
        for k in ("min", "max"):
            if k in d and _finite_or_none(d[k]) is None:
                del d[k]

    manifest = {"schema": SCHEMA_VERSION, "module": "k2450",
                "label": "Source-measure unit", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
