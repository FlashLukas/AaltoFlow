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

The limits here ARE dynamic: the frequency and power ranges are the
intersection of the config envelope and the range the SG12000L reports about
itself at connect, and the phase and vernier controls exist only if the
unit's firmware has them. So the manifest before `start()` differs from the one after it, and
`revision` (in every status frame as `describe_rev`) tells a client when.
"""

from __future__ import annotations

import json
import zlib

from ..config import REFERENCES

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, help="", resolution=None):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract."""
    d = {
        "id": id, "label": label, "kind": kind, "type": type,
        "unit": unit, "group": group, "order": order,
        "writable": (kind == "control") if writable is None else writable,
        "plottable": plottable,
        "read_path": read_path,      # keys/indices into the status dict, or None
    }
    for k, v in (("value", value), ("min", min), ("max", max), ("step", step),
                 ("resolution", resolution),
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


def build_manifest(synth) -> dict:
    """The SG12000L: pure set-and-forget, so every settle rule is `echoes`.

    Nothing here converges -- a synthesiser locks within milliseconds -- so
    there is no "settled" flag. What the service does publish is the value it
    READ BACK from the unit (the poll thread), so `echoes` waits for the
    instrument's own confirmation. Tolerances come from what the instrument can
    resolve: half a step of the 0.5 dB attenuator for power, the configured
    echo tolerance for frequency.
    """
    lim = synth.limits()
    hw = synth.cfg.hardware
    power_tol = max(0.01, 0.5 * float(hw.power_step_dB) + 1e-3)
    fine = synth.fine_power()
    if fine:
        # FINE POWER: the vernier fills the attenuator's steps, and power_dBm
        # is attenuator + vernier. The vernier moves in counts of up to ~0.073
        # dB (the steepest measured slope, 6 GHz), so the delivered level sits
        # within half a count of the request: 0.05 dB is that plus rounding.
        power_tol = 0.05
    params = [
        _p("rf_on", "RF output", "control", "bool", group="Output", order=10,
           read_path=["rf_on"],
           set={"verb": "set_rf", "arg": "on"},
           settle={"policy": "echoes", "key": "rf_on"},
           help="RF output on/off. Adopted from the unit when the service "
                "starts (never changed then); switched OFF when it stops."),

        # Scanned in MHz, commanded and published in Hz. `scale` is on the
        # descriptor rather than inside `set`, so reading and setting cannot
        # disagree -- a scale applied to one direction only would be a
        # factor-of-a-million bug that looks like a broken instrument.
        _p("frequency", "Frequency", "control", "float", unit="MHz",
           group="Signal", order=20, decimals=6, plottable=True,
           min=lim["freq_min_Hz"] / 1e6, max=lim["freq_max_Hz"] / 1e6,
           scale=1e6, read_path=["frequency_Hz"],
           set={"verb": "set_frequency", "arg": "frequency_Hz"},
           settle={"policy": "echoes", "key": "frequency_Hz",
                   "tol": float(hw.freq_echo_tol_Hz)},
           help="CW frequency. Range = your limits AND the unit's own range."),

        _p("power", "Power", "control", "float", unit="dBm", group="Signal",
           order=30, decimals=2, plottable=True,
           step=0.1 if fine else float(hw.power_step_dB),
           # without fine power the attenuator realises ONLY multiples of the
           # step, so scan-core rounds a setpoint to it before sending; with
           # fine power any 0.01 dB value can be asked for
           resolution=0.01 if fine else (float(hw.power_step_dB) or None),
           min=lim["power_min_dBm"], max=lim["power_max_dBm"],
           read_path=["power_dBm"],
           set={"verb": "set_power", "arg": "power_dBm"},
           settle={"policy": "echoes", "key": "power_dBm", "tol": power_tol},
           help=("Output level. The step attenuator moves in "
                 f"{hw.power_step_dB:g} dB steps; the vernier fills in between "
                 "(dB per count measured on the lab unit), so the level asked "
                 "for is delivered to about 0.05 dB (0.1 dB around 6 GHz). "
                 + ("The unit's measured power calibration corrects the "
                    "attenuator steps too (" + synth.power_calibration().describe()
                    + "). " if synth.power_calibration() is not None else
                    "Without a power calibration file the attenuator steps are "
                    "taken as exact (they are not above ~4 GHz: up to ~0.5 dB). ")
                 + "attenuator_dBm in the status is the step attenuator alone."
                 if fine else
                 "Calibrated output level. The step attenuator moves in "
                 f"{hw.power_step_dB:g} dB steps: a request between two steps is "
                 "rounded to the nearest one (the unit itself ignores an "
                 "off-step value).")),
    ]
    if not fine and (synth.has_vernier() or not synth.status().connected):
        # With fine power the vernier is the module's own business (it fills
        # the attenuator's steps), so it is not offered as a control.
        # Same rule as phase below: offered before connect, dropped on a
        # connected unit whose firmware does not answer VERNIER?.
        params.append(
            _p("vernier", "Power vernier", "control", "int", unit="",
               group="Signal", order=31, step=1,
               min=int(lim["vernier_min"]), max=int(lim["vernier_max"]),
               read_path=["vernier"],
               set={"verb": "set_vernier", "arg": "vernier"},
               # an integer echoes exactly; 0.5 only absorbs a float round trip
               settle={"policy": "echoes", "key": "vernier", "tol": 0.5},
               help="Fine output-power trim in raw counts, + = more power. "
                    "Measured on the lab unit: ~0.045 dB/count near 0 at 1-4 "
                    "GHz (+100 = about +4 dB, -200 = about -12 dB), but it "
                    "varies with frequency and power -- not calibrated."))
    if synth.has_phase() or not synth.status().connected:
        # Offered before connect (we do not know yet) and on units that have
        # it; a connected unit without phase control drops it, and the
        # revision change tells every client.
        params.append(
            _p("phase", "Phase", "control", "float", unit="deg", group="Signal",
               order=40, decimals=2, step=1.0,
               min=lim["phase_min_deg"], max=lim["phase_max_deg"],
               read_path=["phase_deg"],
               set={"verb": "set_phase", "arg": "phase_deg"},
               settle={"policy": "echoes", "key": "phase_deg",
                       "tol": float(hw.phase_echo_tol_deg)}))
    params += [
        _p("reference", "10 MHz reference", "control", "enum", group="Reference",
           order=50, options=list(REFERENCES), read_path=["reference"],
           set={"verb": "set_reference", "arg": "mode"},
           settle={"policy": "echoes", "key": "reference"},
           help="internal TCXO, the rear MCX input, or auto-detect."),
        # recorded with a scan, so a data file says whether its power axis
        # includes the measured attenuator correction
        _p("power_calibrated", "Power calibrated", "indicator", "bool",
           group="Signal", order=32, read_path=["power_calibrated"],
           help="True when the unit's measured power calibration "
                "(dssg_power_calibration.json) is loaded and in use."),
        _p("ext_ref_detected", "External reference present", "indicator",
           "bool", group="Reference", order=51, read_path=["ext_ref_detected"]),

        _p("usb_volts", "USB supply", "indicator", "float", unit="V",
           group="Status", order=3, decimals=2, plottable=True,
           read_path=["usb_volts"],
           help="Below ~4.7 V the unit misbehaves: use a better port or cable."),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=4, read_path=["hw_error"]),
        # the SAFETY verb as a button (control.py: a viewer may always send
        # it), so the suite's Control tab offers it to a viewer too
        _p("rf_off", "RF off", "action", "action", group="Output", order=90,
           help="Switch the RF output off. Allowed for anyone, also a viewer."),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "dssg",
                "label": "Microwave signal generator (SG12000L)",
                "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
