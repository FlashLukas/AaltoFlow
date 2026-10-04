"""describe.py -- this service's self-description: what can be shown and driven.

The `describe` verb answers "what knobs do you have?" in a form generic enough
that a client can build a control panel for a module it has never heard of.
Two consumers: the reconfigurable control screen reads the manifest DIRECTLY,
scan-core projects it into Settables and Gettables. Full contract:
INSTRUMENT_MODULE_GUIDE.md section 6b.

THE RULE THAT KEEPS THIS HONEST: nothing here restates a value that lives
somewhere else. Every limit is looked up from cfg or from the brain when the
manifest is built.

THIS MODULE'S LIMITS ARE DYNAMIC: the wavelength range is the range of the
grating the instrument is on (or going to). A grating change therefore changes
the manifest, its `revision` moves, and every status frame's `describe_rev`
tells a client to re-fetch. The manifest's SHAPE also follows the config: the
filter wheel and exit-port controls exist only when those accessories are
fitted, so a scan cannot be built around hardware that is not there.
"""

from __future__ import annotations

import json
import zlib

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


def _move_timeout_s(mono) -> float:
    """How long a scan may wait for one move before calling it stuck.

    Derived from the datasheet slew rate (205 nm/s at 1200 l/mm, scaling as
    1200/lines) over the longest range, plus a grating swap, doubled, plus a
    margin -- generous on purpose: a timeout is for a stuck instrument, not a
    slow one."""
    cfg = mono.cfg
    worst = 0.0
    for n in range(1, int(cfg.gratings.count) + 1):
        lines, _, lo, hi = cfg.gratings.of(n)
        slew = float(cfg.sim.slew_nm_per_s_at_1200) * 1200.0 / max(1, lines)
        worst = max(worst, (hi - lo) / max(1e-6, slew))
    return round(2.0 * (worst + float(cfg.sim.grating_change_s)) + 30.0, 1)


def build_manifest(mono) -> dict:
    """The monochromator. Moves take seconds, so every mechanical control
    settles with `adopt_then_flag`: the status must first show OUR target
    (target_nm / grating_target / ...), and only then is `moving = False`
    believed. Watching `moving` alone would return on the frame from before
    the command and measure a whole scan one step behind (gotcha #2)."""
    cfg = mono.cfg
    acc = cfg.accessories
    lo, hi = mono.live_limits()
    n_grat = max(1, min(3, int(cfg.gratings.count)))
    timeout = _move_timeout_s(mono)

    def moving_settle(target_key, tol):
        return {"policy": "adopt_then_flag", "setpoint_key": target_key,
                "flag_key": "moving", "invert": True, "tol": tol}

    params = [
        _p("wavelength", "Wavelength", "control", "float", unit="nm",
           group="Wavelength", order=10, decimals=3, step=1.0, plottable=True,
           min=lo, max=hi, read_path=["wavelength_nm"],
           set={"verb": "set_wavelength", "arg": "wavelength_nm"},
           settle=moving_settle("target_nm", 1e-3),
           help="Centre wavelength at the exit slit. The range is the current "
                "grating's; it changes with the grating."),
        _p("target_nm", "Wavelength target", "indicator", "float", unit="nm",
           group="Wavelength", order=11, decimals=3, read_path=["target_nm"]),
        _p("moving", "Moving", "indicator", "bool", group="Wavelength", order=12,
           read_path=["moving"]),
        _p("bandpass", "Bandpass", "indicator", "float", unit="nm",
           group="Wavelength", order=13, decimals=3, plottable=True,
           read_path=["bandpass_nm"],
           help="Spectral resolution = dispersion x slit width (typed in: fixed "
                "slits cannot be read back)."),

        # int controls (grating / filter / port): their min/max are SETTING
        # limits, not a promise about the readback -- scan-core stores a
        # control as int32 whatever they say. The readback is None, never a
        # fake 0, before the first read or while the filter wheel is between
        # positions (monochromator.status).
        _p("grating", "Grating", "control", "int", group="Grating", order=20,
           min=1, max=n_grat, step=1, read_path=["grating"],
           set={"verb": "set_grating", "arg": "grating"},
           settle=moving_settle("grating_target", 0.5),
           help="Swap gratings. The shutter closes during the swap (the drive "
                "sweeps past zero order) and the wavelength is restored after."),
        _p("grating_lines", "Grating lines", "indicator", "int", unit="l/mm",
           group="Grating", order=21, read_path=["grating_lines"]),
        _p("grating_label", "Grating label", "indicator", "string",
           group="Grating", order=22, read_path=["grating_label"]),

        _p("shutter", "Shutter open", "control", "bool", group="Shutter", order=30,
           read_path=["shutter_open"],
           set={"verb": "set_shutter", "arg": "open"},
           settle={"policy": "echoes", "key": "shutter_open"}),
        # the SAFETY verb as a button (control.py: a viewer may always send
        # it), so the suite's Control tab offers it to a viewer too
        _p("close_shutter", "Close shutter", "action", "action", group="Shutter", order=31,
           help="Close the shutter (jumps the move queue). Allowed for anyone, "
                "also a viewer."),
    ]
    if acc.filter_wheel:
        params += [
            _p("filter", "Filter", "control", "int", group="Filter", order=40,
               min=1, max=max(1, min(6, int(acc.filter_count))), step=1,
               read_path=["filter"],
               set={"verb": "set_filter", "arg": "filter"},
               settle=moving_settle("filter_target", 0.5),
               help="Filter wheel position (order-sorting filters)."),
            _p("filter_label", "Filter label", "indicator", "string",
               group="Filter", order=41, read_path=["filter_label"]),
        ]
    if acc.dual_port:
        params.append(
            _p("port", "Exit port", "control", "int", group="Output", order=50,
               min=1, max=2, step=1, read_path=["port"],
               set={"verb": "set_port", "arg": "port"},
               settle=moving_settle("port_target", 0.5),
               help="1 = axial, 2 = lateral (motorised flip mirror)."))
    params += [
        _p("step_position", "Drive position", "indicator", "int", unit="steps",
           group="Status", order=60, read_path=["step_position"]),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=61, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=62,
           read_path=["idn"]),
        _p("error_text", "Last error", "indicator", "string", group="Status",
           order=63, read_path=["error_text"]),

        _p("abort", "Abort motion", "action", "action", group="Wavelength",
           order=90, help="Stop the wavelength drive and drop queued moves."),
        _p("step", "Step drive", "action", "action", group="Wavelength", order=91,
           args=[{"name": "steps", "label": "Steps", "type": "int", "unit": "steps",
                  "default": 10, "min": -100000, "max": 100000}],
           help="Nudge the drive by motor steps (+ = longer wavelength)."),
        _p("calibrate", "Calibrate here", "action", "action", group="Wavelength",
           order=92, danger=True,
           args=[{"name": "wavelength_nm", "label": "True wavelength", "type": "float",
                  "unit": "nm", "default": 546.074, "min": 0.0, "max": hi}],
           help="Declare the CURRENT position to be this wavelength. Rewrites the "
                "grating offset stored in the instrument."),
    ]
    for p in params:
        if p["kind"] == "control":
            p["timeout_s"] = timeout
    # Abort is safe to run from a scan routine. The reply only means FILED
    # (the worker sends ABORT at its next poll), so "done" is `moving` going
    # false. `moving` cannot fall early: the brain only clears it after a read
    # that follows the abort (the command generation guards that read).
    for p in params:
        if p["id"] == "abort":
            p["wait"] = {"ready": {"policy": "flag_only", "key": "moving",
                                   "invert": True},
                         "timeout_s": 30}
    manifest = {"schema": SCHEMA_VERSION, "module": "cs260",
                "label": "Monochromator", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
