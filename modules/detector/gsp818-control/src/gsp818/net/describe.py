"""describe.py -- the spectrum analyser's self-description: what can be shown,
driven and scanned.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. Nothing here restates a value
that lives somewhere else; every bound is read from the analyser when the
manifest is built.

What makes this manifest different from most: its main detectors are ARRAYS.
`power` (the spectrum, dBm) and `norm` (minus the thru reference, dB) are REAL
traces with their own hardware-swept dimension, `freq`:

  * `dims` names that dimension; its coordinate comes from `get_frequencies`
    (in MHz), read ONCE per scan, not per point. Both declare the SAME dim
    name, so the engine gives them one shared coordinate.
  * `read` says how to fetch the value: a COMMAND (`get_trace`), because a
    trace is too big for the status stream. A real array is a plain list.
  * `acquire` makes the scan trigger a fresh acquisition and wait for THAT one
    (acq_id) before reading -- a cold read would return the previous point's
    trace and nothing would raise (gotcha #17).

Scalar detectors in the same acquire group (peak frequency and level, the
noise floor, the overload flag) cost no extra sweep: one trigger, one wait,
several reads.

ACTIONS WITH A `wait` BLOCK. `take_reference` is safe to run from a scan
routine ("thru in, take reference, then sweep the DUT's bias"), and the `wait`
block is how the module says so AND how a caller knows it finished: the id
from the reply, then wait until status shows that id and not acquiring.
`clear_reference` is immediate. `acquire` / `abort` carry no `wait`: they are
front-panel buttons, and a scan acquires through the detectors' `acquire` block.

What is dynamic: start's maximum is stop minus the minimum span (and vice
versa); the centre's range depends on the span and the span's on the centre.
Changing any of those changes `revision`, and every status frame carries it
as `describe_rev`, so clients re-fetch. The simulated-bench group is present
only when the backend IS the simulator: on the real analyser those knobs would
change nothing.
"""

from __future__ import annotations

import json
import math
import zlib

from ..analyzer import BENCH_LIMITS
from ..model import DETECTORS, DUTS

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


def build_manifest(sa) -> dict:
    cfg = sa.cfg
    lim = cfg.limits
    slo, shi = sa.start_limits()
    plo, phi = sa.stop_limits()
    clo, chi = sa.center_limits()
    wlo, whi = sa.span_limits()
    simulated = bool(getattr(sa, "simulated", True))

    acquire = {
        "group": "sweep",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": cfg.acquisition.timeout_s,
    }
    # The frequency axis of every trace detector: one declaration, shared by name.
    freq_dim = [{"name": "freq", "label": "Frequency", "unit": "MHz",
                 "length": int(cfg.sweep.points),
                 "coord_verb": "get_frequencies", "coord_key": "values_MHz"}]

    def ctrl(id, label, unit, echo_key, verb, arg, group, order, lo, hi, *, scale=None,
             decimals=None, step=None, type="float", tol=1e-6, help="", read_key=None):
        return _p(id, label, "control", type, unit=unit, group=group, order=order,
                  min=lo, max=hi, scale=scale, decimals=decimals, step=step,
                  read_path=[read_key or echo_key], set={"verb": verb, "arg": arg},
                  settle={"policy": "echoes", "key": echo_key, "tol": tol}, help=help)

    def switch(id, label, key, verb, group, order, help="", danger=False):
        return _p(id, label, "control", "bool", group=group, order=order, read_path=[key],
                  set={"verb": verb, "arg": "on"}, settle={"policy": "echoes", "key": key},
                  help=help, danger=danger)

    params = [
        # -- frequency -------------------------------------------------------------------
        ctrl("start", "Start", "MHz", "start_Hz", "set_start", "start_Hz", "Frequency", 10,
             slo / 1e6, shi / 1e6, scale=1e6, decimals=6, step=1.0, tol=1.0),
        ctrl("stop", "Stop", "MHz", "stop_Hz", "set_stop", "stop_Hz", "Frequency", 20,
             plo / 1e6, phi / 1e6, scale=1e6, decimals=6, step=1.0, tol=1.0),
        ctrl("center", "Center", "MHz", "center_Hz", "set_center", "center_Hz", "Frequency", 30,
             clo / 1e6, chi / 1e6, scale=1e6, decimals=6, step=1.0, tol=1.0,
             help="Moves start and stop together. The span stays unless it no longer "
                  "fits in the band around the new centre, then it shrinks."),
        ctrl("span", "Span", "MHz", "span_Hz", "set_span", "span_Hz", "Frequency", 40,
             wlo / 1e6, whi / 1e6, scale=1e6, decimals=6, step=1.0, tol=1.0,
             help="Around the current centre, up to where an edge meets the band edge."),
        ctrl("points", "Points", "", "points", "set_points", "points", "Frequency", 50,
             lim.points_min, lim.points_max, type="int", step=1, tol=0.5,
             help="Changing it between scan points is refused by the scan engine: "
                  "the trace would stop being rectangular."),

        # -- bandwidth / amplitude / sweep ----------------------------------------------------
        ctrl("rbw", "RBW", "kHz", "rbw_set_Hz", "set_rbw", "rbw_Hz", "Bandwidth", 10,
             lim.rbw_min_Hz / 1e3, lim.rbw_max_Hz / 1e3, scale=1e3, decimals=3, tol=1e-3,
             read_key="rbw_Hz",
             help="Resolution bandwidth. Setting it switches RBW auto off. The noise "
                  "floor moves 10 dB per decade of RBW; the sweep time as 1/RBW^2."),
        switch("rbw_auto", "RBW auto", "rbw_auto", "set_rbw_auto", "Bandwidth", 11,
               "RBW follows the span (about span/100)."),
        ctrl("vbw", "VBW", "kHz", "vbw_set_Hz", "set_vbw", "vbw_Hz", "Bandwidth", 20,
             lim.vbw_min_Hz / 1e3, lim.vbw_max_Hz / 1e3, scale=1e3, decimals=3, tol=1e-3,
             read_key="vbw_Hz",
             help="Video bandwidth: VBW << RBW smooths the noise (not its mean)."),
        switch("vbw_auto", "VBW auto", "vbw_auto", "set_vbw_auto", "Bandwidth", 21),
        ctrl("ref_level", "Reference level", "dBm", "ref_level_dBm", "set_ref_level",
             "ref_level_dBm", "Amplitude", 10, lim.ref_level_min_dBm, lim.ref_level_max_dBm,
             decimals=2, step=1.0, tol=1e-6,
             help="Top of the screen. With attenuation auto it also sets the attenuator."),
        ctrl("atten", "Input attenuation", "dB", "atten_set_dB", "set_atten", "atten_dB",
             "Amplitude", 20, 0.0, lim.atten_max_dB, decimals=0, step=1.0, tol=0.5,
             read_key="atten_dB",
             help="Whole dB. Each dB lifts the noise floor by a dB; too little "
                  "overloads the mixer (see Overload)."),
        switch("atten_auto", "Attenuation auto", "atten_auto", "set_atten_auto", "Amplitude", 21),
        switch("preamp", "Preamplifier", "preamp", "set_preamp", "Amplitude", 30,
               "20 dB front-end gain: noise floor ~20 dB lower, overloads 20 dB earlier."),
        ctrl("sweep_time", "Sweep time", "s", "sweep_time_set_s", "set_sweep_time",
             "sweep_time_s", "Sweep", 10, lim.sweep_time_min_s, lim.sweep_time_max_s,
             decimals=3, tol=1e-6, read_key="sweep_time_s",
             help="Setting it switches sweep-time auto off. Faster than auto = "
                  "amplitude errors (the RBW filter has no time to settle)."),
        switch("sweep_time_auto", "Sweep time auto", "sweep_time_auto",
               "set_sweep_time_auto", "Sweep", 11),
        _p("detector", "Detector", "control", "enum", group="Sweep", order=20,
           options=list(DETECTORS), read_path=["detector"],
           set={"verb": "set_detector", "arg": "detector"},
           settle={"policy": "echoes", "key": "detector"},
           help="auto = normal above 1 MHz span, pos_peak below. sample can miss "
                "a carrier narrower than a display bin."),
        ctrl("averages", "Averages", "", "averages", "set_averages", "averages", "Sweep", 30,
             lim.averages_min, lim.averages_max, type="int", step=1, tol=0.5,
             help="Sweeps averaged (in linear power) per acquisition."),
        switch("continuous", "Continuous sweep", "continuous", "set_continuous", "Sweep", 40,
               "Sweep on its own between acquisitions, like a front panel."),

        # -- tracking generator ---------------------------------------------------------------
        switch("tg_on", "Tracking generator", "tg_on", "set_tg", "Tracking generator", 10,
               "RF out of GEN OUTPUT, following the sweep (100 kHz - 1.8 GHz). "
               "Read from the instrument at start (left as it is); off when the "
               "service stops.", danger=True),
        ctrl("tg_level", "TG level", "dBm", "tg_level_dBm", "set_tg_level", "level_dBm",
             "Tracking generator", 20, lim.tg_level_min_dBm, lim.tg_level_max_dBm,
             decimals=1, step=1.0, tol=1e-6,
             help="A reference taken at another level no longer normalises."),
        # the SAFETY verb as a button (control.py: a viewer may always send
        # it), so the suite's Control tab offers it to a viewer too
        _p("tg_off", "TG off", "action", "action", group="Tracking generator", order=30,
           help="Switch the tracking generator output off. Allowed for anyone, "
                "also a viewer."),

        # -- measurement: the scan detectors (one acquisition feeds all of them) ------------
        _p("power", "Spectrum", "indicator", "array", unit="dBm", group="Measurement",
           order=10, acquire=acquire, dtype="float", shape=["freq"], dims=freq_dim,
           read={"verb": "get_trace", "key": "power_dBm",
                 "args": {"which": "sample", "quantity": "power"}},
           help="The displayed trace in dBm, power-averaged over `averages` sweeps."),
        _p("norm", "Transmission (trace - thru reference)", "indicator", "array", unit="dB",
           group="Measurement", order=11, acquire=acquire, dtype="float", shape=["freq"],
           dims=freq_dim,
           read={"verb": "get_trace", "key": "norm_dB",
                 "args": {"which": "sample", "quantity": "norm"}},
           help="Scalar network analysis: the DUT's |S21| in dB with the generator "
                "ripple and cables divided out. Refused (the scan stops) with no "
                "reference, the TG off, or a reference of another sweep or TG level."),
        _p("peak_freq", "Peak frequency", "indicator", "float", unit="MHz",
           group="Measurement", order=20, decimals=6, scale=1e6,
           read_path=["sample", "peak_Hz"], acquire=acquire,
           help="The highest point of the averaged trace (peak-search marker)."),
        _p("peak_level", "Peak level", "indicator", "float", unit="dBm",
           group="Measurement", order=21, decimals=2,
           read_path=["sample", "peak_dBm"], acquire=acquire),
        _p("noise_floor", "Noise floor (median)", "indicator", "float", unit="dBm",
           group="Measurement", order=22, decimals=2,
           read_path=["sample", "floor_dBm"], acquire=acquire,
           help="Median of the trace: the floor when most of the span is empty."),
        _p("overload", "Overload", "indicator", "bool", group="Measurement", order=23,
           read_path=["sample", "overload"], acquire=acquire,
           help="The input mixer was compressed during the acquisition: the levels "
                "are too low. Raise the attenuation or the reference level."),
        _p("acquire", "Acquire trace", "action", "action", group="Measurement", order=1,
           help="Average the next fresh sweeps and latch the result."),
        # a SAFETY verb (control.py): a viewer may always cancel an acquisition
        _p("abort", "Abort acquisition", "action", "action", group="Measurement", order=2,
           help="Cancel a running acquisition or reference. Allowed for anyone, "
                "also a viewer."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=3, read_path=["acquiring"]),
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=4, read_path=["acq_id"]),

        # -- reference ------------------------------------------------------------------------
        _p("take_reference", "Take thru reference", "action", "action", group="Reference",
           order=1,
           wait={"target_key": "acq_id",
                 "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                           "flag_key": "acquiring", "invert": True},
                 "timeout_s": cfg.acquisition.timeout_s},
           help="Acquire like `acquire` and keep the result as the reference for "
                "norm. Tracking generator on, a THRU where the device goes."),
        _p("clear_reference", "Clear reference", "action", "action", group="Reference",
           order=2, wait={"ready": {"policy": "immediate"}}),
        _p("reference_present", "Reference present", "indicator", "bool",
           group="Reference", order=10, read_path=["reference", "present"]),
        _p("reference_tg_level", "Reference TG level", "indicator", "float", unit="dBm",
           group="Reference", order=11, decimals=1, read_path=["reference", "tg_level_dBm"]),

        # -- live ---------------------------------------------------------------------------
        _p("live_peak_freq", "Peak frequency (live)", "indicator", "float", unit="MHz",
           group="Live", order=10, decimals=6, scale=1e6, plottable=True,
           read_path=["peak_Hz"]),
        _p("live_peak_level", "Peak level (live)", "indicator", "float", unit="dBm",
           group="Live", order=11, decimals=2, plottable=True, read_path=["peak_dBm"]),
        _p("live_floor", "Noise floor (live)", "indicator", "float", unit="dBm",
           group="Live", order=12, decimals=2, plottable=True, read_path=["floor_dBm"]),
        _p("live_overload", "Overload (live)", "indicator", "bool", group="Live", order=13,
           read_path=["overload"]),
        _p("sweeps", "Sweeps", "indicator", "int", group="Live", order=14,
           read_path=["sweeps"]),
        _p("detector_in_use", "Detector in use", "indicator", "string", group="Live",
           order=15, read_path=["detector_in_use"]),

        # -- status ------------------------------------------------------------------------
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),
    ]

    if simulated:
        def bench_ctrl(name, label, unit, order, decimals, help, scale=None, type="float"):
            lo, hi = BENCH_LIMITS[name]
            if scale:
                lo, hi = lo / scale, hi / scale
            return _p(name, label, "control", type, unit=unit, group="Bench (simulation)",
                      order=order, min=lo, max=hi, decimals=decimals, scale=scale,
                      read_path=[name],
                      set={"verb": "set_bench", "arg": "value", "extra": {"name": name}},
                      settle={"policy": "echoes", "key": name, "tol": 1e-9}, help=help)
        params += [
            _p("dut", "Device under test", "control", "enum", group="Bench (simulation)",
               order=10, options=list(DUTS), read_path=["dut"],
               set={"verb": "set_dut", "arg": "dut"}, settle={"policy": "echoes", "key": "dut"},
               help="What sits between GEN OUTPUT and RF INPUT: take the reference "
                    "with thru, then switch to the filter."),
            bench_ctrl("dut_center_Hz", "DUT centre / cut-off", "MHz", 20, 3,
                       "Bandpass centre, or lowpass 3 dB cut-off.", scale=1e6),
            bench_ctrl("dut_bw_Hz", "DUT bandwidth", "MHz", 30, 3, "Bandpass 3 dB width.",
                       scale=1e6),
            bench_ctrl("dut_order", "DUT order", "", 40, 0, "Butterworth order.", type="int"),
            bench_ctrl("dut_loss_dB", "DUT insertion loss", "dB", 50, 2, "In the passband."),
            bench_ctrl("dut_isolation_dB", "DUT isolation", "dB", 60, 1,
                       "Where the stop band bottoms out."),
            bench_ctrl("cable_loss_dB_at_1GHz", "Cable loss at 1 GHz", "dB", 70, 2,
                       "Rises as sqrt(f)."),
            bench_ctrl("tg_ripple_dB", "TG flatness", "dB", 80, 2,
                       "Ripple of the generator output; a thru reference divides it out."),
        ]

    # Bounds that are not known must not appear as NaN.
    for d in params:
        for k in ("min", "max"):
            if k in d and not (isinstance(d[k], (int, float)) and math.isfinite(d[k])):
                del d[k]

    from ..backends import ANALYSER_NAME
    label = f"Spectrum analyser {ANALYSER_NAME}" + (" (simulated)" if simulated else "")
    manifest = {"schema": SCHEMA_VERSION, "module": "gsp818", "label": label,
                "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
