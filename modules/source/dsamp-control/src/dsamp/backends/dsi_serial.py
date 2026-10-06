"""The real DS Instruments smart amplifier, over its USB virtual COM port.

This is the ONLY file that touches `pyserial`, and it imports it LAZILY (inside
open()), so the package imports and the simulator runs on a PC without it.
On the lab PC: `uv sync --extra gui --extra real`, then
`uv run scripts/run_service.py --real --port COMn`.

Sources used (all from dsinstruments.com, read 2026-09-27):
  [CL]  "DS Instruments - PA/GB Amplifier SCPI Command List (v3.1 Sept 2022)",
        file GB6000L-Command-List.pdf (covers PA2500L, GB6000L, PA6000L,
        GB20000, PA20000, GB30000).
  [UM]  "PA & GB SERIES Microwave Amplifier User Manual", version March 2025.
  [DS]  GB6000 datasheet Rev 3 v1.2 (the OLDER GB6000, whose command set was
        `AMP ON|OFF` and `VATT 0-1000`; kept here only as a warning, see below).

From [CL], the commands we use:
    GAIN <v>        set the gain, "0.5dB steps - 0 to 31"
    GAIN?           return the gain value
    OUTP:STAT ON|OFF / OUTP:STAT?   amplifier stage on/off / state
    *IDN?  *CLS (not used at start)  *TEMP? (C)  *SYSVOLTS? (USB volts)  SYST:ERR?
    *BUTTONS ON     re-enable the front-panel buttons after remote control
COM settings: 115200 bps, 8 data bits, 1 stop, no parity, no flow control,
command terminator = linefeed.

What [CL] does NOT say, and is therefore marked # VERIFY below:
  * the REPLY format of every query (a bare number? a number with a unit, like
    "20C" or "5.17V" as the vendor GUI displays? "ON"/"1"?). The parsers here
    accept a leading number with any trailing unit, and ON/1/TRUE for a state.
  * whether a set command sends back any line (an echo, "OK"). We never read
    after a write, so a stray reply would be picked up by the NEXT query: the
    query helper therefore flushes the input buffer first.
  * whether GAIN accepts a fractional value in 0.5 dB steps ("GAIN 10.5") or
    wants an index. The example in [CL] is "GAIN 10".
  * the real gain range of the unit: [CL] says 0..31, the GB6000L web page
    says 0..+28 dB typical. It lives in config.hardware, not here.
  * The unit on the bench is a GB6000L (confirmed by Lukas, 2026-09-27), so
    the GAIN/OUTP command list applies. (The OLDER GB6000 of [DS] spoke
    `AMP ON|OFF` + `VATT 0-1000` instead; not supported, not needed.)
"""

from __future__ import annotations

import re
import time

from ..hwlock import claim

# The name this module's claims carry: another service that finds the COM port
# busy is told "... already in use by dsamp (pid N)".
MODULE = "dsamp"

_NUMBER = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")


def parse_number(reply: str) -> float:
    """First number in a reply: '20C' -> 20.0, '5.17V' -> 5.17, '+12.5' -> 12.5."""
    m = _NUMBER.search(reply or "")
    if not m:
        raise ValueError(f"no number in reply {reply!r}")
    return float(m.group(0))


def parse_state(reply: str) -> bool:
    """'ON' / '1' / 'TRUE' (any case, surrounding text allowed) -> True."""
    r = (reply or "").strip().upper()
    if r in ("1", "ON", "TRUE") or r.startswith("ON"):
        return True
    if r in ("0", "OFF", "FALSE") or r.startswith("OFF"):
        return False
    raise ValueError(f"cannot read an on/off state from {reply!r}")


class DsiSerialAmp:
    """Drives a physical DS Instruments GB/PA amplifier. Implements AmpBackend."""

    def __init__(self, port: str = "COM5", baud: int = 115200,
                 timeout_s: float = 1.0, buttons_on_exit: bool = True):
        self._port = port
        self._baud = int(baud)
        self._timeout_s = float(timeout_s)
        self._buttons_on_exit = bool(buttons_on_exit)
        self._ser = None
        self._idn = ""
        self._lock = None          # our claim on the COM port while it is open

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        import serial                                    # lazy: only for real hardware
        # Claim the COM port FIRST (Lukas's rule: one physical instrument = one
        # physical address = one service). Windows already refuses a second
        # open of a COM port, but with a cryptic "Access is denied" and without
        # saying WHO holds it; the claim names the holder, and "com5", "COM5"
        # and "\\.\COM5" all count as the same amplifier. Raises HardwareBusy
        # before a single byte goes to the device.
        self._lock = claim(self._port, MODULE)
        try:
            self._ser = serial.Serial(self._port, self._baud, bytesize=8, parity="N",
                                      stopbits=1, timeout=self._timeout_s,
                                      write_timeout=self._timeout_s)
        except BaseException:
            # A failed open must not leave the port claimed, or the NEXT start
            # (after fixing the cable / the port number) would be refused.
            self._release()
            raise
        time.sleep(0.2)                                  # VERIFY: boot/enumeration settle
        self._ser.reset_input_buffer()                   # local buffer only, not the device
        # NOTHING is written to the amplifier here except the *IDN? query: the
        # brain reads OUTP:STAT? and GAIN? next and ADOPTS them (Lukas's rule,
        # 2026-09-27: read the state at start, change nothing). The *CLS and
        # OUTP:STAT OFF that used to be here are gone; *CLS was harmless but
        # served no purpose, since we do not read the error queue at start.
        # VERIFY: does the firmware lock the front-panel buttons as soon as it
        # gets ANY command, a query included (remote mode)? If so, that is the
        # only state a start-up changes; close() gives them back (*BUTTONS ON).
        try:
            self._idn = self._query("*IDN?")             # VERIFY: reply format
        except Exception:
            self._idn = ""

    def close(self, output_off: bool = True) -> None:
        if self._ser is None:
            # Never opened (or already closed): nothing is sent -- we do not
            # own the device -- but a claim left from a half-done open goes.
            self._release()
            return
        try:
            if output_off:                               # not on a restart (keep_outputs)
                self._write("OUTP:STAT OFF")             # stage off on the way out
            if self._buttons_on_exit:
                self._write("*BUTTONS ON")               # VERIFY [CL]: give the panel back
        finally:
            try:
                self._ser.close()
            finally:
                self._ser = None
                self._release()                          # port free for the next service

    def _release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    # ---- low-level -------------------------------------------------------

    def _write(self, cmd: str) -> None:
        # [CL]: the terminator is a linefeed. ASCII only on the wire.
        self._ser.write((cmd + "\n").encode("ascii"))
        self._ser.flush()

    def _query(self, cmd: str) -> str:
        # Throw away anything unread (a possible echo/ack of an earlier write,
        # see the VERIFY list above) so the reply we read belongs to THIS query.
        self._ser.reset_input_buffer()
        self._write(cmd)
        line = self._ser.readline()                      # VERIFY: one line per reply
        if not line:
            raise TimeoutError(f"no reply to {cmd!r} within {self._timeout_s} s")
        return line.decode("ascii", errors="replace").strip()

    def check_errors(self) -> list[str]:
        """Drain SYST:ERR? until it reports no error. VERIFY [CL]: reply format."""
        errors = []
        for _ in range(10):                              # guard against a runaway queue
            r = self._query("SYST:ERR?")
            if not r or r.lstrip("+").startswith("0") or "NO ERROR" in r.upper():
                break
            errors.append(r)
        return errors

    # ---- AmpBackend --------------------------------------------------------

    def set_output(self, on: bool) -> None:
        self._write(f"OUTP:STAT {'ON' if on else 'OFF'}")   # [CL]

    def read_output(self) -> bool:
        return parse_state(self._query("OUTP:STAT?"))       # VERIFY: reply ON/OFF or 1/0

    def set_gain(self, dB: float) -> None:
        # [CL]'s only example is "GAIN 10" (0.5 dB steps). `:g` sends exactly
        # that form for whole dB ("GAIN 10", not "GAIN 10.00") and "GAIN 10.5"
        # for a half step, so the documented case is byte-identical to the manual.
        # VERIFY [CL]: does the firmware accept the ".5" (or want an index)?
        self._write(f"GAIN {float(dB):g}")

    def read_gain(self) -> float:
        return parse_number(self._query("GAIN?"))            # VERIFY: reply format

    def read_temperature(self) -> float:
        return parse_number(self._query("*TEMP?"))           # VERIFY: "20C" or "20"?

    def read_supply(self) -> float:
        v = parse_number(self._query("*SYSVOLTS?"))          # VERIFY: volts or millivolts?
        # the vendor GUI shows "5.17V"; a reply in mV would read ~5000
        return v / 1000.0 if v > 100.0 else v

    def idn(self) -> str:
        return self._idn
