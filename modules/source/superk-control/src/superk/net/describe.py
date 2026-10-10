"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of, and
scan-core can turn into Settables / Gettables / actions. Full contract:
INSTRUMENT_MODULE_GUIDE.md section 6b.

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every limit is looked up from cfg or from the brain's live
state when the manifest is built, never copied into a literal.

DYNAMIC LIMITS: the wavelength range of every line IS the active AOTF crystal's
range (VIS-nIR, nIR2 or IR -- one RF driver, one crystal at a time). Switching
the crystal changes min/max of 8 wavelength controls, so `revision` (a CRC over
the manifest without values) moves and every status frame's `describe_rev`
tells clients to re-fetch.

SAFETY: switching emission on is an ACTION flagged `danger` (a control panel
asks before firing it). It is not a sweepable control on purpose: nobody
should scan a class 4 laser's emission as an axis. A scan routine can still run
it (its `wait` block waits until the laser REPORTS emission).
"""

from __future__ import annotations

import json
import zlib

from ..config import N_LINES
from ..laser import EMISSION_STATES

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, wait=None, danger=False, help="", bits=None,
       ramp=None):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract.

    `type` (and an indicator's min/max/bits) also tells scan-core how to STORE
    the value (developer notes 4b): an indicator's min/max/bits are a promise
    the instrument cannot break, so they are only given where that is true."""
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
                 ("wait", wait), ("help", help), ("bits", bits), ("ramp", ramp)):
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

    A list of keys rather than a dotted string; an int element indexes into a
    per-line list (index 0 = line 1).
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


#: The sweep pace a client is offered first (nm/s): 5 nm/s crosses a 50 nm
#: feature in 10 s -- slow enough for a lock-in at a few ms time constant.
SWEEP_RATE_DEFAULT_NM_PER_S = 5.0


def _line_params(laser, n: int) -> list[dict]:
    """Wavelength + amplitude controls of line n (1-based), expanded FLAT so a
    panel can place one and scan-core can sweep one (guide section 6b)."""
    lo, hi = laser.wavelength_range()
    lim = laser.cfg.limits
    r_lo, r_hi = laser.ramp_rate_limits()
    i = n - 1
    group = "Line 1 (scan)" if n == 1 else f"Line {n}"
    return [
        # Settle = the driver's wavelength register READ BACK equals the
        # command. The AOTF itself retunes in microseconds (only the RF
        # frequency changes), so the echo is the honest and sufficient check.
        # Tolerance: the register stores whole picometres.
        _p(f"wavelength_{n}", f"Line {n} wavelength", "control", "float",
           unit="nm", group=group, order=10 * n, decimals=3, step=1.0,
           plottable=(n == 1), min=lo, max=hi,
           read_path=["wavelength_nm", i],
           set={"verb": "set_wavelength", "arg": "wavelength_nm",
                "extra": {"line": n}},
           settle={"policy": "echoes", "key": "wavelength_nm", "index": i,
                   "tol": 0.002},
           # A CONTINUOUS SWEEP for fly scans (2026-10-10; guide 6b,
           # "Ramps"): the SERVICE walks this line's wavelength register
           # (softramp.py) and records every value it sent; the fly scan bins
           # by that COMMANDED wavelength (the AOTF follows its RF frequency
           # in microseconds). One sweep at a time over all lines; the record
           # holds all 8 lines in one group, the unswept ones forward-filled.
           ramp={"kind": "software",
                 "start": {"verb": "ramp_wavelength",
                           "args": {"to": "wavelength_nm", "rate": "rate_nm_per_s"},
                           "extra": {"line": n}},
                 "stop": {"verb": "ramp_stop"},
                 "rate": {"unit": "nm/s", "min": r_lo, "max": r_hi,
                          "default": max(r_lo, min(r_hi, SWEEP_RATE_DEFAULT_NM_PER_S))},
                 "readback": {"stream": {"group": "ramp", "channel": f"wavelength_{n}"},
                              "measured": False},
                 "done": {"key": "ramping", "id_key": "ramp_id"}},
           help=f"Limited to the active crystal ({laser.active_filter()}, "
                f"{lo:g}..{hi:g} nm)."),
        _p(f"amplitude_{n}", f"Line {n} amplitude", "control", "float",
           unit="%", group=group, order=10 * n + 1, decimals=1, step=1.0,
           plottable=(n == 1), min=0.0, max=lim.amplitude_max_pct,
           read_path=["amplitude_pct", i],
           set={"verb": "set_amplitude", "arg": "amplitude_pct",
                "extra": {"line": n}},
           settle={"policy": "echoes", "key": "amplitude_pct", "index": i,
                   "tol": 0.051},
           help="RF amplitude of this AOTF channel; 0 % = line off. The "
                "diffraction efficiency saturates, so more is not always more light."),
    ]


def build_manifest(laser) -> dict:
    """The SuperK EXTREME + SELECT: a set-and-forget source with safety actions."""
    lim = laser.cfg.limits
    params = [
        # ---- laser -------------------------------------------------------
        # Action ids ARE the verbs (a control panel and scan-core send the id).
        _p("emission_on", "Emission ON", "action", "action", group="Laser",
           order=1, danger=True,
           wait={"ready": {"policy": "flag_only", "key": "emission_on"},
                 "timeout_s": 60},
           help="CLASS 4 LASER. Refused while the interlock is not OK. Finished "
                "when the laser reports emission."),
        _p("emission_off", "Emission OFF", "action", "action", group="Laser",
           order=2,
           wait={"ready": {"policy": "flag_only", "key": "emission_on",
                           "invert": True},
                 "timeout_s": 30}),
        _p("reset_interlock", "Reset interlock", "action", "action",
           group="Laser", order=3, wait={"ready": {"policy": "immediate"}},
           help="Acknowledge a closed interlock. Does not switch emission on."),
        _p("power", "Power level", "control", "float", unit="%", group="Laser",
           order=10, decimals=1, step=1.0, plottable=True,
           min=lim.power_min_pct, max=lim.power_max_pct,
           read_path=["power_pct"],
           set={"verb": "set_power", "arg": "power_pct"},
           settle={"policy": "echoes", "key": "power_pct", "tol": 0.051},
           help="EXTREME power level. The ceiling is limits.power_max_pct in the "
                ".ini, deliberately below 100 %."),
        _p("emission", "Emission", "indicator", "bool", group="Laser", order=20,
           read_path=["emission_on"]),
        # an enum, not a string: the brain produces only these values
        # (laser.EMISSION_STATES), so scan-core can store a 1-byte code
        _p("emission_state", "Emission state", "indicator", "enum",
           group="Laser", order=21, options=list(EMISSION_STATES),
           read_path=["emission_state"]),
        _p("emission_guarded", "Lost-client guard", "indicator", "bool",
           group="Laser", order=22, read_path=["emission_guarded"],
           help="True while a remote GUI owns the emission: if it falls silent "
                "for hardware.client_timeout_s the service switches emission "
                "off. Emission switched on by a scan routine is not guarded."),
        _p("interlock_ok", "Interlock OK", "indicator", "bool", group="Laser",
           order=22, read_path=["interlock_ok"]),
        _p("interlock", "Interlock", "indicator", "string", group="Laser",
           order=23, read_path=["interlock"]),
        _p("inlet_temp", "Inlet temperature", "indicator", "float", unit="C",
           group="Laser", order=24, decimals=1, plottable=True,
           read_path=["inlet_temp_C"]),

        # ---- AOTF --------------------------------------------------------
        _p("rf", "AOTF RF", "control", "bool", group="Filter", order=1,
           read_path=["rf_on"],
           set={"verb": "set_rf", "arg": "on"},
           settle={"policy": "echoes", "key": "rf_on"},
           help="RF drive of the active crystal. Off = no line comes out."),
        _p("filter", "Crystal", "control", "enum", group="Filter", order=2,
           options=laser.filter_names(), read_path=["filter"],
           set={"verb": "set_filter", "arg": "filter"},
           # no settle block: an enum is not scannable in scan-core (its
           # Settables are numeric), and the switch is done when the reply
           # comes (the brain selects the crystal inside the command).
           help="Which AOTF crystal the single RF driver drives. Changes the "
                "wavelength limits of every line. A crystal in the other SELECT "
                "housing needs the RF cable moved by hand (refused otherwise)."),
        _p("filter_min", "Crystal min", "indicator", "float", unit="nm",
           group="Filter", order=3, decimals=1, read_path=["filter_min_nm"]),
        _p("filter_max", "Crystal max", "indicator", "float", unit="nm",
           group="Filter", order=4, decimals=1, read_path=["filter_max_nm"]),
        # bits 8: the driver's "connected crystal" register is ONE byte
        # (nktp.read_crystal reads it with _r8), so 0..255 is everything it
        # can report -- a promise the hardware keeps, not a guess at NKT's
        # numbering (which a firmware might extend).
        _p("crystal_no", "Connected crystal", "indicator", "int",
           group="Filter", order=5, read_path=["crystal"], bits=8,
           help="NKT's number of the crystal the RF driver reaches, READ from "
                "the driver (1, 2 = the SELECT with the lower bus address, "
                "3, 4 = the other; 0 = none)."),
        # the sweep's stop is a SAFETY verb (a viewer may send it), and every
        # safety verb is also an action, so the suite's Control tab offers it
        _p("ramp_stop", "Stop sweep", "action", "action", group="Filter", order=7,
           wait={"ready": {"policy": "immediate"}},
           help="End a wavelength sweep (ramp_wavelength) where it is. The "
                "verb returns once no further step will be written."),
        _p("ramping", "Sweeping", "indicator", "bool", group="Filter", order=6,
           read_path=["ramping"],
           help="True while a wavelength sweep (ramp_wavelength) walks a line."),
        _p("crystal_temp", "Crystal temperature", "indicator", "float",
           unit="C", group="Filter", order=5, decimals=1, plottable=True,
           read_path=["crystal_temp_C"]),
    ]
    for n in range(1, N_LINES + 1):
        params += _line_params(laser, n)
    params += [
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "superk",
                "label": "Supercontinuum laser", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
