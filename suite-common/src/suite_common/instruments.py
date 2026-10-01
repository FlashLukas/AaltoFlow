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
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

from . import hwlock

#: what a module's [hardware] bus accepts (modules.ADDRESS_BUSES)
BUSES = ("visa", "serial", "ip")
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

    @property
    def key(self) -> str:
        """One spelling per physical instrument: hwlock's, plus VISA's name
        for a serial port that is not a COMn ("ASRL/dev/ttyUSB0::INSTR" is
        /dev/ttyUSB0 on Linux)."""
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
        f = Found(address=str(res), bus=bus_of(res))
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
        f = Found(address=str(p.device), bus="serial", identity=desc, detail=", ".join(bits))
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
    order = {"gpib": 0, "usb": 1, "tcpip": 2, "serial": 3, "other": 4}
    out.sort(key=lambda f: (order.get(f.bus, 9), _natural(f.address)))
    return out, notes


def _natural(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


# --------------------------------------------- which module can take it? ---

def address_for(f: Found, bus: str) -> str | None:
    """`f` written the way a module of this [hardware] bus wants it, or None
    when it does not fit (a GPIB instrument for a COM-port module)."""
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
        a = address_for(f, m.address_bus)
        if a:
            out.append((m, a))
    return out
