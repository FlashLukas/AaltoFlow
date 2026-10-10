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

What moves at runtime here: the RF ranges come from the config's safety
envelope (they move when someone edits it), and the SHAPE of the manifest
follows the reference: the external reference frequency is a control only
while the external reference is selected, and a read-only indicator
otherwise. `revision` travels in every status frame as `describe_rev`, so a
client notices either change without polling the whole manifest.
"""

from __future__ import annotations

import json
import zlib

from ..config import REFERENCE_SOURCES

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1

#: The sweep pace a client is offered first, per knob, in WIRE units per
#: second (clamped to the configured paces): 10 MHz/s -- a 100 MHz FMR line
#: in 10 s, slow enough for a lock-in at a few ms time constant; 1 dB/s;
#: 10 deg/s.
SWEEP_RATE_DEFAULTS = {"frequency": 10.0e6, "power": 1.0, "phase": 10.0}


def sweep_block(synth, ch: str, knob: str, *, wire_arg: str, rate_arg: str,
                rate_unit: str, scale: float = 1.0) -> dict:
    """The `ramp` block of one channel's knob (guide 6b, "Ramps"): a
    CONTINUOUS SWEEP a fly scan can fly. The SERVICE walks the knob
    (softramp.py) and records every value it sent; the fly scan bins by that
    COMMANDED value (measured: false -- why, see synthesizer.py "the
    SWEEPS"). The channel travels as a fixed extra argument, exactly as in
    the control's `set`. The sweep's name -- in ramp_stop, the status keys
    and the stream channel -- is the control's id, "<ch>_<knob>". `to` and
    the rate are scaled like the set (MHz in the scan, Hz on the wire); the
    limits are the live config paces, never literals."""
    name = f"{ch}_{knob}"
    lo, hi = synth._rate_limits(knob)
    default = max(lo, min(hi, SWEEP_RATE_DEFAULTS[knob]))
    return {"kind": "software",
            "start": {"verb": f"ramp_{knob}",
                      "args": {"to": wire_arg, "rate": rate_arg},
                      "extra": {"channel": ch}},
            # stops THIS channel's knob only (no `knob`: every sweep)
            "stop": {"verb": "ramp_stop", "extra": {"knob": name}},
            "rate": {"unit": rate_unit, "min": lo / scale, "max": hi / scale,
                     "default": default / scale},
            "readback": {"stream": {"group": "ramp", "channel": name},
                         "measured": False},
            "done": {"key": f"{name}_ramping", "id_key": f"{name}_ramp_id"}}


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, help="", timeout_s=None,
       wait=None, ramp=None):
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
                 ("timeout_s", timeout_s), ("wait", wait), ("ramp", ramp),
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


def _channel_params(ch: str, synth, base_order: int) -> list:
    """The descriptors of ONE output channel, ids prefixed "a_" / "b_".

    Flat ids (not one descriptor with a channel argument) so a panel can place
    one channel and scan-core can sweep one: `windfreak.a_frequency`.

    SETTLE: every control waits for the same two things -- the service echoes
    the value it PUSHED to the instrument (`<ch>_<knob>`), and then
    `<ch>_settled`, which is true once that push is the latest request for the
    channel AND (while the output is on) its PLL reports lock. That is
    `adopt_then_flag`: the echo
    guards against reading the "settled" left over from the previous point
    (gotcha #2), the flag against measuring on an unlocked synthesizer.
    """
    C = ch.upper()
    grp = f"Channel {C}"
    extra = {"channel": ch}
    lim = synth.cfg.limits

    def settle(key):
        return {"policy": "adopt_then_flag", "setpoint_key": key,
                "flag_key": f"{ch}_settled"}

    return [
        _p(f"{ch}_rf_on", f"{C} RF output", "control", "bool", group=grp,
           order=base_order + 1, read_path=[f"{ch}_rf_on"],
           set={"verb": "set_rf", "arg": "on", "extra": extra},
           settle=settle(f"{ch}_rf_on"), timeout_s=10.0,
           help="Output on/off. Off = muted and output amplifier unpowered."),

        # Scanned in MHz, commanded and published in Hz. `scale` is on the
        # descriptor rather than inside `set`, so reading and setting cannot
        # disagree -- a scale applied in one direction only would be a
        # factor-of-a-million bug that looks like a broken instrument.
        _p(f"{ch}_frequency", f"{C} frequency", "control", "float", unit="MHz",
           group=grp, order=base_order + 2, decimals=7, plottable=True,
           min=lim.freq_min_Hz / 1e6, max=lim.freq_max_Hz / 1e6, scale=1e6,
           read_path=[f"{ch}_frequency_Hz"],
           set={"verb": "set_frequency", "arg": "frequency_Hz", "extra": extra},
           settle=settle(f"{ch}_frequency_Hz"), timeout_s=10.0,
           ramp=sweep_block(synth, ch, "frequency", wire_arg="frequency_Hz",
                            rate_arg="rate_Hz_per_s", rate_unit="MHz/s", scale=1e6),
           help="Resolution = the channel spacing (100 Hz by default). Above "
                "20 GHz the output is not power-calibrated."),

        _p(f"{ch}_power", f"{C} power", "control", "float", unit="dBm",
           group=grp, order=base_order + 3, decimals=2, step=0.1, plottable=True,
           min=lim.power_min_dBm, max=lim.power_max_dBm,
           read_path=[f"{ch}_power_dBm"],
           set={"verb": "set_power", "arg": "power_dBm", "extra": extra},
           settle=settle(f"{ch}_power_dBm"), timeout_s=10.0,
           ramp=sweep_block(synth, ch, "power", wire_arg="power_dBm",
                            rate_arg="rate_dB_per_s", rate_unit="dB/s"),
           help="The instrument levels to this value when it can (about -40 dBm "
                "to +20 dBm at low frequency, falling to ~+6 dBm at 24 GHz); "
                "watch the leveled indicator."),

        _p(f"{ch}_phase", f"{C} phase", "control", "float", unit="deg",
           group=grp, order=base_order + 4, decimals=2, step=1.0,
           min=lim.phase_min_deg, max=lim.phase_max_deg,
           read_path=[f"{ch}_phase_deg"],
           set={"verb": "set_phase", "arg": "phase_deg", "extra": extra},
           settle=settle(f"{ch}_phase_deg"), timeout_s=10.0,
           ramp=sweep_block(synth, ch, "phase", wire_arg="phase_deg",
                            rate_arg="rate_deg_per_s", rate_unit="deg/s"),
           help="Relative to the phase at service start (the instrument has "
                "no absolute phase readback). Meaningful between A and B only "
                "when both run at the same frequency."),

        _p(f"{ch}_locked", f"{C} PLL locked", "indicator", "bool", group=grp,
           order=base_order + 5, read_path=[f"{ch}_locked"]),
        _p(f"{ch}_leveled", f"{C} power leveled", "indicator", "bool", group=grp,
           order=base_order + 6, read_path=[f"{ch}_leveled"],
           help="False when the requested power is outside what the instrument "
                "can level at this frequency (it then gets as close as it can)."),
        _p(f"{ch}_frequency_actual", f"{C} frequency (readback)", "indicator",
           "float", unit="MHz", group=grp, order=base_order + 7, decimals=7,
           scale=1e6, plottable=True, read_path=[f"{ch}_frequency_actual_Hz"],
           help="What the instrument reports: the request snapped to its grid."),
    ]


def build_manifest(synth) -> dict:
    """The manifest of the two-channel synthesizer (see module docstring)."""
    cfg = synth.cfg
    lim = cfg.limits
    external = cfg.reference.source == "external"
    params = []
    params += _channel_params("a", synth, 10)
    params += _channel_params("b", synth, 20)
    params.append(
        _p("reference", "Reference", "control", "enum", group="Reference",
           order=1, options=list(REFERENCE_SOURCES), read_path=["reference"],
           set={"verb": "set_reference", "arg": "source"},
           settle={"policy": "echoes", "key": "reference"},
           help="Clock for BOTH PLLs. 'external' needs a 10-100 MHz signal on "
                "REF IN at the declared frequency, or neither channel locks."))
    if external:
        params.append(
            _p("ext_ref", "External reference", "control", "float", unit="MHz",
               group="Reference", order=2, decimals=3,
               min=lim.ext_ref_min_MHz, max=lim.ext_ref_max_MHz,
               read_path=["ext_ref_MHz"],
               set={"verb": "set_ext_ref", "arg": "ext_MHz"},
               settle={"policy": "adopt_then_flag", "setpoint_key": "ext_ref_MHz",
                       "flag_key": "ref_settled"}, timeout_s=10.0,
               help="Must match the signal on REF IN."))
    else:
        # A control only while it does something: with an internal reference
        # the declared external frequency is ignored, so it is shown read-only.
        params.append(
            _p("ext_ref", "External reference (unused)", "indicator", "float",
               unit="MHz", group="Reference", order=2, decimals=3,
               read_path=["ext_ref_MHz"]))
    params += [
        _p("ref_settled", "All PLLs locked", "indicator", "bool",
           group="Reference", order=3, read_path=["ref_settled"]),
        _p("all_rf_off", "All RF off", "action", "action", group="Output",
           order=1,
           # The reply only means ACCEPTED; the worker switches the outputs a
           # moment later. Wait for `rf_all_off` (both outputs off in the
           # instrument, nothing pending), so an after_scan "RF off" step
           # really is off before the scan reports done.
           wait={"ready": {"policy": "flag_only", "key": "rf_all_off"},
                 "timeout_s": 10.0},
           help="Switch both outputs off. Usable as a scan routine step."),
        _p("rf_all_off", "Both outputs off", "indicator", "bool",
           group="Output", order=2, read_path=["rf_all_off"]),
        _p("ramping", "Sweeping", "indicator", "bool", group="Output", order=3,
           read_path=["ramping"],
           help="True while a sweep (ramp_frequency / ramp_power / ramp_phase) "
                "walks a knob of either channel."),
        _p("temperature", "Temperature", "indicator", "float", unit="degC",
           group="Status", order=1, decimals=1, plottable=True,
           read_path=["temperature_C"]),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=2, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=3,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=4, read_path=["hw_error"]),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "windfreak",
                "label": "Windfreak SynthHD PRO v2 (2-channel RF synthesizer)",
                "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
