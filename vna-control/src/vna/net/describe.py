"""describe.py -- the VNA's self-description: what can be shown, driven and scanned.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. Nothing here restates a value
that lives somewhere else; every bound is read from the Analyzer when the
manifest is built.

What makes this manifest different from the others: its main detectors are
ARRAYS. `s` (the selected S-parameter) and `u` = (S - S_ref)/S_ref are complex
traces with their own hardware-swept dimension, `freq`:

  * `dims` names that dimension; its coordinate comes from `get_frequencies`
    (in GHz), read ONCE per scan, not per point. `s` and `u` declare the SAME
    dim name, so the engine gives them one shared coordinate.
  * `read` says how to fetch the value: a COMMAND (`get_trace`), because a
    trace is too big for the status stream. Complex arrives as {"re", "im"}.
  * `acquire` makes the scan trigger a fresh sweep and wait for THAT sweep
    (acq_id) before reading -- a cold read would return the previous point's
    trace and nothing would raise.

Scalar detectors in the same acquire group (dip frequency, dip depth, the field
and angle the sweep saw) cost no extra sweep: one trigger, one wait, several reads.

ACTIONS WITH A `wait` BLOCK. `take_reference` is safe to run from a scan routine
("go to 150 mT at 45 deg, take the reference, then sweep"), and the `wait` block
is how the module says so AND how a caller knows it finished: take the id from
the reply, then wait until status shows that id and not acquiring -- the same
rule as an acquisition, for the same stale-status reason (gotcha #17).
`clear_reference` is immediate. `acquire` / `abort` carry no `wait`: they are
front-panel buttons, and a scan acquires through the detectors' `acquire` block.

What is dynamic: `start`'s maximum is `stop` minus the minimum span, and vice
versa; the `s` label names the S-parameter. Changing any of those changes
`revision`, and every status frame carries it as `describe_rev`, so clients
re-fetch. The simulated-sample group is only present when the backend IS the
simulator: on the real analyser those knobs would change nothing.
"""

from __future__ import annotations

import json
import math
import zlib

from ..analyzer import GEOMETRIES, SAMPLE_LIMITS, SPARAMS, ANGLE_LIMIT_DEG
from ..field import FIELD_SOURCES

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


def build_manifest(vna) -> dict:
    cfg = vna.cfg
    lim = cfg.limits
    slo, shi = vna.start_limits()
    plo, phi = vna.stop_limits()
    simulated = bool(getattr(vna, "simulated", True))
    sparam = cfg.sweep.sparam

    acquire = {
        "group": "sweep",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": cfg.acquisition.timeout_s,
    }
    # The frequency axis of every trace detector: one declaration, shared by name.
    freq_dim = [{"name": "freq", "label": "Frequency", "unit": "GHz",
                 "length": int(cfg.sweep.points),
                 "coord_verb": "get_frequencies", "coord_key": "values_GHz"}]

    def sweep_ctrl(id, label, unit, key, verb, arg, order, lo, hi, *, scale=None,
                   decimals=None, step=None, type="float", tol=1e-6, help=""):
        return _p(id, label, "control", type, unit=unit, group="Sweep", order=order,
                  min=lo, max=hi, scale=scale, decimals=decimals, step=step,
                  read_path=[key], set={"verb": verb, "arg": arg},
                  settle={"policy": "echoes", "key": key, "tol": tol}, help=help)

    def sample_ctrl(name, label, unit, order, decimals, help):
        lo, hi = SAMPLE_LIMITS[name]
        status_key = "dip_set_dB" if name == "dip_dB" else name
        return _p(name, label, "control", "float", unit=unit, group="Sample (simulation)",
                  order=order, min=lo, max=hi, decimals=decimals, read_path=[status_key],
                  set={"verb": "set_sample", "arg": "value", "extra": {"name": name}},
                  settle={"policy": "echoes", "key": status_key, "tol": 1e-9}, help=help)

    params = [
        # -- sweep -------------------------------------------------------------------
        sweep_ctrl("start", "Start", "GHz", "start_Hz", "set_start", "start_Hz", 10,
                   slo / 1e9, shi / 1e9, scale=1e9, decimals=6, step=0.1, tol=1.0),
        sweep_ctrl("stop", "Stop", "GHz", "stop_Hz", "set_stop", "stop_Hz", 20,
                   plo / 1e9, phi / 1e9, scale=1e9, decimals=6, step=0.1, tol=1.0),
        sweep_ctrl("points", "Points", "", "points", "set_points", "points", 30,
                   lim.points_min, lim.points_max, type="int", step=1, tol=0.5,
                   help="Changing it between scan points is refused by the scan engine: "
                        "the trace would stop being rectangular."),
        sweep_ctrl("ifbw", "IF bandwidth", "kHz", "ifbw_Hz", "set_ifbw", "ifbw_Hz", 40,
                   lim.ifbw_min_Hz / 1e3, lim.ifbw_max_Hz / 1e3, scale=1e3, decimals=3,
                   tol=1e-3, help="Narrower = less noise (as sqrt) and a slower sweep."),
        sweep_ctrl("power", "Source power", "dBm", "power_dBm", "set_power", "power_dBm", 50,
                   lim.power_min_dBm, lim.power_max_dBm, decimals=1, step=1.0),
        sweep_ctrl("averages", "Averages", "", "averages", "set_averages", "averages", 60,
                   lim.averages_min, lim.averages_max, type="int", step=1, tol=0.5,
                   help="Sweeps averaged (coherently) per acquisition."),
        _p("sparam", "S-parameter", "control", "enum", group="Sweep", order=65,
           options=list(SPARAMS), read_path=["sparam"],
           set={"verb": "set_sparam", "arg": "sparam"},
           settle={"policy": "echoes", "key": "sparam"},
           help="What is measured. A reference taken with another one no longer "
                "matches (u is refused)."),
        _p("sweep_time", "Sweep time", "indicator", "float", unit="s", group="Sweep",
           order=70, decimals=3, read_path=["sweep_time_s"]),
        _p("continuous", "Continuous sweep", "control", "bool", group="Sweep", order=80,
           read_path=["continuous"], set={"verb": "set_continuous", "arg": "on"},
           settle={"policy": "echoes", "key": "continuous"},
           help="Sweep on its own between acquisitions, like a front panel."),

        # -- measurement: the scan detectors (one acquisition feeds all of them) ------
        _p("s", f"S-parameter ({sparam})", "indicator", "array", group="Measurement",
           order=10, acquire=acquire, dtype="complex", shape=["freq"], dims=freq_dim,
           read={"verb": "get_trace", "key": "s",
                 "args": {"which": "sample", "quantity": "s"}},
           help="Complex S-parameter as measured (raw: line loss and delay included). "
                "Stored as s_real / s_imag."),
        # The label carries the word the old LabVIEW program used ("Permeability,
        # Real/Imag") as well as the formula: this is the detector an FMR
        # measurement is actually after, and it has to be findable by the name
        # it is known by, not only by its definition.
        _p("u", "Permeability u = (S - S_ref)/S_ref", "indicator", "array", group="Measurement",
           order=11, acquire=acquire, dtype="complex", shape=["freq"], dims=freq_dim,
           read={"verb": "get_trace", "key": "u",
                 "args": {"which": "sample", "quantity": "u"}},
           help="The trace relative to the brain's reference: the cables cancel and "
                "only the sample remains. Refused (the scan stops) with no reference "
                "or one taken with another S-parameter or sweep."),
        # The logarithmic form of the same measurement. S = A exp(i eta chi)
        # through a line with a film on it, so ln(S/S_ref) IS the susceptibility
        # up to the constant eta -- exactly, not only for a shallow line, where
        # u ~ ln(1 + u). No prefactor: eta belongs to the waveguide and the
        # film, and a made-up one would produce confident numbers in no units.
        _p("ln_ratio", "ln(S / S_ref)", "indicator", "array", group="Measurement",
           order=12, acquire=acquire, dtype="complex", shape=["freq"], dims=freq_dim,
           read={"verb": "get_trace", "key": "ln",
                 "args": {"which": "sample", "quantity": "ln"}},
           help="Complex logarithm of the trace over the reference: proportional "
                "to the susceptibility at any line depth (u is its small-signal "
                "limit). Principal branch, so the imaginary part wraps at +-pi. "
                "Refused, like u, with no matching reference."),
        _p("dip_freq", "Dip frequency", "indicator", "float", unit="GHz",
           group="Measurement", order=20, decimals=6, scale=1e9,
           read_path=["sample", "dip_Hz"], acquire=acquire,
           help="Deepest point below a fitted baseline. Needs a span much wider "
                "than the line."),
        _p("dip_depth", "Dip depth", "indicator", "float", unit="dB",
           group="Measurement", order=21, decimals=3,
           read_path=["sample", "dip_dB"], acquire=acquire),
        _p("sweep_field", "Field during sweep", "indicator", "float", unit="mT",
           group="Measurement", order=22, decimals=3,
           read_path=["sample", "field_mT"], acquire=acquire,
           help="The field the sample saw (from the magnet service), averaged over "
                "the sweeps."),
        _p("sweep_angle", "Field angle during sweep", "indicator", "float", unit="deg",
           group="Measurement", order=23, decimals=2,
           read_path=["sample", "angle_deg"], acquire=acquire,
           help="The in-plane field angle the sample saw, averaged on the circle."),
        _p("sweep_field_ok", "Field was live", "indicator", "bool",
           group="Measurement", order=24, read_path=["sample", "field_ok"], acquire=acquire),
        _p("acquire", "Acquire trace", "action", "action", group="Measurement", order=1,
           help="Average the next fresh sweeps and latch the result."),
        _p("abort", "Abort acquisition", "action", "action", group="Measurement", order=2),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=3, read_path=["acquiring"]),
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=4, read_path=["acq_id"]),

        # -- reference -----------------------------------------------------------------
        _p("take_reference", "Take reference", "action", "action", group="Reference",
           order=1,
           wait={"target_key": "acq_id",
                 "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                           "flag_key": "acquiring", "invert": True},
                 "timeout_s": cfg.acquisition.timeout_s},
           help="Acquire like `acquire` and keep the result as the reference for u. "
                "Take it where the sample does not resonate in the band."),
        _p("clear_reference", "Clear reference", "action", "action", group="Reference",
           order=2, wait={"ready": {"policy": "immediate"}}),
        _p("reference_present", "Reference present", "indicator", "bool",
           group="Reference", order=10, read_path=["reference", "present"]),
        _p("reference_field", "Reference field", "indicator", "float", unit="mT",
           group="Reference", order=11, decimals=3, read_path=["reference", "field_mT"]),
        _p("reference_angle", "Reference angle", "indicator", "float", unit="deg",
           group="Reference", order=12, decimals=2, read_path=["reference", "angle_deg"]),

        # -- live ------------------------------------------------------------------------
        _p("live_dip_freq", "Dip frequency (live)", "indicator", "float", unit="GHz",
           group="Live", order=10, decimals=6, scale=1e9, plottable=True,
           read_path=["dip_Hz"]),
        _p("live_dip_depth", "Dip depth (live)", "indicator", "float", unit="dB",
           group="Live", order=11, decimals=3, plottable=True, read_path=["dip_dB"]),
        _p("sweeps", "Sweeps", "indicator", "int", group="Live", order=12,
           read_path=["sweeps"]),

        # -- the field (read from a magnet service) ----------------------------------------
        _p("field_source", "Field source", "control", "enum", group="Field",
           order=10, options=list(FIELD_SOURCES), read_path=["field_source_set"],
           set={"verb": "set_field_source", "arg": "source"},
           settle={"policy": "echoes", "key": "field_source_set"},
           help="mag2d = the vector magnet's measured Bx, By; clMag = the 1-axis "
                "magnet's measured field (angle 0); manual = typed in."),
        _p("manual_field", "Manual field", "control", "float", unit="mT",
           group="Field", order=20, decimals=3,
           min=-lim.manual_field_max_mT, max=lim.manual_field_max_mT,
           read_path=["manual_field_mT"],
           set={"verb": "set_manual_field", "arg": "field_mT"},
           settle={"policy": "echoes", "key": "manual_field_mT", "tol": 1e-9},
           help="Used when the source is manual, and as the fallback before "
                "the magnet has been heard."),
        _p("manual_angle", "Manual angle", "control", "float", unit="deg",
           group="Field", order=21, decimals=2, min=-ANGLE_LIMIT_DEG, max=ANGLE_LIMIT_DEG,
           read_path=["manual_angle_deg"],
           set={"verb": "set_manual_angle", "arg": "angle_deg"},
           settle={"policy": "echoes", "key": "manual_angle_deg", "tol": 1e-9}),
        _p("field", "Field", "indicator", "float", unit="mT", group="Field",
           order=30, decimals=3, plottable=True, read_path=["field_mT"]),
        _p("field_angle", "Field angle", "indicator", "float", unit="deg", group="Field",
           order=31, decimals=2, plottable=True, read_path=["angle_deg"]),
        _p("field_ok", "Field is live", "indicator", "bool", group="Field",
           order=32, read_path=["field_ok"]),
        _p("field_in_use", "Field in use", "indicator", "string",
           group="Field", order=33, read_path=["field_source"]),

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
            _p("f_res_model", "Kittel resonance (model)", "indicator", "float", unit="GHz",
               group="Field", order=40, decimals=6, scale=1e9, plottable=True,
               read_path=["f_res_model_Hz"],
               help="The simulator's exact answer at the current field -- the truth "
                    "the measured dip should find."),
            sample_ctrl("ms_mT", "mu0 Ms", "mT", 10, 1, "YIG: ~176 mT."),
            sample_ctrl("gamma_GHz_per_T", "gamma/2pi", "GHz/T", 20, 3, "28.0 for g = 2."),
            sample_ctrl("alpha", "Gilbert damping", "", 30, 6, "Thin YIG films: 1e-4 ... 1e-3."),
            sample_ctrl("dh0_mT", "mu0 dH0", "mT", 40, 3, "Inhomogeneous linewidth (FWHM)."),
            sample_ctrl("h_anis_mT", "Anisotropy field", "mT", 50, 3, "Added to |H|."),
            sample_ctrl("hk_mT", "Uniaxial anisotropy mu0 Hk", "mT", 52, 3,
                        "In-plane uniaxial anisotropy; the line then depends on the "
                        "field angle."),
            sample_ctrl("easy_axis_deg", "Easy axis", "deg", 54, 2,
                        "Direction of the uniaxial easy axis in the film plane."),
            sample_ctrl("dip_dB", "Dip depth at reference", "dB", 60, 2,
                        "Coupling: the depth 50 mT above saturation."),
            _p("geometry", "Geometry", "control", "enum", group="Sample (simulation)", order=70,
               options=list(GEOMETRIES), read_path=["geometry"],
               set={"verb": "set_geometry", "arg": "geometry"},
               settle={"policy": "echoes", "key": "geometry"}),
        ]

    # Bounds that are not known must not appear as NaN.
    for d in params:
        for k in ("min", "max"):
            if k in d and not (isinstance(d[k], (int, float)) and math.isfinite(d[k])):
                del d[k]

    label = ("Vector network analyser (simulated)" if simulated
             else "Keysight PNA-X N5222A")
    manifest = {"schema": SCHEMA_VERSION, "module": "vna", "label": label,
                "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
