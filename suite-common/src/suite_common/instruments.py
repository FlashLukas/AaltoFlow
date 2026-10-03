"""Which instruments can this PC see, and at which address?

Mission Control's "Instruments on this PC" is built on this (no Qt here, so
the tests can feed it a fake pyvisa / pyserial). Three sources:

* VISA (pyvisa): GPIB, USB-TMC, LAN (VXI-11 / HiSLIP) and the serial ports
  VISA knows. A VISA instrument is ASKED who it is (``*IDN?``) -- that query is
  what VISA instruments are built to answer.
* Serial ports (pyserial): every COM port with what its USB chip says about
  itself (description, manufacturer, VID:PID, serial number). NOTHING is
  sent to a serial port unless a person asks for that port (``ask_serial``):
  a stray "*IDN?" at the wrong baud rate can upset a motor controller or a
  laser, and a serial port cannot tell us beforehand what is behind it.
* A typed address (``ask_address``): an instrument on the network that does
  not announce itself -- an IP, or a full VISA resource string.

And one rule above all: an address a running service HOLDS (hwlock, "one
physical address, one service") is never opened -- the row says who holds it.

pyvisa, pyvisa-py and pyserial are optional (mission-control's extra
"instruments"); without them the scan says what to install.

Two more sources for instruments that are NOT VISA or COM (Thorlabs Kinesis,
IDS cameras, NI DAQ, Signal Hound, Zurich, Thorlabs TLPMX), both LIST-ONLY --
nothing is opened, no byte is sent, no setting changes:

* the USB device list of the operating system (usb_devices.py, stdlib only),
  named through usb_devices.KNOWN_USB;
* MODULE PROBES: a module may declare ``[hardware] probe = "scripts/probe.py"``
  in its module.toml. The probe runs in the module's OWN environment (where its
  vendor library is installed -- Mission Control never installs a vendor SDK),
  asks that library which devices it can see, and prints one JSON line::

      {"devices": [{"address": "...", "identity": "...", "detail": "...",
                    "lock": "..."}], "note": ""}

  ``address`` is what the module's service takes (its [hardware] address_arg);
  ``lock`` (optional) is the address its backend CLAIMS in hwlock when that is
  spelled differently ("CAMERA::<serial>"), so a held device is shown as held.

A vendor list often HIDES a device that is open (pylablib's Kinesis list was
empty while the kim service held the KIM101, lab PC 2026-10-03). So a probe
also reports what its own module holds (hwlock), and the USB list -- which the
operating system keeps whether a device is open or not -- still shows it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import hwlock
from . import usb_devices

#: what a module's [hardware] bus accepts (modules.ADDRESS_BUSES). "device" =
#: an id the module understands verbatim (a Kinesis serial, "Dev1", "dev1234")
BUSES = ("visa", "serial", "ip", "device")
#: a module probe that has not answered after this long is given up
PROBE_TIMEOUT_S = 20.0
#: the *IDN? timeout: an absent GPIB address costs this much, so keep it short
IDN_TIMEOUT_MS = 1500


@dataclass
class Found:
    """One address this PC can see."""
    address: str                 # what a module wants: "GPIB0::6::INSTR", "COM5", ...
    bus: str                     # "gpib" | "usb" | "tcpip" | "serial" | "other"
    identity: str = ""           # the *IDN? answer, or a COM port's USB description
    detail: str = ""             # manufacturer, VID:PID, serial number, the VISA name ...
    held_by: str = ""            # the module whose service holds it ("" = free)
    error: str = ""              # why it could not be asked
    asked: bool = False          # True when bytes were sent to it (*IDN?)
    aliases: list = field(default_factory=list)   # other spellings of the same port
    #: where the row came from: "VISA", "COM port", "USB list", "kim probe" ...
    source: str = ""
    #: the module that drives this device, when known (KNOWN_USB or the probe)
    suggest: str = ""
    #: the address a backend CLAIMS (hwlock) when it differs from `address`
    lock: str = ""
    #: the modules whose probe listed it: "Use for module..." offers them its address
    found_by: list = field(default_factory=list)
    usb_id: str = ""              # "1313:807B" for a row from the USB list
    serial_no: str = ""           # the USB serial number, when the device has one
    #: False for a USB device KNOWN_USB does not recognise (hidden by default)
    known: bool = True

    @property
    def key(self) -> str:
        """One spelling per physical instrument: hwlock's, plus VISA's name
        for a serial port that is not a COMn ("ASRL/dev/ttyUSB0::INSTR" is
        /dev/ttyUSB0 on Linux)."""
        if self.lock:
            return hwlock.normalize(self.lock)
        m = re.fullmatch(r"ASRL(.*\D.*)::INSTR", self.address.strip(), flags=re.IGNORECASE)
        return hwlock.normalize(m.group(1) if m else self.address)


def bus_of(address: str) -> str:
    u = str(address).strip().upper()
    if u.startswith("GPIB"):
        return "gpib"
    if u.startswith("USB"):
        return "usb"
    if u.startswith("TCPIP") or re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}(:\d+)?", u):
        return "tcpip"
    if u.startswith("ASRL") or re.fullmatch(r"COM\d+", u) or u.startswith("/DEV/"):
        return "serial"
    return "other"


def held_by_address() -> dict[str, str]:
    """{normalized address: module} for every address a service holds now."""
    out = {}
    for info in hwlock.held():
        norm = info.get("normalized") or hwlock.normalize(info.get("address", ""))
        out[norm] = str(info.get("module") or "a service")
    return out


# ------------------------------------------------------------------- VISA ---

class MissingPackage(RuntimeError):
    """pyvisa / pyserial is not installed; the message says how to get it."""


def resource_manager():
    """A pyvisa ResourceManager: the installed VISA (NI / Keysight) first,
    pyvisa-py (pure Python) when there is none."""
    try:
        import pyvisa
    except ImportError:
        raise MissingPackage("pyvisa is not installed: in mission-control run "
                             "'uv sync --extra instruments'") from None
    try:
        return pyvisa.ResourceManager()
    except (OSError, ValueError):
        try:
            return pyvisa.ResourceManager("@py")
        except Exception as exc:
            raise MissingPackage(f"no VISA library found ({exc}); install NI-VISA, or "
                                 f"pyvisa-py ('uv sync --extra instruments')") from None


def _idn(rm, address: str, timeout_ms: int) -> tuple[str, str]:
    """(identity, error) of one VISA address."""
    inst = None
    try:
        inst = rm.open_resource(address, open_timeout=timeout_ms)
        inst.timeout = timeout_ms
        return str(inst.query("*IDN?")).strip(), ""
    except Exception as exc:
        return "", _short(exc)
    finally:
        if inst is not None:
            try:
                inst.close()
            except Exception:
                pass


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    first = text[0] if text else type(exc).__name__
    if "VI_ERROR_TMO" in first or "timeout" in first.lower():
        return "no answer to *IDN? (timeout)"
    return first[:160]


def scan_visa(rm=None, ask: bool = True, timeout_ms: int = IDN_TIMEOUT_MS,
              held: dict | None = None) -> list[Found]:
    """Every VISA resource, asked *IDN? unless it is serial or held."""
    rm = rm or resource_manager()
    held = held_by_address() if held is None else held
    try:
        import warnings
        with warnings.catch_warnings():
            # pyvisa-py warns about optional helpers (psutil, zeroconf) it
            # would use to search the LAN more widely; the extra installs them
            warnings.simplefilter("ignore")
            resources = list(rm.list_resources("?*"))
    except Exception as exc:
        # NI-VISA raises VI_ERROR_RSRC_NFOUND when it finds nothing at all
        if "RSRC_NFOUND" in str(exc):
            return []
        raise
    out = []
    for res in resources:
        # an INTERFACE (a GPIB board, the Prologix adapter pyvisa-py imagines
        # on every serial port) is not an instrument: not listed, never asked
        if not str(res).upper().endswith(("::INSTR", "::SOCKET")):
            continue
        f = Found(address=str(res), bus=bus_of(res), source="VISA")
        f.held_by = held.get(f.key, "")
        if f.held_by:
            f.detail = "held by a running service: not opened"
        elif f.bus not in ("gpib", "usb", "tcpip"):
            # serial (or unknown): no bytes unless a person asks for it
            f.detail = "not asked (use 'Ask this port')" if f.bus == "serial" else "not asked"
        elif ask:
            f.identity, f.error = _idn(rm, f.address, timeout_ms)
            f.asked = True
        out.append(f)
    return out


# ----------------------------------------------------------------- serial ---

def _comports():
    try:
        from serial.tools import list_ports
    except ImportError:
        raise MissingPackage("pyserial is not installed: in mission-control run "
                             "'uv sync --extra instruments'") from None
    return list_ports.comports()


def scan_serial(ports=None, held: dict | None = None) -> list[Found]:
    """Every serial port, described by its USB chip. Sends nothing."""
    ports = _comports() if ports is None else ports
    held = held_by_address() if held is None else held
    out = []
    for p in ports:
        bits = []
        if getattr(p, "manufacturer", None):
            bits.append(str(p.manufacturer))
        if getattr(p, "vid", None) is not None and getattr(p, "pid", None) is not None:
            bits.append(f"USB {p.vid:04X}:{p.pid:04X}")
        if getattr(p, "serial_number", None):
            bits.append(f"serial {p.serial_number}")
        desc = str(getattr(p, "description", "") or "")
        if desc in ("n/a", str(p.device)):
            desc = ""
        f = Found(address=str(p.device), bus="serial", identity=desc, detail=", ".join(bits),
                  source="COM port")
        if getattr(p, "serial_number", None):
            f.serial_no = str(p.serial_number)
        if getattr(p, "vid", None) is not None and getattr(p, "pid", None) is not None:
            f.usb_id = f"{p.vid:04X}:{p.pid:04X}"
        f.held_by = held.get(f.key, "")
        out.append(f)
    return out


def ask_serial(port: str, baud: int = 9600, query: str = "*IDN?",
               timeout_s: float = 1.0, held: dict | None = None) -> Found:
    """Send one query to ONE serial port, on a person's request, and read a line.

    Refused (no bytes sent) when a service holds the port."""
    held = held_by_address() if held is None else held
    f = Found(address=port, bus="serial")
    f.held_by = held.get(f.key, "")
    if f.held_by:
        f.error = f"held by {f.held_by}: not opened"
        return f
    try:
        import serial
    except ImportError:
        raise MissingPackage("pyserial is not installed: in mission-control run "
                             "'uv sync --extra instruments'") from None
    try:
        with serial.Serial(port, baudrate=int(baud), timeout=timeout_s,
                           write_timeout=timeout_s) as s:
            s.reset_input_buffer()
            s.write((query + "\r\n").encode("ascii"))
            f.asked = True
            line = s.readline().decode("ascii", "replace").strip()
        f.identity = line
        if not line:
            f.error = f"no answer to {query} at {baud} baud"
    except Exception as exc:
        f.error = _short(exc)
    return f


# --------------------------------------------------------- a typed address ---

def ask_address(text: str, rm=None, timeout_ms: int = IDN_TIMEOUT_MS,
                held: dict | None = None) -> Found:
    """Ask a typed address who it is: a VISA resource string as it is, or a
    bare IP / host name as VXI-11 (inst0) first, then a raw SCPI socket (5025).
    A COM port is refused here -- that is ask_serial, with a baud rate."""
    held = held_by_address() if held is None else held
    text = str(text).strip()
    if bus_of(text) == "serial":
        return Found(address=text, bus="serial", error="a serial port: use 'Ask this port'")
    if "::" in text:
        tries = [text]
    else:
        host, _, port = text.partition(":")
        tries = ([f"TCPIP0::{host}::{port}::SOCKET"] if port else
                 [f"TCPIP0::{host}::inst0::INSTR", f"TCPIP0::{host}::5025::SOCKET"])
    first = Found(address=tries[0], bus=bus_of(tries[0]))
    first.held_by = held.get(first.key, "")
    if first.held_by:
        first.error = f"held by {first.held_by}: not opened"
        return first
    rm = rm or resource_manager()
    last = first
    for addr in tries:
        f = Found(address=addr, bus=bus_of(addr), asked=True)
        if addr.upper().endswith("::SOCKET"):
            f.detail = "raw socket"
        f.identity, f.error = _idn(rm, addr, timeout_ms)
        if f.identity:
            return f
        last = f
    return last


# ------------------------------------------------------------- everything ---

def scan(ask_visa: bool = True, rm=None, ports=None,
         timeout_ms: int = IDN_TIMEOUT_MS) -> tuple[list[Found], list[str]]:
    """VISA + serial, merged; (found, notes). A note says what could not be
    looked at (a package missing, no VISA library) -- never an exception."""
    notes: list[str] = []
    held = held_by_address()
    serial_rows: list[Found] = []
    visa_rows: list[Found] = []
    try:
        serial_rows = scan_serial(ports, held=held)
    except MissingPackage as exc:
        notes.append(str(exc))
    try:
        visa_rows = scan_visa(rm, ask=ask_visa, timeout_ms=timeout_ms, held=held)
    except MissingPackage as exc:
        notes.append(str(exc))
    except Exception as exc:
        notes.append(f"VISA could not list its instruments: {_short(exc)}")
    # VISA lists the serial ports too (ASRL5::INSTR): one row per port, the
    # serial one (it knows the USB chip), with the VISA name kept
    by_key = {f.key: f for f in serial_rows}
    out = list(serial_rows)
    for f in visa_rows:
        twin = by_key.get(f.key)
        if twin is not None:
            twin.aliases.append(f.address)
            continue
        out.append(f)
    _sort(out)
    return out, notes


#: the table's order: VISA buses, COM ports, then the vendor-only devices
_ORDER = {"gpib": 0, "usb": 1, "tcpip": 2, "serial": 3, "device": 4, "usb-device": 5, "other": 6}


def _sort(rows: list) -> None:
    rows.sort(key=lambda f: (_ORDER.get(f.bus, 9), not f.known, _natural(f.address)))


def _natural(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


# --------------------------------------------- which module can take it? ---

def address_for(f: Found, bus: str, key: str = "") -> str | None:
    """`f` written the way a module of this [hardware] bus wants it, or None
    when it does not fit (a GPIB instrument for a COM-port module).

    `key` is the module's key. A vendor-only device (a probe's row, a USB-list
    row) is offered ONLY to the module whose probe found it or that KNOWN_USB
    names: a Kinesis serial means nothing to a lock-in module."""
    mine = bool(key) and (key in f.found_by or f.suggest == key)
    if f.bus in ("device", "usb-device"):
        if bus == "device":
            if key and key in f.found_by:
                return f.address               # the probe wrote it the module's way
            if mine and f.serial_no:
                return f.serial_no             # a USB-list row: its serial
            return None
        if bus == "visa" and mine:
            if key in f.found_by:
                return f.address
            if f.usb_id and f.serial_no:       # a USB-TMC style resource from the ids
                vid, pid = f.usb_id.split(":")
                return f"USB0::0x{vid}::0x{pid}::{f.serial_no}::INSTR"
        return None
    if bus == "device":
        # a VISA / COM row that the module's own probe also listed
        return f.address if key and key in f.found_by else None
    if bus == "visa":
        if f.bus != "serial":
            return f.address
        for a in f.aliases:                      # VISA's own name for the port
            return a
        m = re.fullmatch(r"COM(\d+)", f.address.upper())
        return f"ASRL{int(m.group(1))}::INSTR" if m else f"ASRL{f.address}::INSTR"
    if bus == "serial":
        if f.bus != "serial":
            return None
        norm = hwlock.normalize(f.address)
        if norm.startswith("COM") and sys.platform == "win32":
            return norm
        m = re.fullmatch(r"ASRL(.+)::INSTR", f.address, flags=re.IGNORECASE)
        return m.group(1) if m and not m.group(1).isdigit() else f.address
    if bus == "ip":
        if f.bus != "tcpip":
            return None
        m = re.fullmatch(r"TCPIP\d*::([^:]+)(?:::.*)?", f.address, flags=re.IGNORECASE)
        return m.group(1) if m else f.address.split(":")[0]
    return None


def modules_for(f: Found, modules) -> list[tuple]:
    """[(module, address in its form)] for every LOCAL module that can take `f`."""
    out = []
    for m in modules:
        if getattr(m, "remote", False) or not getattr(m, "address_arg", ""):
            continue
        a = address_for(f, m.address_bus, getattr(m, "key", ""))
        if a:
            out.append((m, a))
    return out


# --------------------------------------------------------- the USB list ---

#: why a device that is plugged in can be missing from its vendor's list
HELD_NOTE = "a vendor list does not show a device while a service has it open"


def usb_rows(devices, held: dict | None = None) -> list[Found]:
    """The operating system's USB devices as rows (bus "usb-device").
    Listed from the OS's records, never opened."""
    held = held_by_address() if held is None else held
    out = []
    for d in devices:
        k = usb_devices.known(d.vid, d.pid)
        maker, model, module = k if k else ("", "", "")
        who = " ".join(x for x in (maker, model) if x) or d.name or "USB device"
        if k is None:
            who = f"{d.name or 'USB device'} (not a known instrument)"
        bits = [b for b in ((d.name if k is not None and d.name and d.name not in who else ""),
                            f"USB {d.usb_id}", f"serial {d.serial}" if d.serial else "") if b]
        f = Found(address=d.address, bus="usb-device", identity=who, detail=", ".join(bits),
                  source="USB list", suggest=module, usb_id=d.usb_id, serial_no=d.serial,
                  known=k is not None)
        f.held_by = held.get(f.key, "")
        out.append(f)
    return out


# ----------------------------------------------------------- module probes ---

def module_python(project_dir) -> Path | None:
    """A module's own interpreter: <module>/.venv (what the launcher starts
    services with), or the venv dev.ps1 keeps in %LOCALAPPDATA%/uv-venvs/<folder>
    for a tree inside OneDrive (gotcha #8)."""
    d = Path(project_dir)
    ext = Path(os.environ.get("LOCALAPPDATA", "")) / "uv-venvs" / d.name
    for cand in (d / ".venv" / "Scripts" / "python.exe", d / ".venv" / "bin" / "python",
                 ext / "Scripts" / "python.exe", ext / "bin" / "python"):
        if cand.exists():
            return cand
    return None


def parse_probe(text: str, key: str) -> tuple[list[Found], list[str]]:
    """A probe's output -> (rows, notes). The LAST non-empty line is the JSON
    (a vendor library may print a banner before it)."""
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        return [], [f"{key} probe printed nothing"]
    try:
        data = json.loads(lines[-1])
        devices = data.get("devices", [])
        if not isinstance(devices, list):
            raise ValueError("'devices' is not a list")
    except (ValueError, AttributeError) as exc:
        return [], [f"{key} probe: not the expected JSON line ({exc})"]
    rows, notes = [], []
    for dev in devices:
        if not isinstance(dev, dict) or not str(dev.get("address", "")).strip():
            notes.append(f"{key} probe: a device without an address was skipped")
            continue
        address = str(dev["address"]).strip()
        bus = bus_of(address)
        # "other": the vendor library also listed a device that is NOT for
        # this module (Kinesis' FTDI scan sees the Signal Hound TG44A). Shown
        # as information; it never suggests, and is never offered to, the
        # module whose probe happened to see it.
        other = bool(dev.get("other"))
        rows.append(Found(address=address, bus="device" if bus == "other" else bus,
                          identity=str(dev.get("identity", "") or ""),
                          detail=str(dev.get("detail", "") or ""),
                          lock=str(dev.get("lock", "") or ""),
                          source=f"{key} probe", suggest="" if other else key,
                          found_by=[] if other else [key]))
    note = str(data.get("note", "") or "").strip()
    if note:
        notes.append(f"{key}: {note}")
    return rows, notes


def run_probe(spec, python=None, timeout_s: float = PROBE_TIMEOUT_S) -> tuple[list[Found], list[str]]:
    """Run one module's probe in its own environment. Never raises."""
    key = spec.key
    py = python or module_python(spec.dir)
    if py is None:
        return [], [f"{key}: no environment yet (run 'uv sync --all-extras' in its "
                    f"folder) -- its probe was not run"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run([str(py), str(spec.probe)], cwd=str(spec.dir), capture_output=True,
                           timeout=timeout_s, creationflags=flags,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return [], [f"{key} probe gave no answer within {timeout_s:g} s"]
    except OSError as exc:
        return [], [f"{key} probe could not start: {exc}"]
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        err = [ln for ln in r.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        return [], [f"{key} probe failed (exit {r.returncode})"
                    + (f": {err[-1][:200]}" if err else "")]
    return parse_probe(out, key)


def probe_modules(modules, held: dict | None = None, python_for=None,
                  timeout_s: float = PROBE_TIMEOUT_S, runner=None) -> tuple[list[Found], list[str]]:
    """Every LOCAL module's probe, all at once (one thread each: a vendor
    library can take seconds to enumerate, and they do not wait on each other).
    `runner(spec, python)` replaces run_probe in tests."""
    held = held_by_address() if held is None else held
    runner = runner or (lambda m, py: run_probe(m, python=py, timeout_s=timeout_s))
    todo = [m for m in modules if not getattr(m, "remote", False)
            and getattr(m, "probe", "") and getattr(m, "dir", None) is not None]
    results: dict = {}

    def one(m):
        try:
            py = python_for(m.dir) if python_for else None
            results[m.key] = runner(m, py)
        except Exception as exc:                       # never take the scan down
            results[m.key] = ([], [f"{m.key} probe: {exc}"])
    threads = [threading.Thread(target=one, args=(m,), daemon=True, name=f"probe-{m.key}")
               for m in todo]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout_s + 5)
    rows, notes = [], []
    for m in todo:
        r, n = results.get(m.key, ([], [f"{m.key} probe did not finish"]))
        rows += r
        notes += n
    for f in rows:
        f.held_by = f.held_by or held.get(f.key, "")
    return rows, notes


# --------------------------------------------------------------- merging ---

def _twin(f: Found, rows: list) -> Found | None:
    """The row of `rows` that describes the same device as `f`: the same
    hwlock key, or the same USB serial number (in its serial field, or in the
    address / lock / details of a probe, VISA or COM row)."""
    for r in rows:
        if r.key == f.key:
            return r
    mine = _serial_variants(f.serial_no)
    if mine:
        for r in rows:
            if r.serial_no and (_serial_variants(r.serial_no) & mine) and (
                    not r.usb_id or not f.usb_id or r.usb_id == f.usb_id):
                return r
            text = " ".join((r.address, r.lock, r.detail)).upper()
            for s in mine:
                # the serial as a whole word, optionally followed by FTDI's
                # channel letter (pyserial's "DS000001A" for USB's "DS000001")
                if re.search(r"(?<![0-9A-Z])" + re.escape(s) + r"[A-D]?(?![0-9A-Z])", text):
                    return r
    return None


def _serial_variants(serial: str) -> set:
    """A USB serial and its spelling without FTDI's channel letter.

    FTDI's driver appends the channel (A, B on a dual chip) to the serial it
    reports through FTDIBUS -- and pyserial passes that on -- while Windows'
    USB\\ entry has the bare serial. The lab PC's DS Instruments generator
    showed as two rows (COM3 with "...A", the USB list without) until both
    spellings were compared. Short serials (< 4 characters) match nothing."""
    s = str(serial or "").strip().upper()
    if len(s) < 4:
        return set()
    out = {s}
    if s[-1] in "ABCD" and len(s) > 4:
        out.add(s[:-1])
    return out


def merge(rows: list, extra: list) -> list:
    """`rows` plus `extra`, one row per physical device. A probe's row for an
    instrument VISA also lists, or a USB-list row for a device a probe (or a
    COM port) already shows, is folded into that row -- which then also says
    who found it and which module drives it."""
    out = list(rows)
    for f in extra:
        twin = _twin(f, out)
        if twin is None:
            out.append(f)
            continue
        if f.bus == "serial" and twin.bus == "usb-device":
            # a USB-list row that is also a COM port: the port is what a
            # serial module (dssg, superk ...) takes, the USB serial stays in
            # serial_no. Without this the result depended on which list was
            # merged first (the lab's SG12000L showed its FTDI serial).
            twin.address, twin.bus = f.address, f.bus
            twin.aliases = twin.aliases or list(f.aliases)
        if f.source and f.source not in twin.source:
            twin.source = f"{twin.source} + {f.source}" if twin.source else f.source
        for k in f.found_by:
            if k not in twin.found_by:
                twin.found_by.append(k)
        twin.suggest = twin.suggest or f.suggest
        twin.usb_id = twin.usb_id or f.usb_id
        twin.serial_no = twin.serial_no or f.serial_no
        twin.held_by = twin.held_by or f.held_by
        twin.known = twin.known or f.known
        if f.identity and (not twin.identity or twin.identity.endswith("(not a known instrument)")):
            twin.identity = f.identity
        if f.detail and f.detail not in twin.detail:
            twin.detail = f"{twin.detail}; {f.detail}" if twin.detail else f.detail
    for r in out:
        # a running service HOLDS it: the best evidence of what the device is
        # (it also names an ambiguous FTDI cable correctly). hwlock's module
        # name is the module key; "a service" (no name in the lock) says nothing.
        if r.held_by and not r.suggest and r.held_by != "a service":
            r.suggest = r.held_by
            r.known = True
    _sort(out)
    return out


def _explain_held(rows: list) -> None:
    """A device a running service holds may be missing from its vendor's list
    (FTDI does not enumerate an open device): say so on its row."""
    for f in rows:
        if f.held_by and f.bus in ("device", "usb-device") and HELD_NOTE not in f.detail:
            f.detail = (f.detail + "; " if f.detail else "") + \
                f"held by the running {f.held_by} service ({HELD_NOTE})"


def scan_vendor(modules=(), usb: bool = True, probes: bool = True, python_for=None,
                usb_list=None, runner=None) -> tuple[list[Found], list[str]]:
    """The two list-only sources: module probes, then the OS's USB list,
    merged (a device both list is one row). (rows, notes); never raises.
    `usb_list` / `runner` replace the real ones in tests."""
    held = held_by_address()
    notes: list[str] = []
    rows: list[Found] = []
    if probes:
        try:
            r, n = probe_modules(modules, held=held, python_for=python_for, runner=runner)
        except Exception as exc:
            r, n = [], [f"module probes: {exc}"]
        rows = merge(rows, r)
        notes += n
    if usb:
        devices, n = (usb_list or usb_devices.list_usb)()
        notes += n
        rows = merge(rows, usb_rows(devices, held=held))
    _explain_held(rows)
    return rows, notes
