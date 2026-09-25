"""describe.py -- this service's self-description: what can be shown and driven.

Contract: INSTRUMENT_MODULE_GUIDE.md section 6b. The rule that keeps it honest:
nothing here restates a value that lives elsewhere -- every bound is looked up
from cfg when the manifest is built.

Three things make this manifest more interesting than smb's:

1. THE MEASUREMENT IS AN ACQUISITION. The detectors a scan should record
   (x1 ... theta2, aux1, aux2) read the LATCHED sample, and carry an `acquire`
   block: trigger `acquire`, then wait until status shows that acquisition's id
   with `acquiring` false. `target_key` tells the coordinator to take the id
   from the trigger's REPLY, so its wait cannot be satisfied by the previous
   point's status (adopt_then_flag, on the detector side). All ten share one
   group, so a scan point costs ONE settle-and-latch, not ten.

   The same quantities also appear as `live_*` indicators for a front panel.
   They are fresh but NOT settled; the labels say so.

2. THE MANIFEST CHANGES SHAPE WITH THE REFERENCE MODE. With an internal
   reference `freq1` is a control (we set the oscillator). With an external
   reference it is an indicator (a PLL measures it), and a PLL-lock indicator
   appears. The id stays `freq1` either way, so a recipe that records it works
   in both modes. The revision changes, so clients re-fetch.

3. PER-CHANNEL SETTLE KEYS ARE LIST ENTRIES. Status carries two-element lists
   (index 0 = channel 1), so each settle block names `key` plus `index`.
"""

from __future__ import annotations

import json
import zlib

#: Bumped only if the descriptor FORMAT changes in a way clients must notice.
SCHEMA_VERSION = 1


def _p(id, label, kind, type, *, unit="", group="", order=0, value=None,
       min=None, max=None, step=None, decimals=None, options=None,
       writable=None, plottable=False, read_path=None, scale=None, set=None,
       settle=None, args=None, danger=False, acquire=None, help=""):
    """One descriptor. See INSTRUMENT_MODULE_GUIDE.md for the field contract."""
    d = {
        "id": id, "label": label, "kind": kind, "type": type,
        "unit": unit, "group": group, "order": order,
        "writable": (kind == "control") if writable is None else writable,
        "plottable": plottable,
        "read_path": read_path,
    }
    for k, v in (("value", value), ("min", min), ("max", max), ("step", step),
                 ("decimals", decimals), ("options", options), ("scale", scale),
                 ("set", set), ("settle", settle), ("args", args),
                 ("acquire", acquire), ("help", help)):
        if v is not None and v != "":
            d[k] = v
    if danger:
        d["danger"] = True
    return d


def manifest_revision(manifest: dict) -> int:
    """CRC over the manifest with `value` stripped -- derived, never hand-bumped."""
    skeleton = [
        {k: v for k, v in p.items() if k != "value"}
        for p in manifest.get("parameters", [])
    ]
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(blob.encode("utf-8"))


def read_path(status: dict, path):
    """Resolve a descriptor's `read_path` (a list of keys / indices)."""
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


#: The quantities measured per channel: (id stem, label, unit, status key, decimals)
_MEASURED = (
    ("x", "X", "V", "x", 6),
    ("y", "Y", "V", "y", 6),
    ("r", "R", "V", "r", 6),
    ("theta", "Theta", "deg", "theta_deg", 2),
)


def build_manifest(lockin) -> dict:
    cfg = lockin.cfg
    lim = cfg.limits
    acq = cfg.acquisition

    # One acquisition, shared by every scan detector.
    acquire = {
        "group": "sample",
        "trigger_verb": "acquire",
        "target_key": "acq_id",
        "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                  "flag_key": "acquiring", "invert": True},
        "timeout_s": acq.timeout_s,
    }

    params = []
    for i in range(2):
        n = i + 1
        ch = cfg.channel(i)
        grp = f"Channel {n}"
        external = ch.reference == "external"

        # -- settings ----------------------------------------------------------
        # Time constant: offered in ms (scale 1e-3), commanded and published in
        # s. The readback is what the HARDWARE applied (it may round); the
        # settle check compares against what we ASKED for, which is echoed
        # exactly -- comparing against the rounded value would never match.
        params.append(_p(
            f"tc{n}", f"Ch{n} time constant", "control", "float", unit="ms",
            group=grp, order=10, decimals=4, scale=1e-3,
            min=lim.tc_min_s * 1e3, max=lim.tc_max_s * 1e3,
            read_path=["tc_s", i],
            set={"verb": "set_time_constant", "arg": "time_constant_s",
                 "extra": {"channel": n}},
            settle={"policy": "echoes", "key": "tc_set_s", "index": i,
                    "tol": 1e-12},
            help="Low-pass time constant. A scan point waits several of these "
                 "(see the settle-time indicator) before it is recorded."))

        params.append(_p(
            f"order{n}", f"Ch{n} filter order", "control", "int", group=grp,
            order=20, min=lim.order_min, max=lim.order_max, step=1,
            read_path=["order", i],
            set={"verb": "set_order", "arg": "order", "extra": {"channel": n}},
            settle={"policy": "echoes", "key": "order", "index": i, "tol": 0.5},
            help="1..8 cascaded RC stages (6 dB/oct each). Higher order rejects "
                 "noise better but settles more slowly."))

        params.append(_p(
            f"ref{n}", f"Ch{n} reference", "control", "enum", group=grp,
            order=5, options=["internal", "external"],
            read_path=["reference", i],
            set={"verb": "set_reference", "arg": "mode", "extra": {"channel": n}},
            help="external: a PLL locks the oscillator to the reference input "
                 "and the frequency is measured. internal: set the frequency."))

        if external:
            params.append(_p(
                f"freq{n}", f"Ch{n} reference frequency", "indicator", "float",
                unit="Hz", group=grp, order=30, decimals=4, plottable=True,
                read_path=["ref_freq_Hz", i],
                help="Measured by the PLL on the external reference."))
            params.append(_p(
                f"locked{n}", f"Ch{n} PLL locked", "indicator", "bool",
                group=grp, order=31, read_path=["pll_locked", i]))
        else:
            params.append(_p(
                f"freq{n}", f"Ch{n} reference frequency", "control", "float",
                unit="Hz", group=grp, order=30, decimals=4, plottable=True,
                min=lim.freq_min_Hz, max=lim.freq_max_Hz,
                read_path=["ref_freq_Hz", i],
                set={"verb": "set_frequency", "arg": "frequency_Hz",
                     "extra": {"channel": n}},
                settle={"policy": "echoes", "key": "freq_set_Hz", "index": i,
                        "tol": 1e-6}))

        params.append(_p(
            f"settle{n}", f"Ch{n} settle time", "indicator", "float", unit="ms",
            group=grp, order=40, decimals=2, scale=1e-3,
            read_path=["settle_s", i],
            help=f"Time to reach {acq.settle_percent:g} % of a step with the "
                 f"applied time constant and order."))

        # -- measurement: latched (scan) and live (panel) ------------------------
        for j, (stem, label, unit, key, dec) in enumerate(_MEASURED):
            params.append(_p(
                f"{stem}{n}", f"Ch{n} {label}", "indicator", "float", unit=unit,
                group="Measurement", order=100 + 10 * i + j,
                decimals=dec, read_path=["sample", key, i], acquire=acquire,
                help="Settled and latched by `acquire`: safe to record in a scan."))
            params.append(_p(
                f"live_{stem}{n}", f"Ch{n} {label} (live, unsettled)", "indicator",
                "float", unit=unit, group="Live", order=200 + 10 * i + j,
                decimals=dec, plottable=True, read_path=["live", key, i]))

    for k in range(2):
        params.append(_p(
            f"aux{k + 1}", f"AUX IN {k + 1}", "indicator", "float", unit="V",
            group="Measurement", order=150 + k, decimals=4,
            read_path=["sample", "aux_in", k], acquire=acquire,
            help="Latched with the demodulator sample, averaged over the same window."))
        params.append(_p(
            f"live_aux{k + 1}", f"AUX IN {k + 1} (live)", "indicator", "float",
            unit="V", group="Live", order=250 + k, decimals=4, plottable=True,
            read_path=["live", "aux_in", k]))

    params += [
        _p("acquire", "Acquire sample", "action", "action", group="Measurement",
           order=1,
           help="Wait the settle time, average, and latch one sample."),
        _p("acquiring", "Acquiring", "indicator", "bool", group="Measurement",
           order=2, read_path=["acquiring"]),
        _p("acq_id", "Acquisition #", "indicator", "int", group="Measurement",
           order=3, read_path=["acq_id"]),
        _p("connected", "Connected", "indicator", "bool", group="Status",
           order=1, read_path=["connected"]),
        _p("idn", "Instrument", "indicator", "string", group="Status", order=2,
           read_path=["idn"]),
        _p("hw_error", "Hardware error", "indicator", "string", group="Status",
           order=3, read_path=["hw_error"]),
    ]

    manifest = {"schema": SCHEMA_VERSION, "module": "hf2",
                "label": "Lock-in amplifier (HF2LI)", "parameters": params}
    manifest["revision"] = manifest_revision(manifest)
    return manifest
