"""describe.py -- the VNA's self-description: what can be shown, driven and scanned.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. Nothing here restates a value
that lives somewhere else; every bound is read from the Analyzer when the
manifest is built.

What makes this manifest different from the others: its main detectors are
ARRAYS. `s_<sp>` (an S-parameter, e.g. s_s21) and `u_<sp>` = (S - S_ref)/S_ref
are complex traces with their own hardware-swept dimension, `freq`:

  * `dims` names that dimension; its coordinate comes from `get_frequencies`
    (in GHz), read ONCE per scan, not per point. Every trace declares the SAME
    dim name, so the engine gives them one shared coordinate.
  * `read` says how to fetch the value: a COMMAND (`get_trace`), because a
    trace is too big for the status stream. Complex arrives as {"re", "im"}.
  * `acquire` makes the scan trigger a fresh sweep and wait for THAT sweep
    (acq_id) before reading -- a cold read would return the previous point's
    trace and nothing would raise.

Scalar detectors in the same acquire group (dip frequency, dip depth, the field
and angle the sweep saw) cost no extra sweep: one trigger, one wait, several reads.

STREAMS (fly scans, 2026-10-09). The primary's s / u / ln_ratio carry a `stream`
block (group "trace"): in a fly scan every completed sweep is one sample, the
whole trace, stamped at the sweep's middle (delay 0). The `point_<k>` detectors
-- one per frequency in cfg.stream.points_Hz -- are SCALAR complex channels of
the same stream, each stamped at the moment its point was measured. The point
detectors come and go with that list (and their labels with the grid), so the
manifest revision moves when either changes.

SEVERAL S-PARAMETERS FROM ONE SWEEP (2026-11-10), EVERY TRACE NAMED BY ITS
S-PARAMETER (2026-11-11). Each S-parameter one sweep measures -- `sparam`
first, then every entry of `extra_sparams` -- has `s_<sp>`, `u_<sp>` and
`ln_ratio_<sp>` (lower case: s_s21, u_s11): the same freq dim, the same
acquire group (one trigger fills them all), read with get_trace's `sparam`
argument. Until 2026-11-11 the primary was plain s / u / ln_ratio (Lukas:
name them all "like s11 and s21"). The primary's three also stream in a fly
scan; the extras are acquired only. The `extra_sparams` control is only
listed when the backend can do it.

ACTIONS WITH A `wait` BLOCK. `take_reference` is safe to run from a scan routine
("go to 150 mT at 45 deg, take the reference, then sweep"), and the `wait` block
is how the module says so AND how a caller knows it finished: take the id from
the reply, then wait until status shows that id and not acquiring -- the same
rule as an acquisition, for the same stale-status reason (gotcha #17).
`clear_reference` is immediate. `acquire` / `abort` carry no `wait`: they are
front-panel buttons, and a scan acquires through the detectors' `acquire` block.

What is dynamic: `start`'s maximum is `stop` minus the minimum span, and vice
versa; the trace ids name their S-parameters. Changing any of those changes
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


def _point_channels(vna, acquire, sparam) -> list[dict]:
    """One SCALAR complex detector per stream point (cfg.stream.points_Hz).

    In a FLY scan it streams the S-parameter at that one frequency, stamped at
    the moment the point was measured within each sweep (analyzer.py, THE
    STREAM); in a STEPPED scan it is read from the acquired trace like the
    other measurement detectors (same acquire group: no extra sweep). The
    label names the grid point really used (the nearest to what was asked),
    and it changes -- with the manifest revision -- when the sweep does."""
    out = []
    for p in vna.stream_points():
        ch = p["channel"]
        out.append(_p(
            f"point_{ch[1:]}", f"{sparam} at {p['freq_Hz'] / 1e9:.6g} GHz", "indicator",
            "float", group="Measurement", order=13, dtype="complex",
            acquire=acquire, stream={"group": "trace", "channel": ch},
            read={"verb": "get_point", "key": "value",
                  "args": {"channel": ch, "which": "sample", "quantity": "s"}},
            point_Hz=p["freq_Hz"], requested_Hz=p["requested_Hz"],
            help=f"{sparam} at one frequency of the sweep: grid point "
                 f"{p['index']} of {int(vna.cfg.sweep.points)} "
                 f"(asked for {p['requested_Hz'] / 1e9:.6g} GHz). Complex; stored "
                 f"as _real / _imag. In a fly scan each value carries the moment "
                 f"its point was measured. Set the list in Settings > stream."))
    return out


def _trace_channels(vna, acquire, freq_dim) -> list[dict]:
    """s / u / ln_ratio of EVERY S-parameter one sweep measures, each named by
    its S-parameter: s_s21, u_s21, ln_ratio_s21, then s_s11 ... for the extras
    (cfg.sweep.extra_sparams). One acquire group: one trigger fills them all.

    Why every id carries its S-parameter (Blue Box, 2026-11-11, Lukas: "like
    s11 and s21"): the primary used to be plain s / u / ln_ratio, so a
    detector list showed which S-parameter only for the extras. Now the id IS
    the quantity, and it stays the same whichever one is the primary. The
    primary (listed first) also streams in a fly scan; the extras are
    acquired, not streamed."""
    out = []
    for n, sp in enumerate(vna.measured_sparams()):
        key, primary = sp.lower(), n == 0
        same = "" if primary else ", from the SAME sweep as " + vna.measured_sparams()[0]
        base = 10 if primary else 14 + 3 * n
        for qid, quantity, label, help in (
                ("s", "s", f"S-parameter {sp}",
                 f"Complex {sp} as measured (raw: line loss and delay included){same}. "
                 f"Stored as s_{key}_real / _imag."),
                # "Permeability": the word the old LabVIEW program used. The
                # detector an FMR measurement is after has to be findable by the
                # name it is known by, not only by its definition.
                ("u", "u", f"Permeability u = ({sp} - {sp}_ref)/{sp}_ref",
                 f"{sp} relative to the brain's reference: the cables cancel and only "
                 f"the sample remains. Refused (the scan stops) with no reference, one "
                 f"taken on another sweep, or one without {sp}."),
                # The logarithmic form: S = A exp(i eta chi) through a line with a
                # film on it, so ln(S/S_ref) IS the susceptibility up to the
                # constant eta, at any line depth (u is the small-signal limit).
                # No prefactor: eta belongs to the waveguide and the film.
                ("ln_ratio", "ln", f"ln({sp} / {sp}_ref)",
                 f"Complex logarithm of {sp} over the reference's {sp}: proportional "
                 f"to the susceptibility at any line depth (u is its small-signal "
                 f"limit). Principal branch: the imaginary part wraps at +-pi. "
                 f"Refused, like u, with no matching reference.")):
            extra = {}
            if primary:
                extra["stream"] = {"group": "trace", "channel": quantity}
            out.append(_p(
                f"{qid}_{key}", label, "indicator", "array", group="Measurement",
                order=base + ("s", "u", "ln_ratio").index(qid), acquire=acquire,
                dtype="complex", shape=["freq"], dims=freq_dim,
                read={"verb": "get_trace", "key": quantity,
                      "args": {"which": "sample", "quantity": quantity, "sparam": sp}},
                help=help, **extra))
    return out


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
                   slo / 1e9, shi / 1e9, scale=1e9, decimals=6, step=0.1, tol=1.0,
                   help="First frequency of the sweep. Its maximum follows Stop (minus "
                        "the minimum span). Changing it restarts a running acquisition, "
                        "and a reference taken on another band no longer matches (u "
                        "is refused)."),
        sweep_ctrl("stop", "Stop", "GHz", "stop_Hz", "set_stop", "stop_Hz", 20,
                   plo / 1e9, phi / 1e9, scale=1e9, decimals=6, step=0.1, tol=1.0,
                   help="Last frequency of the sweep. Its minimum follows Start (plus "
                        "the minimum span). Same effects as Start."),
        sweep_ctrl("points", "Points", "", "points", "set_points", "points", 30,
                   lim.points_min, lim.points_max, type="int", step=1, tol=0.5,
                   help="Changing it between scan points is refused by the scan engine: "
                        "the trace would stop being rectangular."),
        sweep_ctrl("ifbw", "IF bandwidth", "kHz", "ifbw_Hz", "set_ifbw", "ifbw_Hz", 40,
                   lim.ifbw_min_Hz / 1e3, lim.ifbw_max_Hz / 1e3, scale=1e3, decimals=3,
                   tol=1e-3, help="Narrower = less noise (as sqrt) and a slower sweep."),
        sweep_ctrl("power", "Source power", "dBm", "power_dBm", "set_power", "power_dBm", 50,
                   lim.power_min_dBm, lim.power_max_dBm, decimals=1, step=1.0,
                   help="Source power at the port. Higher = less noise, until the "
                        "sample or an amplifier is driven out of its linear range."),
        sweep_ctrl("averages", "Averages", "", "averages", "set_averages", "averages", 60,
                   lim.averages_min, lim.averages_max, type="int", step=1, tol=0.5,
                   help="Sweeps averaged (coherently) per acquisition."),
        _p("sparam", "S-parameter", "control", "enum", group="Sweep", order=65,
           options=list(SPARAMS), read_path=["sparam"],
           set={"verb": "set_sparam", "arg": "sparam"},
           settle={"policy": "echoes", "key": "sparam"},
           help="The primary S-parameter: what is measured (detectors s_<sp>, u_<sp>, "
                "ln_ratio_<sp>; the primary also streams in a fly scan). A reference "
                "without it no longer matches (u is refused)."),
        *([_p("extra_sparams", "Also measure", "control", "string", group="Sweep", order=66,
              read_path=["extra_sparams"],
              set={"verb": "set_extra_sparams", "arg": "sparams"},
              settle={"policy": "echoes", "key": "extra_sparams"},
              help="More S-parameters from the SAME sweep, comma separated, e.g. "
                   "S11 (with S21 as the S-parameter). Each adds s_/u_/ln_ratio_<sp> "
                   "detectors. S11 and S21 share the forward sweep; adding S12 or S22 "
                   "sweeps the reverse direction too (twice the time). Empty = none. "
                   "Write it in canonical form (S11,S22): the setting echoes that.")]
          if vna.multi_sparams else []),
        # RF source output (2026-11-10): a control where the backend can switch
        # it, else an indicator (null = the analyser cannot say)
        *([_p("rf_output", "RF output", "control", "bool", group="Sweep", order=55,
              read_path=["rf_output"], set={"verb": "set_rf_output", "arg": "on"},
              settle={"policy": "echoes", "key": "rf_output"},
              help="The analyser's source output. OFF: acquisitions are refused "
                   "(a sweep would measure noise). Connecting and stopping this "
                   "module never change it.")]
          if vna.can_switch_rf else []),
        _p("sweep_time", "Sweep time", "indicator", "float", unit="s", group="Sweep",
           order=70, decimals=3, read_path=["sweep_time_s"],
           help="Predicted time of ONE sweep with the current points and IF bandwidth "
                "(twice that when S12/S22 and S11/S21 are measured together). An "
                "acquisition takes this x averages, plus a little."),
        _p("continuous", "Continuous sweep", "control", "bool", group="Sweep", order=80,
           read_path=["continuous"], set={"verb": "set_continuous", "arg": "on"},
           settle={"policy": "echoes", "key": "continuous"},
           help="Sweep on its own between acquisitions, like a front panel."),

        # -- measurement: the scan detectors (one acquisition feeds all of them) ------
        *_trace_channels(vna, acquire, freq_dim),
        *_point_channels(vna, acquire, sparam),
        _p("streaming", "Streaming (fly scan)", "indicator", "bool", group="Measurement",
           order=19, read_path=["streaming"],
           help="A fly scan is recording every sweep (the analyser sweeps back "
                "to back meanwhile)."),
        _p("dip_freq", "Dip frequency", "indicator", "float", unit="GHz",
           group="Measurement", order=20, decimals=6, scale=1e9,
           read_path=["sample", "dip_Hz"], acquire=acquire,
           help="Deepest point below a fitted baseline. Needs a span much wider "
                "than the line."),
        _p("dip_depth", "Dip depth", "indicator", "float", unit="dB",
           group="Measurement", order=21, decimals=3,
           read_path=["sample", "dip_dB"], acquire=acquire,
           help="How deep the dip goes below the fitted baseline in the latched "
                "acquisition (fresh at every scan point), in dB."),
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
           group="Measurement", order=24, read_path=["sample", "field_ok"], acquire=acquire,
           help="True when the field filed with this acquisition was heard from the "
                "magnet service during every sweep; False = a stale or manual value."),
        _p("acquire", "Acquire trace", "action", "action", group="Measurement", order=1,
           help="Average the next fresh sweeps and latch the result."),
        _p("abort", "Abort acquisition", "action", "action", group="Measurement", order=2,
           help="Cancel the running acquisition. It is latched as aborted (a scan "
                "waiting for it fails instead of reading an older trace); aborting a "
                "Take reference also clears the old reference."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=3, read_path=["acquiring"],
           help="An acquisition (or a reference) is being measured now."),
        # A counter from 0 that only counts up: min 0 is the promise
        # scan-core stores it by (an int is stored by its declared range).
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=4, min=0, read_path=["acq_id"],
           help="Number of the latest acquisition started; it counts up from 0. A scan "
                "waits until THIS number has finished, so it never reads an older one."),

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
           order=2, wait={"ready": {"policy": "immediate"}},
           help="Forget the reference. u and ln_ratio are refused until a new one is "
                "taken."),
        _p("reference_present", "Reference present", "indicator", "bool",
           group="Reference", order=10, read_path=["reference", "present"],
           help="A reference is stored (u and ln_ratio need one that matches the "
                "sweep)."),
        _p("reference_field", "Reference field", "indicator", "float", unit="mT",
           group="Reference", order=11, decimals=3, read_path=["reference", "field_mT"],
           help="The field the stored reference was taken at, as the field source "
                "reported it."),
        _p("reference_angle", "Reference angle", "indicator", "float", unit="deg",
           group="Reference", order=12, decimals=2, read_path=["reference", "angle_deg"],
           help="The in-plane field angle the stored reference was taken at."),

        # -- live ------------------------------------------------------------------------
        _p("live_dip_freq", "Dip frequency (live)", "indicator", "float", unit="GHz",
           group="Live", order=10, decimals=6, scale=1e9, plottable=True,
           read_path=["dip_Hz"],
           help="Dip in the LATEST sweep of any kind (live status value, for panels "
                "and plots, not for scan points; use Dip frequency for those)."),
        _p("live_dip_depth", "Dip depth (live)", "indicator", "float", unit="dB",
           group="Live", order=11, decimals=3, plottable=True, read_path=["dip_dB"],
           help="Depth of the dip in the latest sweep (live status value, not for "
                "scan points; use Dip depth for those)."),
        _p("sweeps", "Sweeps", "indicator", "int", group="Live", order=12,
           min=0, read_path=["sweeps"], help="Completed sweeps since start."),

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
           settle={"policy": "echoes", "key": "manual_angle_deg", "tol": 1e-9},
           help="In-plane field angle used when the source is manual (0 deg = the "
                "x axis of the magnet)."),
        _p("field", "Field", "indicator", "float", unit="mT", group="Field",
           order=30, decimals=3, plottable=True, read_path=["field_mT"],
           help="The field the sample sits in now, from the field source (live "
                "status value). Scans record Field during sweep instead."),
        _p("field_angle", "Field angle", "indicator", "float", unit="deg", group="Field",
           order=31, decimals=2, plottable=True, read_path=["angle_deg"],
           help="The in-plane field angle now, from the field source (live status "
                "value)."),
        _p("field_ok", "Field is live", "indicator", "bool", group="Field",
           order=32, read_path=["field_ok"],
           help="The field source is being heard now; False = its last value is "
                "stale, or the manual value stands in."),
        _p("field_in_use", "Field in use", "indicator", "string",
           group="Field", order=33, read_path=["field_source"],
           help="Which source the field really comes from, with its health, e.g. "
                "'manual' or 'mag2d (stale 3.2 s)'."),

        # -- status ------------------------------------------------------------------------
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"],
           help="The service is connected to the analyser (or the simulator)."),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"],
           help="What the analyser reports about itself (*IDN?)."),
        _p("hw_error", "Error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"],
           help="The last hardware error; empty while everything works. A sweep "
                "with an error is not used."),
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
               settle={"policy": "echoes", "key": "geometry"},
               help="Simulator only: field in the film plane (in_plane) or normal to "
                    "it (out_of_plane); picks the Kittel formula."),
        ]

    # Bounds that are not known must not appear as NaN.
    for d in params:
        for k in ("min", "max"):
            if k in d and not (isinstance(d[k], (int, float)) and math.isfinite(d[k])):
                del d[k]

    from ..backends import analyser_name
    label = ("Vector network analyser (simulated)" if simulated
             else analyser_name(vna.cfg))
    manifest = {"schema": SCHEMA_VERSION, "module": "vna", "label": label,
                "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
