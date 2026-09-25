"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of. It
is the same idea as camera-control's GenICam-style `features()`, promoted from
one camera's parameter panel to a suite-wide contract.

Who uses it:
  * the reconfigurable control screen -- pick a module, pick parameters, drop
    numeric readouts and graphs onto a panel
  * scan-core -- turns the manifest straight into Settables and Gettables, so
    the coordinator needs no per-instrument code at all
  * anything else that wants a knob list without importing this package

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every limit is *looked up* from cfg or the calibration when the
manifest is built, never copied into a literal. The old LabVIEW VI was painful
because adding one knob meant editing seven places; a hand-maintained manifest
would quietly become the eighth, and a wrong limit in a manifest does not
announce itself -- it just draws a slider with the wrong range.

LIMITS ARE NOT STATIC. clMag's field range comes from the loaded calibration, so
it is 0..0 with no calibration and changes the moment one is loaded or measured.
Clients therefore cannot fetch this once and cache it forever. `revision` (also
published in every status frame as `describe_rev`) changes whenever the
structure or the bounds change, so a client can compare cheaply and re-fetch
only when it matters. It is *derived* from the manifest content rather than
bumped by hand, because a counter someone has to remember to increment is a
counter that will be wrong.
"""

from __future__ import annotations

import json
import zlib

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, set=None, settle=None,
       args=None, danger=False, help=""):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract."""
    d = {
        "id": id, "label": label, "kind": kind, "type": type,
        "unit": unit, "group": group, "order": order,
        "writable": (kind == "control") if writable is None else writable,
        "plottable": plottable,
        "read_path": read_path,      # list of keys into the status dict, or None
    }
    for k, v in (("value", value), ("min", min), ("max", max), ("step", step),
                 ("decimals", decimals), ("options", options), ("set", set),
                 ("settle", settle), ("args", args), ("help", help)):
        if v is not None and v != "":
            d[k] = v
    if danger:
        d["danger"] = True
    return d


def build_manifest(ctrl) -> dict:
    """The full manifest, with every bound read live from cfg / calibration."""
    cfg = ctrl.cfg
    lim = cfg.limits
    imax = lim.current_max_A

    # The field range IS the calibration range -- with no calibration the
    # service refuses every setpoint, so advertising a range would be a lie.
    cal = getattr(ctrl, "calibration", None)
    if cal is not None and getattr(cal, "currents_A", None):
        f_lo, f_hi = cal.range_mT
    else:
        f_lo = f_hi = None

    params = [
        # ---- controls ------------------------------------------------------
        _p("field", "Magnetic field", "control", "float", unit="mT",
           group="Field", order=10, min=f_lo, max=f_hi,
           step=lim.field_step_mT, decimals=3, plottable=True,
           read_path=["measured_field_mT"],
           set={"verb": "set_field", "arg": "field_mT",
                "extra": {"use_pid": True}},
           settle={"policy": "adopt_then_flag",
                   "setpoint_key": "setpoint_field_mT",
                   "flag_key": "field_stable"},
           help="Closed-loop setpoint. The service ramps, runs a PI seek and "
                "raises field_stable when it has held within tolerance."
           if f_lo is not None else
           "NO CALIBRATION LOADED -- the service will refuse any setpoint. "
           "Run a calibration or load a saved one first."),

        _p("current", "Magnet current", "control", "float", unit="A",
           group="Field", order=20, min=-imax, max=imax, step=0.05,
           decimals=3, plottable=True, read_path=["current_A"],
           set={"verb": "set_current", "arg": "current_A"},
           settle={"policy": "state_in", "key": "state",
                   "states": ["IDLE", "HOLD"]},
           help="Direct current control, bypassing the field loop."),

        _p("locked", "Lock output", "control", "bool", group="Safety", order=30,
           read_path=["locked"], set={"verb": "set_lock", "arg": "locked"},
           settle={"policy": "echoes", "key": "locked"}),

        _p("stabilizer", "Long-term stabilizer", "control", "bool",
           group="Field", order=40,
           # Deliberately no read_path: the service does not publish this in
           # status, so a client must not pretend to show its state.
           set={"verb": "set_stabilizer", "arg": "enabled"},
           settle={"policy": "immediate"},
           help="Slow drift correction; only acts in IDLE/HOLD."),

        # ---- indicators ----------------------------------------------------
        _p("state", "State", "indicator", "string", group="Status", order=1,
           read_path=["state"],
           options=["IDLE", "RAMPING", "SEEK", "STABLE", "HOLD", "DEMAG",
                    "CALIBRATE"]),
        _p("setpoint_field", "Field setpoint", "indicator", "float", unit="mT",
           group="Status", order=2, decimals=3, read_path=["setpoint_field_mT"]),
        _p("measured_field", "Measured field", "indicator", "float", unit="mT",
           group="Status", order=3, decimals=3, plottable=True,
           read_path=["measured_field_mT"]),
        _p("field_stable", "Field stable", "indicator", "bool",
           group="Status", order=4, read_path=["field_stable"]),

        # ---- actions -------------------------------------------------------
        _p("demag", "Demagnetise", "action", "action", group="Routines",
           order=50, danger=True,
           args=[{"name": "amplitude_A", "label": "Start amplitude",
                  "type": "float", "unit": "A", "default": 1.5,
                  "min": 0.0, "max": imax}],
           settle={"policy": "state_in", "key": "state", "states": ["IDLE"]},
           help="Decaying alternating current steps down to zero."),

        _p("calibrate", "Run calibration", "action", "action", group="Routines",
           order=60, danger=True,
           args=[{"name": "n_per_leg", "label": "Points per leg",
                  "type": "int", "default": 50, "min": 5, "max": 500},
                 {"name": "dwell_s", "label": "Dwell", "type": "float",
                  "unit": "s", "default": 0.5, "min": 0.0, "max": 10.0}],
           settle={"policy": "state_in", "key": "state", "states": ["IDLE"]},
           help="Sweeps current and records the B-vs-I curve. Replaces the "
                "active calibration, and so changes the field limits."),
    ]

    params += _aux_params(cfg)
    manifest = {
        "schema": SCHEMA_VERSION,
        "module": "clMag",
        "label": "Magnet field controller",
        "parameters": params,
    }
    manifest["revision"] = manifest_revision(manifest)
    return manifest


def _aux_params(cfg) -> list:
    """The AUX I/O channels on the same NI card, expanded one id per channel.

    Flat per-channel ids ("aux_ao0", not "aux_ao" plus an index argument) so a
    control screen can place a single channel on a panel, and so scan-core can
    sweep or record one without special handling.
    """
    aux = getattr(cfg, "aux", None)
    if aux is None:
        return []
    out = []
    for i, ch in enumerate(aux.ao_list()):
        short = ch.split("/")[-1]
        out.append(_p(f"aux_{short}", f"AUX {short}", "control", "float",
                      unit="V", group="AUX out", order=100 + i,
                      min=aux.v_min, max=aux.v_max, step=0.1, decimals=3,
                      read_path=["aux", "ao", ch],
                      set={"verb": "aux_set_ao", "arg": "volts",
                           "extra": {"channel": ch}},
                      settle={"policy": "immediate"},
                      help="The 6259 cannot read its own analog outputs back, "
                           "so this reports the COMMANDED value."))
    for i, ch in enumerate(aux.ai_list()):
        short = ch.split("/")[-1]
        out.append(_p(f"aux_{short}", f"AUX {short}", "indicator", "float",
                      unit="V", group="AUX in", order=200 + i, decimals=4,
                      plottable=True, read_path=["aux", "ai", ch],
                      help="Single analog input sample. This is where the "
                           "photodiode / Kerr signal lands."))
    for i, line in enumerate(aux.do_list()):
        short = line.split("/")[-1]
        out.append(_p(f"aux_{short}", f"AUX {short}", "control", "bool",
                      group="AUX out", order=300 + i,
                      read_path=["aux", "do", line],
                      set={"verb": "aux_set_do", "arg": "state",
                           "extra": {"line": line}},
                      settle={"policy": "immediate"}))
    return out


def manifest_revision(manifest: dict) -> int:
    """A checksum over the parts of the manifest a client must react to.

    Derived, not hand-bumped. `value` is excluded on purpose -- it changes many
    times a second and is delivered by the status stream anyway; a revision that
    changed with it would tell clients to re-fetch constantly and mean nothing.
    What IS included is structure and bounds, so loading a calibration (which
    moves the field limits) shows up immediately and nobody has to remember to
    say so.
    """
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` against a status dict.

    A list of keys rather than a dotted string, because AUX channel names
    ("Dev1/ai1") and future ids may contain dots and a dotted path could not be
    split back apart unambiguously.
    """
    if not path:
        return None
    cur = status
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur
