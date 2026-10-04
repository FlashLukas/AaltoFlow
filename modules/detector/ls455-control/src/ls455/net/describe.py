"""describe.py -- the gaussmeter's self-description: what can be shown and driven.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. The rule that keeps it honest:
nothing here restates a value that lives somewhere else. Every bound is looked
up when the manifest is built -- the range bounds come from the PROBE that is
plugged in (an HST probe spans 3.5 mT .. 35 T, an HSE 0.35 mT .. 3.5 T),
narrowed by the config envelope.

What is dynamic (each changes `revision`, carried by every status frame as
`describe_rev`, so clients re-fetch):
  * `range` is a control on manual range and an indicator on auto range (same
    id both ways);
  * its min/max follow the probe family;
  * `rms_band` is a control only in RMS mode, `dc_digits` only in DC mode,
    and in PEAK mode the peak sub-settings appear as indicators;
  * the detectors' label and help name what they report (DC field, RMS
    field, a peak) -- a meter adopted in peak mode must not look like a DC one;
  * a different PROBE (re-read at reconnect or by `reread_probe`) changes the
    range list and so the range bounds.

Every field is in mT, commanded and published.

The scan detectors (`field`, `field_std`) read the LATCHED sample and carry an
`acquire` block: trigger `acquire`, then wait until status shows that
acquisition's id with `acquiring` false. `live_field` is for panels only.
"""

from __future__ import annotations

import json
import math
import zlib

from ..backends.base import (DC_DIGITS, MODES, PEAK_DISPLAYS, PEAK_MODES,
                             RMS_BANDS, UNIT_CODES)

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, acquire=None, wait=None,
       help=""):
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
    m = cfg.meter
    lim = cfg.limits
    rlo, rhi = meter.range_limits()
    # what the field detectors report, so their labels say it (see docstring)
    kind = {"dc": "DC", "rms": "RMS", "peak": "peak"}.get(m.mode, m.mode)
    quantity = meter.quantity()

    acquire = {
        "group": "sample",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        # settle wait + readings + the configured margin (acquire_timeout_s)
        "timeout_s": round(meter.acquire_timeout_s(), 3),
    }

    params = [
        # -- settings ------------------------------------------------------------
        _p("mode", "Mode", "control", "enum", group="Meter", order=10,
           options=list(MODES), read_path=["mode"],
           set={"verb": "set_mode", "arg": "mode"},
           settle={"policy": "echoes", "key": "mode"},
           help="dc: static field. rms: the AC part (wide band to 20 kHz, "
                "narrow to 1 kHz). peak: the peak detector, its sub-settings as "
                "set on the front panel. measured_field_mT is empty outside dc."),
    ]
    if m.mode == "dc":
        params.append(_p(
            "dc_digits", "DC resolution", "control", "int", unit="digits",
            group="Meter", order=20, min=min(DC_DIGITS), max=max(DC_DIGITS), step=1,
            read_path=["dc_digits"],
            set={"verb": "set_dc_digits", "arg": "digits"},
            settle={"policy": "echoes", "key": "dc_digits", "tol": 0.5},
            help="The 455's filter: 3 = 100 Hz / 30 rdg/s, 4 = 10 Hz, "
                 "5 = 1 Hz / 10 rdg/s. More digits = less noise, slower settling."))
    elif m.mode == "peak":
        params += [
            # ENUMS (scan-core stores an enum as a code + the names, developer
            # notes 4b): the backend maps the meter's RDGMODE codes onto
            # exactly these tuples, and falls back to their first entries.
            _p("peak_mode", "Peak mode", "indicator", "enum", group="Meter",
               order=20, options=list(PEAK_MODES), read_path=["peak_mode"],
               help="periodic: a repeating signal. pulse: a single event, LATCHED "
                    "until reset on the meter -- then a reading is the largest peak "
                    "since that reset, not since the acquisition started. Set on "
                    "the front panel."),
            _p("peak_display", "Peak display", "indicator", "enum", group="Meter",
               order=21, options=list(PEAK_DISPLAYS), read_path=["peak_display"],
               help="Which peak is reported: positive, negative, or with 'both' "
                    "the larger in magnitude (sign kept)."),
        ]
    else:
        params.append(_p(
            "rms_band", "RMS band", "control", "enum", group="Meter", order=20,
            options=list(RMS_BANDS), read_path=["rms_band"],
            set={"verb": "set_rms_band", "arg": "band"},
            settle={"policy": "echoes", "key": "rms_band"},
            help="wide: up to 20 kHz. narrow: up to 1 kHz, less noise."))

    params.append(_p("auto_range", "Auto range", "control", "bool", group="Meter",
                     order=30, read_path=["auto_range"],
                     set={"verb": "set_auto_range", "arg": "on"},
                     settle={"policy": "echoes", "key": "auto_range"},
                     help="Right for a static field. Switch off when the field "
                          "sweeps through several decades, or ranges keep switching."))
    if m.auto_range:
        params.append(_p(
            "range", "Range", "indicator", "float", unit="mT", group="Meter",
            order=40, decimals=4, read_path=["range_mT"],
            # the probe's range list is named here so that describe_rev moves
            # when a different probe is plugged in, also on auto range
            help="Full scale chosen by auto-range. Switch auto-range off to set it. "
                 "This probe's ranges: "
                 + ", ".join(f"{r:g}" for r in meter.status().ranges_mT) + " mT."))
    else:
        params.append(_p(
            "range", "Range", "control", "float", unit="mT", group="Meter",
            order=40, decimals=4, min=rlo, max=rhi, read_path=["range_mT"],
            set={"verb": "set_range", "arg": "range_mT"},
            settle={"policy": "echoes", "key": "range_set_mT", "tol": 1e-9},
            help="Full scale. The meter snaps UP to its next range; the ranges "
                 "are decades and depend on the probe type."))

    params += [
        _p("display_unit", "Front-panel unit", "control", "enum", group="Meter",
           order=50, options=list(UNIT_CODES), read_path=["display_unit"],
           set={"verb": "set_display_unit", "arg": "unit"},
           settle={"policy": "echoes", "key": "display_unit"},
           help="Only what the meter's own display shows. Everything here is in mT."),
        _p("settle_s", "Filter settling", "indicator", "float", unit="s",
           group="Meter", order=60, decimals=3, read_path=["settle_s"],
           help="An acquisition ignores readings for this long after its trigger."),

        _p("relative", "Relative mode", "control", "bool", group="Relative", order=10,
           read_path=["relative"],
           set={"verb": "set_relative", "arg": "on"},
           settle={"policy": "echoes", "key": "relative"}),
        _p("rel_setpoint", "Relative setpoint", "control", "float", unit="mT",
           group="Relative", order=20, decimals=4,
           min=-lim.rel_setpoint_max_mT, max=lim.rel_setpoint_max_mT,
           read_path=["rel_setpoint_mT"],
           set={"verb": "set_relative", "arg": "setpoint_mT"},
           settle={"policy": "echoes", "key": "rel_setpoint_mT", "tol": 1e-9}),
        _p("relative_here", "Relative to present field", "action", "action",
           group="Relative", order=30,
           wait={"ready": {"policy": "immediate"}},
           help="Relative mode on, with the field measured now as the setpoint."),
        _p("field_rel", "Field - setpoint (live)", "indicator", "float", unit="mT",
           group="Relative", order=40, decimals=5, plottable=True,
           read_path=["field_rel_mT"]),

        _p("acq_readings", "Readings per acquisition", "control", "int",
           group="Measurement", order=5, step=1,
           min=lim.readings_min, max=lim.readings_max,
           read_path=["acq_readings"],
           set={"verb": "set_acquisition", "arg": "readings"},
           settle={"policy": "echoes", "key": "acq_readings", "tol": 0.5},
           help="An acquisition averages this many fresh, settled readings."),

        # -- measurement: latched (scan) and live (panel) -------------------------
        _p("field", f"Field ({kind})", "indicator", "float", unit="mT",
           group="Measurement", order=10, decimals=5,
           read_path=["sample", "field_mT"], acquire=acquire,
           help=f"Mean of fresh readings latched by `acquire`: safe to record in "
                f"a scan. Reports the {quantity}."),
        _p("field_std", f"Field std. dev. ({kind})", "indicator", "float", unit="mT",
           group="Measurement", order=11, decimals=5,
           read_path=["sample", "std_mT"], acquire=acquire,
           help=f"Standard deviation of the readings in that acquisition ({quantity})."),
        _p("live_field", f"Field ({kind}, live)", "indicator", "float", unit="mT",
           group="Live", order=20, decimals=5, plottable=True,
           read_path=["field_mT"], help=f"The {quantity}, as read now."),
        _p("quantity", "Reading is", "indicator", "string", group="Live", order=19,
           read_path=["quantity"],
           help="What the field readings are, following the meter's mode."),
        _p("flag", "Reading flag", "indicator", "string", group="Live", order=21,
           read_path=["flag"],
           help="'overload' when the manual range is too small, 'no probe'."),

        _p("acquire", "Acquire sample", "action", "action", group="Measurement",
           order=1, help="Average the next fresh readings and latch the result."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=2, read_path=["acquiring"]),
        # A counter from 0 that only goes up: min=0 is a promise the code
        # keeps (scan-core picks the storage from it, developer notes 4b). No
        # max: it is unbounded in principle.
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=3, min=0, read_path=["acq_id"]),

        # -- probe zero -----------------------------------------------------------------
        _p("zero", "Zero probe", "action", "action", group="Zero", order=1,
           danger=True,
           help="PROBE IN THE ZERO-GAUSS CHAMBER FIRST: whatever field it sees "
                "now becomes the new zero, and every later reading is off by it."),
        _p("clear_zero", "Clear probe zero", "action", "action", group="Zero",
           order=2, danger=True,
           help="Forget the stored zero; readings then include the probe offset."),
        _p("zeroing", "Zeroing", "indicator", "bool", group="Zero", order=3,
           read_path=["zeroing"]),

        # -- status -------------------------------------------------------------------
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("probe", "Probe type", "indicator", "string", group="Probe", order=1,
           read_path=["probe"],
           help="HSE / HST / UHS, as the meter reports it (TYPE?). Decides the ranges."),
        _p("probe_desc", "Probe", "indicator", "string", group="Probe", order=2,
           read_path=["probe_desc"]),
        _p("probe_serial", "Probe serial", "indicator", "string", group="Probe",
           order=3, read_path=["probe_serial"]),
        _p("probe_sensitivity", "Probe sensitivity", "indicator", "float",
           unit="mV/kG", group="Probe", order=4, decimals=4,
           read_path=["probe_sensitivity_mV_per_kG"]),
        _p("probe_geometry", "Probe geometry", "indicator", "string", group="Probe",
           order=5, read_path=["probe_geometry"],
           help="axial or transverse. From the config (hardware.probe_geometry): "
                "the 455 does not report it."),
        _p("reread_probe", "Re-read probe", "action", "action", group="Probe",
           order=6, wait={"ready": {"policy": "immediate"}},
           help="Ask the meter again which probe is plugged in (after swapping "
                "it). The ranges and their limits follow the probe."),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=4, read_path=["hw_error"]),
    ]

    # Bounds that are not known (no probe yet) must not appear as NaN.
    for d in params:
        for k in ("min", "max"):
            if k in d and _finite_or_none(d[k]) is None:
                del d[k]

    manifest = {"schema": SCHEMA_VERSION, "module": "ls455",
                "label": "Gaussmeter", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
