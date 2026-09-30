"""describe.py -- this service's self-description: what can be shown and driven.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. The rule that keeps it honest:
nothing here restates a value that lives elsewhere -- every bound and every
list of options is looked up from cfg / tables.py / the live state when the
manifest is built.

What is special about the SR830's manifest:

1. THE MEASUREMENT IS AN ACQUISITION (the hf2 design). The detectors a scan
   records (x, y, r, theta, aux1..4, sample_freq, overload) read the LATCHED
   sample and share ONE `acquire` block: trigger `acquire`, then wait until
   status shows THAT id with `acquiring` false (`target_key`, gotcha #17). One
   settle-and-latch per scan point, not one per detector. The same quantities
   appear as `live_*` indicators for a front panel: fresh, NOT settled.

2. DISCRETE SETTINGS ARE ENUMS. Sensitivity and time constant are fixed steps
   on an SR830, so they are `enum` controls whose options are the front-panel
   labels. scan-core registers enums read-only (it sweeps numbers); a script
   can still set them, and the control screen shows drop-downs.

3. THE MANIFEST CHANGES SHAPE WITH THE STATE, and the revision follows:
     * reference internal -> `freq` is a control; external -> an indicator,
       plus an `unlocked` lamp;
     * current input (I1M / I100M) -> X, Y, R are in A and the sensitivity
       options read "10 nA" instead of "10 mV";
     * detection frequency above 200 Hz -> time constants above 30 s vanish
       from the options (the SR830 refuses them there);
     * the harmonic's maximum is 102 kHz / frequency, and the frequency's
       maximum is 102 kHz / harmonic.

4. AUTO FUNCTIONS ARE SCAN ROUTINE ACTIONS. Auto Gain / Reserve / Phase carry a
   `wait` block keyed on the run number they return, so "auto phase before
   the scan" waits until THAT run has finished (for Auto Phase: including the
   settling of the outputs on the new phase).
"""

from __future__ import annotations

import json
import zlib

from .. import tables

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


def _enum(id, label, group, order, options, status_key, verb, arg, help=""):
    """An enum control that reads back from `status_key` and is set by `verb`."""
    return _p(id, label, "control", "enum", group=group, order=order,
              options=list(options), read_path=[status_key],
              set={"verb": verb, "arg": arg}, help=help)


def build_manifest(lockin) -> dict:
    cfg = lockin.cfg
    lim = cfg.limits
    acq = cfg.acquisition
    ref = cfg.reference
    src = cfg.input.source
    unit = tables.unit_for(src)
    external = ref.source == "external"
    f_lo, f_hi = lockin._freq_range()
    tc_now = lockin.status_tc_s()

    # One acquisition, shared by every scan detector.
    acquire = {
        "group": "sample",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": acq.timeout_s,
    }
    # The time constants on offer: up to the configured limit, and not above
    # 30 s while the detection frequency is above 200 Hz.
    i_max = tables.tc_index(lim.tc_max)
    if lockin._detect_hz() > tables.TC_LONG_MAX_FREQ_HZ:
        i_max = min(i_max, tables.TC_LONG_FIRST_INDEX - 1)
    tc_options = tables.TC_LABELS[:i_max + 1]
    sens_options = tables.SENS_LABELS_A if tables.is_current(src) else tables.SENS_LABELS_V

    params = []
    G = "Reference"
    params.append(_enum("ref_source", "Reference source", G, 1, ("internal", "external"),
                        "reference_source", "set_reference_source", "source",
                        help="internal: the SR830's oscillator, frequency set here (and "
                             "scannable). external: it locks to REF IN and measures it."))
    if external:
        params.append(_p("freq", "Reference frequency", "indicator", "float", unit="Hz",
                         group=G, order=10, decimals=4, plottable=True,
                         read_path=["ref_freq_Hz"],
                         help="Measured from the external reference."))
        params.append(_p("unlocked", "Reference unlocked", "indicator", "bool", group=G,
                         order=11, read_path=["unlocked"]))
    else:
        params.append(_p("freq", "Reference frequency", "control", "float", unit="Hz",
                         group=G, order=10, decimals=4, plottable=True,
                         min=f_lo, max=f_hi, read_path=["ref_freq_Hz"],
                         set={"verb": "set_frequency", "arg": "frequency_Hz"},
                         settle={"policy": "echoes", "key": "freq_set_Hz", "tol": 1e-6},
                         help="Internal oscillator. Upper limit = 102 kHz / harmonic."))
    params.append(_p("detect_freq", "Detection frequency", "indicator", "float", unit="Hz",
                     group=G, order=12, decimals=4, read_path=["detect_freq_Hz"],
                     help="Reference x harmonic: the frequency actually demodulated."))
    params.append(_p("harmonic", "Harmonic", "control", "int", group=G, order=20,
                     min=1, max=lockin._harmonic_max(), step=1, read_path=["harmonic"],
                     set={"verb": "set_harmonic", "arg": "harmonic"},
                     settle={"policy": "echoes", "key": "harmonic_set", "tol": 0.5}))
    params.append(_p("phase", "Reference phase", "control", "float", unit="deg",
                     group=G, order=30, min=-180.0, max=180.0, decimals=2,
                     read_path=["phase_deg"],
                     set={"verb": "set_phase", "arg": "phase_deg"},
                     settle={"policy": "echoes", "key": "phase_set_deg", "tol": 0.006}))
    params.append(_enum("trigger", "External trigger", G, 40, tables.TRIGGERS, "trigger",
                        "set_trigger", "trigger",
                        help="How REF IN is read in external mode. Below 1 Hz use TTL."))
    params.append(_p("sine_out", "Sine out amplitude", "control", "float", unit="V",
                     group=G, order=50, min=lim.sine_min_V, max=lim.sine_max_V,
                     step=0.002, decimals=3, read_path=["sine_out_V"],
                     set={"verb": "set_sine_out", "arg": "sine_out_V"},
                     settle={"policy": "echoes", "key": "sine_out_set_V", "tol": 0.0011},
                     help="Vrms, 2 mV steps. Cannot be switched off: 4 mV is its minimum."))

    G = "Input"
    params += [
        _enum("input_source", "Input", G, 1, tables.INPUT_SOURCES, "input_source",
              "set_input_source", "source",
              help="A, A-B, or a current input (1 Mohm / 100 Mohm gain): X, Y, R in A."),
        _enum("input_ground", "Shield", G, 2, tables.GROUNDS, "input_ground",
              "set_input_ground", "ground"),
        _enum("input_coupling", "Coupling", G, 3, tables.COUPLINGS, "input_coupling",
              "set_input_coupling", "coupling"),
        _enum("line_filter", "Line notch", G, 4, tables.LINE_FILTERS, "line_filter",
              "set_line_filter", "line_filter"),
    ]

    G = "Gain and filter"
    params += [
        _enum("sensitivity", "Sensitivity", G, 1, sens_options, "sensitivity",
              "set_sensitivity", "sensitivity",
              help="Full scale. Too small overloads the output; too large wastes "
                   "resolution. Auto Gain picks one."),
        _p("full_scale", "Full scale", "indicator", "float", unit=unit, group=G,
           order=2, read_path=["full_scale"]),
        _enum("reserve", "Dynamic reserve", G, 3, tables.RESERVES, "reserve",
              "set_reserve", "reserve",
              help="How much larger than full scale an interfering signal may be "
                   "before the input overloads. More reserve = more noise."),
        _enum("time_constant", "Time constant", G, 10, tc_options, "time_constant",
              "set_time_constant", "time_constant",
              help="A scan point waits several of these (see settle time)."),
        _p("tc_s", "Time constant (s)", "indicator", "float", unit="s", group=G,
           order=11, read_path=["tc_s"]),
        _enum("slope", "Filter slope", G, 12, tables.SLOPES, "slope", "set_slope", "slope",
              help="6 dB/oct per RC stage. Steeper rejects noise better but settles "
                   "more slowly (99 %: 5 / 7 / 9 / 10 time constants)."),
        _p("sync_filter", "Synchronous filter", "control", "bool", group=G, order=13,
           read_path=["sync_filter"],
           set={"verb": "set_sync_filter", "arg": "enabled"},
           settle={"policy": "echoes", "key": "sync_filter", "tol": 0.5},
           help="Removes 2f ripple below 200 Hz; adds one period to the settle time."),
        _p("settle", "Settle time", "indicator", "float", unit="ms", group=G, order=20,
           decimals=2, scale=1e-3, read_path=["settle_s"],
           help=f"Time to reach {acq.settle_percent:g} % of a step with the APPLIED "
                f"time constant and slope."),
        _p("ovl_input", "Input overload", "indicator", "bool", group=G, order=30,
           read_path=["overload", "input"]),
        _p("ovl_filter", "Filter overload", "indicator", "bool", group=G, order=31,
           read_path=["overload", "filter"]),
        _p("ovl_output", "Output overload", "indicator", "bool", group=G, order=32,
           read_path=["overload", "output"]),
    ]

    G = "Aux out"
    for k in range(4):
        params.append(_p(
            f"aux_out{k + 1}", f"AUX OUT {k + 1}", "control", "float", unit="V",
            group=G, order=k + 1, min=lim.aux_out_min_V, max=lim.aux_out_max_V,
            step=0.001, decimals=3, read_path=["aux_out_set_V", k],
            set={"verb": "set_aux_out", "arg": "volts", "extra": {"channel": k + 1}},
            settle={"policy": "echoes", "key": "aux_out_set_V", "index": k, "tol": 6e-4},
            help="Rear-panel DC output, 1 mV resolution. Set to 0 V when the service "
                 "stops (safety.aux_out_zero_on_stop)."))

    # -- measurement: latched (scan) and live (panel) --------------------------------
    G = "Measurement"
    measured = (("x", "X", unit, "x", 6), ("y", "Y", unit, "y", 6),
                ("r", "R", unit, "r", 6), ("theta", "Theta", "deg", "theta_deg", 2))
    for j, (pid, label, u, key, dec) in enumerate(measured):
        params.append(_p(
            pid, label, "indicator", "float", unit=u, group=G, order=100 + j,
            decimals=dec, read_path=["sample", key], acquire=acquire,
            stream={"group": "demod", "channel": pid},
            help="Settled and latched by `acquire`: safe to record in a scan. In a "
                 "FLY scan it is recorded continuously instead (stream)."))
        params.append(_p(
            f"live_{pid}", f"{label} (live, unsettled)", "indicator", "float", unit=u,
            group="Live", order=200 + j, decimals=dec, plottable=True,
            read_path=["live", key]))
    for k in range(4):
        params.append(_p(
            f"aux{k + 1}", f"AUX IN {k + 1}", "indicator", "float", unit="V", group=G,
            order=150 + k, decimals=4, read_path=["sample", "aux_in", k], acquire=acquire,
            stream={"group": "demod", "channel": f"aux{k + 1}"},
            help="Latched with the X/Y sample, averaged over the same window."))
        params.append(_p(
            f"live_aux{k + 1}", f"AUX IN {k + 1} (live)", "indicator", "float", unit="V",
            group="Live", order=250 + k, decimals=4, plottable=True,
            read_path=["live", "aux_in", k]))
    params += [
        _p("sample_freq", "Reference frequency (sample)", "indicator", "float", unit="Hz",
           group=G, order=160, decimals=4, read_path=["sample", "freq_Hz"],
           acquire=acquire,
           help="The reference frequency averaged over the sample: worth recording "
                "with an external reference."),
        _p("overload", "Overload during sample", "indicator", "int", group=G, order=161,
           read_path=["sample", "overload"], acquire=acquire,
           help="1 if the input, filter or output overloaded at any time while the "
                "sample settled or averaged: that point is not to be trusted."),
    ]

    # -- actions ----------------------------------------------------------------------
    auto_wait = {"target_key": "auto_id",
                 "ready": {"policy": "adopt_then_flag", "setpoint_key": "auto_id",
                           "flag_key": "auto_busy", "invert": True},
                 "timeout_s": max(30.0, 50.0 * tc_now)}
    params += [
        _p("acquire", "Acquire sample", "action", "action", group=G, order=1,
           help="Wait the settle time, average, and latch one sample."),
        _p("acquiring", "Acquiring", "indicator", "bool", group=G, order=2,
           read_path=["acquiring"]),
        _p("acq_id", "Acquisition #", "indicator", "int", group=G, order=3,
           read_path=["acq_id"]),
        _p("auto_gain", "Auto gain", "action", "action", group="Auto", order=1,
           wait=auto_wait,
           help="The SR830 picks the sensitivity. Does nothing above a 1 s time constant."),
        _p("auto_reserve", "Auto reserve", "action", "action", group="Auto", order=2,
           wait=auto_wait, help="The lowest reserve that does not overload the input."),
        _p("auto_phase", "Auto phase", "action", "action", group="Auto", order=3,
           wait=auto_wait,
           help="Rotates the reference phase so the signal is on X (theta -> 0). "
                "Finished once the outputs have settled on the new phase."),
        _p("auto_busy", "Auto running", "indicator", "bool", group="Auto", order=10,
           read_path=["auto_busy"]),
        _p("auto_id", "Auto run #", "indicator", "int", group="Auto", order=11,
           read_path=["auto_id"]),
        _p("auto_note", "Last auto result", "indicator", "string", group="Auto",
           order=12, read_path=["auto_note"]),
        # the SAFETY verb as a button (control.py: a viewer may always send
        # it), so the suite's Control tab offers it to a viewer too
        _p("output_off", "Outputs off", "action", "action", group="Reference", order=90,
           help="SINE OUT to its 4 mV minimum (an SR830 cannot switch it off) and "
                "every AUX OUT to 0 V. Allowed for anyone, also a viewer."),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),
    ]

    manifest = {"schema": SCHEMA_VERSION, "module": "sr830",
                "label": "Lock-in amplifier (SR830)", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
