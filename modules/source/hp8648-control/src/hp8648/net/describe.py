"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of.

Two consumers, two projections of one source: the reconfigurable control screen
reads this manifest DIRECTLY, while scan-core projects it into Settables and
Gettables. Full contract: INSTRUMENT_MODULE_GUIDE.md section 6b.

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every limit is looked up from cfg, from spec.py or from the
brain when the manifest is built, never copied into a literal.

THE LIMIT THAT MOVES: the power ceiling depends on the CURRENT frequency (the
8648D is specified to +13 dBm up to 2500 MHz and +10 dBm above). So `power.max`
is the brain's live ceiling, and `revision` -- a checksum over the manifest --
changes when the frequency crosses 2500 MHz. Every status frame carries it as
`describe_rev`, so a client notices without polling the whole manifest.
"""

from __future__ import annotations

import json
import zlib

from .. import spec

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
    is a counter that will eventually be wrong. `value` is excluded on purpose:
    it changes all the time and travels in the status stream anyway.
    """
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` against a status dict.

    A list of keys rather than a dotted string, because ids can contain dots.
    An int element indexes into a list.
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


def build_manifest(src) -> dict:
    """The CW generator: pure set-and-forget, so every settle rule is `echoes`.

    Nothing converges -- there is no "settled" flag, because there is nothing
    to wait for beyond the synthesiser switching. The brain reads back only
    after the switching time, so waiting for the ECHO of the value is a real
    "it is there" check, not just "it was accepted".

    The echo tolerances are the instrument's RESOLUTION (spec.py), not a
    guess: the box keeps 10 Hz and 0.1 dB, so a scan asking for -12.34 dBm is
    answered with -12.3 and must still count as settled.
    """
    ceiling = src.power_ceiling()
    f_lo, f_hi = src.freq_limits()      # envelope, never wider than the box
    params = [
        _p("rf_on", "RF output", "control", "bool", group="Output", order=10,
           read_path=["rf_on"],
           set={"verb": "set_rf", "arg": "on"},
           settle={"policy": "echoes", "key": "rf_on"},
           help="Master RF on/off. Adopted from the instrument at start "
                "(never switched by connecting); OFF when the service stops. "
                "Switching ON also re-arms a tripped reverse-power protection."),

        # Scanned in MHz, commanded and published in Hz. `scale` sits on the
        # descriptor so reading and setting cannot disagree.
        _p("frequency", "Frequency", "control", "float", unit="MHz",
           group="Signal", order=20, decimals=5, plottable=True,
           min=f_lo / 1e6, max=f_hi / 1e6,
           scale=1e6, read_path=["frequency_Hz"],
           set={"verb": "set_frequency", "arg": "frequency_Hz"},
           settle={"policy": "echoes", "key": "frequency_Hz",
                   "tol": spec.FREQ_RESOLUTION_HZ},
           help="CW frequency, 10 Hz resolution. Crossing 2500 MHz changes "
                "the power ceiling (and this manifest's revision)."),

        _p("power", "Power", "control", "float", unit="dBm", group="Signal",
           order=30, decimals=1, plottable=True, step=spec.POWER_RESOLUTION_DB,
           min=src.power_floor(), max=ceiling,
           read_path=["power_dBm"],
           set={"verb": "set_power", "arg": "power_dBm"},
           settle={"policy": "echoes", "key": "power_dBm",
                   "tol": spec.POWER_RESOLUTION_DB / 2 + 1e-6},
           help="Output level, 0.1 dB resolution. The maximum is LIVE: the "
                "tighter of your envelope and the instrument's specified "
                "maximum at the current frequency."),

        _p("power_ceiling", "Power ceiling", "indicator", "float", unit="dBm",
           group="Signal", order=35, decimals=1,
           read_path=["power_ceiling_dBm"]),
        _p("rpp_tripped", "Reverse power tripped", "indicator", "bool",
           group="Status", order=3, read_path=["rpp_tripped"],
           help="The instrument's reverse-power protection fired and turned "
                "the RF off. Remove the source, then switch RF on to re-arm."),
        _p("level_unspecified", "Level outside spec", "indicator", "bool",
           group="Status", order=4, read_path=["level_unspecified"]),
        _p("modulation_off", "All modulation off", "indicator", "bool",
           group="Status", order=5, read_path=["modulation_off"]),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=6, read_path=["hw_error"]),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "hp8648",
                "label": "RF generator (HP 8648D)", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
