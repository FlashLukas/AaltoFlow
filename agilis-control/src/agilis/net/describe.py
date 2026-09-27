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

LIMITS ARE DYNAMIC HERE. An armed leash REPLACES the absolute travel clamp,
and a new step-size calibration changes what those steps mean in um, so the
bounds are read from the brain's published effective limits (`limit_lo` /
`limit_hi`) and step sizes. Either change moves `revision`, and every status
frame carries `describe_rev`.
"""

from __future__ import annotations

import json
import zlib

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, stream=None, help=""):
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
                 ("stream", stream), ("help", help)):
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


AXES = ("X", "Y")


def build_manifest(brain) -> dict:
    """Two slip-stick axes whose um limits move with the leash and the calibration.

    Positions are offered in MICROMETRES (the estimate the brain keeps from the
    forward/backward step tallies). Their bounds are the effective STEP bounds
    (leash applied when armed) times the MEAN step size -- nominal, because on
    an asymmetric open-loop axis the um reached at a given counter depends on
    the path. The settle policy is adopt_then_flag on `target_um`: the brain
    keeps a commanded um verbatim, so a scan waits for ITS move, not for the
    `moving=False` left over from the previous point (gotcha #2).
    """
    cfg = brain.cfg
    st = brain.status()
    lo_steps = list(st.limit_lo)
    hi_steps = list(st.limit_hi)
    k_mean = list(st.um_per_step)
    leashed = bool(st.leash)

    params = []
    for i, ax in enumerate(AXES):
        low = ax.lower()
        k = k_mean[i] if k_mean[i] > 0 else 0.05
        params += [
            _p(f"position_{low}", f"Position {ax}", "control", "float",
               unit="um", group="Position", order=10 + i,
               min=lo_steps[i] * k, max=hi_steps[i] * k, decimals=3,
               plottable=True, read_path=["position_um", i],
               set={"verb": "move_to_um", "arg": "position",
                    "extra": {"axis": ax}},
               settle={"policy": "adopt_then_flag", "setpoint_key": "target_um",
                       "flag_key": "moving", "invert": True, "index": i},
               # a fly scan records the position estimate continuously and
               # bins its detectors by it (stream_start / _read / _stop)
               stream={"group": "position", "channel": low},
               help=("Open-loop ESTIMATE from counted steps x measured step size "
                     "(forward and backward separately). "
                     + ("Bounds are the LEASH box around the datum."
                        if leashed else
                        "Bounds are the travel limits; arming the leash narrows them."))),
            _p(f"moving_{low}", f"Moving {ax}", "indicator", "bool",
               group="Status", order=20 + i, read_path=["moving", i]),
            _p(f"steps_{low}", f"Step counter {ax}", "indicator", "int",
               group="Position", order=30 + i, read_path=["position_steps", i]),
            _p(f"amplitude_fwd_{low}", f"Step amplitude {ax} +", "control", "int",
               group="Drive", order=40 + 2 * i, min=1, max=50, step=1,
               read_path=["amplitude_fwd", i],
               set={"verb": "set_amplitude", "arg": "value",
                    "extra": {"axis": ax, "direction": 1}},
               settle={"policy": "echoes", "key": "amplitude_fwd", "index": i},
               help="Height of the drive sawtooth (SU), not a length. Changing it "
                    "changes the step size, so the um calibration stops applying."),
            _p(f"amplitude_bwd_{low}", f"Step amplitude {ax} -", "control", "int",
               group="Drive", order=41 + 2 * i, min=1, max=50, step=1,
               read_path=["amplitude_bwd", i],
               set={"verb": "set_amplitude", "arg": "value",
                    "extra": {"axis": ax, "direction": -1}},
               settle={"policy": "echoes", "key": "amplitude_bwd", "index": i},
               help="Backward direction; see the forward amplitude."),
            _p(f"cal_valid_{low}", f"Step size valid {ax}", "indicator", "bool",
               group="Calibration", order=60 + i, read_path=["cal_valid", i],
               help="False when the amplitude differs from the one the step size "
                    "was measured at: um are then approximate."),
            _p(f"estimate_ok_{low}", f"Position estimate valid {ax}", "indicator", "bool",
               group="Calibration", order=62 + i, read_path=["estimate_ok", i],
               help="False once any step since the datum was made at an amplitude "
                    "the step size was not measured at (a changed amplitude, or a "
                    "jog at speed 2/3, which always uses amplitude 50). Set the "
                    "datum again to trust the um."),
            _p(f"um_per_step_fwd_{low}", f"Step size {ax} +", "indicator", "float",
               unit="um", group="Calibration", order=64 + 2 * i, decimals=5,
               read_path=["um_per_step_fwd", i]),
            _p(f"um_per_step_bwd_{low}", f"Step size {ax} -", "indicator", "float",
               unit="um", group="Calibration", order=65 + 2 * i, decimals=5,
               read_path=["um_per_step_bwd", i]),
            _p(f"limit_switch_{low}", f"Limit switch {ax}", "indicator", "bool",
               group="Status", order=70 + i, read_path=["limit_switch", i],
               help="Only stages with a limit switch (e.g. AG-LS25) report it."),
            _p(f"datum_{low}", f"Datum {ax}", "action", "action",
               group="Routines", order=100 + i, danger=True,
               help="HARDWARE reset of the step counter (ZP) -- the closest thing "
                    "this open-loop stage has to a home. Not the same as "
                    "'Zero here', which only moves the display origin."),
        ]

    params += [
        _p("step_large", "Steps: large", "control", "bool", group="Presets",
           order=210, read_path=["step_large"],
           set={"verb": "set_step_size", "arg": "large"},
           settle={"policy": "echoes", "key": "step_large"},
           help=f"Every amplitude to {cfg.motion.large_amplitude} (on) or "
                f"{cfg.motion.small_amplitude} (off)."),
        _p("leash", "Leash armed", "indicator", "bool", group="Limits",
           order=220, read_path=["leash"],
           help="When armed, a symmetric box around the datum REPLACES the "
                "travel limits -- so the position bounds above change."),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=2, read_path=["hw_error"]),
        _p("stop", "STOP", "action", "action", group="Routines", order=90,
           danger=True),
    ]
    # Actions are fired by their id (the control panel and scan routines send
    # the id as the verb). Both finish when the reply comes back, which a
    # `wait` block says, so a scan routine may use them (e.g. Datum X before a
    # scan).
    for p in params:
        if p["kind"] == "action":
            p["wait"] = {"ready": {"policy": "immediate"}}
    manifest = {"schema": SCHEMA_VERSION, "module": "agilis",
                "label": "Agilis stage", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
