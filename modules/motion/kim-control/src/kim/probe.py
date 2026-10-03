"""Which KIM101 controllers can this PC see? -- for Mission Control's
"Instruments on this PC" (module.toml [hardware] probe = "scripts/probe.py").

THE RULE: a probe only LISTS. It never opens a controller, never sends a byte,
never changes a setting. pylablib's ``Thorlabs.list_kinesis_devices()`` asks
the FTDI driver which Kinesis devices are on the USB bus -- the same listing
the real backend already uses to find "the one KIM101" when no serial is set
(backends/kinesis_kim.py, _find_kim101). It does not talk to the controller.

Two things learned on the lab PC (2026-10-03):

* the KIM101 lists as ("97xxxxxx", "Piezo Motor Controller"): an 8-digit
  serial starting with 97;
* a controller a service has OPEN is NOT listed (FTDI does not enumerate an
  open device). So the probe also reports what the kim service holds right
  now (hwlock): "held by the running kim service", not "nothing found".

The address of a row is the Kinesis serial: what ``run_service.py --serial``
takes and what the backend claims in hwlock.
"""

from __future__ import annotations

from . import hwlock

#: Kinesis serials start with a product code; 97 = KIM101 (lab PC 2026-10-03)
KIM101_PREFIX = "97"
#: what pylablib's list says for a KIM101 (lab PC 2026-10-03)
KIM101_DESCRIPTION = "Piezo Motor Controller"
#: why a held controller is missing from the vendor list
HELD = "held by the running kim service (a controller is not listed while it is open)"


def _held_by_kim() -> list[str]:
    """Serials the kim service holds now (its hwlock claims)."""
    try:
        return [str(i.get("address", "")) for i in hwlock.held()
                if i.get("module") == "kim" and i.get("address")]
    except Exception:
        return []


def probe() -> dict:
    """{"devices": [...], "note": "..."} -- never raises."""
    devices, notes = [], []
    try:
        # LAZY: the vendor library only in here, as in the real backend
        from pylablib.devices import Thorlabs        # noqa: PLC0415
    except ImportError:
        Thorlabs = None
        notes.append("pylablib is not installed: run 'uv sync --all-extras' in "
                     "kim-control (and install Thorlabs Kinesis for its USB driver)")
    listed = []
    if Thorlabs is not None:
        try:
            listed = list(Thorlabs.list_kinesis_devices())   # VERIFY: list only, opens none
        except Exception as exc:
            notes.append(f"the Kinesis device list failed: {type(exc).__name__}: {exc}")
    for conn, desc in listed:
        serial, desc = str(conn).strip(), str(desc or "").strip()
        is_kim = serial.startswith(KIM101_PREFIX)
        devices.append({
            "address": serial,
            "identity": "Thorlabs KIM101" if is_kim else f"Thorlabs {desc or 'Kinesis device'}",
            "detail": (f"Kinesis '{desc}', serial {serial}" if desc else f"Kinesis serial {serial}")
                      + ("" if is_kim else " (not a KIM101: serial does not start with 97)"),
        })
    seen = {d["address"] for d in devices}
    for serial in _held_by_kim():
        if serial not in seen:
            devices.append({"address": serial, "identity": "Thorlabs KIM101",
                            "detail": HELD})
    if Thorlabs is not None and not devices:
        notes.append("no Kinesis device listed (is it plugged in, and the Kinesis "
                     "app closed? A controller open in another program is not listed)")
    return {"devices": devices, "note": "; ".join(notes)}
