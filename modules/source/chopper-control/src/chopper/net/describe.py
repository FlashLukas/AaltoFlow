"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of.
Two consumers, two projections of one source: the reconfigurable control screen
reads this manifest DIRECTLY, while scan-core projects it into Settables and
Gettables. Full contract: INSTRUMENT_MODULE_GUIDE.md section 6b.

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every limit is looked up from the brain when the manifest is
built, never copied into a literal.

WHAT IS DYNAMIC here, and why `revision` moves:
  * the frequency range is the range of the RING the reference locks to, on
    the MOUNTED blade (MC1F60: 120 Hz - 6 kHz; MC1F10HP inner ring 20 Hz -
    1 kHz, outer ring 200 Hz - 10 kHz), intersected with the safety envelope;
  * the reference-in / reference-out options are the blade's own list;
  * on EXTERNAL reference the frequency is not ours to set -- it is EXT REF IN
    times N/D -- so `frequency` becomes an indicator (the same trick as hf2's
    external PLL: the manifest's SHAPE follows the mode).
Every status frame carries `describe_rev`, so a client notices at the cost of
one integer compare.

THE SETTLE RULE a scan waits on for `frequency` is adopt_then_flag: the status
must first show OUR setpoint (so a frame from the previous point cannot pass),
and only then is `locked` believed. The brain clears `locked` in the same
critical section in which it stores the setpoint (gotcha #1, #28).
"""

from __future__ import annotations

import json
import zlib

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, timeout_s=None, args=None, danger=False, help="", **extra):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract.
    `extra` carries an action's `wait` block."""
    d = {
        "id": id, "label": label, "kind": kind, "type": type,
        "unit": unit, "group": group, "order": order,
        "writable": (kind == "control") if writable is None else writable,
        "plottable": plottable,
        "read_path": read_path,      # keys/indices into the status dict, or None
    }
    for k, v in (("value", value), ("min", min), ("max", max), ("step", step),
                 ("decimals", decimals), ("options", options), ("scale", scale),
                 ("set", set), ("settle", settle), ("timeout_s", timeout_s),
                 ("args", args), ("help", help), *extra.items()):
        if v is not None and v != "":
            d[k] = v
    if danger:
        d["danger"] = True
    return d


def manifest_revision(manifest: dict) -> int:
    """A checksum over the parts of the manifest a client must react to.

    Derived, not hand-bumped -- a counter someone has to remember to increment
    is a counter that will eventually be wrong. `value` is excluded on purpose:
    it changes many times a second and travels in the status stream anyway.
    """
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` against a status dict (a list of keys;
    an int element indexes into a list)."""
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


def build_manifest(ch) -> dict:
    """The chopper's manifest, built from the brain's LIVE state."""
    cfg = ch.cfg
    st = ch.status()
    lo, hi = ch.freq_limits()
    refs, outs = ch.mode_options()
    from ..blades import blade_by_name
    blade = blade_by_name(st.blade)
    res = blade.resolution_Hz
    dec = 0 if res >= 1 else (1 if res >= 0.1 else 2)
    ring = blade.ring_of(st.ref_mode) if blade.two_ring else ""
    ring_txt = f" ({ring} ring)" if ring else ""
    lock_s = cfg.settle.timeout_s

    params = [
        # ---- run -----------------------------------------------------------
        _p("enabled", "Motor running", "control", "bool", group="Run", order=10,
           read_path=["enabled"],
           set={"verb": "set_enable", "arg": "on"},
           settle={"policy": "echoes", "key": "enabled"},
           help="Start / stop the wheel. Settles as soon as the controller has the "
                "command; use the `start` action to also wait for the lock."),
        _p("start", "Start and lock", "action", "action", group="Run", order=11,
           wait={"target_key": "lock_gen",
                 "ready": {"policy": "adopt_then_flag", "setpoint_key": "lock_gen",
                           "flag_key": "locked"},
                 "timeout_s": lock_s},
           help="Run the wheel and wait until it is locked -- use it in a scan "
                "routine before the first point."),
        _p("stop", "Stop (standby)", "action", "action", group="Run", order=12,
           wait={"ready": {"policy": "immediate"}},
           help="Standby. The wheel coasts down; blade and modes can then be changed."),
        _p("locked", "Locked", "indicator", "bool", group="Run", order=13,
           read_path=["locked"],
           help="Measured wheel frequency within tolerance for settle.hold_s -- or, "
                "with REF OUT on 'target', a timer (see lock_source)."),
        _p("lock_source", "Lock judged by", "indicator", "string", group="Run",
           order=14, read_path=["lock_source"]),
    ]

    # ---- frequency: a control on internal reference, an indicator on external
    if st.external:
        params.append(_p(
            "frequency", "Chopping frequency (from EXT REF IN x N/D)", "indicator",
            "float", unit="Hz", group="Frequency", order=20, decimals=dec,
            plottable=True, read_path=["target_frequency_Hz"],
            help="External reference: the wheel follows EXT REF IN x N / D."))
    else:
        params.append(_p(
            "frequency", f"Chopping frequency{ring_txt}", "control", "float",
            unit="Hz", group="Frequency", order=20, decimals=dec, step=res,
            min=lo, max=hi, plottable=True,
            read_path=["setpoint_frequency_Hz"],
            set={"verb": "set_frequency", "arg": "frequency_Hz"},
            settle={"policy": "adopt_then_flag",
                    "setpoint_key": "setpoint_frequency_Hz", "flag_key": "locked"},
            timeout_s=lock_s,
            help=f"{blade.name}, {st.ref_mode}: {lo:g}..{hi:g} Hz. A scan waits until "
                 f"the wheel is locked at the new frequency."))
    params += [
        _p("measured_frequency", f"Measured frequency{ring_txt}", "indicator", "float",
           unit="Hz", group="Frequency", order=21, decimals=max(dec, 2), plottable=True,
           read_path=["frequency_Hz"],
           help="From the slot sensor (REF OUT). null while REF OUT is on 'target'."),
        _p("freq_error", "Frequency error", "indicator", "float", unit="Hz",
           group="Frequency", order=22, decimals=3, plottable=True,
           read_path=["freq_error_Hz"]),
        _p("refout_frequency", "REF OUT frequency", "indicator", "float", unit="Hz",
           group="Frequency", order=23, decimals=max(dec, 2), plottable=True,
           read_path=["refout_frequency_Hz"]),

        # ---- phase ---------------------------------------------------------
        _p("phase", "Phase", "control", "float", unit="deg", group="Frequency",
           order=30, decimals=0, step=1.0,
           min=float(cfg.limits.phase_min_deg), max=float(cfg.limits.phase_max_deg),
           read_path=["phase_deg"],
           set={"verb": "set_phase", "arg": "phase_deg"},
           settle={"policy": "echoes", "key": "phase_deg", "tol": 0.5},
           help="Phase of the wheel against the reference; changeable while running."),

        # ---- blade and reference modes (standby only) ----------------------
        _p("blade", "Blade", "control", "enum", group="Blade", order=40,
           options=ch.blade_options(), read_path=["blade"],
           set={"verb": "set_blade", "arg": "blade"},
           settle={"policy": "immediate"},
           help="Standby only. The frequency range follows the blade."),
        _p("ref_mode", "Reference in", "control", "enum", group="Blade", order=41,
           options=list(refs), read_path=["ref_mode"],
           set={"verb": "set_ref_mode", "arg": "mode"},
           settle={"policy": "immediate"},
           help="Standby only. internal = crystal synthesiser; external = EXT REF IN. "
                "On a 10/100 blade, -inner / -outer picks the ring that is locked."),
        _p("output_mode", "Reference out", "control", "enum", group="Blade", order=42,
           options=list(outs), read_path=["output_mode"],
           set={"verb": "set_output_mode", "arg": "mode"},
           settle={"policy": "immediate"},
           help="Standby only. A sensor mode (actual / outer / inner) lets the module "
                "MEASURE the wheel; 'target' leaves it blind (lock by timer)."),

        # ---- external reference ------------------------------------------
        _p("nharmonic", "Harmonic multiplier N", "control", "int", group="External",
           order=50, min=1, max=15, step=1, read_path=["nharmonic"],
           set={"verb": "set_harmonics", "arg": "n"},
           settle={"policy": "echoes", "key": "nharmonic", "tol": 0.5},
           help="Standby only. External reference: wheel = EXT REF IN x N / D."),
        _p("dharmonic", "Harmonic divider D", "control", "int", group="External",
           order=51, min=1, max=15, step=1, read_path=["dharmonic"],
           set={"verb": "set_harmonics", "arg": "d"},
           settle={"policy": "echoes", "key": "dharmonic", "tol": 0.5}),
        _p("input_frequency", "EXT REF IN frequency", "indicator", "float", unit="Hz",
           group="External", order=52, decimals=2, read_path=["input_frequency_Hz"]),

        # ---- status ------------------------------------------------------
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "chopper",
                "label": "Optical chopper", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
