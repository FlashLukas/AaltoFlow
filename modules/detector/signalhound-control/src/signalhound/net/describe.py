"""describe.py -- the spectrum analyser's self-description: what can be shown,
driven and scanned.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. Nothing here restates a value
that lives somewhere else; every bound is read from the brain when the
manifest is built.

Its main detector is an ARRAY with a hardware-swept dimension, `freq`:
  * `trace`        -- power per bin in dBm (a real array: dtype "float").
  * `dims` names the dimension; its coordinate comes from `get_frequencies`
    (in GHz), read ONCE per scan.
  * `read` says how to fetch the value: a COMMAND (`get_trace`), because a
    trace is too big for the status stream.
  * `acquire` makes the scan trigger fresh sweeps and wait for THAT
    acquisition (acq_id) before reading (gotcha #17) -- a cold read would
    return the previous point's trace and nothing would raise.
Scalar detectors in the same acquire group (peak frequency and level, noise
floor, overload) cost no extra sweep.

THE TRACKING GENERATOR appears here only as INDICATORS (what it is doing:
unknown, parked -- it has no off --, a CW for shsg, or a TG sweep for shsna). Its verbs -- tg_cw,
tg_sweep_acquire, get_tg_trace, tg_abort -- are a contract with the two
CLIENT MODULES, not scan controls: a scan drives the TG through shsg / shsna,
whose describe offers the scannable controls and detectors.

What is dynamic: the frequency envelope follows the connected MODEL; centre
bounds span and vice versa; RBW's maximum follows the model and bounds VBW;
the `freq` dimension's length is the analyser's grid.
Any change moves `revision`, and every status frame carries it as
`describe_rev`. The simulated-scene group is present only when the backend IS
the simulator.
"""

from __future__ import annotations

import json
import math
import zlib

from ..instruments import DETECTORS
from ..spectrum import TG_MODES


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


def build_manifest(signalhound) -> dict:
    v = signalhound
    cfg = v.cfg
    lim = cfg.limits
    st = v.status()
    clo, chi = v.center_limits()
    slo, shi = v.span_limits()
    flo, fhi = v.freq_range()          # start / stop live in the whole range
    simulated = bool(getattr(v, "simulated", True))

    acquire = {
        "group": "sweep",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": cfg.acquisition.timeout_s,
    }
    # The frequency axis of the trace detector: one declaration, shared by name.
    freq_dim = [{"name": "freq", "label": "Frequency", "unit": "GHz",
                 "length": int(st.points),
                 "coord_verb": "get_frequencies", "coord_key": "values_GHz"}]

    def ctrl(id, label, unit, key, verb, arg, order, lo, hi, *, group="Sweep", scale=None,
             decimals=None, step=None, type="float", tol=1e-6, settle=None, help=""):
        return _p(id, label, "control", type, unit=unit, group=group, order=order,
                  min=lo, max=hi, scale=scale, decimals=decimals, step=step,
                  read_path=[key], set={"verb": verb, "arg": arg},
                  settle=settle or {"policy": "echoes", "key": key, "tol": tol}, help=help)

    def flag(id, label, key, verb, group, order, help=""):
        return _p(id, label, "control", "bool", group=group, order=order, read_path=[key],
                  set={"verb": verb, "arg": "on"}, settle={"policy": "echoes", "key": key},
                  help=help)

    params = [
        # -- sweep ---------------------------------------------------------------------
        ctrl("center", "Centre", "GHz", "center_Hz", "set_center", "center_Hz", 10,
             clo / 1e9, chi / 1e9, scale=1e9, decimals=9, step=0.001, tol=1.0,
             help="Moving it may narrow the span to stay inside the analyser's range."),
        # start / stop: the same sweep said by its ends (the other end is held)
        ctrl("start", "Start", "GHz", "start_Hz", "set_start", "start_Hz", 12,
             flo / 1e9, fhi / 1e9, scale=1e9, decimals=9, step=0.001, tol=1.0,
             help="The low end of the sweep; the stop frequency stays where it is. "
                  "Must stay below the stop."),
        ctrl("stop", "Stop", "GHz", "stop_Hz", "set_stop", "stop_Hz", 14,
             flo / 1e9, fhi / 1e9, scale=1e9, decimals=9, step=0.001, tol=1.0,
             help="The high end of the sweep; the start frequency stays where it is. "
                  "Must stay above the start."),
        ctrl("span", "Span", "MHz", "span_Hz", "set_span", "span_Hz", 20,
             slo / 1e6, shi / 1e6, scale=1e6, decimals=6, step=1.0, tol=1.0,
             help="The widest span depends on the centre. The number of bins "
                  "follows from span and RBW, so a span change between scan points "
                  "makes the trace ragged (the scan engine refuses)."),
        ctrl("ref_level", "Reference level", "dBm", "ref_level_dBm", "set_ref_level",
             "ref_level_dBm", 30, lim.ref_min_dBm, lim.ref_max_dBm, decimals=1, step=1.0,
             help="Top of the screen. The analyser sets its gain/attenuation from it: "
                  "keep it just above the strongest signal (lower = lower noise floor, "
                  "too low = OVERLOAD)."),
        ctrl("rbw", "RBW", "kHz", "rbw_Hz", "set_rbw", "rbw_Hz", 40,
             lim.rbw_min_Hz / 1e3, v.rbw_max() / 1e3, scale=1e3, decimals=4,
             # NOT "echoes": above 100 kHz the analyser only has 250 kHz (and
             # 6 MHz), so a scan point at e.g. 150 kHz is SNAPPED and its echo
             # would never match -- the scan would sit out the whole timeout.
             # The setter stores the (snapped) value before it replies, so the
             # status is already current when the reply arrives: nothing to wait
             # for. The RBW actually used travels with every trace (rbw_Hz).
             settle={"policy": "immediate"},
             help="Resolution bandwidth. Continuous up to 100 kHz, then 250 kHz (and "
                  "6 MHz on the SA124B). Narrower = lower floor (10 dB per decade) and "
                  "a much slower sweep."),
        ctrl("vbw", "VBW", "kHz", "vbw_Hz", "set_vbw", "vbw_Hz", 50,
             lim.rbw_min_Hz / 1e3, cfg.sweep.rbw_Hz / 1e3, scale=1e3, decimals=4, tol=1e-3,
             help="Video bandwidth, at most the RBW: smooths the noise, does not "
                  "lower the floor."),
        _p("detector", "Detector", "control", "enum", group="Sweep", order=55,
           options=list(DETECTORS), read_path=["detector"],
           set={"verb": "set_detector", "arg": "detector"},
           settle={"policy": "echoes", "key": "detector"},
           help="average: the power in the bin; peak: the largest value in the bin "
                "(never misses a narrow tone, reads noise a few dB high)."),
        flag("reject", "Image rejection", "reject", "set_reject", "Sweep", 57,
             "Software image rejection: on for steady signals, off to catch short bursts."),
        ctrl("averages", "Averages", "", "averages", "set_averages", "averages", 60,
             lim.averages_min, lim.averages_max, type="int", step=1, tol=0.5,
             help="Sweeps averaged (in power) per acquisition."),
        flag("continuous", "Continuous sweep", "continuous", "set_continuous", "Sweep", 80,
             "Sweep on its own between acquisitions, like a front panel."),
        # Counts below: whole numbers that cannot go negative (0 = no grid /
        # nothing yet). min 0 is the promise scan-core stores them by; no max,
        # because nothing in the code caps them.
        _p("points", "Bins", "indicator", "int", group="Sweep", order=90, min=0,
           read_path=["points"], help="Chosen by the analyser from span and RBW."),
        _p("bin_width", "Bin width", "indicator", "float", unit="kHz", group="Sweep",
           order=91, decimals=4, scale=1e3, read_path=["bin_Hz"]),
        _p("sweep_time", "Sweep time", "indicator", "float", unit="s", group="Sweep",
           order=92, decimals=3, read_path=["sweep_time_s"]),

        # -- measurement: the scan detectors (one acquisition feeds all of them) ------------
        _p("trace", "Spectrum trace", "indicator", "array", unit="dBm", group="Measurement",
           order=10, acquire=acquire, dtype="float", shape=["freq"], dims=freq_dim,
           read={"verb": "get_trace", "key": "trace",
                 "args": {"which": "sample", "quantity": "trace"}},
           help="Power per bin, averaged in power over the acquisition's sweeps."),
        _p("peak_freq", "Peak frequency", "indicator", "float", unit="GHz",
           group="Measurement", order=20, decimals=9, scale=1e9,
           read_path=["sample", "peak_Hz"], acquire=acquire),
        _p("peak_level", "Peak level", "indicator", "float", unit="dBm",
           group="Measurement", order=21, decimals=2,
           read_path=["sample", "peak_dBm"], acquire=acquire),
        _p("noise_floor", "Noise floor (median)", "indicator", "float", unit="dBm",
           group="Measurement", order=22, decimals=2,
           read_path=["sample", "floor_dBm"], acquire=acquire),
        _p("overloaded", "Overloaded", "indicator", "bool", group="Measurement", order=24,
           read_path=["sample", "overload"], acquire=acquire,
           help="The input compressed during the acquisition: the levels are wrong."),
        _p("acquire", "Acquire trace", "action", "action", group="Measurement", order=1,
           help="Average the next fresh sweeps and latch the result."),
        _p("abort", "Abort acquisition", "action", "action", group="Measurement", order=2),
        # a SAFETY verb (control.py): a viewer may always cancel a TG sweep
        _p("tg_abort", "Abort TG sweep", "action", "action", group="Measurement",
           order=2.5, help="Cancel a running tracking-generator sweep. Allowed for "
                           "anyone, also a viewer."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=3, read_path=["acquiring"]),
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=4, min=0, read_path=["acq_id"]),

        # -- the tracking generator: what it is doing (indicators only) --------------
        # No controls here on purpose: shsg (CW) and shsna (TG sweeps) drive
        # it through the TG contract and offer the scan controls themselves.
        _p("tg_attached", "TG attached", "indicator", "bool", group="Tracking generator",
           order=1, read_path=["tg_attached"]),
        # An ENUM of spectrum.TG_MODES (the brain computes exactly one of them
        # in status()), so scan-core can record it as a code.
        _p("tg_mode", "TG mode", "indicator", "enum", group="Tracking generator", order=2,
           options=list(TG_MODES), read_path=["tg_mode"],
           help="unknown (not readable at start; it may be emitting what another program "
                "left on), parked (the TG44A has no off: parked at the park frequency and "
                "level), cw (a CW source for shsg) or sweep (a TG sweep for shsna -- "
                "spectrum sweeping pauses)."),
        _p("tg_cw_on", "TG CW on", "indicator", "bool", group="Tracking generator", order=3,
           read_path=["tg_cw_on"]),
        _p("tg_cw_freq", "TG CW frequency", "indicator", "float", unit="GHz",
           group="Tracking generator", order=4, decimals=9, scale=1e9,
           read_path=["tg_cw_freq_hz"]),
        _p("tg_cw_level", "TG CW level", "indicator", "float", unit="dBm",
           group="Tracking generator", order=5, decimals=1, read_path=["tg_cw_level_dbm"]),
        _p("tg_park", "TG park frequency", "indicator", "float", unit="GHz",
           group="Tracking generator", order=5.5, decimals=9, scale=1e9,
           read_path=["tg_park_hz"], help="Where 'off' parks the TG (it has no off)."),
        _p("tg_acquiring", "TG sweep running", "indicator", "bool",
           group="Tracking generator", order=6, read_path=["tg_acquiring"]),
        _p("spectrum_paused", "Spectrum paused", "indicator", "string",
           group="Tracking generator", order=7, read_path=["spectrum_paused"],
           help="Why spectrum sweeping is on hold (a TG sweep, or a CW this analyser "
                "cannot sweep with); empty when it is not."),

        # -- live --------------------------------------------------------------------------------
        _p("live_peak_freq", "Peak frequency (live)", "indicator", "float", unit="GHz",
           group="Live", order=10, decimals=9, scale=1e9, plottable=True,
           read_path=["peak_Hz"]),
        _p("live_peak_level", "Peak level (live)", "indicator", "float", unit="dBm",
           group="Live", order=11, decimals=2, plottable=True, read_path=["peak_dBm"]),
        _p("live_floor", "Noise floor (live)", "indicator", "float", unit="dBm",
           group="Live", order=12, decimals=2, plottable=True, read_path=["floor_dBm"]),
        _p("overload", "Overload (live)", "indicator", "bool", group="Live", order=13,
           read_path=["overload"]),
        _p("sweeps", "Sweeps", "indicator", "int", group="Live", order=14, min=0,
           read_path=["sweeps"]),

        # -- status ------------------------------------------------------------------------------
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("model", "Model", "indicator", "string", group="Status", order=3,
           read_path=["device_model"]),
        _p("freq_min", "Lowest frequency", "indicator", "float", unit="GHz", group="Status",
           order=4, decimals=9, scale=1e9, read_path=["freq_min_Hz"]),
        _p("freq_max", "Highest frequency", "indicator", "float", unit="GHz", group="Status",
           order=5, decimals=6, scale=1e9, read_path=["freq_max_Hz"]),
        _p("hw_error", "Error", "indicator", "string", group="Status",
           order=6, read_path=["hw_error"]),
    ]

    if simulated:
        from ..spectrum import SCENE_LIMITS

        def scene(name, label, unit, order, *, scale=None, decimals=3, type="float", help=""):
            lo, hi = SCENE_LIMITS[name]
            return _p(name, label, "control", type, unit=unit, group="Scene (simulation)",
                      order=order, min=lo / (scale or 1), max=hi / (scale or 1), scale=scale,
                      decimals=decimals, read_path=["scene", name],
                      set={"verb": "set_scene", "arg": "value", "extra": {"name": name}},
                      settle={"policy": "echoes", "key": ["scene", name], "tol": 1e-6},
                      help=help)

        def scene_flag(name, label, order, help=""):
            return _p(name, label, "control", "bool", group="Scene (simulation)", order=order,
                      read_path=["scene", name],
                      set={"verb": "set_scene", "arg": "value", "extra": {"name": name}},
                      settle={"policy": "echoes", "key": ["scene", name]}, help=help)

        params += [
            scene_flag("tone_on", "Generator on", 10),
            scene("tone_Hz", "Generator frequency", "GHz", 11, scale=1e9, decimals=9),
            scene("tone_dBm", "Generator level", "dBm", 12, decimals=1),
            scene("harmonic_dBc", "2nd harmonic", "dBc", 13, decimals=1,
                  help="The 3rd harmonic is 10 dB below it."),
            scene("danl_dBm_per_Hz", "DANL", "dBm/Hz", 14, decimals=1,
                  help="Displayed average noise level at a low reference level."),
            scene_flag("dut_inserted", "Filter inserted", 20,
                       "Off = a thru: what shsna takes its reference with."),
            scene("dut_center_Hz", "Filter centre", "GHz", 21, scale=1e9, decimals=6),
            scene("dut_bandwidth_Hz", "Filter bandwidth", "MHz", 22, scale=1e6, decimals=3),
            scene("dut_order", "Filter order", "", 23, type="int", decimals=0),
            scene("dut_loss_dB", "Filter insertion loss", "dB", 24, decimals=2),
            scene("cable_loss_dB_at_1GHz", "Cable loss at 1 GHz", "dB", 25, decimals=2),
            scene("tg_ripple_dB", "TG flatness ripple", "dB", 26, decimals=2),
        ]

    # Bounds that are not known must not appear as NaN.
    for d in params:
        for k in ("min", "max"):
            if k in d and not (isinstance(d[k], (int, float)) and math.isfinite(d[k])):
                del d[k]

    from ..backends import analyser_name
    label = (f"Spectrum analyser (simulated {st.device_model})" if simulated
             else analyser_name(st.device_model))
    manifest = {"schema": SCHEMA_VERSION, "module": "signalhound", "label": label,
                "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
