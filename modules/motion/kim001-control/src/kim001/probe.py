"""Which KIM001 controllers can this PC see? -- for Mission Control's
"Instruments on this PC" (module.toml [hardware] probe = "scripts/probe.py").

THE RULE: a probe only LISTS. It never opens a controller, never sends a byte,
never changes a setting. pylablib's ``Thorlabs.list_kinesis_devices()`` asks
the FTDI driver which Kinesis devices are on the USB bus -- the same listing
the real backend uses to find "the one KIM001" when no serial is set
(backends/kinesis_kim001.py, _find_kim001). It does not talk to the controller.

What a listing can and cannot tell (kim-control learned the first two on the
lab PC, 2026-10-03, with a KIM101):

* a Kinesis serial starts with a product code (the KIM101's is 97). Which
  prefix a KIM001 has is NOT known yet: this probe uses
  hardware.serial_prefix (config.KIM001_SERIAL_PREFIX, # VERIFY). If the
  KIM001 shares the KIM101's 97, a listing cannot tell the two apart -- the
  row then says so, and the backend's model check after opening is what
  refuses a KIM101;
* a controller a service has OPEN is NOT listed (FTDI does not enumerate an
  open device). So the probe also reports what the kim001 service holds right
  now (hwlock): "held by the running kim001 service", not "nothing found".

The address of a row is the Kinesis serial: what ``run_service.py --serial``
takes and what the backend claims in hwlock.
"""

from __future__ import annotations

from . import hwlock
from .config import KIM001_SERIAL_PREFIX

#: non-Thorlabs FTDI devices Kinesis' scan lists, by their description
OTHER_NAMES = {
    "SignalHoundTG": "Signal Hound TG44A tracking generator (seen by Kinesis' FTDI scan)",
}
#: why a held controller is missing from the vendor list
HELD = "held by the running kim001 service (a controller is not listed while it is open)"
#: a matching row cannot be told from a KIM101 by its listing alone
MAYBE = ("serial prefix matches the KIM001 setting -- a KIM101 may share it "
         "(# VERIFY); the service checks the model after opening")


def _prefix() -> str:
    """The KIM001 serial prefix in force: kim001.ini's hardware.serial_prefix
    if the file sets one, else the built-in guess. Reading the file is local
    and harmless; it touches no device."""
    try:
        from .config import load_startup_config
        cfg, _path, _loaded = load_startup_config(None)
        return (cfg.hardware.serial_prefix or "").strip() or KIM001_SERIAL_PREFIX
    except Exception:
        return KIM001_SERIAL_PREFIX


def _held_by_us() -> list[str]:
    """Serials the kim001 service holds now (its hwlock claims)."""
    try:
        return [str(i.get("address", "")) for i in hwlock.held()
                if i.get("module") == "kim001" and i.get("address")]
    except Exception:
        return []


def probe() -> dict:
    """{"devices": [...], "note": "..."} -- never raises."""
    devices, notes = [], []
    prefix = _prefix()
    try:
        # LAZY: the vendor library only in here, as in the real backend
        from pylablib.devices import Thorlabs        # noqa: PLC0415
    except ImportError:
        Thorlabs = None
        notes.append("pylablib is not installed: run 'uv sync --all-extras' in "
                     "kim001-control (and install Thorlabs Kinesis for its USB driver)")
    listed = []
    if Thorlabs is not None:
        try:
            listed = list(Thorlabs.list_kinesis_devices())   # VERIFY: list only, opens none
        except Exception as exc:
            notes.append(f"the Kinesis device list failed: {type(exc).__name__}: {exc}")
    for conn, desc in listed:
        serial, desc = str(conn).strip(), str(desc or "").strip()
        match = bool(prefix) and serial.startswith(prefix)
        dev = {
            "address": serial,
            "identity": "Thorlabs KIM001",
            "detail": (f"Kinesis '{desc}', serial {serial}" if desc
                       else f"Kinesis serial {serial}") + f"; {MAYBE}",
        }
        if not match:
            # Kinesis' scan lists EVERY FTDI device with a description, not only
            # Thorlabs controllers (the Signal Hound TG44A, "SignalHoundTG").
            # Information only: "other" = not for kim001, so it is never
            # suggested for or offered to this module.
            dev["other"] = True
            dev["identity"] = OTHER_NAMES.get(desc, f"{desc or 'Kinesis device'} "
                                                    f"(seen by Kinesis' FTDI scan)")
            dev["detail"] = (f"Kinesis '{desc}', serial {serial}" if desc
                             else f"Kinesis serial {serial}")
            dev["detail"] += f" (not a KIM001: serial does not start with {prefix})"
        devices.append(dev)
    seen = {d["address"] for d in devices}
    held = _held_by_us()
    for d in devices:
        if d["address"] in held:                 # listed AND held: say so
            d["detail"] += "; held by the running kim001 service"
    for serial in held:
        if serial not in seen:
            devices.append({"address": serial, "identity": "Thorlabs KIM001",
                            "detail": HELD})
    if Thorlabs is not None and not devices:
        notes.append("no Kinesis device listed (is it plugged in, and the Kinesis "
                     "app closed? A controller open in another program is not listed)")
    return {"devices": devices, "note": "; ".join(notes)}
