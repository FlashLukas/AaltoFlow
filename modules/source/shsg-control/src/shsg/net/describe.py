"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of.

Two consumers, two projections of one source: the reconfigurable control screen
reads this manifest DIRECTLY (it wants buttons, their arguments, their danger
flags and the group/order layout hints), while scan-core projects it into
Settables and Gettables. Full contract: INSTRUMENT_MODULE_GUIDE.md section 6b.

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every limit is looked up from cfg or from live status when the
manifest is built, never copied into a literal. A manifest that restates a limit
is a limit with two homes, and the wrong one does not announce itself -- it just
draws a slider with the wrong range.

Nothing here has dynamic limits: the TG ranges come from the config's
safety envelope and only change when someone edits it. `revision` still
travels in every status frame as `describe_rev`, so a client notices an
edited envelope without polling the whole manifest.
"""

from __future__ import annotations

import json
import zlib

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1

#: The sweep pace a client is offered first, per knob, in WIRE units per
#: second (clamped to the configured paces): 10 MHz/s -- a 100 MHz line in
#: 10 s, slow enough for a detector at a few ms time constant; 1 dB/s.
SWEEP_RATE_DEFAULTS = {"frequency": 10.0e6, "power": 1.0}


def sweep_block(gen, knob: str, *, wire_arg: str, rate_arg: str, rate_unit: str,
                scale: float = 1.0) -> dict:
    """The `ramp` block of one knob (guide 6b, "Ramps"): a CONTINUOUS SWEEP a
    fly scan can fly. The SERVICE walks the knob (softramp.py), one tg_cw to
    the signalhound service per step, and records every value it sent; the
    fly scan bins by that COMMANDED value (measured: false -- the owner's
    echo is the stored setting, not a measurement; why, see generator.py
    "the SWEEPS"). `to` and the rate are scaled like the set (MHz in the
    scan, Hz on the wire); the limits are the live config paces, never
    literals."""
    lo, hi = gen._rate_limits(knob)
    default = max(lo, min(hi, SWEEP_RATE_DEFAULTS[knob]))
    return {"kind": "software",
            "start": {"verb": f"ramp_{knob}",
                      "args": {"to": wire_arg, "rate": rate_arg}},
            # stops THIS knob's sweep only (no `knob`: every sweep)
            "stop": {"verb": "ramp_stop", "extra": {"knob": knob}},
            "rate": {"unit": rate_unit, "min": lo / scale, "max": hi / scale,
                     "default": default / scale},
            "readback": {"stream": {"group": "ramp", "channel": knob},
                         "measured": False},
            "done": {"key": f"{knob}_ramping", "id_key": f"{knob}_ramp_id"}}


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, help="", ramp=None):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract."""
    d = {
        "id": id, "label": label, "kind": kind, "type": type,
        "unit": unit, "group": group, "order": order,
        "writable": (kind == "control") if writable is None else writable,
        "plottable": plottable,
        "read_path": read_path,      # keys/indices into the status dict, or None
    }
    for k, v in (("value", value), ("min", min), ("max", max), ("step", step),
                 ("decimals", decimals), ("options", options), ("scale", scale),
                 ("set", set), ("settle", settle), ("args", args), ("ramp", ramp),
                 ("help", help)):
        if v is not None and v != "":
            d[k] = v
    if danger:
        d["danger"] = True
    return d


def manifest_revision(manifest: dict) -> int:
    """A checksum over the parts of the manifest a client must react to.

    Derived, not hand-bumped -- a counter someone has to remember to increment
    is a counter that will eventually be wrong. `value` is excluded on purpose:
    it changes many times a second and travels in the status stream anyway, so
    including it would tell clients to re-fetch constantly and mean nothing.
    """
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` against a status dict.

    A list of keys rather than a dotted string, because channel names and ids
    can contain dots and a dotted path could not be split back apart. An int
    element indexes into a per-axis list.
    """
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


def _echo_while_free(key: str, tol: float) -> dict:
    """Settle rule: OUR value is echoed AND the TG is ready (`tg_ready`).

    Two traps, one rule. (1) Commands are fire-and-forget, so right after a set
    the status still shows the OLD value -- we wait for the echo of ours (and
    for the real TG that echo is the signalhound service's APPLIED value, never
    our request). (2) While a network-analyser sweep holds the TG the echo may
    well match -- the owner keeps the CW settings to restore afterwards -- but
    no CW is coming out. Same when the owner cannot read the TG (`tg_unknown`):
    its numbers mean nothing. `tg_ready` is true only when connected, no
    hw_error, not busy and the state known, so `adopt_then_flag` on it says
    exactly what we mean: believe the frame only once it shows our value, and
    then only if the TG is really delivering it.
    """
    return {"policy": "adopt_then_flag", "setpoint_key": key,
            "flag_key": "tg_ready", "tol": tol}


def build_manifest(gen) -> dict:
    """The TG as a CW source: three controls, a few indicators.

    Limits are read from cfg every time (the TG44A's range by default), and the
    echo tolerances too (hardware.echo_tol_*), so a tightened envelope or a
    measured rounding step shows up here without an edit.
    """
    lim = gen.cfg.limits
    hw = gen.cfg.hardware
    params = [
        _p("rf_on", "CW output (off = parked)", "control", "bool", group="Output",
           order=10, read_path=["rf_on"],
           set={"verb": "set_rf", "arg": "on"},
           settle=_echo_while_free("rf_on", 1e-6),
           help="The TG44A cannot be silenced: OFF parks it at the park frequency "
                "and minimum level (see park_Hz / park_dBm)."),
        _p("parked", "Parked (RF off)", "indicator", "bool", group="Output",
           order=11, read_path=["parked"]),
        _p("park_Hz", "Park frequency", "indicator", "float", unit="MHz",
           group="Output", order=12, decimals=6, scale=1e6, read_path=["park_Hz"]),
        _p("park_dBm", "Park level", "indicator", "float", unit="dBm",
           group="Output", order=13, decimals=2, read_path=["park_dBm"]),

        # Scanned in MHz, commanded and published in Hz. `scale` is on the
        # descriptor rather than inside `set`, so reading and setting cannot
        # disagree -- a scale applied to one direction only would be a
        # factor-of-a-million bug that looks like a broken instrument.
        _p("frequency", "Frequency", "control", "float", unit="MHz",
           group="Signal", order=20, decimals=6, plottable=True,
           min=lim.freq_min_Hz / 1e6, max=lim.freq_max_Hz / 1e6,
           scale=1e6, read_path=["frequency_Hz"],
           set={"verb": "set_frequency", "arg": "frequency_Hz"},
           settle=_echo_while_free("frequency_Hz", float(hw.echo_tol_Hz)),
           ramp=sweep_block(gen, "frequency", wire_arg="frequency_Hz",
                            rate_arg="rate_Hz_per_s", rate_unit="MHz/s", scale=1e6)),

        _p("power", "Level", "control", "float", unit="dBm", group="Signal",
           order=30, decimals=2, plottable=True,
           min=lim.power_min_dBm, max=lim.power_max_dBm, step=0.5,
           read_path=["power_dBm"],
           set={"verb": "set_power", "arg": "power_dBm"},
           settle=_echo_while_free("power_dBm", float(hw.echo_tol_dB)),
           ramp=sweep_block(gen, "power", wire_arg="power_dBm",
                            rate_arg="rate_dB_per_s", rate_unit="dB/s"),
           help="The TG44A covers about -30..-10 dBm."),

        _p("ramping", "Sweeping", "indicator", "bool", group="Signal", order=45,
           read_path=["ramping"],
           help="True while a sweep (ramp_frequency / ramp_power) walks a knob. "
                "A sweep never switches the CW on: while parked it only walks "
                "the stored CW setting."),

        _p("tg_busy", "TG busy (SNA sweep)", "indicator", "bool", group="Status",
           order=1, read_path=["tg_busy"],
           help="A scalar-network-analyser sweep holds the tracking generator; "
                "commands are refused until it ends."),
        _p("tg_unknown", "TG state unknown", "indicator", "bool", group="Status",
           order=2, read_path=["tg_unknown"],
           help="The analyser cannot read the TG's state -- it may be emitting. "
                "Set frequency, level and CW on/off explicitly."),
        _p("tg_ready", "TG ready", "indicator", "bool", group="Status",
           order=3, read_path=["tg_ready"]),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=4, read_path=["connected"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=5, read_path=["hw_error"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=6,
           read_path=["idn"]),
        # the SAFETY verb as a button (control.py: a viewer may always send
        # it), so the suite's Control tab offers it to a viewer too
        _p("rf_off", "RF off (park)", "action", "action", group="Signal", order=90,
           help="Park the tracking generator (10 kHz / -30 dBm): the only way "
                "'off' exists on the TG. Allowed for anyone, also a viewer."),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "shsg",
                "label": "Signal generator (Signal Hound TG)", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
