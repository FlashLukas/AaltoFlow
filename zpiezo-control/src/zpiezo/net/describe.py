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

Limits are static (the config's voltage envelope). `revision` still
travels in every status frame as `describe_rev` so a client notices an
edited envelope.
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


def build_manifest(brain) -> dict:
    """The simplest module in the suite: one voltage, held.

    Worth having anyway -- the camera drives focus through this service, so a
    control screen that can show Z alongside the camera view is exactly the sort
    of cross-module panel `describe` exists to make possible.
    """
    lim = brain.cfg.limits
    params = [
        _p("voltage", "Drive voltage", "control", "float", unit="V",
           group="Focus", order=10, min=lim.v_min, max=lim.v_max,
           step=brain.cfg.hardware.step_v, decimals=3, plottable=True,
           read_path=["voltage"],
           set={"verb": "set_voltage", "arg": "volts"},
           settle={"policy": "echoes", "key": "voltage", "tol": 1e-3},
           help="Piezo drive voltage. There is no control loop: it holds what "
                "it is told. Out-of-range requests are CLAMPED, not refused."),

        _p("target", "Target voltage", "indicator", "float", unit="V",
           group="Focus", order=20, decimals=3, read_path=["target"]),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
    ]
    manifest = {"schema": SCHEMA_VERSION, "module": "zpiezo",
                "label": "Z focus piezo", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
