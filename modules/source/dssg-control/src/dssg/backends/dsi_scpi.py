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
  VERNIER <n>        "Fine tune the output power (no units)", e.g.
                     "VERNIER 3", "VERNIER -22"                        [CL]
  VERNIER?           "Return vernier setting"                          [CL]
                     range, sign and dB per count NOT documented -> probed at
                     connect, published as raw integer counts. MEASURED
                     2026-10-07 (fw V7.84): range -800..+100, clamps SILENTLY
                     (no SYST:ERR), + = more power, ~0.045 dB/count near 0
                     at 1-4 GHz (frequency- and power-dependent), kept over
                     POWER and FREQ changes, POWER? excludes it
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

from .. import hwlock

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
                 timeout_s: float = 1.0, phase_mode: str = "auto"):
        self._transport = transport
        self._com_port = com_port
        self._baud = int(baud)
        self._host = host
        self._tcp_port = int(tcp_port)
        self._timeout_s = float(timeout_s)
        self._phase_mode = phase_mode
        # True only if THIS session switched the display off (set_display), so
        # close() switches it back on -- and leaves it alone otherwise.
        self._display_turned_off = False
        self._link = None
        self._has_phase = False
        self._has_vernier = False
        self._idn = ""
        # The claim on this unit's physical address (hwlock.py): held from
        # open() to close() so no second service -- another dssg, or any
        # module pointed at the same COM port / IP -- can talk to it meanwhile.
        self._lock = None

    # ---- lifecycle -------------------------------------------------------

    def address(self) -> str:
        """The PHYSICAL address of the unit this backend drives: the COM port
        (USB) or the host (Ethernet). hwlock keys a network box by its host
        only, so the TCP port does not matter."""
        if self._transport == "tcp":
            if not self._host:
                raise ValueError("hardware.host is empty: set the SG12000L's IP address")
            return self._host
        if self._transport == "serial":
            return self._com_port
        raise ValueError(f"hardware.transport must be 'serial' or 'tcp', "
                         f"not {self._transport!r}")

    def open(self) -> None:
        # Claim the address BEFORE the first byte goes out: if another service
        # holds this unit, HardwareBusy is raised here and we never touch it.
        address = self.address()
        self._lock = hwlock.claim(address, "dssg")
        try:
            self._open_link()
        except BaseException:
            # A failed open must not leave the unit "claimed" by a process
            # that is about to report the error (or exit): drop the half-open
            # link WITHOUT sending anything, then release the claim.
            link, self._link = self._link, None
            if link is not None:
                try:
                    link.close()
                except Exception:
                    pass
            self._release()
            raise

    def reopen(self) -> None:
        """The link died (USB unplugged, the adapter's driver replaced): drop
        it WITHOUT sending anything and open it again. The hwlock claim on the
        address stays ours meanwhile. Opening only clears the error queue and
        reads *IDN? / PHASE? / VERNIER? (adopt rule), so nothing on the unit changes.
        Raises while the port is still missing; the brain tries again later."""
        link, self._link = self._link, None
        if link is not None:
            try:
                link.close()
            except Exception:
                pass
        self._open_link()

    def _release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    def _open_link(self) -> None:
        if self._transport == "tcp":
            self._link = _TcpLink(self._host, self._tcp_port, self._timeout_s)
        else:
            self._link = _SerialLink(self._com_port, self._baud, self._timeout_s)
        # ADOPT, don't initialise (Lukas's rule, 2026-09-27): the unit keeps
        # whatever RF state, frequency, power, reference, buzzer and display
        # it had -- the brain READS them afterwards. The only write is *CLS,
        # which empties the error queue and nothing else; it is needed so the
        # PHASE? probe below is judged on ITS error, not on an old one.
        # (Until 2026-09-27 open() sent OUTP:STAT OFF and *BUZZER OFF here.)
        self._link.write("*CLS")                               # clear old errors only
        self._idn = self._link.query("*IDN?")
        self._has_phase = self._probe_phase()                  # queries only
        self._has_vernier = self._probe_vernier()              # queries only

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

    def _probe_vernier(self) -> bool:
        """Does this firmware answer VERNIER? with a number? [CL] lists it for
        the SG12000L, but an older or newer firmware might not -- so ask, the
        same way as for PHASE?, and offer the control only if it answers
        cleanly. A query changes nothing on the unit (adopt-on-start rule)."""
        try:
            reply = self._link.query("VERNIER?")               # [CL]
            parse_number(reply)                                # VERIFY: reply format
        except Exception:
            self.errors()                                      # drain the complaint
            return False
        return not self.errors()                               # VERIFY: error on unknown cmd

    def close(self, rf_off: bool = True) -> None:
        link, self._link = self._link, None
        if link is None:
            self._release()                                    # nothing open; drop any claim
            return
        try:
            if rf_off:
                link.write("OUTP:STAT OFF")                    # RF off on the way out
            if self._display_turned_off:
                link.write("*DISPLAY ON")                      # leave the front panel usable
                self._display_turned_off = False
        finally:
            try:
                link.close()
            finally:
                self._release()                                # the unit is free for others

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

    # ---- vernier (fine power trim, raw counts) -------------------------------
    # The step attenuator only makes 0.5 dB steps; the VERNIER trims the level
    # in between. [CL] gives the command and two examples (3, -22) but NO
    # range, NO sign convention and NO dB per count, so we pass raw integer
    # counts and let the brain clamp them to cfg limits (a guess, VERIFY).
    # Also unknown (VERIFY): whether a POWER command resets the vernier to 0,
    # whether POWER? includes the vernier's offset, and whether *SAVESTATE
    # stores it across a power cycle.

    def has_vernier(self) -> bool:
        return self._has_vernier

    def set_vernier(self, n: int) -> None:
        if not self._has_vernier:
            raise RuntimeError("this SG12000L firmware has no vernier control")
        # measured: -800..+100; the unit clamps outside that WITHOUT an
        # error, so the brain's limits (config) must keep it inside
        self._link.write(f"VERNIER {int(n)}")                  # [CL]

    def read_vernier(self) -> int:
        if not self._has_vernier:
            return 0
        # round(), not int(): a reply like "3.0" must read as 3, not fail
        return int(round(parse_number(self._link.query("VERNIER?"))))  # VERIFY: format

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

    # ---- front-panel preferences (only on an explicit user change) ---------

    def set_buzzer(self, on: bool) -> None:
        self._link.write(f"*BUZZER {'ON' if on else 'OFF'}")   # [CL]

    def set_display(self, on: bool) -> None:
        self._link.write(f"*DISPLAY {'ON' if on else 'OFF'}")  # [CL]
        self._display_turned_off = not on

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
