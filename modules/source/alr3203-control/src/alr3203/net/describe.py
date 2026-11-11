"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of.

Two consumers, two projections of one source: the reconfigurable control screen
reads this manifest DIRECTLY (it wants buttons, their arguments, their danger
flags and the group/order layout hints), while scan-core projects it into
Settables and Gettables. Full contract: INSTRUMENT_MODULE_GUIDE.md section 6b.

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every limit is looked up from cfg or from the brain when the
manifest is built, never copied into a literal.

THE SHAPE FOLLOWS THE MODE. In current mode the supply offers `current` (the
output, ramped) and `voltage_limit` (the compliance); in voltage mode it offers
`voltage` and `current_limit`. That is how the ALR3203 works (it regulates
whichever of its two setpoints it reaches first; the one you do not turn is
the limit), and offering a knob that does nothing in the present mode would be
a lie. Setpoints are SIGNED: the polarity relay makes the unipolar supply
bipolar (see supply.py). A mode change therefore
changes `revision`, and every status frame carries it as `describe_rev`, so a
client notices without polling the whole manifest.
"""

from __future__ import annotations

import json
import zlib

from ..config import MODES

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, acquire=None, wait=None, help="",
       ramp=None, stream=None):
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
                 ("acquire", acquire), ("wait", wait), ("help", help),
                 ("ramp", ramp), ("stream", stream)):
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
    """Resolve a descriptor's `read_path` against a status dict.

    A list of keys rather than a dotted string, because ids can contain dots.
    An int element indexes into a per-axis list.
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


def _ramped(key: str) -> dict:
    """Settle rule for a RAMPED setpoint: arrived when the service has ADOPTED
    the new target (gotcha #2: right after the command the status still shows
    the old one) AND the ramp is over. Watching `ramping` alone would return at
    once on the frame from before the command."""
    return {"policy": "adopt_then_flag", "setpoint_key": key,
            "flag_key": "ramping", "invert": True}


#: The sweep pace a client is offered first: 0.1 A/s -- gentle on a coil, a
#: 1 A row in 10 s.
SWEEP_RATE_DEFAULT_A_PER_S = 0.1


def _current_ramp(lim) -> dict:
    """The `ramp` block of the current (guide 6b, "Ramps"): the SERVICE
    walks the setpoint (softramp.py); a fly scan bins by the MEASURED current
    the worker streams (a coil lags the programmed value by L/R)."""
    lo, hi = sorted((float(lim.sweep_rate_min_A_per_s), float(lim.rate_max_A_per_s)))
    return {
        "kind": "software",
        "start": {"verb": "ramp_current", "args": {"to": "current_A",
                                                   "rate": "rate_A_per_s"}},
        "stop": {"verb": "ramp_stop"},
        "rate": {"unit": "A/s", "min": lo, "max": hi,
                 "default": max(lo, min(hi, SWEEP_RATE_DEFAULT_A_PER_S))},
        "readback": {"stream": {"group": "measure", "channel": "current"},
                     "measured": True},
        "done": {"key": "ramping", "id_key": "ramp_id"},
    }


def build_manifest(supply) -> dict:
    """The magnet supply: ramped setpoints (adopt_then_flag on `ramping`),
    limits applied at once (echoes), and ACQUIRED measurements for a scan."""
    cfg = supply.cfg
    mode = supply.mode
    ilo, ihi = supply.current_range()
    vlo, vhi = supply.voltage_range()
    lim = cfg.limits

    acquire = {
        "group": "sample",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": float(cfg.acquisition.timeout_s),
    }

    params = [
        _p("mode", "Mode", "control", "enum", group="Output", order=5,
           # the supply's own MODES tuple: open() refuses any other mode
           options=list(MODES), read_path=["mode"],
           set={"verb": "set_mode", "arg": "mode"},
           settle={"policy": "echoes", "key": "mode"},
           help="With the output ON the switch is bumpless: the supply takes "
                "over the MEASURED value of the new quantity, then the other "
                "setpoint goes to its full range."),
        _p("output", "Output", "control", "bool", group="Output", order=10,
           read_path=["output_request"],
           set={"verb": "set_output", "arg": "on"},
           settle=_ramped("output_request"),
           help="On: ramps from 0 to the setpoint. Off: ramps to 0, THEN "
                "switches the output off."),
    ]
    if mode == "current":
        params += [
            _p("current", "Current setpoint", "control", "float", unit="A",
               group="Output", order=20, decimals=4, step=0.01, plottable=True,
               min=ilo, max=ihi, read_path=["current_set_A"],
               set={"verb": "set_current", "arg": "current_A"},
               settle=_ramped("current_set_A"),
               # a CONTINUOUS SWEEP for fly scans (2026-10-10), current mode only
               ramp=_current_ramp(lim),
               help="Signed: negative = the polarity relay inverted. Ramped at "
                    "the current ramp rate; a sign change stops at 0, waits for "
                    "the current to stop, flips the relay. Settled = all done."),
            _p("voltage_limit", "Voltage limit", "control", "float", unit="V",
               group="Output", order=30, decimals=3, step=0.1,
               min=0.0, max=supply.voltage_limit_max(),
               read_path=["voltage_limit_V"],
               set={"verb": "set_voltage_limit", "arg": "voltage_V"},
               settle={"policy": "echoes", "key": "voltage_limit_V", "tol": 1e-9},
               help="The voltage setpoint while in current mode (a magnitude; "
                    "keep it at the full 32 V). Applied at once, not ramped."),
        ]
    else:
        params += [
            _p("voltage", "Voltage setpoint", "control", "float", unit="V",
               group="Output", order=20, decimals=4, step=0.01, plottable=True,
               min=vlo, max=vhi, read_path=["voltage_set_V"],
               set={"verb": "set_voltage", "arg": "voltage_V"},
               settle=_ramped("voltage_set_V"),
               help="Signed: negative = the polarity relay inverted. Ramped at "
                    "the voltage ramp rate; a sign change goes through 0 and the relay."),
            _p("current_limit", "Current limit", "control", "float", unit="A",
               group="Output", order=30, decimals=4, step=0.01,
               min=0.0, max=supply.current_limit_max(),
               read_path=["current_limit_A"],
               set={"verb": "set_current_limit", "arg": "current_A"},
               settle={"policy": "echoes", "key": "current_limit_A", "tol": 1e-9},
               help="The current setpoint while in voltage mode (a magnitude; "
                    "keep it at the full 6 A). Applied at once."),
        ]

    params += [
        # ---- ramp -------------------------------------------------------------
        _p("ramp_rate_current", "Ramp rate (current mode)", "control", "float",
           unit="A/s", group="Ramp", order=10, decimals=4, step=0.01,
           min=1e-6, max=lim.rate_max_A_per_s, read_path=["ramp_rate_A_per_s"],
           set={"verb": "set_ramp", "arg": "rate_A_per_s"},
           settle={"policy": "echoes", "key": "ramp_rate_A_per_s", "tol": 1e-9},
           help="How fast the current setpoint is walked to a new value, in A/s. Applies to later setpoint changes."),
        _p("ramp_rate_voltage", "Ramp rate (voltage mode)", "control", "float",
           unit="V/s", group="Ramp", order=20, decimals=4, step=0.1,
           min=1e-6, max=lim.rate_max_V_per_s, read_path=["ramp_rate_V_per_s"],
           set={"verb": "set_ramp", "arg": "rate_V_per_s"},
           settle={"policy": "echoes", "key": "ramp_rate_V_per_s", "tol": 1e-9},
           help="How fast the voltage setpoint is walked to a new value, in V/s. Applies to later setpoint changes."),
        _p("ramp_enabled", "Ramp enabled", "control", "bool", group="Ramp",
           order=30, read_path=["ramp_enabled"],
           set={"verb": "set_ramp", "arg": "enabled"},
           settle={"policy": "echoes", "key": "ramp_enabled"}, danger=True,
           help="Off = setpoints are applied as STEPS. Never with a coil."),
        _p("programmed", "Programmed now", "indicator", "float",
           unit="A" if mode == "current" else "V", group="Ramp", order=40,
           decimals=4, plottable=True, read_path=["programmed"],
           help="Where the ramp is: the value on the main channel right now."),
        _p("ramping", "Ramping", "indicator", "bool", group="Ramp", order=50,
           read_path=["ramping"],
           help="True while the setpoint (or the output on/off) is still being ramped. Last status value; scans use it as their settle flag."),

        # ---- measurement: latched (scan) and live (panel) ----------------------
        _p("measured_voltage", "Measured voltage", "indicator", "float", unit="V",
           group="Measurement", order=10, decimals=4,
           read_path=["sample", "voltage_V"], acquire=acquire,
           help="Mean of fresh readings latched by `acquire`: safe in a scan."),
        _p("measured_current", "Measured current", "indicator", "float", unit="A",
           group="Measurement", order=11, decimals=5,
           read_path=["sample", "current_A"], acquire=acquire,
           help="Mean current of fresh readings latched by `acquire`: a reading taken at each scan point, unlike 'Current (live)'."),
        _p("measured_voltage_std", "Measured voltage std. dev.", "indicator",
           "float", unit="V", group="Measurement", order=12, decimals=5,
           read_path=["sample", "voltage_std_V"], acquire=acquire,
           help="Standard deviation of the voltage readings averaged in the last `acquire`. Latched with the mean, safe in a scan."),
        _p("measured_current_std", "Measured current std. dev.", "indicator",
           "float", unit="A", group="Measurement", order=13, decimals=6,
           read_path=["sample", "current_std_A"], acquire=acquire,
           help="Standard deviation of the current readings averaged in the last `acquire`. Latched with the mean, safe in a scan."),
        _p("acq_readings", "Readings per acquisition", "control", "int",
           group="Measurement", order=5, step=1, min=1, max=1000,
           read_path=["acq_readings"],
           set={"verb": "set_acquisition", "arg": "readings"},
           settle={"policy": "echoes", "key": "acq_readings", "tol": 0.5},
           help="Number of readings averaged in one `acquire`. More readings: less noise, longer acquisition. Applies to the next acquire."),
        _p("acquire", "Acquire sample", "action", "action", group="Measurement",
           order=1, wait={"target_key": "acq_id", "ready": acquire["ready"],
                          "timeout_s": acquire["timeout_s"]},
           help="Wait the settle time, average fresh readings, latch them."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=2, read_path=["acquiring"],
           help="True while an `acquire` is collecting readings. Last status value; scans use it to wait for the result."),
        # A counter from 0 that only goes up: min=0 is a promise the code
        # keeps (scan-core picks the storage from it, developer notes 4b). No
        # max: it is unbounded in principle.
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=3, min=0, read_path=["acq_id"],
           help="Running number of the last finished acquisition. Scans use it to be sure they read the sample they triggered."),
        _p("live_voltage", "Voltage (live)", "indicator", "float", unit="V",
           group="Live", order=10, decimals=4, plottable=True,
           read_path=["voltage_V"],
           # every measurement, for fly scans (one group: one start/read/stop)
           stream={"group": "measure", "channel": "voltage"},
           help="Output voltage from the status stream. Last status value, for panels, not for scan points (use 'Measured voltage' there)."),
        _p("live_current", "Current (live)", "indicator", "float", unit="A",
           group="Live", order=11, decimals=5, plottable=True,
           read_path=["current_A"],
           stream={"group": "measure", "channel": "current"},
           help="Output current from the status stream. Last status value, for panels, not for scan points (use 'Measured current' there)."),
        _p("sweeping", "Sweeping", "indicator", "bool", group="Ramp", order=55,
           read_path=["sweeping"],
           help="True while a current sweep (ramp_current) walks the setpoint."),
        _p("power", "Power", "indicator", "float", unit="W", group="Live",
           order=12, decimals=3, plottable=True, read_path=["power_W"],
           help="|V I|. The ALR3203 delivers at most 96 W (6 A only up to 16 V)."),
        _p("at_limit", "At limit", "indicator", "bool", group="Live", order=13,
           read_path=["at_limit"],
           help="The other setpoint (or the 96 W envelope) holds the output: "
                "the setpoint is not reached."),
        _p("power_limited", "On the 96 W limit", "indicator", "bool", group="Live",
           order=14, read_path=["power_limited"],
           help="The supply sits on its power envelope: it gives less than asked."),
        _p("resistance", "Resistance V/I", "indicator", "float", unit="ohm",
           group="Live", order=15, decimals=3, plottable=True,
           read_path=["resistance_ohm"],
           help="Measured V / I, for information (no limit acts on it). "
                "NaN below 10 mA."),
        _p("regulation", "Regulating", "indicator", "string", group="Live",
           order=16, read_path=["regulation"],
           help="What the supply regulates now: voltage (CV), current (CC) or off."),
        # ---- the polarity relay ---------------------------------------------------
        _p("relay_inverted", "Polarity inverted", "indicator", "bool",
           group="Polarity", order=10, read_path=["relay_inverted"],
           help="The polarity relay: true = the current runs the other way "
                "(negative setpoints). Empty = not known yet."),
        _p("polarity_switching", "Switching polarity", "indicator", "bool",
           group="Polarity", order=11, read_path=["polarity_switching"],
           help="At zero, waiting for the current to stop before the relay flips."),
        _p("relay_error", "Relay error", "indicator", "string", group="Polarity",
           order=12, read_path=["relay_error"],
           help="Text of the last polarity relay problem; empty when the relay behaves."),

        # ---- safety -------------------------------------------------------------
        # the SAFETY verbs as buttons (control.py: a viewer may always send
        # them), so the suite's Control tab offers them to a viewer too
        _p("output_off", "Output off (ramp down)", "action", "action",
           group="Safety", order=0,
           help="Ramp to zero, then switch the output off (the normal way, "
                "right for a coil). Allowed for anyone, also a viewer."),
        _p("output_off_now", "Output off NOW (no ramp)", "action", "action",
           group="Safety", order=1, danger=True,
           help="Emergency only: switches off without ramping. With a coil "
                "attached its stored energy goes into the supply."),
        _p("ramp_stop", "Stop the sweep", "action", "action", group="Safety",
           order=3,
           help="End a current sweep (ramp_current) where it is. Allowed for "
                "anyone, also a viewer: it only stops something moving."),
        _p("output_state", "Output switch", "indicator", "bool", group="Safety",
           order=2, read_path=["output"],
           help="True while the supply output is actually switched on."),

        # ---- status ---------------------------------------------------------------
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"],
           help="True while the service has an open connection to the supply."),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"],
           help="The instrument's identification string, read when it was opened."),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"],
           help="Text of the last failed instrument access; empty when communication is fine."),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "alr3203",
                "label": "ELC ALR3203 magnet supply", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
