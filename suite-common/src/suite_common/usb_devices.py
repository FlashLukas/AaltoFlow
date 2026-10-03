"""Which USB devices are plugged into this PC -- a LIST, nothing more.

Mission Control's "Instruments on this PC" finds VISA instruments and COM
ports. Many lab instruments are neither: a Thorlabs Kinesis controller, an IDS
camera, an NI DAQ card, a Signal Hound or a Thorlabs power meter talk through
their maker's own driver. Every one of them is still a USB device, and the
operating system keeps a list of those -- with the maker's vendor id (VID),
the product id (PID) and usually the serial number. This module reads that
list. It needs no vendor software and no extra package (stdlib only), and it
NEVER opens a device: the operating system answers from its own records.

* Windows: ONE PowerShell call (Get-CimInstance Win32_PnPEntity), the same
  records Device Manager shows. FTDI-based instruments (Thorlabs APT/Kinesis)
  appear a second time under FTDIBUS\\; both spellings are read and merged.
* Linux: /sys/bus/usb/devices (idVendor, idProduct, serial, product).

KNOWN_USB names a device from its ids and says which AaltoFlow module drives
it. An entry is only "verified" when the ids were seen on a real instrument
(the module notes say so); everything else carries # VERIFY until the lab PC
confirms it.

`list_usb()` never raises: what could not be read becomes a note.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: Give up on PowerShell after this many seconds (a busy PC can take a few).
TIMEOUT_S = 15.0

# (vid, pid) -> (maker, model, module key); vid alone -> the maker's fallback.
# The module key is the AaltoFlow module that drives the device ("" = none).
#
# "Verified" = seen in Device Manager on the lab PC (Get-PnpDevice -PresentOnly,
# 2026-10-03) or in a module's hardware notes. Everything else is # VERIFY.
KNOWN_USB: dict = {
    # --- verified on the lab PC ---------------------------------------------
    # "APT USB Device", class USB: the KIM101 (Kinesis = FTDI chip with a
    # Thorlabs PID). Its instance id ends in the 8-digit Kinesis serial. Other
    # Kinesis controllers (BSC203, K-Cubes) share 0403:FAF0 -- the serial's
    # first two digits tell them apart (97 = KIM101).
    (0x0403, 0xFAF0): ("Thorlabs", "Kinesis controller (KIM101: serial 97...)", "kim"),
    # "PM160", class ThorlabsUSBDevice: the PM16-121 (pm16 notes, 2026-09-15)
    (0x1313, 0x807B): ("Thorlabs", "PM16 power meter", "pm16"),
    # class IDSU3VCameras: the U3-386xCP-M. Windows' name carries the serial.
    (0x1409, 0x8000): ("IDS Imaging", "U3 camera", "camera"),
    # "USB-6001", class DAQDevice
    (0x3923, 0x76BF): ("National Instruments", "USB-6001 DAQ", "usb6001"),
    # "USB Composite Device" (+ "USB Serial Converter A"/"B"): the SA44B is a
    # dual FTDI chip; the composite parent carries the serial
    (0x0403, 0x6010): ("Signal Hound", "SA44B spectrum analyser (FTDI dual)", "signalhound"),
    # --- AMBIGUOUS: generic FTDI ids, every USB-serial cable looks the same.
    # Never named by VID:PID alone -- on the lab PC 0403:6001 is the USB-TG44A
    # tracking generator and 0403:6015 the DS Instruments SG12000L, but any
    # cable with these chips reads the same. Module left empty on purpose.
    (0x0403, 0x6001): ("FTDI", "FT232 USB-serial (could be: TG44A tracking generator / "
                               "DS Instruments generator / any USB-serial cable)", ""),
    (0x0403, 0x6015): ("FTDI", "FT-X USB-serial (could be: DS Instruments generator / "
                               "TG44A tracking generator / any USB-serial cable)", ""),
    # --- from module code / notes, not seen on the lab PC -------------------
    # PM400 console: the TLPMX wrapper's PID table (pm400 notes, question 1)
    (0x1313, 0x807D): ("Thorlabs", "PM400 power meter", "pm400"),          # VERIFY
    (0x1313, 0x8075): ("Thorlabs", "PM400 power meter (DFU on)", "pm400"),  # VERIFY
    # CCS200 spectrometer: tlccs.py's search pattern (ccs200 notes, VERIFY 2)
    (0x1313, 0x8089): ("Thorlabs", "CCS200 spectrometer", "ccs200"),       # VERIFY
    # Zurich HF2LI: not present on the lab PC; its USB ids are not in the repo.
    # Listed by the hf2 probe instead (LabOne discovery).                  # VERIFY
    # --- maker fallbacks: the maker is named, the module is NOT guessed ------
    0x1313: ("Thorlabs", "", ""),            # vendor id of the verified PM16
    0x0403: ("FTDI", "USB-serial chip (any cable or instrument)", ""),
    0x1409: ("IDS Imaging", "camera", ""),   # vendor id of the verified U3 camera
    0x3923: ("National Instruments", "", ""),  # vendor id of the verified USB-6001
    0x2184: ("GW Instek", "", "gsp818"),     # gsp.py GW_INSTEK_USB_VID    # VERIFY
}


@dataclass
class UsbDevice:
    """One USB device the operating system lists as present."""
    vid: int
    pid: int
    name: str = ""            # what the OS calls it ("APT USB Device")
    usb_class: str = ""       # Windows PnP class ("USB", "Image", ...) or the USB class code
    serial: str = ""          # "" when the device reports none
    instance: str = ""        # the OS's own id (PNPDeviceID / sysfs name)
    manufacturer: str = ""

    @property
    def usb_id(self) -> str:
        return f"{self.vid:04X}:{self.pid:04X}"

    @property
    def address(self) -> str:
        """What a module could be handed: the serial, else the OS's id."""
        return self.serial or self.instance


def known(vid: int, pid: int) -> tuple[str, str, str] | None:
    """(maker, model, module key) for these ids, or None. An exact (vid, pid)
    entry wins over the maker fallback."""
    return KNOWN_USB.get((vid, pid)) or KNOWN_USB.get(vid)


# --------------------------------------------------------------- Windows ---

_PS = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
       "Get-CimInstance Win32_PnPEntity -Filter "
       "\"PNPDeviceID LIKE 'USB\\\\VID_%' OR PNPDeviceID LIKE 'FTDIBUS\\\\%'\" | "
       "Select-Object Name,PNPDeviceID,PNPClass,Manufacturer,Status | "
       "ConvertTo-Json -Compress")

_ID = re.compile(r"VID_([0-9A-F]{4})[&+]PID_([0-9A-F]{4})(.*)", re.IGNORECASE)


def parse_pnp_id(pnp: str) -> tuple[int, int, str, bool] | None:
    """(vid, pid, serial, is_interface) from a PNPDeviceID, or None.

    USB\\VID_1313&PID_807B\\100000001      -> serial 100000001
    USB\\VID_046A&PID_0113&MI_01\\6&AD..   -> an INTERFACE of a composite device
    USB\\VID_413C&PID_301A\\5&AC91B4A&0&3  -> no serial: the last part is Windows'
                                            own instance id (it has '&')
    FTDIBUS\\VID_0403+PID_FAF0+97101234A\\0000 -> serial 97101234 (FTDI's driver
                                            appends a channel letter, A)
    """
    parts = str(pnp).split("\\")
    if len(parts) < 2:
        return None
    m = _ID.fullmatch(parts[1])
    if not m:
        return None
    vid, pid, rest = int(m.group(1), 16), int(m.group(2), 16), m.group(3)
    if parts[0].upper() == "FTDIBUS":
        # "97101234A": FTDI's driver appends the channel letter (A, B on a
        # dual chip). Kept here; merge_twins matches it to the USB\ entry
        # whose serial is the part before it.
        return vid, pid, rest.lstrip("+"), False
    interface = "&MI_" in rest.upper()
    last = parts[2] if len(parts) > 2 else ""
    serial = "" if (interface or "&" in last) else last
    return vid, pid, serial, interface


def parse_windows(text: str) -> list[UsbDevice]:
    """The PowerShell JSON (an object for one device, a list for several)."""
    text = (text or "").strip()
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    found: list[UsbDevice] = []
    interfaces: list[UsbDevice] = []
    for rec in data:
        if not isinstance(rec, dict):
            continue
        parsed = parse_pnp_id(rec.get("PNPDeviceID") or "")
        if parsed is None:
            continue
        vid, pid, serial, interface = parsed
        name = str(rec.get("Name") or "")
        if "hub" in name.lower() and str(rec.get("PNPClass") or "").upper() == "USB":
            continue                       # a hub is not an instrument
        dev = UsbDevice(vid, pid, name=name, usb_class=str(rec.get("PNPClass") or ""),
                        serial=serial, instance=str(rec.get("PNPDeviceID") or ""),
                        manufacturer=str(rec.get("Manufacturer") or ""))
        (interfaces if interface else found).append(dev)
    # A composite device shows once as itself and once per interface: keep
    # an interface only when its parent is not listed (one row per device).
    parents = {(d.vid, d.pid) for d in found}
    seen_if = set()
    for d in interfaces:
        if (d.vid, d.pid) not in parents and (d.vid, d.pid) not in seen_if:
            seen_if.add((d.vid, d.pid))
            found.append(d)
    return merge_twins(found)


def merge_twins(devices: list[UsbDevice]) -> list[UsbDevice]:
    """One row per physical device: an FTDI instrument is listed as
    USB\\VID_0403&PID_FAF0\\<serial> AND FTDIBUS\\VID_0403+PID_FAF0+<serial>A
    (a dual chip once more, ...B). The USB\\ entry is kept; an FTDIBUS entry
    without a USB\\ twin stays, with the channel letter taken off when the
    rest is the Kinesis-style all-digit serial."""
    def ftdi(d):
        return d.instance.upper().startswith("FTDIBUS")
    out: list[UsbDevice] = []
    by_key: dict = {}
    for d in sorted(devices, key=ftdi):                  # USB\ entries first
        if not ftdi(d):
            if d.serial:
                by_key[(d.vid, d.pid, d.serial.upper())] = d
            out.append(d)
            continue
        s = d.serial.upper()
        cands = [s, s[:-1]] if len(s) > 1 and s[-1] in "ABCD" else [s]
        if any((d.vid, d.pid, c) in by_key for c in cands):
            continue                                     # the USB\ twin is listed
        if len(cands) > 1 and d.serial[:-1].isdigit():
            d.serial = d.serial[:-1]
        key = (d.vid, d.pid, d.serial.upper())
        if key in by_key:                                # channel B of a dual chip
            continue
        by_key[key] = d
        out.append(d)
    return out


def _windows() -> list[UsbDevice]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS],
                       capture_output=True, timeout=TIMEOUT_S, creationflags=flags)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace").strip()[:200]
                           or f"PowerShell exit code {r.returncode}")
    return parse_windows(r.stdout.decode("utf-8", "replace"))


# ----------------------------------------------------------------- Linux ---

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def parse_sysfs(root: Path | str = "/sys/bus/usb/devices") -> list[UsbDevice]:
    """Every device folder under /sys/bus/usb/devices (interfaces, named like
    1-2:1.0, and root hubs, usbN, are skipped)."""
    root = Path(root)
    out = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if ":" in d.name or d.name.startswith("usb"):
            continue
        vid, pid = _read(d / "idVendor"), _read(d / "idProduct")
        try:
            v, p = int(vid, 16), int(pid, 16)
        except ValueError:
            continue
        cls = _read(d / "bDeviceClass")
        if cls == "09":                     # a hub
            continue
        out.append(UsbDevice(v, p, name=_read(d / "product"), usb_class=cls,
                             serial=_read(d / "serial"), instance=d.name,
                             manufacturer=_read(d / "manufacturer")))
    return out


# ------------------------------------------------------------ everything ---

def list_usb() -> tuple[list[UsbDevice], list[str]]:
    """(devices present now, notes). Never raises."""
    try:
        if sys.platform == "win32":
            return _windows(), []
        if os.path.isdir("/sys/bus/usb/devices"):
            return parse_sysfs(), []
        return [], [f"USB devices: no device list on this system ({sys.platform})"]
    except subprocess.TimeoutExpired:
        return [], [f"USB devices: Windows did not answer within {TIMEOUT_S:g} s"]
    except Exception as exc:
        return [], [f"USB devices could not be listed: {type(exc).__name__}: {exc}"]
