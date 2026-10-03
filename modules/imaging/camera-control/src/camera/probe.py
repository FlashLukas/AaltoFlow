"""Which IDS cameras can this PC see? -- for Mission Control's "Instruments on
this PC" (module.toml [hardware] probe = "scripts/probe.py").

THE RULE: a probe only LISTS. It never opens a camera, never starts a stream,
never changes a setting. IDS peak's DeviceManager.Update() + Devices() is the
host enumerating the USB3 Vision devices -- the same step the real backend
does BEFORE it claims and opens one (backends/ids.py, open()). Reading a
device DESCRIPTOR (serial, model name) does not open the camera's control
channel; OpenDevice() would, and is never called here.

The address of a row is the camera's SERIAL: what ``run_service.py --camera``
takes (IDSCamera picks a device by serial or display name). The backend claims
``CAMERA::<serial>`` in hwlock (backends/claim.py), so that is the row's
"lock" -- a camera the running service holds shows as held.

The GenICam/Harvester backend is not listed: it needs a vendor .cti producer
file the module does not configure (genicam.py), and harvesters is not
installed by default.
"""

from __future__ import annotations

from . import hwlock
from .backends import claim as hwclaim

HELD = "held by the running camera service (IDS peak may not list a camera while it is open)"


def _held_serials() -> list[str]:
    """Serials of the cameras the camera service holds now (hwlock)."""
    out = []
    try:
        for i in hwlock.held():
            a = str(i.get("address", ""))
            if i.get("module") == hwclaim.MODULE and a.upper().startswith("CAMERA::"):
                out.append(a.split("::", 1)[1])
    except Exception:
        pass
    return out


def _text(fn) -> str:
    try:
        return str(fn()).strip()
    except Exception:
        return ""


def probe() -> dict:
    """{"devices": [...], "note": "..."} -- never raises."""
    devices, notes = [], []
    try:
        from ids_peak import ids_peak             # LAZY, as in backends/ids.py
    except ImportError:
        ids_peak = None
        notes.append("ids_peak is not installed: install IDS peak, then run "
                     "'uv sync --all-extras' in camera-control")
    if ids_peak is not None:
        initialized = False
        try:
            ids_peak.Library.Initialize()                      # VERIFY: no device opened
            initialized = True
            dm = ids_peak.DeviceManager.Instance()
            dm.Update()                                        # VERIFY: enumerate only
            for d in dm.Devices():                             # descriptors, not devices
                serial = _text(d.SerialNumber)                 # VERIFY
                model = _text(d.ModelName)                     # VERIFY: ModelName on a descriptor
                name = _text(d.DisplayName)                    # VERIFY
                if not serial:
                    continue
                bits = [f"serial {serial}"]
                # DisplayName carries IDS' device id ("1409<hex>U3-...-0"),
                # i.e. an identifier: fine on screen, but tests / renders /
                # docs use made-up ones only
                if name and name != model:
                    bits.append(name)
                devices.append({"address": serial,
                                "identity": f"IDS {model}" if model else "IDS camera",
                                "detail": "IDS peak, " + ", ".join(bits),
                                "lock": hwclaim.camera_address(serial)})
        except Exception as exc:
            notes.append(f"IDS peak could not list cameras: {type(exc).__name__}: {exc}")
        finally:
            if initialized:
                try:
                    ids_peak.Library.Close()                   # VERIFY
                except Exception:
                    pass
    seen = {d["address"] for d in devices}
    held = _held_serials()
    for d in devices:
        # IDS peak still lists a camera the service has open (lab PC
        # 2026-10-03): the row must say it is held, not look free
        if d["address"] in held:
            d["detail"] += "; held by the running camera service"
    for serial in held:
        if serial not in seen:
            devices.append({"address": serial, "identity": "IDS camera", "detail": HELD,
                            "lock": hwclaim.camera_address(serial)})
    if ids_peak is not None and not devices and not notes:
        notes.append("no IDS camera listed (USB3 cable, IDS peak Cockpit closed?)")
    return {"devices": devices, "note": "; ".join(notes)}
