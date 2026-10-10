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

Nothing here has dynamic limits: the RF ranges come from the config's
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
#: second (clamped to the configured paces): 10 MHz/s -- a 100 MHz FMR line
#: in 10 s, slow enough for a lock-in at a few ms time constant; 1 dB/s;
#: 10 deg/s.
SWEEP_RATE_DEFAULTS = {"frequency": 10.0e6, "power": 1.0, "phase": 10.0}


def sweep_block(gen, knob: str, *, wire_arg: str, rate_arg: str, rate_unit: str,
                scale: float = 1.0) -> dict:
    """The `ramp` block of one knob (guide 6b, "Ramps"): a CONTINUOUS SWEEP a
    fly scan can fly. The SERVICE walks the knob (softramp.py) and records
    every value it sent; the fly scan bins by that COMMANDED value
    (measured: false -- why, see generator.py "the SWEEPS"). `to` and the
    rate are scaled like the set (MHz in the scan, Hz on the wire); the
    limits are the live config paces, never literals."""
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


def build_manifest(gen) -> dict:
    """The RF generator: pure set-and-forget, so every settle rule is `echoes`.

    Nothing here converges -- there is no "settled" flag to wait on, because
    there is nothing to wait for. What the service does report is the value it
    is currently holding, so `echoes` waits for that confirmation. Weaker than a
    closed-loop settle, but a real check rather than a sleep.
    """
    lim = gen.cfg.limits
    params = [
        _p("rf_on", "RF output", "control", "bool", group="Output", order=10,
           read_path=["rf_on"],
           set={"verb": "set_rf", "arg": "on"},
           settle={"policy": "echoes", "key": "rf_on"},
           help="Master RF on/off."),

        # Scanned in MHz, commanded and published in Hz. `scale` is on the
        # descriptor rather than inside `set`, so reading and setting cannot
        # disagree -- a scale applied to one direction only would be a
        # factor-of-a-million bug that looks like a broken instrument.
        _p("frequency", "Frequency", "control", "float", unit="MHz",
           group="Signal", order=20, decimals=6, plottable=True,
           min=lim.freq_min_Hz / 1e6, max=lim.freq_max_Hz / 1e6,
           scale=1e6, read_path=["frequency_Hz"],
           set={"verb": "set_frequency", "arg": "frequency_Hz"},
           settle={"policy": "echoes", "key": "frequency_Hz", "tol": 1.0},
           ramp=sweep_block(gen, "frequency", wire_arg="frequency_Hz",
                            rate_arg="rate_Hz_per_s", rate_unit="MHz/s", scale=1e6)),

        _p("power", "Power", "control", "float", unit="dBm", group="Signal",
           order=30, decimals=2, plottable=True,
           min=lim.power_min_dBm, max=lim.power_max_dBm, step=0.1,
           read_path=["power_dBm"],
           set={"verb": "set_power", "arg": "power_dBm"},
           settle={"policy": "echoes", "key": "power_dBm", "tol": 1e-3},
           ramp=sweep_block(gen, "power", wire_arg="power_dBm",
                            rate_arg="rate_dB_per_s", rate_unit="dB/s"),
           help="Ranges are option-dependent; widen the limits in the .ini if "
                "your unit has the high-power option."),

        _p("phase", "Phase", "control", "float", unit="deg", group="Signal",
           order=40, decimals=2, step=1.0,
           min=lim.phase_min_deg, max=lim.phase_max_deg,
           read_path=["phase_deg"],
           set={"verb": "set_phase", "arg": "phase_deg"},
           settle={"policy": "echoes", "key": "phase_deg", "tol": 1e-3},
           ramp=sweep_block(gen, "phase", wire_arg="phase_deg",
                            rate_arg="rate_deg_per_s", rate_unit="deg/s")),

        _p("ramping", "Sweeping", "indicator", "bool", group="Signal", order=45,
           read_path=["ramping"],
           help="True while a sweep (ramp_frequency / ramp_power / ramp_phase) "
                "walks a knob."),

        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        # the SAFETY verb as a button (control.py: a viewer may always send
        # it), so the suite's Control tab offers it to a viewer too
        _p("rf_off", "RF off", "action", "action", group="Output", order=90,
           help="Switch the RF output off. Allowed for anyone, also a viewer."),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "smb",
                "label": "RF signal generator", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
