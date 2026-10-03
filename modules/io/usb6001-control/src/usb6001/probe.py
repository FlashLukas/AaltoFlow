"""Which NI DAQ cards can this PC see? -- for Mission Control's "Instruments on
this PC" (module.toml [hardware] probe = "scripts/probe.py").

THE RULE: a probe only LISTS. It never creates a task, never reads or writes
a channel, never changes a setting. ``nidaqmx.system.System.local().devices``
asks the NI-DAQmx driver for the devices NI MAX shows; reading a device's
product type and serial number is a property query, the same one the real
backend makes before its first task (backends/nidaq.py, open()).

The address of a row is the DAQmx device NAME ("Dev1"): what
``run_service.py --device`` takes and the first thing the backend claims in
hwlock -- so a card the running service holds shows as held.

Every card is listed (a USB-6002 or a PCIe card too): the row says which
product it is, and only a USB-6001 is what this module was written for.
"""

from __future__ import annotations

from . import hwlock

MODULE = "usb6001"
HELD = "held by the running usb6001 service"


def _serial_text(serial) -> str:
    """NI MAX shows a serial in hex (backends/nidaq.py serial_address)."""
    if isinstance(serial, int):
        return f"{serial:08X}"                  # VERIFY: Windows' USB id uses 8 hex digits
    return str(serial or "").strip().upper()


def probe() -> dict:
    """{"devices": [...], "note": "..."} -- never raises."""
    devices, notes = [], []
    try:
        import nidaqmx                          # LAZY, as in backends/nidaq.py
        import nidaqmx.system
    except ImportError:
        nidaqmx = None
        notes.append("nidaqmx is not installed: run 'uv sync --all-extras' in "
                     "usb6001-control (and install the NI-DAQmx driver from ni.com)")
    if nidaqmx is not None:
        try:
            for dev in nidaqmx.system.System.local().devices:   # VERIFY: list only
                name = str(getattr(dev, "name", "") or "").strip()  # VERIFY
                if not name:
                    continue
                product = _attr(dev, "product_type")             # VERIFY
                serial = _serial_text(_attr(dev, "serial_num", raw=True))  # VERIFY
                bits = [f"DAQmx name {name}"]
                if serial:
                    bits.append(f"serial {serial}")
                if product and "6001" not in product:
                    bits.append("not a USB-6001")
                devices.append({"address": name,
                                "identity": f"NI {product}" if product else "NI DAQ device",
                                "detail": ", ".join(bits)})
        except Exception as exc:
            notes.append(f"NI-DAQmx could not list its devices: {type(exc).__name__}: {exc}")
    seen = {d["address"].upper() for d in devices}
    try:
        for info in hwlock.held():
            a = str(info.get("address", ""))
            if info.get("module") == MODULE and a and not a.upper().startswith("NI-DAQ-SN:") \
                    and a.upper() not in seen:
                devices.append({"address": a, "identity": "NI DAQ device", "detail": HELD})
    except Exception:
        pass
    if nidaqmx is not None and not devices and not notes:
        notes.append("no NI-DAQmx device listed (plugged in? does NI MAX show it?)")
    return {"devices": devices, "note": "; ".join(notes)}


def _attr(dev, name: str, raw: bool = False):
    try:
        v = getattr(dev, name)
    except Exception:
        return None if raw else ""
    return v if raw else str(v or "").strip()
