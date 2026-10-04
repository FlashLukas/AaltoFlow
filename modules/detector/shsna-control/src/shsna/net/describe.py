"""describe.py -- the scalar network analyser's self-description: what can be
shown, driven and scanned.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. Nothing here restates a value
that lives somewhere else; every bound is read from the Analyzer when the
manifest is built.

ARRAY DETECTORS. `transmission` (|S21| in dB against the thru reference) and
`raw` (what the analyser measured, in dB relative to the TG output -- the
unit the TG44A reports) are traces with their own
hardware-swept dimension, `freq`:

  * `dims` names it; its coordinate comes from `get_frequencies` (in MHz),
    read ONCE per scan. The grid is the ANALYSER's -- the reference's grid when
    one was taken for this band -- so a scan should take the reference (or
    acquire once) before it starts; `get_frequencies` refuses otherwise rather
    than let a scan file its data on a guess.
  * `read` says how to fetch the value: a COMMAND (`get_trace`), because a
    trace is too big for the status stream.
  * `acquire` makes the scan trigger a fresh TG sweep and wait for THAT sweep
    (target_key acq_id) before reading -- a cold read would return the
    previous point's trace and nothing would raise.

  * `window` (2026-09-28) says the detector can be acquired over a WINDOW of
    its own dimension: `{"arg": "window", "unit": "bin", "min_bins": 11}`
    means the `acquire` trigger takes `window: [i0, i1]`, INCLUSIVE bin
    indices of the freq coordinate, and only those bins are swept. The value
    read afterwards is still FULL-LENGTH, null outside the window (the reply
    also carries `window`). scan-core uses it for FMR in field: it predicts
    the line from the field and sweeps a window around it, filling the rest
    itself and saving a measured-mask.

SCALAR DETECTORS (peak transmission, where it is, band-averaged transmission,
-3 dB bandwidth, raw peak) share the same acquire group -- one trigger, one
wait, several reads -- and are FETCHED too (`get_result`), not read from
status: a failed acquisition then raises at the read, with its reason, instead
of handing the scan a stale or empty number.

ACTIONS WITH A `wait` BLOCK. `take_reference` is safe to run from a scan
routine; the `wait` block says how a caller knows it finished (the id from the
reply, then status showing that id and not acquiring -- gotcha #17) and the
`check` says whether it SUCCEEDED (`acq_error` == ""), so a failed thru makes
the routine raise instead of the scan dividing by nothing. `clear_reference`
is immediate. `acquire` / `abort` carry no `wait`: they are front-panel
buttons; a scan acquires through the detectors' `acquire` block.

What is dynamic: `start`'s maximum is `stop` minus the minimum span and vice
versa, and the freq dim's length is known only once the grid is. Changing
those changes `revision`, and every status frame carries it as
`describe_rev`, so clients re-fetch. The simulation group is present only when
the backend IS the simulator.
"""

from __future__ import annotations

import json
import math
import zlib

from ..analyzer import TG_MODES, WINDOW_MIN_BINS

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, acquire=None, help="", **extra):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract.
    `extra` carries the array-detector fields (dtype, shape, dims), `read`,
    and an action's `wait` block."""
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


def build_manifest(shsna) -> dict:
    cfg = shsna.cfg
    lim = cfg.limits
    slo, shi = shsna.start_limits()
    plo, phi = shsna.stop_limits()
    simulated = bool(getattr(shsna, "simulated", True))

    acquire = {
        "group": "sweep",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": cfg.acquisition.timeout_s,
    }
    # The frequency axis of both trace detectors: one declaration, shared by
    # name, so the engine gives them ONE coordinate.
    dim = {"name": "freq", "label": "Frequency", "unit": "MHz",
           "coord_verb": "get_frequencies", "coord_key": "values_MHz"}
    n = shsna.grid_points() if hasattr(shsna, "grid_points") else None
    if n:
        dim["length"] = int(n)
    freq_dim = [dim]

    # the windowed-acquisition contract (see the module docstring)
    window = {"arg": "window", "unit": "bin", "min_bins": WINDOW_MIN_BINS}

    def sweep_ctrl(id, label, unit, key, verb, arg, order, lo, hi, *, scale=None,
                   decimals=None, step=None, type="float", tol=1e-6, help=""):
        return _p(id, label, "control", type, unit=unit, group="Sweep", order=order,
                  min=lo, max=hi, scale=scale, decimals=decimals, step=step,
                  read_path=[key], set={"verb": verb, "arg": arg},
                  settle={"policy": "echoes", "key": key, "tol": tol}, help=help)

    def result(id, label, unit, key, order, *, quantity="transmission", scale=None,
               decimals=3, help=""):
        return _p(id, label, "indicator", "float", unit=unit, group="Measurement",
                  order=order, decimals=decimals, scale=scale, acquire=acquire,
                  read={"verb": "get_result", "key": key,
                        "args": {"quantity": quantity, "source": "sample"}}, help=help)

    params = [
        # -- sweep -------------------------------------------------------------------
        sweep_ctrl("start", "Start", "MHz", "start_Hz", "set_start", "start_Hz", 10,
                   slo / 1e6, shi / 1e6, scale=1e6, decimals=6, step=10.0, tol=1.0),
        sweep_ctrl("stop", "Stop", "MHz", "stop_Hz", "set_stop", "stop_Hz", 20,
                   plo / 1e6, phi / 1e6, scale=1e6, decimals=6, step=10.0, tol=1.0),
        sweep_ctrl("points", "Points", "", "points", "set_points", "points", 30,
                   lim.points_min, lim.points_max, type="int", step=1, tol=0.5,
                   help="Asked for; the analyser has the last word on its grid (the SA "
                        "API allows at most 1001). A reference taken with another count "
                        "no longer matches."),
        sweep_ctrl("rbw", "RBW", "kHz", "rbw_Hz", "set_rbw", "rbw_Hz", 40,
                   0.0, lim.rbw_max_Hz / 1e3, scale=1e3, decimals=3, tol=1e-3,
                   help="0 = the analyser's default for TG sweeps. Narrower = lower "
                        "floor (more dynamic range), slower."),
        sweep_ctrl("averages", "Averages", "", "averages", "set_averages", "averages", 50,
                   lim.averages_min, lim.averages_max, type="int", step=1, tol=0.5,
                   help="Sweeps averaged in linear power per acquisition."),
        _p("sweep_time", "Sweep time (estimate)", "indicator", "float", unit="s",
           group="Sweep", order=60, decimals=3, read_path=["sweep_time_s"]),
        _p("continuous", "Continuous sweep", "control", "bool", group="Sweep", order=70,
           read_path=["continuous"], set={"verb": "set_continuous", "arg": "on"},
           settle={"policy": "echoes", "key": "continuous"},
           help="Sweep on its own between acquisitions. Each TG sweep pauses the "
                "analyser's spectrum display and any signal-generator output."),

        # -- measurement: the scan detectors (one acquisition feeds all of them) ------
        _p("transmission", "Transmission |S21|", "indicator", "array", unit="dB",
           group="Measurement", order=10, acquire=acquire, dtype="float",
           shape=["freq"], dims=freq_dim, window=window,
           read={"verb": "get_trace", "key": "transmission",
                 "args": {"which": "transmission", "source": "sample"}},
           help="Measured minus the thru reference, per frequency. Refused "
                "(the scan stops) with no reference or one taken on another grid."),
        _p("raw", "Measured (rel. TG output)", "indicator", "array", unit="dB",
           group="Measurement", order=11, acquire=acquire, dtype="float",
           shape=["freq"], dims=freq_dim, window=window,
           read={"verb": "get_trace", "key": "raw", "args": {"which": "raw", "source": "sample"}},
           help="What the analyser measured, in dB relative to the TG output, "
                "before the reference (TG ripple, cables and the pad included)."),
        result("peak_transmission", "Peak transmission", "dB", "peak_transmission_db", 20),
        result("peak_freq", "Peak frequency", "MHz", "peak_freq_hz", 21, scale=1e6, decimals=4),
        result("mean_transmission", "Mean transmission", "dB", "mean_transmission_db", 22,
               help="Band-averaged POWER transmission, 10 log10 of the mean ratio."),
        result("bw3", "-3 dB bandwidth", "MHz", "bw3_hz", 23, scale=1e6, decimals=4,
               help="Width around the peak within 3 dB of it; empty when that "
                    "region reaches the edge of the sweep."),
        result("raw_peak", "Peak measured (rel. TG output)", "dB", "peak_db", 24, quantity="raw",
               help="Needs no reference."),
        _p("acquire", "Acquire", "action", "action", group="Measurement", order=1,
           help="Run one TG acquisition and latch it."),
        _p("abort", "Abort acquisition", "action", "action", group="Measurement", order=2),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=3, read_path=["acquiring"]),
        # A counter that starts at 0 and only counts up: min 0 is a promise
        # the brain keeps (scan-core stores an int by its declared range).
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=4, min=0, read_path=["acq_id"]),
        _p("acq_error", "Acquisition error", "indicator", "string", group="Measurement",
           order=5, read_path=["acq_error"],
           help="Empty when the last acquisition is a measurement; else why not."),

        # -- reference -----------------------------------------------------------------
        _p("take_reference", "Take thru reference", "action", "action", group="Reference",
           order=1,
           wait={"target_key": "acq_id",
                 "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                           "flag_key": "acquiring", "invert": True},
                 "timeout_s": cfg.acquisition.timeout_s,
                 "check": {"key": "acq_error", "equals": ""}},
           help="Acquire like `acquire` with the DUT replaced by a thru, and keep "
                "the result as the reference for transmission."),
        _p("clear_reference", "Clear reference", "action", "action", group="Reference",
           order=2, wait={"ready": {"policy": "immediate"}}),
        _p("reference_present", "Reference present", "indicator", "bool",
           group="Reference", order=10, read_path=["reference", "present"]),
        # 0 while there is no reference, else the bins of the trace it holds
        _p("reference_points", "Reference points", "indicator", "int",
           group="Reference", order=12, min=0, read_path=["reference", "points"]),

        # -- live ------------------------------------------------------------------------
        _p("live_peak", "Peak measured (live)", "indicator", "float", unit="dB",
           group="Live", order=10, decimals=2, plottable=True, read_path=["last_peak_db"],
           help="Relative to the TG output, before the reference."),
        _p("live_peak_transmission", "Peak transmission (live)", "indicator", "float",
           unit="dB", group="Live", order=11, decimals=2, plottable=True,
           read_path=["last_peak_transmission_db"]),
        _p("sweeps", "Sweeps", "indicator", "int", group="Live", order=12,
           min=0, read_path=["sweeps"], help="Completed sweeps since start."),

        # -- the owner of the analyser -----------------------------------------------------
        _p("owner", "Analyser service", "indicator", "string", group="Analyser", order=1,
           read_path=["owner", "address"],
           help="The signalhound service this module sends its TG sweeps to."),
        _p("owner_reachable", "Service reachable", "indicator", "bool", group="Analyser",
           order=2, read_path=["owner", "reachable"]),
        _p("tg_attached", "TG attached", "indicator", "bool", group="Analyser",
           order=3, read_path=["owner", "tg_attached"]),
        # An ENUM of the owner's TG modes (scan-core stores it as a code); the
        # brain reports None, never "", while the owner has not said one.
        _p("tg_mode", "TG mode", "indicator", "enum", group="Analyser", order=4,
           options=list(TG_MODES), read_path=["owner", "tg_mode"]),

        # -- status ------------------------------------------------------------------------
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),
    ]

    if simulated:
        params += [
            _p("dut_inserted", "DUT inserted", "control", "bool", group="Simulation",
               order=10, read_path=["sim_dut_inserted"],
               set={"verb": "set_sim", "arg": "value", "extra": {"name": "dut_inserted"}},
               settle={"policy": "echoes", "key": "sim_dut_inserted"},
               help="False = the thru a reference is taken with."),
            _p("sim_tg_attached", "TG attached (sim)", "control", "bool", group="Simulation",
               order=11, read_path=["sim_tg_attached"],
               set={"verb": "set_sim", "arg": "value", "extra": {"name": "tg_attached"}},
               settle={"policy": "echoes", "key": "sim_tg_attached"}),
            _p("pad", "Attenuator", "control", "float", unit="dB", group="Simulation",
               order=12, min=0.0, max=80.0, decimals=1, read_path=["sim_pad_dB"],
               set={"verb": "set_sim", "arg": "value", "extra": {"name": "pad_dB"}},
               settle={"policy": "echoes", "key": "sim_pad_dB", "tol": 1e-9},
               help="The fixed pad between TG and analyser (20 dB on the bench)."),
            _p("fmr_on", "Magnetic film (FMR)", "control", "bool", group="Simulation",
               order=20, read_path=["sim_fmr_on"],
               set={"verb": "set_sim", "arg": "value", "extra": {"name": "fmr_on"}},
               settle={"policy": "echoes", "key": "sim_fmr_on"},
               help="The DUT becomes a waveguide with a magnetic film that absorbs "
                    "at the Kittel frequency of the field it sits in."),
        ]
        if cfg.sim.fmr_on:
            # Present only while the film is on (the manifest's SHAPE follows
            # the mode, like hf2's reference; describe_rev moves with it).
            params += [
                _p("sim_field", "Film field (manual)", "control", "float", unit="mT",
                   group="Simulation", order=21, min=-5000.0, max=5000.0, decimals=3,
                   read_path=["sim_manual_field_mT"],
                   set={"verb": "set_sim", "arg": "value", "extra": {"name": "manual_field_mT"}},
                   settle={"policy": "echoes", "key": "sim_manual_field_mT", "tol": 1e-9},
                   help="Used when the field source is 'manual' (and as the fallback "
                        "before a magnet is heard)."),
                _p("sim_angle", "Film field angle (manual)", "control", "float", unit="deg",
                   group="Simulation", order=22, min=-360.0, max=360.0, decimals=2,
                   read_path=["sim_manual_angle_deg"],
                   set={"verb": "set_sim", "arg": "value", "extra": {"name": "manual_angle_deg"}},
                   settle={"policy": "echoes", "key": "sim_manual_angle_deg", "tol": 1e-9}),
                _p("sim_field_in_use", "Film field (in use)", "indicator", "float", unit="mT",
                   group="Simulation", order=23, decimals=3, plottable=True,
                   read_path=["sim_field_mT"]),
                _p("sim_angle_in_use", "Film field angle (in use)", "indicator", "float",
                   unit="deg", group="Simulation", order=24, decimals=2,
                   read_path=["sim_angle_deg"]),
                _p("sim_fres", "Kittel frequency (sim)", "indicator", "float", unit="MHz",
                   group="Simulation", order=25, scale=1e6, decimals=3, plottable=True,
                   read_path=["sim_fres_Hz"],
                   help="Where the simulated film absorbs now; empty when there is no "
                        "line (out-of-plane below saturation)."),
                _p("sim_field_source", "Film field from", "indicator", "string",
                   group="Simulation", order=26, read_path=["sim_field_source"]),
            ]

    # Bounds that are not known must not appear as NaN.
    for d in params:
        for k in ("min", "max"):
            if k in d and not (isinstance(d[k], (int, float)) and math.isfinite(d[k])):
                del d[k]

    label = ("Scalar network analyser (simulated)" if simulated
             else "Scalar network analyser (Signal Hound TG44A via signalhound)")
    manifest = {"schema": SCHEMA_VERSION, "module": "shsna", "label": label,
                "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
