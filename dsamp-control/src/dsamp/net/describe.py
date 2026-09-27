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

The gain limits ARE dynamic in the sense that matters: they are the
intersection of the device range (config.hardware) and the safety ceiling
(config.limits), both editable live with set_config. `revision` is derived
from the manifest, so it moves when either does and travels in every status
frame as `describe_rev`; a client re-fetches only then.
"""

from __future__ import annotations

import json
import zlib

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, wait=None, timeout_s=None, danger=False, help=""):
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
                 ("set", set), ("settle", settle), ("args", args), ("wait", wait),
                 ("timeout_s", timeout_s),
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


def build_manifest(amp) -> dict:
    """The amplifier: set-and-forget, so every control settles by `echoes`.

    Nothing converges: the device holds the gain it was told. What the status
    does report is the gain READ BACK by the poll thread, so `echoes` on
    `gain_dB` waits until the device itself confirms the new setting -- a real
    check, not a sleep. The tolerance is half a gain step, because the brain
    snaps a request to the device step: ask for 10.2 dB, the device holds 10.0,
    and that IS the arrival.
    """
    cfg = amp.cfg
    lim, hw = cfg.limits, cfg.hardware
    lo, hi = amp.gain_range()
    step = hw.gain_step_dB
    # a settle that can never be satisfied would stall a scan for its whole
    # timeout; an echo can take up to one poll period plus the round trip
    poll_s = 1.0 / max(0.2, hw.poll_hz)
    timeout_s = max(5.0, 10.0 * poll_s)
    params = [
        _p("amp_on", "Amplifier on", "control", "bool", group="Output", order=10,
           read_path=["amp_on"],
           set={"verb": "set_amp", "arg": "on"},
           settle={"policy": "echoes", "key": "amp_on"}, timeout_s=timeout_s,
           danger=True,
           help="Switches the amplifier stage. The output must be terminated in "
                "50 ohm before this goes on: a reflected output can destroy the stage."),

        _p("gain", "Gain setting", "control", "float", unit="dB", group="Gain",
           order=20, decimals=2, step=step, plottable=True,
           min=lo, max=hi, read_path=["gain_dB"],
           set={"verb": "set_gain", "arg": "gain_dB"},
           settle={"policy": "echoes", "key": "gain_dB", "tol": step / 2.0 + 1e-6},
           timeout_s=timeout_s,
           help=f"Snapped to the device's {step:g} dB step. The ceiling is the "
                f"safety limit (limits.gain_max_dB) or the device maximum, "
                f"whichever is lower. The real gain falls with frequency, see "
                f"'Estimated gain'."),

        # Scanned in MHz, commanded and published in Hz: `scale` is on the
        # descriptor so reading and setting cannot disagree.
        _p("frequency", "Signal frequency", "control", "float", unit="MHz",
           group="Operating point", order=30, decimals=3,
           min=lim.freq_min_Hz / 1e6, max=lim.freq_max_Hz / 1e6, scale=1e6,
           read_path=["frequency_Hz"],
           set={"verb": "set_frequency", "arg": "frequency_Hz"},
           settle={"policy": "echoes", "key": "frequency_Hz", "tol": 1.0},
           timeout_s=timeout_s,
           help="What goes through the amplifier. Bookkeeping only (the device has "
                "no frequency setting): it feeds the gain/output estimate."),

        _p("input_power", "Input level", "control", "float", unit="dBm",
           group="Operating point", order=31, decimals=2, step=0.5,
           min=lim.input_min_dBm, max=lim.input_max_dBm,
           read_path=["input_dBm"],
           set={"verb": "set_input_power", "arg": "input_dBm"},
           settle={"policy": "echoes", "key": "input_dBm", "tol": 1e-6},
           timeout_s=timeout_s,
           help="Expected input level. Bookkeeping only: it feeds the output "
                "estimate. The device's absolute maximum input is +10 dBm."),

        _p("est_gain", "Estimated gain", "indicator", "float", unit="dB",
           group="Estimate", order=40, decimals=2, plottable=True,
           read_path=["est_gain_dB"],
           help="Gain setting minus the datasheet's typical roll-off at the "
                "signal frequency. A typical curve, not a calibration."),
        _p("est_output", "Estimated output", "indicator", "float", unit="dBm",
           group="Estimate", order=41, decimals=2, plottable=True,
           read_path=["est_output_dBm"]),
        _p("compression", "Compression", "indicator", "float", unit="dB",
           group="Estimate", order=42, decimals=2, read_path=["compression_dB"],
           help=f"How far the estimate is into compression (P1dB {hw.p1db_dBm:g} dBm)."),
        _p("output_warning", "Output above warning level", "indicator", "bool",
           group="Estimate", order=43, read_path=["output_warning"]),

        _p("temperature", "Temperature", "indicator", "float", unit="C",
           group="Health", order=50, decimals=1, plottable=True,
           read_path=["temperature_C"]),
        _p("supply", "USB supply", "indicator", "float", unit="V",
           group="Health", order=51, decimals=2, plottable=True,
           read_path=["supply_V"]),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),

        # The panic button, usable in a scan routine (e.g. after_scan). The
        # routine waits until the device ITSELF reads back "off" (amp_on is the
        # poll thread's readback), not merely until the command was accepted:
        # a scan that goes on to touch the sample right after should not trust
        # an off that has not been confirmed. A stale frame cannot fool this:
        # the only stale value that satisfies it is "already off".
        _p("amp_off", "Amplifier OFF", "action", "action", group="Output", order=11,
           wait={"ready": {"policy": "flag_only", "key": "amp_on", "invert": True},
                 "timeout_s": timeout_s},
           help="Switch the amplifier stage off."),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "dsamp",
                "label": "RF amplifier", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
