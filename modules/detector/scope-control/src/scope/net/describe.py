"""describe.py -- the scope's self-description: what can be shown, driven and
recorded.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. Nothing here restates a value
that lives somewhere else; every bound is read from the Scope when the
manifest is built.

THE DETECTORS. One acquisition (`acquire`: restart the average, wait for
`averages` FRESH triggered traces, latch) feeds all of them -- one trigger,
one wait, many reads:
  * TRACES: `ch1`, `ch2` (filtered average, in each channel's physical unit),
    plus `ch1_raw`, `ch2_raw` when keep_raw is on. ARRAY detectors with their
    own dimension `time` (the record's time axis, from `get_time`), fetched
    with `get_trace` because they are too big for the status stream.
  * per channel: mean, rms, peak-to-peak, amplitude, frequency;
  * `phase_21` -- CH2's phase relative to CH1 at CH1's fundamental;
  * the LOOP (Y = loop_y against X = loop_x): Hc+, Hc-, Hc, bias, Ms (the
    Kerr amplitude), Mr, squareness, background slope, area.
The `acquire` block makes scan-core trigger and wait for THAT acquisition
(acq_id, gotcha #17) before reading -- a cold read would return the previous
point's trace and nothing would raise.

THE CONTROLS. The scope's own settings (V/div, offset, coupling, probe,
time/div, delay, trigger) settle on "the module echoes what was ASKED
(`<key>_set`) and has written it and read the scope back"
(`settings_settled`): the scope snaps V/div and time/div to its own steps, so
the value it really holds is a separate indicator. The module's settings
(averages, points, filter, units, loop) are immediate.

What is dynamic: the acquisition timeout grows with `averages`, the trace
detectors' length is `points`, raw traces exist only with keep_raw, and the
units follow the channels' physical settings. Any of these changes
`revision`, which every status frame carries as `describe_rev`. The simulated
bench's knobs exist only when the backend IS the simulator.
"""

from __future__ import annotations

import json
import math
import zlib

from ..config import COUPLINGS, TRIGGER_SOURCES, TRIGGER_SLOPES, TRIGGER_MODES

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, acquire=None, help="", **extra):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract.
    `extra` carries the array-detector fields (dtype, shape, dims, read) and an
    action's `wait` block."""
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
                 ("acquire", acquire), ("help", help), *extra.items()):
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




def acquisition_timeout_s(cfg, record_s: float = 0.0) -> float:
    """How long one acquisition may take: `averages` + 1 traces, each taking
    the longer of one period of the slowest planned trigger rate and the
    record itself (a 20 s record at 0.5 s/div cannot come faster than every
    ~20 s, whatever the trigger), x 1.5, + 10 s; never less than timeout_s.
    `record_s`: the length of the records seen so far (0 = not known yet)."""
    a = cfg.acquisition
    per_trace = max(1.0 / max(float(a.min_trigger_hz), 1e-3), 1.2 * float(record_s or 0.0))
    need = 1.5 * (int(a.averages) + 1) * per_trace + 10.0
    return round(max(float(a.timeout_s), need), 1)


_LOOP = (("hc_plus", "Hc+", "x", "Coercive field on the rising branch."),
         ("hc_minus", "Hc-", "x", "Coercive field on the falling branch."),
         ("hc", "Hc", "x", "(Hc+ - Hc-)/2."),
         ("bias", "Exchange bias", "x", "(Hc+ + Hc-)/2: the loop's shift."),
         ("ms", "Ms (Kerr amplitude)", "y", "Half the jump between the saturation levels."),
         ("mr", "Mr (remanence)", "y", "Y at X = 0, about the mid level."),
         ("squareness", "Squareness Mr/Ms", "", ""),
         ("slope", "Background slope", "y/x", "Linear background (Faraday, substrate) "
                                              "fitted in the high-field ends."),
         ("area", "Loop area", "x*y", "Per cycle; only when the record closes."))

_VALUES = (("mean", "mean", ""), ("rms", "rms", ""), ("pk2pk", "peak-to-peak", ""),
           ("amplitude", "amplitude", ""), ("frequency", "frequency", "Hz"))


def build_manifest(scope) -> dict:
    cfg = scope.cfg
    st = scope.status()
    chans = list(scope.channels)
    # the record length rounded UP to 1 s steps, so its jitter does not move
    # the manifest revision
    timeout = acquisition_timeout_s(cfg, math.ceil(float(st.get("record_s") or 0.0)))
    npts = int(cfg.acquisition.points)
    unit = {ch: cfg.channel(ch).phys_unit for ch in chans}
    acquire = {
        "group": "scope",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": timeout,
    }
    time_dim = [{"name": "time", "label": "Time", "unit": "s", "length": npts,
                 "coord_verb": "get_time", "coord_key": "values"}]

    def scope_setting(id, label, type, key, verb, arg, group, order, extra=None, **kw):
        """A SCOPE setting: echo what was asked, then the read-back flag."""
        return _p(id, label, "control", type, group=group, order=order,
                  read_path=[key],
                  set={"verb": verb, "arg": arg, **({"extra": extra} if extra else {})},
                  settle={"policy": "adopt_then_flag", "setpoint_key": f"{key}_set",
                          "flag_key": "settings_settled"} if type in ("float", "bool")
                  else {"policy": "echoes", "key": f"{key}_set"},
                  timeout_s=10.0, **kw)

    params = []
    # -- channels ---------------------------------------------------------------
    for n, ch in enumerate(chans):
        C = ch.upper()
        g = f"{C} input"
        ex = {"channel": ch}
        base = 10 * (n + 1)
        params += [
            scope_setting(f"{ch}_enabled", f"{C} on", "bool", f"{ch}_enabled",
                          "set_channel_enabled", "on", g, base + 1, ex),
            scope_setting(f"{ch}_vdiv", f"{C} V/div", "float", f"{ch}_vdiv_V", "set_vdiv",
                          "vdiv_V", g, base + 2, ex, unit="V", decimals=4, min=1e-3, max=10.0,
                          help="At the probe tip. The scope snaps to its 1-2-5 steps; "
                               "the screen is +-4 divisions around the offset, and a "
                               "trace beyond it is CLIPPED (an acquisition warns)."),
            scope_setting(f"{ch}_offset", f"{C} offset", "float", f"{ch}_offset_V",
                          "set_offset", "offset_V", g, base + 3, ex, unit="V", decimals=4),
            scope_setting(f"{ch}_coupling", f"{C} coupling", "enum", f"{ch}_coupling",
                          "set_coupling", "coupling", g, base + 4, ex, options=list(COUPLINGS)),
            scope_setting(f"{ch}_probe", f"{C} probe", "float", f"{ch}_probe", "set_probe",
                          "probe", g, base + 5, ex, unit="x", decimals=0, min=1.0, max=1000.0),
            _p(f"{ch}_phys_scale", f"{C} {unit[ch]} per volt", "control", "float",
               unit=f"{unit[ch]}/V", group=f"{C} quantity", order=base + 6, decimals=6,
               read_path=[f"{ch}_phys_scale"],
               set={"verb": "set_physical", "arg": "scale", "extra": ex},
               settle={"policy": "echoes", "key": f"{ch}_phys_scale", "tol": 1e-12},
               help=f"What {C} measures: quantity = scale x volts + offset (e.g. mT/V "
                    f"of the Hall probe). Changing it restarts the average."),
            _p(f"{ch}_phys_offset", f"{C} {unit[ch]} at 0 V", "control", "float",
               unit=unit[ch], group=f"{C} quantity", order=base + 7, decimals=6,
               read_path=[f"{ch}_phys_offset"],
               set={"verb": "set_physical", "arg": "offset", "extra": ex},
               settle={"policy": "echoes", "key": f"{ch}_phys_offset", "tol": 1e-12}),
        ]
    # -- timebase and trigger -----------------------------------------------------
    params += [
        scope_setting("tdiv", "Time/div", "float", "tdiv_s", "set_tdiv", "tdiv_s",
                      "Timebase", 1, unit="s", decimals=9,
                      help="The scope snaps to its own steps. Must not change during a "
                           "scan (the trace length and time axis would)."),
        scope_setting("delay", "Trigger delay", "float", "delay_s", "set_delay", "delay_s",
                      "Timebase", 2, unit="s", decimals=9,
                      help="Positive moves the window later (more after the trigger); "
                           "the trigger stays at t = 0."),
        _p("sample_rate", "Sample rate", "indicator", "float", unit="Sa/s",
           group="Timebase", order=3, read_path=["sample_rate_Hz"]),
        scope_setting("trigger_source", "Trigger source", "enum", "trigger_source",
                      "set_trigger_source", "source", "Trigger", 1,
                      options=list(TRIGGER_SOURCES)),
        scope_setting("trigger_level", "Trigger level", "float", "trigger_level_V",
                      "set_trigger_level", "level_V", "Trigger", 2, unit="V", decimals=4),
        scope_setting("trigger_slope", "Trigger slope", "enum", "trigger_slope",
                      "set_trigger_slope", "slope", "Trigger", 3,
                      options=list(TRIGGER_SLOPES)),
        scope_setting("trigger_mode", "Trigger mode", "enum", "trigger_mode",
                      "set_trigger_mode", "mode", "Trigger", 4, options=list(TRIGGER_MODES),
                      help="auto / normal / single / stop. A stopped scope makes no "
                           "traces: an acquisition is refused."),
        _p("trigger_rate", "Trigger rate", "indicator", "float", unit="Hz",
           group="Trigger", order=5, decimals=2, plottable=True,
           read_path=["trigger_rate_Hz"]),
        _p("rolling", "Roll mode (no triggered records)", "indicator", "bool",
           group="Timebase", order=4, read_path=["rolling"],
           help="At slow time bases the scope rolls; acquisitions are refused there."),
        _p("settings_settled", "Scope settings applied", "indicator", "bool",
           group="Trigger", order=6, read_path=["settings_settled"]),
    ]
    # -- acquisition, filter, loop: the module's own ----------------------------------
    params += [
        _p("averages", "Averages", "control", "int", group="Acquisition", order=1,
           min=1, max=100000, step=1, read_path=["averages"],
           set={"verb": "set_averages", "arg": "averages"},
           settle={"policy": "echoes", "key": "averages", "tol": 0.5},
           help=f"Fresh traces per acquisition (and in the live average). At the "
                f"slowest planned trigger rate ({cfg.acquisition.min_trigger_hz:g} Hz) "
                f"an acquisition may take up to {timeout:g} s."),
        _p("points", "Points per trace", "control", "int", group="Acquisition", order=2,
           min=16, max=100000, step=1, read_path=["points"],
           set={"verb": "set_points", "arg": "points"},
           settle={"policy": "echoes", "key": "points", "tol": 0.5},
           help="The record is reduced to this many samples (neighbours averaged). "
                "Must not change during a scan."),
        _p("keep_raw", "Record raw traces too", "control", "bool", group="Acquisition",
           order=3, read_path=["keep_raw"], set={"verb": "set_keep_raw", "arg": "on"},
           settle={"policy": "echoes", "key": "keep_raw"}),
        _p("running_n", "Traces in the average", "indicator", "int", group="Acquisition",
           order=4, min=0, read_path=["running_n"]),
        _p("restart_average", "Restart average", "action", "action", group="Acquisition",
           order=5, wait={"ready": {"policy": "immediate"}}),
        _p("lowpass", "Low-pass", "control", "float", unit="Hz", group="Filter", order=1,
           min=0.0, decimals=3, read_path=["lowpass_Hz"],
           set={"verb": "set_filter", "arg": "lowpass_Hz"},
           settle={"policy": "echoes", "key": "lowpass_Hz"},
           help="Zero-phase, the same on every channel. 0 = off."),
        _p("highpass", "High-pass", "control", "float", unit="Hz", group="Filter", order=2,
           min=0.0, decimals=3, read_path=["highpass_Hz"],
           set={"verb": "set_filter", "arg": "highpass_Hz"},
           settle={"policy": "echoes", "key": "highpass_Hz"},
           help="0 = off. A high-pass removes the DC level: the loop numbers then "
                "lose their mid level."),
        _p("filter_order", "Filter order", "control", "int", group="Filter", order=3,
           min=1, max=8, step=1, read_path=["filter_order"],
           set={"verb": "set_filter", "arg": "order"},
           settle={"policy": "echoes", "key": "filter_order", "tol": 0.5}),
        _p("loop_x", "Loop X", "control", "enum", group="Loop", order=1,
           options=chans, read_path=["loop_x"], set={"verb": "set_loop", "arg": "x"},
           settle={"policy": "echoes", "key": "loop_x"}),
        _p("loop_y", "Loop Y", "control", "enum", group="Loop", order=2,
           options=chans, read_path=["loop_y"], set={"verb": "set_loop", "arg": "y"},
           settle={"policy": "echoes", "key": "loop_y"}),
        _p("sat_fraction", "Saturation above", "control", "float", group="Loop", order=3,
           min=0.3, max=0.98, decimals=2, step=0.05, read_path=["sat_fraction"],
           set={"verb": "set_analysis", "arg": "sat_fraction"},
           settle={"policy": "echoes", "key": "sat_fraction", "tol": 1e-9},
           help="Fraction of the largest |X| above which the sample counts as "
                "saturated (fits the levels and the background)."),
        _p("subtract_background", "Subtract background", "control", "bool", group="Loop",
           order=4, read_path=["subtract_background"],
           set={"verb": "set_analysis", "arg": "subtract_background"},
           settle={"policy": "echoes", "key": "subtract_background"}),
    ]
    # -- the measurement ------------------------------------------------------------------
    params += [
        _p("acquire", "Acquire", "action", "action", group="Measurement", order=1,
           help="Restart the average, wait for the averaged fresh traces, latch."),
        _p("abort", "Abort acquisition", "action", "action", group="Measurement", order=2,
           help="Allowed for anyone, also a viewer."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement", order=3,
           read_path=["acquiring"]),
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement", order=4,
           min=0, read_path=["acq_id"]),
    ]
    order = 10
    for ch in chans:
        for raw in ((False, True) if cfg.acquisition.keep_raw else (False,)):
            tid = f"{ch}_raw" if raw else ch
            params.append(_p(
                tid, f"{ch.upper()} trace" + (" (unfiltered)" if raw else ""), "indicator",
                "array", unit=unit[ch], group="Measurement", order=order, acquire=acquire,
                dtype="float", shape=["time"], dims=time_dim,
                read={"verb": "get_trace", "key": tid, "args": {"which": "sample"}},
                help=f"{cfg.channel(ch).phys_label or ch.upper()}, averaged over the "
                     f"acquisition" + ("" if raw else ", filtered") + "."))
            order += 1
    for ch in chans:
        for key, what, u in _VALUES:
            params.append(_p(f"{ch}_{key}", f"{ch.upper()} {what}", "indicator", "float",
                             unit=u or unit[ch], group="Measurement", order=order,
                             read_path=["sample", ch, key], acquire=acquire))
            order += 1
    if len(chans) > 1:
        params.append(_p("phase_21", "Phase CH2 - CH1", "indicator", "float", unit="deg",
                         group="Measurement", order=order, decimals=2,
                         read_path=["sample", "phase_21_deg"], acquire=acquire,
                         help="At CH1's fundamental; positive = CH2 leads."))
        order += 1
        ux, uy = unit.get(cfg.analysis.loop_x, ""), unit.get(cfg.analysis.loop_y, "")
        for key, label, kind, help in _LOOP:
            u = {"x": ux, "y": uy, "y/x": f"{uy}/{ux}", "x*y": f"{ux}*{uy}", "": ""}[kind]
            params.append(_p(f"loop_{key}", f"Loop {label}", "indicator", "float", unit=u,
                             group="Loop result", order=order,
                             read_path=["sample", "loop", key], acquire=acquire, help=help))
            order += 1
    # -- live (not scan-safe: the running average now) -------------------------------------
    for ch in chans:
        params.append(_p(f"live_{ch}_amplitude", f"{ch.upper()} amplitude (live)",
                         "indicator", "float", unit=unit[ch], group="Live", order=order,
                         plottable=True, read_path=["live", ch, "amplitude"]))
        order += 1
    params.append(_p("live_loop_hc", "Loop Hc (live)", "indicator", "float",
                     unit=unit.get(cfg.analysis.loop_x, ""), group="Live", order=order,
                     plottable=True, read_path=["live", "loop", "hc"]))
    # -- status ----------------------------------------------------------------------------
    params += [
        _p("connected", "Connected", "indicator", "bool", group="Status", order=1,
           read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Error", "indicator", "string", group="Status", order=3,
           read_path=["hw_error"]),
    ]
    if scope.simulated:
        for i, (name, label, u) in enumerate((("hc_mT", "Coercive field", "mT"),
                                               ("ms_V", "Kerr half-jump", "V"),
                                               ("drive_Hz", "Drive frequency", "Hz"),
                                               ("noise_V", "Noise", "V"))):
            params.append(_p(f"sim_{name}", f"{label} (simulation)", "control", "float",
                             unit=u, group="Simulated bench", order=i + 1,
                             read_path=[f"sim_{name}"],
                             set={"verb": "set_sim", "arg": "value", "extra": {"name": name}},
                             settle={"policy": "echoes", "key": f"sim_{name}", "tol": 1e-9}))
    for d in params:                      # unknown bounds must not appear as NaN
        for k in ("min", "max"):
            if k in d and not (isinstance(d[k], (int, float)) and math.isfinite(d[k])):
                del d[k]
    label = (f"Oscilloscope {scope.caps.get('model', '')}"
             + (" (simulated)" if scope.simulated else ""))
    manifest = {"schema": SCHEMA_VERSION, "module": "scope", "label": label,
                "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
