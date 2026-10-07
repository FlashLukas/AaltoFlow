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

What moves at runtime here -- and a function generator's manifest moves more
than most:
  * the RANGES are the brain's live envelope: the lab's limits (Settings)
    narrowed by the instrument's range for the CURRENT waveform and LOAD. A
    ramp stops at 1 MHz where a sine goes to 60 MHz; at "high-Z" the volts
    double. Change either and min/max change.
  * the SHAPE follows the waveform: a DC level has no frequency or phase
    (they become read-only indicators), duty is a control only for a pulse,
    symmetry only for a ramp, amplitude not for DC. While "CH2 follows CH1"
    is on, CH2's frequency and phase are indicators (the brain sets them) and
    the phase OFFSET is the control.
`revision` travels in every status frame as `describe_rev`, so a client
notices any of this without polling the whole manifest.
"""

from __future__ import annotations

import json
import zlib

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, help="", timeout_s=None,
       wait=None, resolution=None):
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
                 ("timeout_s", timeout_s), ("wait", wait), ("help", help),
                 ("resolution", resolution)):
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


def _channel_params(gen, ch: str, base_order: int) -> list:
    """The descriptors of ONE output channel, ids prefixed "ch1_" / "ch2_".

    Flat ids (not one descriptor with a channel argument) so a panel can place
    one channel and scan-core can sweep one: `afg.ch1_amplitude`.

    SETTLE: every control waits for the same two things -- the service echoes
    the value it was asked for (`<ch>_<knob>`), and then `<ch>_settled`, true
    once that request has been SENT and the instrument READ BACK agreeing with
    it. That is `adopt_then_flag`: the echo guards against the "settled" left
    over from the previous point (gotcha #2), the flag against measuring while
    the instrument still holds the old setting -- or a coerced one
    (`<ch>_mismatch` then says which).
    """
    C = ch.upper()
    grp = C
    extra = {"channel": ch}
    want = gen.desired(ch)
    wf = want.get("waveform", "sine")
    env = gen.envelope(ch)
    follows = ch == "ch2" and gen.follows()                  # frequency
    phase_follows = ch == "ch2" and gen.phase_follows()
    # the instrument's phase step (AFG1062: whole degrees). Declared, so a
    # scan rounds its phase values to it and settles on the rounded value
    # instead of waiting for 31.4 deg on a unit that holds 31.
    phase_res = gen.phase_resolution() or None
    has_freq = wf not in ("dc", "noise")
    nrb = set(gen.not_read_back(ch))
    NRB = (" NOT READ BACK on this instrument (its firmware lacks the query): "
           "the value shown is the one last set from here.")

    def settle(key):
        return {"policy": "adopt_then_flag", "setpoint_key": key,
                "flag_key": f"{ch}_settled"}

    out = [
        _p(f"{ch}_output", f"{C} output", "control", "bool", group=grp,
           order=base_order + 1, read_path=[f"{ch}_output"],
           set={"verb": "set_output", "arg": "on", "extra": extra},
           settle=settle(f"{ch}_output"), timeout_s=10.0,
           help="Output on/off. 'All outputs off' switches every channel off."),
        _p(f"{ch}_waveform", f"{C} waveform", "control", "enum", group=grp,
           order=base_order + 2,
           options=list(gen.caps.get("waveforms", ())) + (["arb"] if wf == "arb" else []),
           read_path=[f"{ch}_waveform"],
           set={"verb": "set_waveform", "arg": "waveform", "extra": extra},
           settle={"policy": "echoes", "key": f"{ch}_waveform"},
           help="'arb' = an arbitrary / special waveform left on the instrument; "
                "kept, but it cannot be selected from here."),
    ]
    if has_freq and not follows:
        out.append(_p(f"{ch}_frequency", f"{C} frequency", "control", "float", unit="Hz",
                      group=grp, order=base_order + 3, decimals=6, plottable=True,
                      min=env["freq_min_Hz"], max=env["freq_max_Hz"],
                      read_path=[f"{ch}_frequency_Hz"],
                      set={"verb": "set_frequency", "arg": "frequency_Hz", "extra": extra},
                      settle=settle(f"{ch}_frequency_Hz"), timeout_s=10.0,
                      help="The maximum depends on the waveform (AFG1062: sine 60 MHz, "
                           "square / pulse 25 MHz, ramp 1 MHz) and on the lab limit."))
    else:
        why = "set by CH1 (CH2 follows CH1)" if follows else f"no frequency for {wf}"
        out.append(_p(f"{ch}_frequency", f"{C} frequency", "indicator", "float", unit="Hz",
                      group=grp, order=base_order + 3, decimals=6, plottable=True,
                      read_path=[f"{ch}_frequency_Hz"], help=why))
    if wf != "dc":
        out.append(_p(f"{ch}_amplitude", f"{C} amplitude", "control", "float", unit="Vpp",
                      group=grp, order=base_order + 4, decimals=4, step=0.01, plottable=True,
                      min=env["amp_min_Vpp"], max=env["amp_max_Vpp"],
                      read_path=[f"{ch}_amplitude_Vpp"],
                      set={"verb": "set_amplitude", "arg": "amplitude_Vpp", "extra": extra},
                      settle=settle(f"{ch}_amplitude_Vpp"), timeout_s=10.0,
                      help="Peak-to-peak INTO THE LOAD SETTING (double on an open "
                           "input when the load is 50 ohm). |offset| + amplitude/2 "
                           "may not pass the peak limit; the amplitude stops there."))
    else:
        out.append(_p(f"{ch}_amplitude", f"{C} amplitude", "indicator", "float",
                      unit="Vpp", group=grp, order=base_order + 4, decimals=4,
                      read_path=[f"{ch}_amplitude_Vpp"], help="no amplitude for DC"))
    out.append(_p(f"{ch}_offset", f"{C} offset" if wf != "dc" else f"{C} DC level",
                  "control", "float", unit="V", group=grp, order=base_order + 5,
                  decimals=4, step=0.01, plottable=True,
                  min=-env["peak_max_V"], max=env["peak_max_V"],
                  read_path=[f"{ch}_offset_V"],
                  set={"verb": "set_offset", "arg": "offset_V", "extra": extra},
                  settle=settle(f"{ch}_offset_V"), timeout_s=10.0,
                  help="Into the load setting. Limited so that |offset| + "
                       "amplitude/2 stays within the peak limit (the offset stops "
                       "there when it is the one being set)."))
    if has_freq and not phase_follows:
        out.append(_p(f"{ch}_phase", f"{C} phase", "control", "float", unit="deg",
                      group=grp, order=base_order + 6, decimals=2, step=1.0,
                      min=-180.0, max=360.0, read_path=[f"{ch}_phase_deg"],
                      set={"verb": "set_phase", "arg": "phase_deg", "extra": extra},
                      settle=settle(f"{ch}_phase_deg"), timeout_s=10.0,
                      resolution=phase_res,
                      help="Start phase. Kept as asked (-90 stays -90); the instrument "
                           "gets the same angle in 0..360, in its own steps (AFG1062: "
                           "whole degrees). Between the two channels it means something "
                           "only at the same frequency and after 'Align phase'."))
    else:
        out.append(_p(f"{ch}_phase", f"{C} phase", "indicator", "float", unit="deg",
                      group=grp, order=base_order + 6, decimals=2,
                      read_path=[f"{ch}_phase_deg"]))
    if wf == "pulse":
        out.append(_p(f"{ch}_duty", f"{C} duty cycle", "control", "float", unit="%",
                      group=grp, order=base_order + 7, decimals=2, step=1.0,
                      min=env["duty_min_pct"], max=env["duty_max_pct"],
                      read_path=[f"{ch}_duty_pct"],
                      set={"verb": "set_duty", "arg": "duty_pct", "extra": extra},
                      settle=settle(f"{ch}_duty_pct"), timeout_s=10.0,
                      help=NRB.strip() if "duty_pct" in nrb else ""))
    if wf == "ramp":
        out.append(_p(f"{ch}_symmetry", f"{C} ramp symmetry", "control", "float", unit="%",
                      group=grp, order=base_order + 7, decimals=2, step=1.0,
                      min=0.0, max=100.0, read_path=[f"{ch}_symmetry_pct"],
                      set={"verb": "set_symmetry", "arg": "symmetry_pct", "extra": extra},
                      settle=settle(f"{ch}_symmetry_pct"), timeout_s=10.0,
                      help="50 = triangle, 100 = rising saw, 0 = falling saw."
                           + (NRB if "symmetry_pct" in nrb else "")))
    load = "high-Z" if want.get("load_ohm") is None else f"{want['load_ohm']:g}"
    if gen.caps.get("load_settable"):
        out.append(_p(f"{ch}_load", f"{C} load setting", "control", "enum", unit="ohm",
                      group=grp, order=base_order + 8,
                      options=["50", "high-Z"] + ([load] if load not in ("50", "high-Z") else []),
                      read_path=[f"{ch}_load"],
                      set={"verb": "set_load", "arg": "load", "extra": extra},
                      settle={"policy": "echoes", "key": f"{ch}_load"},
                      help="What the instrument assumes is connected. It changes the "
                           "MEANING of the volts, not the output stage: the AFG "
                           "rescales amplitude and offset when it changes."))
    out += [
        _p(f"{ch}_peak", f"{C} peak voltage", "indicator", "float", unit="V", group=grp,
           order=base_order + 9, decimals=3, read_path=[f"{ch}_peak_V"],
           help="|offset| + amplitude/2: the highest voltage the output reaches."),
        _p(f"{ch}_settled", f"{C} settled", "indicator", "bool", group=grp,
           order=base_order + 10, read_path=[f"{ch}_settled"]),
        _p(f"{ch}_mismatch", f"{C} not as asked", "indicator", "string", group=grp,
           order=base_order + 11, read_path=[f"{ch}_mismatch"],
           help="Empty when the instrument's read-back agrees with the request."),
        _p(f"{ch}_not_read_back", f"{C} not read back", "indicator", "string",
           group=grp, order=base_order + 14, read_path=[f"{ch}_not_read_back"],
           help="Knobs this instrument cannot report (e.g. ramp symmetry on the "
                "AFG1062 firmware V1.0.2). Their value is the one last set from "
                "here; settled does not check them."),
        _p(f"{ch}_mode", f"{C} mode", "indicator", "string", group=grp,
           order=base_order + 12, read_path=[f"{ch}_mode"],
           help="continuous, or burst / sweep / modulated (set at the instrument; "
                "this module leaves those modes alone)."),
        _p(f"{ch}_frequency_actual", f"{C} frequency (read back)", "indicator", "float",
           unit="Hz", group=grp, order=base_order + 13, decimals=6, plottable=True,
           read_path=[f"{ch}_frequency_actual_Hz"]),
    ]
    return out


# The reply of an operation verb carries its number; the status the number of
# the last one FINISHED, so a wait cannot be fooled by the frame from before
# the command (gotcha #17).
_OP_WAIT = {"target_key": "op_id",
            "ready": {"policy": "adopt_then_flag", "setpoint_key": "op_id",
                      "flag_key": "op_ok"},
            "timeout_s": 10.0}


def build_manifest(gen) -> dict:
    """The manifest of the generator (see module docstring)."""
    params = []
    for n, ch in enumerate(gen.channels):
        params += _channel_params(gen, ch, 10 * (n + 1))
    if len(gen.channels) > 1:
        phase_res = gen.phase_resolution() or None
        follow = gen.follows()
        params.append(
            _p("follow", "CH2 frequency follows CH1", "control", "bool", group="Coupling",
               order=1, read_path=["follow"], set={"verb": "set_follow", "arg": "on"},
               settle={"policy": "echoes", "key": "follow"},
               help="CH2 takes CH1's frequency; the channels are re-aligned after "
                    "every change. For a synchronous trigger square on CH2 next to "
                    "the drive on CH1."))
        params.append(
            _p("phase_follow", "CH2 phase follows CH1", "control", "bool", group="Coupling",
               order=2, read_path=["phase_follow_set"],
               set={"verb": "set_phase_follow", "arg": "on"},
               settle={"policy": "echoes", "key": "phase_follow_set"},
               help="While the frequency follows: CH2's phase = CH1's + the offset. "
                    "Off: CH2's phase is its own setting (Lukas 2026-10-07)."))
        if gen.phase_follows():
            params.append(
                _p("phase_offset", "CH2 phase offset", "control", "float", unit="deg",
                   group="Coupling", order=3, decimals=2, step=1.0, min=-180.0, max=360.0,
                   resolution=phase_res,
                   read_path=["phase_offset_deg"],
                   set={"verb": "set_phase_offset", "arg": "deg"},
                   settle={"policy": "adopt_then_flag", "setpoint_key": "phase_offset_deg",
                           "flag_key": "ch2_settled"}, timeout_s=10.0,
                   help="CH2 phase minus CH1 phase. Scannable: e.g. move the "
                        "trigger relative to the drive."))
        else:
            params.append(
                _p("phase_offset", "CH2 phase offset (unused)", "indicator", "float",
                   unit="deg", group="Coupling", order=3, decimals=2,
                   read_path=["phase_offset_deg"]))
        if gen.caps.get("phase_align"):
            params.append(
                _p("align_phase", "Align phase", "action", "action", group="Coupling",
                   order=4, wait=_OP_WAIT,
                   help="Restart both channels' phase together (the AFG's 'Align "
                        "phase'). Done automatically while CH2 follows CH1. The "
                        "outputs restart: a glitch on a driven magnet."))
    params += [
        _p("outputs_off", "All outputs off", "action", "action", group="Output",
           order=1, wait=_OP_WAIT,
           help="Switch every output off. Usable as a scan routine step, and by a "
                "viewer (the safety action)."),
        _p("all_off", "All outputs off (state)", "indicator", "bool",
           group="Output", order=2, read_path=["all_off"]),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "afg",
                "label": f"Function generator ({gen.caps.get('model', 'AFG')})",
                "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
