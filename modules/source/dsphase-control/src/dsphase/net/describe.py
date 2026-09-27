"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of.
Two consumers: the reconfigurable control screen reads it directly, scan-core
projects it into Settables and Gettables. Contract: INSTRUMENT_MODULE_GUIDE.md
section 6b.

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every limit and step is looked up from cfg when the manifest is
built. In particular the phase STEP and the settle TOLERANCE come from
cfg.device.phase_step_deg -- set a PS6000P's 5.625 deg there and the manifest,
its revision, and every client's slider follow.
"""

from __future__ import annotations

import json
import zlib

from ..phasemath import decimals_for

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, help=""):
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
                 ("set", set), ("settle", settle), ("args", args),
                 ("help", help)):
        if v is not None and v != "":
            d[k] = v
    if danger:
        d["danger"] = True
    return d


def manifest_revision(manifest: dict) -> int:
    """A checksum over the parts of the manifest a client must react to.

    Derived, not hand-bumped -- a counter someone has to remember to increment
    is a counter that will eventually be wrong. `value` is excluded: it changes
    all the time and travels in the status stream anyway.
    """
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` against a status dict (a list of keys,
    because ids may contain dots). Returns None for a missing branch."""
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


def build_manifest(brain) -> dict:
    """The phase shifter: set-and-forget, so every settle rule is `echoes` -- but
    the echoed values are READBACKS from the unit (the brain's worker reads it
    back), so an echo means "the unit holds it", not merely "we remember it".

    The tolerance is half a step: a scan may ask for 33.3 deg, the unit can
    only hold 33.5, and 33.5 is the honest arrival.
    """
    cfg = brain.cfg
    lim, dev = cfg.limits, cfg.device
    ph_step, att_step = float(dev.phase_step_deg), float(dev.att_step_dB)
    params = [
        _p("phase", "Phase shift", "control", "float", unit="deg",
           group="Phase", order=10, plottable=True,
           min=lim.phase_min_deg, max=lim.phase_max_deg,
           step=ph_step, decimals=decimals_for(ph_step),
           read_path=["phase_deg"],
           set={"verb": "set_phase", "arg": "phase_deg"},
           settle={"policy": "echoes", "key": "phase_deg",
                   "tol": max(ph_step, 0.0) / 2.0 + 1e-6},
           help=f"Rounded to the {ph_step:g} deg device step and wrapped into "
                "-180..+180 before sending; reported back in the branch you asked "
                "for, so a 0..360 sweep echoes its own numbers."),

        _p("attenuation", "Output attenuation", "control", "float", unit="dB",
           group="Output", order=20, plottable=True,
           min=lim.att_min_dB, max=lim.att_max_dB,
           step=att_step, decimals=decimals_for(att_step),
           read_path=["attenuation_dB"],
           set={"verb": "set_attenuation", "arg": "attenuation_dB"},
           settle={"policy": "echoes", "key": "attenuation_dB",
                   "tol": max(att_step, 0.0) / 2.0 + 1e-6},
           help="Output step attenuator. 0 dB = full output (about +10 dBm)."),

        _p("output_on", "RF output", "control", "bool", group="Output", order=10,
           read_path=["output_on"],
           set={"verb": "set_output", "arg": "on"},
           settle={"policy": "echoes", "key": "output_on"},
           help="Output on/off, read back from the unit."),

        _p("frequency", "Carrier frequency", "control", "float", unit="MHz",
           group="Phase", order=20, decimals=3, step=1.0,
           min=lim.freq_min_MHz, max=lim.freq_max_MHz,
           read_path=["frequency_MHz"],
           set={"verb": "set_frequency", "arg": "frequency_MHz"},
           # Not read back from the unit (it has no such query), so an echo
           # would only confirm our own memory; say so honestly.
           settle={"policy": "immediate"},
           help=("The carrier you feed in. Sent to the unit ONLY if a frequency "
                 "command is configured (device.freq_command); otherwise it "
                 "just selects the datasheet accuracy band and is filed with "
                 "the data.")),

        _p("phase_device", "Phase (device, -180..180)", "indicator", "float",
           unit="deg", group="Phase", order=30, decimals=decimals_for(ph_step),
           read_path=["phase_device_deg"]),
        _p("accuracy", "Datasheet phase accuracy", "indicator", "float",
           unit="deg", group="Phase", order=40, decimals=1,
           read_path=["accuracy_deg"],
           help="PS6000L datasheet +- band for this carrier and setting."),

        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "dsphase",
                "label": "RF phase shifter", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
