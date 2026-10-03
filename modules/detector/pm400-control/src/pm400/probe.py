"""Which PM400 power meters can this PC see? -- for Mission Control's
"Instruments on this PC" (module.toml [hardware] probe = "scripts/probe.py").

THE RULE: a probe only LISTS. It never opens a meter (TLPMX_init is never
called), never reads a power, never changes a setting. TLPMX's resource
listing -- TLPMX_findRsrc, TLPMX_getRsrcName, TLPMX_getRsrcInfo, all with
vi = 0, i.e. WITHOUT a session -- is what backends/tlpmx.py list_resources()
does (the same calls pm16-control's list_devices.py ran on the lab PC with a
PM16, 2026-09-15; never yet with a PM400).

A meter on Thorlabs' own USB driver is NOT visible to NI-VISA, so the VISA
scan may not find it: this probe is how it shows up.

The address of a row is the TLPMX resource name (USB0::0x1313::0x807D::
<serial>::INSTR): what ``run_service.py --resource`` takes and what the
backend claims in hwlock.

TLPMX lists EVERY Thorlabs meter (a PM400, a PM100D ...); only the PM400 family
is reported as a device here, the others are counted in the note -- each has
its own module and its own probe.

Gotcha #23: getRsrcInfo's "available" flag can say "in use" for a free meter
(seen in a process holding a ZeroMQ context). It is shown as a hint, never
used to decide anything.
"""

from __future__ import annotations

from . import hwlock
from .backends.tlpmx import TLPMXError, list_resources

MODULE = "pm400"
#: the PM400's USB product ids (TLPMX wrapper table, backends/tlpmx.py): 0x807D,
#: 0x8075 with the firmware-update interface on.  # VERIFY on the console
PID_TEXTS = ("0X807D", "0X8075")
HELD = "held by the running pm400 service"


def _mine(r: dict) -> bool:
    res = str(r.get("resource", "")).upper()
    return str(r.get("model", "")).upper().startswith("PM400") or \
        any(p in res for p in PID_TEXTS)


def probe(lister=list_resources) -> dict:
    """{"devices": [...], "note": "..."} -- never raises. `lister` = a fake in tests."""
    devices, notes = [], []
    try:
        found = lister()                    # VERIFY: findRsrc/getRsrcName/getRsrcInfo, vi = 0
    except TLPMXError as exc:
        found = None
        notes.append(f"TLPMX unavailable: {exc}")
    except Exception as exc:
        found = None
        notes.append(f"TLPMX could not list meters: {type(exc).__name__}: {exc}")
    others = 0
    for r in found or []:
        if not _mine(r):
            others += 1
            continue
        bits = [f"S/N {r.get('serial', '')}".strip(), "TLPMX"]
        if not r.get("available", True):
            bits.append("TLPMX says in use (can be wrong, gotcha #23)")
        devices.append({"address": str(r.get("resource", "")),
                        "identity": f"Thorlabs {r.get('model', '') or 'PM400'}",
                        "detail": ", ".join(b for b in bits if b and b != "S/N")})
    if others:
        notes.append(f"{others} other Thorlabs meter(s) listed by TLPMX, not a PM400")
    seen = {hwlock.normalize(d["address"]) for d in devices}
    try:
        for info in hwlock.held():
            a = str(info.get("address", ""))
            if info.get("module") == MODULE and a and hwlock.normalize(a) not in seen:
                devices.append({"address": a, "identity": "Thorlabs PM400", "detail": HELD})
    except Exception:
        pass
    if found is not None and not devices and not notes:
        notes.append("no PM400 listed (plugged in and switched on? Thorlabs OPM closed?)")
    return {"devices": devices, "note": "; ".join(notes)}
