"""The real DS Instruments SG12000L, spoken in SCPI over USB (virtual COM port)
or Ethernet (raw TCP socket).

This is the ONLY file that talks to the instrument. `pyserial` is imported
LAZILY inside open() (and only for the serial transport), so the package still
imports and the simulator still runs on a PC without it. The TCP transport uses
only the standard library.

Sources used (all from dsinstruments.com, read 2026-09-27):
  [CL]  "SG6000L & SG12000L - SCPI Command List" v2.1, Sept 2022
  [AN]  "Ethernet Remote Operation Programming Guide" V1.2, 2022
  [UM]  "SG SERIES User Manual", June 2024
  [DS]  "SG6000 Series Signal Generators" datasheet V3.6, Dec 2022

What they say, and how sure we are
----------------------------------
  COM settings       115200 bps, 8 bits, 1 stop, no parity, no flow   [CL]
  terminator         linefeed "\\n" on commands                        [CL]
                     replies end in "\\r\\n" (the C# example crops it)  [AN]
  TCP port           10001, "fixed for all DSI models"                 [AN]
  FREQ:CW <f>        e.g. "FREQ:CW 400MHZ"; FREQ:CW? reply format NOT documented
  FREQ:MIN?/MAX?     "in Hz"                                           [CL]
  POWER <dBm>        e.g. "POWER -12.5"; POWER:MIN?/MAX? in dBm        [CL]
  OUTP:STAT ON|OFF   OUTP:STAT? reply format NOT documented
  PHASE <deg>        NOT in [CL] for the SG12000L; listed in [AN]'s combined
                     table ("PHASE 90", "PHASE -30") and the shop page advertises
                     0-360 deg phase control -> probed at connect
  *INTERNALREF 1|0|A internal / external / auto-detect at power-on    [CL]
  *REFMODE?          "Return the current reference setting" -- format unknown
  *EXTREF?           "Is an external reference signal detected?"      [CL]
  *REFUPDATE         "Re-initiate the reference frequency system"     [CL]
  *SYSVOLTS?         USB voltage                                       [CL]
  SYST:ERR?          pending error codes -- format unknown
  *BUZZER / *DISPLAY ON|OFF                                            [CL]

Every call whose REPLY FORMAT or exact spelling is not confirmed by those
documents is marked `# VERIFY`. The parsers below are deliberately tolerant
(they accept "ON"/"1", bare numbers or numbers with units) so a small format
surprise costs a log line, not a crash.

The brain clamps every value to the safety limits BEFORE it reaches this
backend, so here we simply forward commands and read back.
"""

from __future__ import annotations

import re
import time

_UNITS = {"HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9,
          "DBM": 1.0, "DEG": 1.0, "V": 1.0}
_NUM = re.compile(r"([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)\s*([A-Za-z]*)")


def parse_number(text: str, default_scale: float = 1.0) -> float:
    """'1000000000' / '1000.000MHZ' / '-7.5 dBm' / '5.07V' -> float.

    A unit suffix, if present, wins over `default_scale` (the scale assumed for
    a bare number)."""
    m = _NUM.search(text or "")
    if not m:
        raise ValueError(f"no number in reply {text!r}")
    value = float(m.group(1))
    unit = m.group(2).upper()
    if unit in _UNITS:
        return value * _UNITS[unit]
    return value * default_scale


def parse_bool(text: str) -> bool:
    """'ON' / '1' / 'YES' / 'TRUE' -> True, anything else False."""
    t = (text or "").strip().upper()
    return t in ("1", "ON", "YES", "TRUE") or t.startswith("ON") or t.startswith("YES")


def parse_reference(text: str) -> str:
    """Map a *REFMODE? reply onto internal / external / auto."""
    t = (text or "").strip().upper()
    if t.startswith("A") or "AUTO" in t:
        return "auto"
    if t in ("0",) or "EXT" in t:
        return "external"
    return "internal"


# ---- the two transports: one line out, one line back ----------------------

class _SerialLink:
    """USB virtual COM port. `serial` is imported here, lazily."""

    def __init__(self, port: str, baud: int, timeout_s: float):
        import serial                                   # pyserial, extra `real`
        self._s = serial.Serial(port=port, baudrate=baud, bytesize=8,
                                parity="N", stopbits=1, timeout=timeout_s,
                                write_timeout=timeout_s)

    def write(self, line: str) -> None:
        self._s.write((line + "\n").encode("ascii"))

    def query(self, line: str) -> str:
        # Throw away anything unread first: if the firmware ever answers a SET
        # command (not documented either way -- VERIFY), that stray line would
        # otherwise be taken as the answer to the NEXT query, and every reading
        # would be one step behind.
        self._s.reset_input_buffer()                    # VERIFY: do SETs reply?
        self.write(line)
        raw = self._s.readline()
        if not raw:
            raise TimeoutError(f"no reply to {line!r}")
        return raw.decode("ascii", "replace").strip()

    def close(self) -> None:
        self._s.close()


class _TcpLink:
    """Ethernet option: a plain TCP socket, one SCPI line per request."""

    def __init__(self, host: str, port: int, timeout_s: float):
        import socket                                   # standard library
        if not host:
            raise ValueError("hardware.host is empty: set the SG12000L's IP address")
        self._sock = socket.create_connection((host, port), timeout=timeout_s)
        self._timeout = float(timeout_s)
        self._sock.settimeout(self._timeout)
        self._buf = b""

    def write(self, line: str) -> None:
        self._sock.sendall((line + "\n").encode("ascii"))    # VERIFY: "\n" vs "\r\n" on TCP

    def _drain(self) -> None:
        """Discard bytes already waiting (see _SerialLink.query for why)."""
        self._buf = b""
        self._sock.setblocking(False)
        try:
            while self._sock.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass
        finally:
            self._sock.settimeout(self._timeout)    # back to blocking-with-timeout

    def query(self, line: str) -> str:
        self._drain()
        self.write(line)
        deadline = time.monotonic() + self._timeout
        while b"\n" not in self._buf:
            if time.monotonic() > deadline:
                raise TimeoutError(f"no reply to {line!r}")
            chunk = self._sock.recv(256)
            if not chunk:
                raise ConnectionError("SG12000L closed the TCP connection")
            self._buf += chunk
        reply, _, self._buf = self._buf.partition(b"\n")
        return reply.decode("ascii", "replace").strip()

    def close(self) -> None:
        self._sock.close()


class DsiSG12000L:
    """Drives a physical SG12000L. Implements the MicrowaveSource interface."""

    def __init__(self, transport: str = "serial", com_port: str = "COM5",
                 baud: int = 115200, host: str = "", tcp_port: int = 10001,
                 timeout_s: float = 1.0, phase_mode: str = "auto",
                 mute_buzzer: bool = True, display_off: bool = False):
        self._transport = transport
        self._com_port = com_port
        self._baud = int(baud)
        self._host = host
        self._tcp_port = int(tcp_port)
        self._timeout_s = float(timeout_s)
        self._phase_mode = phase_mode
        self._mute_buzzer = bool(mute_buzzer)
        self._display_off = bool(display_off)
        self._link = None
        self._has_phase = False
        self._idn = ""

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        if self._transport == "tcp":
            self._link = _TcpLink(self._host, self._tcp_port, self._timeout_s)
        elif self._transport == "serial":
            self._link = _SerialLink(self._com_port, self._baud, self._timeout_s)
        else:
            raise ValueError(f"hardware.transport must be 'serial' or 'tcp', "
                             f"not {self._transport!r}")
        # RF OFF first, before anything else can go wrong.
        self._link.write("OUTP:STAT OFF")
        self._link.write("*CLS")                               # clear old errors
        self._idn = self._link.query("*IDN?")
        if self._mute_buzzer:
            self._link.write("*BUZZER OFF")
        if self._display_off:
            self._link.write("*DISPLAY OFF")
        self._has_phase = self._probe_phase()

    def _probe_phase(self) -> bool:
        """Does this firmware understand PHASE? The 2022 SG12000L list [CL] has
        no phase command; the shop page advertises one. Ask, and check that the
        question did not just put an error in the queue."""
        if self._phase_mode == "off":
            return False
        if self._phase_mode == "on":
            return True
        try:
            reply = self._link.query("PHASE?")                  # VERIFY: spelling
            parse_number(reply)
        except Exception:
            self.errors()                                      # drain the complaint
            return False
        return not self.errors()                               # VERIFY: error on unknown cmd

    def close(self) -> None:
        link, self._link = self._link, None
        if link is None:
            return
        try:
            link.write("OUTP:STAT OFF")                        # RF off on the way out
            if self._display_off:
                link.write("*DISPLAY ON")                      # leave the front panel usable
        finally:
            link.close()

    def idn(self) -> str:
        return self._idn

    # ---- capabilities (read once by the brain at start) ---------------------

    def freq_range(self) -> tuple[float, float]:
        return (parse_number(self._link.query("FREQ:MIN?")),    # "in Hz" [CL]
                parse_number(self._link.query("FREQ:MAX?")))

    def power_range(self) -> tuple[float, float]:
        return (parse_number(self._link.query("POWER:MIN?")),   # dBm [CL]
                parse_number(self._link.query("POWER:MAX?")))

    def has_phase(self) -> bool:
        return self._has_phase

    # ---- RF output -------------------------------------------------------

    def set_output(self, on: bool) -> None:
        self._link.write(f"OUTP:STAT {'ON' if on else 'OFF'}")

    def read_output(self) -> bool:
        return parse_bool(self._link.query("OUTP:STAT?"))      # VERIFY: "ON"/"1"?

    # ---- frequency -------------------------------------------------------

    def set_frequency(self, hz: float) -> None:
        # MHZ with 6 decimals keeps 1 Hz resolution and uses the unit spelling
        # the command list shows ("FREQ:CW 400MHZ").
        self._link.write(f"FREQ:CW {hz / 1e6:.6f}MHZ")

    def read_frequency(self) -> float:
        # A bare number is assumed to be Hz, like FREQ:MIN?/MAX?
        return parse_number(self._link.query("FREQ:CW?"), default_scale=1.0)  # VERIFY: format

    # ---- power -------------------------------------------------------------

    def set_power(self, dBm: float) -> None:
        self._link.write(f"POWER {dBm:.2f}")

    def read_power(self) -> float:
        return parse_number(self._link.query("POWER?"))        # VERIFY: dBm, rounded to 0.5?

    # ---- phase -------------------------------------------------------------

    def set_phase(self, deg: float) -> None:
        if not self._has_phase:
            raise RuntimeError("this SG12000L firmware has no phase control")
        self._link.write(f"PHASE {deg:.2f}")                   # VERIFY: [AN] only

    def read_phase(self) -> float:
        if not self._has_phase:
            return 0.0
        return parse_number(self._link.query("PHASE?"))        # VERIFY

    # ---- reference ---------------------------------------------------------

    def set_reference(self, mode: str) -> None:
        code = {"internal": "1", "external": "0", "auto": "A"}[mode]
        self._link.write(f"*INTERNALREF {code}")
        # [UM]: detection normally happens only at boot; *REFUPDATE re-runs it
        # so the choice takes effect now.
        self._link.write("*REFUPDATE")                         # VERIFY: needed / harmless?

    def read_reference(self) -> str:
        return parse_reference(self._link.query("*REFMODE?"))  # VERIFY: reply format

    def external_ref_detected(self) -> bool:
        return parse_bool(self._link.query("*EXTREF?"))        # VERIFY: reply format

    # ---- health --------------------------------------------------------------

    def usb_volts(self) -> float:
        return parse_number(self._link.query("*SYSVOLTS?"))    # VERIFY: "5.07" or "5.07V"

    def errors(self) -> list[str]:
        """Drain SYST:ERR?. Treat "0", "+0,..." or "No error" as the end."""
        out = []
        for _ in range(10):                                    # never loop forever
            resp = self._link.query("SYST:ERR?")               # VERIFY: reply format
            code = resp.split(",", 1)[0].strip()
            if (not resp or code in ("0", "+0") or "NO ERR" in resp.upper()):
                break
            # An unknown "no error" wording would otherwise be read as a new
            # error ten times per drain, i.e. ten warnings every second. A
            # queue that repeats itself is not being drained: stop.
            if resp in out:
                break
            out.append(resp)
        return out
