"""The real ELC ALR3203 over its USB (virtual COM port) link, via pyserial.

This is the ONLY file that touches `serial` (pyserial), and it imports it
LAZILY (inside open(), not at module top) -- so the package still imports and
the simulator still runs on a PC without it. On the lab PC:
    uv sync --extra gui --extra real       (gotcha #29: name every extra)
plus ELC's USB driver (ALR32xx.inf, www.elc.fr), after which the supply shows
up as a COM port in the Device Manager.

Sources of every command below:
  * the ALR3203 manual (N_ALR3203_EN, 2021), APPENDIX A "Operating codes";
  * ELC's own Python library ALR32XX (github.com/elc-construction-electronique/
    Librairie-Python-ALR32XX, MIT): the serial settings 9600 baud, 7 data bits,
    even parity, 1 stop bit, and the reply handling.
We do NOT use that library: its auto-connect opens EVERY COM port on the PC
and writes an IDN request to each (other instruments included), and it keeps
one global serial object. Here the port is the configured one, claimed first.
RUN ON THE INSTRUMENT 2026-11-10 (Blue Box, firmware "VERSION 1.2"): 9600 7E1,
answers end in CR, 3-20 ms each, nothing extra. Read exactly as sent / received:
    0 IDN RD    -> 0 OK ALR3203 VERSION 1.2 \r    (a blank before the CR)
    0 MODE RD   -> 0 OK 0          (output off)
    0 OUT RD    -> 0 OK 0
    0 VOLT RD   -> 0 OK 10000      (mV: 10 V stored)
    0 CURR RD   -> 0 OK 0
    0 VOLT MES  -> 0 OK 0          (output off: 0)
    0 OVP RD    -> 0 OK 32200 ;  0 OCP RD -> 0 OK 6100
    0 SERIAL RD -> 0 ERR           (not supported by this firmware)
Then the service drove a coil in both modes and through the relay (see
docs/VERIFIED_INSTRUMENTS.md). Calls still marked # VERIFY are the ones that
did not show up in that test.

THE PROTOCOL (manual, Appendix A):
    command  "0 <PARAM> <CMD> [value]\\r"     0 = the USB address
             PARAM = VOLT CURR OVP OCP OUT REM MODE IDN SERIAL RCL STO
             CMD   = WR (write) | RD (read the setpoint) | MES (measure)
    answer   "0 OK [value]\\r" | "0 ERR\\r" (syntax) | "0 LOCAL\\r" (front panel
             has control: command refused)
    units    VOLT in mV (0..32200), CURR in mA (0..6100), integers.
    examples "0 VOLT WR 1250" = 1.25 V;  "0 CURR MES" -> "0 OK 450" = 450 mA.
    MODE RD  0 = output off, 1 = voltage regulation (CV), 2 = current (CC).

START-UP READS, IT DOES NOT WRITE (Lukas, 2026-09-27): open() asks IDN only;
read_state() then asks OUT, VOLT, CURR and MODE. Writing needs the supply in
REMOTE mode ("LOCAL" otherwise): REM WR 1 is sent the first time a write is
refused with LOCAL (it locks the front panel, it does not change the output),
and REM WR 0 on close gives the front panel back.
"""

from __future__ import annotations

from ..hwlock import claim

# The name this module signs its hardware claims with. Another service that
# finds the port taken reads it in its error message ("... in use by alr3203").
MODULE = "alr3203"

_MAX_V_MV = 32200        # manual Appendix A: VOLT WR [0-32200]
_MAX_I_MA = 6100         # manual Appendix A: CURR WR [0-6100]


class ElcError(RuntimeError):
    """The supply answered ERR (syntax) or LOCAL, or did not answer."""


class SerialALR3203:
    """Drives a physical ELC ALR3203. Implements SupplyBackend."""

    def __init__(self, port: str = "", baud: int = 9600, timeout_s: float = 1.0):
        self._port = str(port).strip()
        self._baud = int(baud)
        self._timeout_s = float(timeout_s)
        self._ser = None
        self._idn = ""
        self._remote = False     # REM WR 1 sent in this session
        self._lock = None        # our claim on the COM port (see open())

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        if not self._port:
            raise RuntimeError("no COM port configured for the ALR3203: set "
                               "[hardware] com (e.g. COM5) or start the service "
                               "with --com COM5 (Device Manager > Ports shows it)")
        # ONE INSTRUMENT, ONE SERVICE: claim the port BEFORE a byte is sent. A
        # second alr3203 service on the same port gets HardwareBusy naming the
        # holder, and never touches the supply.
        self._lock = claim(self._port, MODULE)
        try:
            self._open_claimed()
        except BaseException:
            # a failed open must not leave the port claimed; writes nothing
            self._disconnect()
            raise

    def _open_claimed(self) -> None:
        try:
            import serial                                   # lazy: real hardware only
        except ImportError as exc:                          # pragma: no cover
            raise RuntimeError("pyserial is not installed: run "
                               "`uv sync --extra gui --extra real`") from exc
        # VERIFY: 9600 7E1 (ELC's ALR32XX library uses exactly this)
        self._ser = serial.Serial(self._port, baudrate=self._baud,
                                  bytesize=serial.SEVENBITS,
                                  parity=serial.PARITY_EVEN,
                                  stopbits=serial.STOPBITS_ONE,
                                  timeout=self._timeout_s,
                                  write_timeout=self._timeout_s)
        try:
            self._ser.reset_input_buffer()                 # drop a stale half answer
        except Exception:
            pass
        # checked 2026-11-10: "0 OK ALR3203 VERSION 1.2 " (trailing blank)
        self._idn = self._query("IDN RD") or "ELC ALR3203"
        if "ALR" not in self._idn.upper():
            raise RuntimeError(f"{self._port} answered {self._idn!r}, not an ELC ALR "
                               f"supply -- wrong COM port?")

    def read_state(self) -> dict:
        """What the supply is set to right now, from READS only."""
        output = self._query("OUT RD").strip() == "1"                 # checked: "0"/"1"
        volts = self._num(self._query("VOLT RD")) / 1000.0           # checked: mV
        amps = self._num(self._query("CURR RD")) / 1000.0            # checked: mA
        return {"output": output, "voltage_V": volts, "current_A": amps,
                "regulation": self.read_regulation()}

    def close(self, output_off: bool = True) -> None:
        try:
            if self._ser is not None:
                if output_off:
                    self._write("OUT WR 0")                           # VERIFY
                if self._remote:
                    # give the front panel back (ELC's library does the same)
                    try:
                        self._write("REM WR 0")                       # VERIFY
                    except Exception:
                        pass
        finally:
            self._disconnect()

    def disconnect(self) -> None:
        """Let go WITHOUT writing anything."""
        self._disconnect()

    def _disconnect(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self._remote = False
        # Release LAST: only once our port is closed may another service open it.
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    # ---- programming -----------------------------------------------------

    def program_voltage(self, volts: float) -> None:
        mv = int(round(max(0.0, float(volts)) * 1000.0))
        self._write(f"VOLT WR {min(mv, _MAX_V_MV)}")                  # VERIFY

    def program_current(self, amps: float) -> None:
        ma = int(round(max(0.0, float(amps)) * 1000.0))
        self._write(f"CURR WR {min(ma, _MAX_I_MA)}")                  # VERIFY

    def set_output(self, on: bool) -> None:
        self._write(f"OUT WR {1 if on else 0}")                       # VERIFY

    # ---- measurement -----------------------------------------------------

    def measure_voltage(self) -> float:
        return self._num(self._query("VOLT MES")) / 1000.0            # VERIFY mV

    def measure_current(self) -> float:
        return self._num(self._query("CURR MES")) / 1000.0            # VERIFY mA

    def read_regulation(self) -> str:
        code = self._query("MODE RD").strip()                         # checked: 0 = off
        return {"1": "voltage", "2": "current"}.get(code, "off")

    def idn(self) -> str:
        return self._idn

    def serial_number(self) -> str:
        """The supply's serial number, or "" -- the rig's firmware (VERSION
        1.2) answers SERIAL RD with "0 ERR" (checked 2026-11-10), and a
        missing serial must not stop anything that only wants to show it."""
        try:
            return self._query("SERIAL RD").strip()
        except ElcError:
            return ""

    # ---- the wire ------------------------------------------------------------

    def _transact(self, body: str) -> tuple[str, str]:
        """Send "0 <body>\\r", return (status, value) of the answer."""
        if self._ser is None:
            raise ElcError("ALR3203 not open")
        self._ser.write(f"0 {body}\r".encode("ascii"))
        raw = self._ser.read_until(b"\r")
        text = raw.decode("ascii", errors="replace").strip()
        if not text:
            raise ElcError(f"no answer to {body!r} within {self._timeout_s:g} s")
        parts = text.split()
        # VERIFY: the answer starts with the address "0"; tolerate its absence
        if parts and parts[0] == "0":
            parts = parts[1:]
        status = parts[0].upper() if parts else ""
        value = " ".join(parts[1:])
        return status, value

    def _write(self, body: str) -> None:
        status, _ = self._transact(body)
        if status == "LOCAL" and not self._remote:
            # VERIFY: the supply refuses remote writes until REM WR 1. Taking
            # remote locks the front panel; it does not change the output.
            s2, _ = self._transact("REM WR 1")
            if s2 != "OK":
                raise ElcError(f"could not take remote control (REM WR 1 -> {s2}); "
                               f"enable PC control on the front panel (2nd > USB)")
            self._remote = True
            status, _ = self._transact(body)
        if status != "OK":
            raise ElcError(f"{body!r} refused: {status or 'empty answer'}")

    def _query(self, body: str) -> str:
        status, value = self._transact(body)
        if status != "OK":
            raise ElcError(f"{body!r} refused: {status or 'empty answer'}")
        return value

    @staticmethod
    def _num(value: str) -> float:
        """The number in an answer. ELC's library slices fixed columns; the
        last token is safer if the format differs a little."""
        tokens = value.split()
        if not tokens:
            raise ElcError("answer without a value")
        return float(tokens[-1])
