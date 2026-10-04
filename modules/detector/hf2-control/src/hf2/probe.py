"""Which Zurich Instruments lock-ins can this PC see? -- for Mission Control's
"Instruments on this PC" (module.toml [hardware] probe = "scripts/probe.py").

THE RULE: a probe only LISTS. It does not connect to the data server
(ziDAQServer is never created), never subscribes, never sets a node.
LabOne's ``zhinst.core.ziDiscovery`` finds devices the way the LabOne UI's
device list does -- ``findAll()`` gives the device ids ("dev1234"), ``get(id)``
what discovery knows about one (type, interfaces, which server holds it) --
without opening a session to any of them.

The address of a row is the device id: what ``run_service.py --device`` takes
and what the backend claims in hwlock (zhinst_hf2.py open()).

Not verified on an HF2 (none on the lab PC, 2026-10-03): discovery may well
list only the newer instruments (MFLI / UHFLI, data server 8004), because an
HF2 is reached through its own HF2 data server on port 8005. If nothing is
listed, the device id is in the LabOne UI or on the instrument's label.
"""

from __future__ import annotations

from . import hwlock

MODULE = "hf2"
HELD = "held by the running hf2 service"


def probe() -> dict:
    """{"devices": [...], "note": "..."} -- never raises."""
    devices, notes = [], []
    try:
        import zhinst.core                       # LAZY, as in backends/zhinst_hf2.py
    except ImportError:
        zhinst = None
        notes.append("zhinst-core is not installed: install LabOne, then add "
                     "zhinst-core (the same version) to hf2-control and uv sync")
    if zhinst is not None:
        try:
            disc = zhinst.core.ziDiscovery()                # VERIFY: no server connection
            for dev_id in disc.findAll():                   # VERIFY: list of ids
                dev_id = str(dev_id).strip().lower()
                if not dev_id:
                    continue
                try:
                    props = dict(disc.get(dev_id) or {})    # VERIFY: a dict of properties
                except Exception:
                    props = {}
                kind = str(props.get("devicetype", "") or "")          # VERIFY key
                bits = [f"device id {dev_id}"]
                ifaces = props.get("interfaces")                       # VERIFY key
                if ifaces:
                    bits.append("via " + ", ".join(str(i) for i in ifaces))
                server = props.get("serveraddress")                    # VERIFY key
                if server:
                    bits.append(f"data server {server}:{props.get('serverport', '')}".rstrip(":"))
                if kind and not kind.upper().startswith("HF2"):
                    bits.append("not an HF2")
                devices.append({"address": dev_id,
                                "identity": f"Zurich Instruments {kind}".strip()
                                            if kind else "Zurich Instruments lock-in",
                                "detail": ", ".join(bits)})
        except Exception as exc:
            notes.append(f"LabOne discovery failed: {type(exc).__name__}: {exc}")
    seen = {d["address"].upper() for d in devices}
    try:
        for info in hwlock.held():
            a = str(info.get("address", ""))
            if info.get("module") != MODULE or not a:
                continue
            if a.upper() in seen:                # listed AND held: say so
                for d in devices:
                    if d["address"].upper() == a.upper():
                        d["detail"] += "; " + HELD
            else:
                devices.append({"address": a, "identity": "Zurich Instruments HF2LI",
                                "detail": HELD})
    except Exception:
        pass
    if zhinst is not None and not devices and not notes:
        notes.append("LabOne discovery lists no device (an HF2 may only be visible "
                     "through its data server: read its id in the LabOne UI)")
    return {"devices": devices, "note": "; ".join(notes)}
