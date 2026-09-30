"""describe.py -- this service's self-description: what can be shown and driven.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. The rule that keeps it honest:
nothing here restates a value that lives elsewhere -- every bound is looked up
from cfg / the brain / tables.py when the manifest is built.

What makes this manifest move (each change gives a new `revision`, and every
status frame carries `describe_rev`, so clients re-fetch):

1. FAST MODE decides the time-constant range (5 ms minimum without it) and the
   slopes on offer (only 6 and 12 dB/oct with it).
2. THE INPUT decides the unit (V, or A in the two current modes) of every
   measured quantity and the list of sensitivities.
3. THE REFERENCE decides the frequency limit (on an internal reference the
   harmonic divides it) and whether a "reference locked" indicator exists.
4. THE TIME CONSTANT decides the acquisition timeout (a long one settles
   slowly; the timeout grows with it rather than failing a scan).

THE MEASUREMENT IS AN ACQUISITION. The detectors a scan should record (x, y,
r, theta, adc1, adc2, and the sample's overload / lock flags) read the LATCHED
sample and share ONE `acquire` group: trigger `acquire`, then wait until status
shows that acquisition's id with `acquiring` false. `target_key` makes the wait
target the id from the trigger's REPLY, so it cannot be satisfied by the
previous point's status (gotcha #17). The same quantities also appear as
`live_*` indicators for a front panel; they are fresh but NOT settled.

The measured detectors also carry a `stream` block, so a FLY scan records them
continuously instead (stream_start / stream_read / stream_stop).

The auto operations are ACTIONS with a `wait` block, numbered like the
acquisitions, so a scan routine can run "auto-phase, then measure".
"""

from __future__ import annotations

import json
import zlib

from .. import tables
from ..config import REF_SOURCES, INPUT_MODES

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, acquire=None, stream=None,
       wait=None, help=""):
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
                 ("acquire", acquire), ("stream", stream), ("wait", wait),
                 ("help", help)):
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
    """Resolve a descriptor's `read_path` (a list of keys / indices)."""
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


#: Measured quantities: (id, label, unit or None = the input's unit, sample/live key, decimals)
_MEASURED = (
    ("x", "X", None, "x", 9),
    ("y", "Y", None, "y", 9),
    ("r", "R (magnitude)", None, "r", 9),
    ("theta", "Theta (phase)", "deg", "theta_deg", 2),
)


def build_manifest(lockin) -> dict:
    cfg = lockin.cfg
    lim = cfg.limits
    ref, sig, flt = cfg.reference, cfg.signal, cfg.filter
    unit = tables.unit_for(sig.input)
    allowed_tc = lockin.allowed_tcs()
    table = lockin.sensitivity_table()

    # One acquisition, shared by every scan detector.
    acquire = {
        "group": "sample",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": round(lockin.acquire_timeout_s(), 3),
    }
    # The auto operations: numbered, finished when status shows the id idle,
    # and SUCCEEDED only if they left no error behind.
    auto_wait = {
        "target_key": "auto_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "auto_id",
                  "flag_key": "auto_busy", "invert": True},
        "check": {"key": "auto_error", "equals": ""},
        "timeout_s": 300.0,
    }

    params = [
        # -- reference channel + oscillator -----------------------------------
        _p("ref", "Reference source", "control", "enum", group="Reference", order=1,
           options=list(REF_SOURCES), read_path=["ref_source"],
           set={"verb": "set_reference", "arg": "source"},
           help="internal: detect at our own oscillator. ext_ttl / ext_analog: "
                "follow the signal on the REF IN connector."),
        _p("freq", "Oscillator frequency", "control", "float", unit="Hz",
           group="Reference", order=10, decimals=4, plottable=True,
           min=lim.freq_min_Hz, max=lockin.freq_max_Hz(),
           read_path=["freq_set_Hz"],
           set={"verb": "set_frequency", "arg": "frequency_Hz"},
           settle={"policy": "echoes", "key": "freq_set_Hz", "tol": 1e-6},
           help="OSC OUT frequency. On an internal reference it is also the "
                "reference, so the limit is the instrument's range divided by "
                "the harmonic."),
        _p("ref_freq", "Reference frequency (measured)", "indicator", "float",
           unit="Hz", group="Reference", order=11, decimals=4, plottable=True,
           read_path=["ref_freq_Hz"],
           help="The instrument's frequency meter. Reads 0 while an external "
                "reference is unlocked."),
        _p("demod_freq", "Detection frequency", "indicator", "float", unit="Hz",
           group="Reference", order=12, decimals=4, read_path=["demod_freq_Hz"],
           help="harmonic x reference: where the lock-in actually listens."),
        _p("amplitude", "Oscillator amplitude", "control", "float", unit="V",
           group="Reference", order=20, decimals=6, min=0.0,
           max=lim.amplitude_max_V, read_path=["amplitude_V"],
           set={"verb": "set_amplitude", "arg": "amplitude_V"},
           settle={"policy": "echoes", "key": "amplitude_V", "tol": 1e-9},
           danger=True,
           help="OSC OUT in V rms. Drives whatever is connected to it. Read "
                "from the instrument at start (never changed then); returns to "
                "0 V when the service stops unless osc_off_on_shutdown is off."),
        _p("phase", "Reference phase", "control", "float", unit="deg",
           group="Reference", order=30, decimals=3, min=-180.0, max=180.0,
           read_path=["phase_deg"],
           set={"verb": "set_phase", "arg": "phase_deg"},
           settle={"policy": "echoes", "key": "phase_deg", "tol": 1e-6}),
        _p("harmonic", "Harmonic", "control", "int", group="Reference", order=40,
           min=1, max=lockin._harmonic_max(), step=1, read_path=["harmonic"],
           set={"verb": "set_harmonic", "arg": "harmonic"},
           settle={"policy": "echoes", "key": "harmonic", "tol": 0.5},
           help="Detect at n x the reference (2f, 3f ...)."),

        # -- signal channel ---------------------------------------------------------
        _p("input", "Signal input", "control", "enum", group="Signal", order=1,
           options=list(INPUT_MODES), read_path=["input"],
           set={"verb": "set_input", "arg": "mode"},
           help="A, -B, A-B (differential), ground (test), or a current mode on "
                "the B(I) connector -- then everything is measured in amps."),
        _p("coupling", "Coupling", "control", "enum", group="Signal", order=2,
           options=["AC", "DC"], read_path=["coupling"],
           set={"verb": "set_coupling", "arg": "coupling"}),
        _p("sensitivity", "Sensitivity (full scale)", "control", "enum",
           group="Signal", order=10,
           options=[tables.sensitivity_label(i, sig.input) for i in sorted(table)],
           read_path=["sensitivity"],
           set={"verb": "set_sensitivity", "arg": "sensitivity"},
           help="Outputs beyond 300 % of full scale clip and flag an overload; "
                "a range far above the signal adds noise. Auto-sensitivity picks "
                "the range that puts R at 30-90 %."),
        _p("full_scale", "Full scale", "indicator", "float", unit=unit,
           group="Signal", order=11, decimals=12, read_path=["full_scale"]),
        _p("overload_input", "Input overload", "indicator", "bool", group="Signal",
           order=20, read_path=["overload", "input"]),
        _p("overload_output", "Output overload", "indicator", "bool",
           group="Signal", order=21, read_path=["overload", "output"]),

        # -- output filter --------------------------------------------------------------
        # Time constant: offered in ms (scale 1e-3), commanded in s. The
        # instrument only has the 1-2-5 table, so the service snaps to the
        # nearest entry: the readback is what it APPLIED; the settle check
        # compares against what we ASKED for, which is echoed exactly.
        _p("tc", "Time constant", "control", "float", unit="ms", group="Filter",
           order=10, decimals=4, scale=1e-3,
           min=min(allowed_tc) * 1e3, max=max(allowed_tc) * 1e3,
           read_path=["tc_s"],
           set={"verb": "set_time_constant", "arg": "time_constant_s"},
           settle={"policy": "echoes", "key": "tc_set_s", "tol": 1e-12},
           help="Any value is accepted; the instrument uses the nearest of its "
                "1-2-5 steps. A scan point waits several of these."),
        _p("slope", "Filter slope", "control", "enum", group="Filter", order=20,
           options=[tables.slope_label(s) for s in tables.allowed_slopes(flt.fast_mode)],
           read_path=["slope"], set={"verb": "set_slope", "arg": "slope"},
           help="6 dB/oct per RC stage. Steeper rejects noise better but "
                "settles more slowly."),
        _p("fast_mode", "Fast mode", "control", "bool", group="Filter", order=30,
           read_path=["fast_mode"], set={"verb": "set_fast_mode", "arg": "enabled"},
           settle={"policy": "echoes", "key": "fast_mode", "tol": 0.5},
           help="On: time constants down to 10 us, but only 6 or 12 dB/oct. "
                "Off: 5 ms and up, all four slopes."),
        _p("settle", "Settle time", "indicator", "float", unit="ms",
           group="Filter", order=40, decimals=2, scale=1e-3, read_path=["settle_s"],
           help=f"Time to reach {cfg.acquisition.settle_percent:g} % of a step "
                f"with the applied time constant and slope."),
    ]

    if ref.source != "internal":
        params.append(_p(
            "ref_locked", "Reference locked", "indicator", "bool",
            group="Reference", order=13, read_path=["ref_locked"],
            help="False while the reference channel has not locked to REF IN."))

    # -- measurement: latched (scan) and live (panel) --------------------------------
    for j, (pid, label, u, key, dec) in enumerate(_MEASURED):
        params.append(_p(
            pid, label, "indicator", "float", unit=u or unit, group="Measurement",
            order=100 + j, decimals=dec, read_path=["sample", key], acquire=acquire,
            stream={"group": "demod", "channel": pid},
            help="Settled and latched by `acquire`: safe to record in a scan. "
                 "In a FLY scan it is recorded continuously instead (stream)."))
        params.append(_p(
            f"live_{pid}", f"{label} (live, unsettled)", "indicator", "float",
            unit=u or unit, group="Live", order=200 + j, decimals=dec,
            plottable=True, read_path=["live", key]))
    for k in range(2):
        params.append(_p(
            f"adc{k + 1}", f"ADC{k + 1} input", "indicator", "float", unit="V",
            group="Measurement", order=150 + k, decimals=4,
            read_path=["sample", "adc", k], acquire=acquire,
            stream={"group": "demod", "channel": f"adc{k + 1}"},
            help="Rear-panel auxiliary input, averaged over the same window."))
        params.append(_p(
            f"live_adc{k + 1}", f"ADC{k + 1} input (live)", "indicator", "float",
            unit="V", group="Live", order=250 + k, decimals=4, plottable=True,
            read_path=["live", "adc", k]))
    params += [
        _p("live_r_fs", "R / full scale (live)", "indicator", "float",
           group="Live", order=240, decimals=3, plottable=True,
           read_path=["live", "r_fs"],
           help="1.0 = full scale; above 3.0 the outputs clip."),
        _p("sample_overload", "Sample overloaded", "indicator", "bool",
           group="Measurement", order=160, read_path=["sample", "overload"],
           acquire=acquire,
           help="True if ANY reading in the sample's window was overloaded: "
                "record it next to the data so a bad point is recognisable."),
        _p("sample_locked", "Sample on a locked reference", "indicator", "bool",
           group="Measurement", order=161, read_path=["sample", "ref_locked"],
           acquire=acquire),
        _p("acquire", "Acquire sample", "action", "action", group="Measurement",
           order=1, help="Wait the settle time, average, and latch one sample."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=2, read_path=["acquiring"]),
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=3, read_path=["acq_id"]),
        _p("auto_phase", "Auto phase", "action", "action", group="Auto", order=1,
           wait=auto_wait,
           help="Rotate the reference phase so the signal lies on +X (AQN)."),
        _p("auto_sensitivity", "Auto sensitivity", "action", "action", group="Auto",
           order=2, wait=auto_wait,
           help="Choose the range that puts R at 30-90 % of full scale (AS)."),
        _p("auto_measure", "Auto measure", "action", "action", group="Auto", order=3,
           wait=auto_wait, help="Auto sensitivity, then auto phase (ASM)."),
        _p("auto_busy", "Auto operation running", "indicator", "bool", group="Auto",
           order=10, read_path=["auto_busy"]),
        # the SAFETY verb as a button (control.py: a viewer may always send
        # it), so the suite's Control tab offers it to a viewer too
        _p("output_off", "Oscillator off", "action", "action", group="Reference", order=90,
           help="OSC OUT amplitude to 0 V. Allowed for anyone, also a viewer."),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),
    ]

    manifest = {"schema": SCHEMA_VERSION, "module": "sr7230",
                "label": "Lock-in amplifier (7230)", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
