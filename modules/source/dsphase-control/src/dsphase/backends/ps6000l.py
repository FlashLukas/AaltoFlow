"""The real DS Instruments PS6000L phase shifter over its USB virtual COM port.

THE ONLY FILE THAT TOUCHES THE HARDWARE LIBRARY (pyserial), and it imports it
lazily inside open(), so the package imports on a PC without pyserial.
Install it with `uv sync --extra gui --extra real` (gotcha #29).

Sources used (both from dsinstruments.com):
  * "DS Instruments - Phase Shifter SCPI Command List (V3)", version 1.5
    (PS6000L-R3-Command-List.pdf): COM BAUD 115200, command terminator LINEFEED,
    commands PHASE / PHASE?, ATT / ATT?, OUTP:STAT ON|OFF / OUTP:STAT?, *IDN?,
    *PING? (-> "PONG!"), SYST:ERR?, SYST:DBG?, *RST, *DISPLAY, *BUZZER,
    *SAVESTATE, *UNITNAME.
  * PS6000L R3 datasheet V3.1: 400-6000 MHz, -180..+180 deg, 0.5 deg step,
    output attenuator 30 dB range in 0.25 dB steps, response time < 500 us.

What the documents do NOT say, and is therefore marked # VERIFY below:
  * the exact REPLY format of every query (a bare number? with a unit? "ON" or
    "1"?). The parsers are deliberately tolerant: the first number in the line,
    and ON/1/TRUE for the output state.
  * whether a SET command sends back any reply (we drain stray input before
    every query so an unexpected echo cannot be mistaken for the answer);
  * whether PHASE accepts +180 exactly and fractional values like 44.5;
  * whether the unit has ANY carrier-frequency command. The V3 list has none,
    yet *SAVESTATE is described as "save frequency & attenuation", which hints
    at one. Device.freq_command stays empty until Lukas confirms.
"""

from __future__ import annotations

import re
import threading
import time

from .. import hwlock

_NUMBER = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")


def parse_number(reply: str) -> float:
    """First number in a reply line: '44.5', '44.5 DEG', 'PHASE 44.5' -> 44.5."""
    m = _NUMBER.search(reply or "")
    if not m:
        raise ValueError(f"no number in reply {reply!r}")
    return float(m.group(0))


def _num(x: float) -> str:
    """A number with the decimals it needs: 90.0 -> '90', 44.5 -> '44.5',
    13.25 -> '13.25', 5.625 -> '5.625'."""
    text = f"{float(x):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def parse_on(reply: str) -> bool:
    """'ON' / '1' / 'TRUE' -> True; anything else -> False."""
    return (reply or "").strip().upper() in ("ON", "1", "TRUE")


class PS6000L:
    """Drives a PS6000L on a COM port with DS Instruments' SCPI-like commands."""

    def __init__(self, port: str, baud: int = 115200, timeout_s: float = 1.0,
                 freq_command: str = ""):
        self.port = port
        self.baud = int(baud)
        self.timeout_s = float(timeout_s)
        self.freq_command = freq_command or ""
        self._ser = None
        self._idn = ""
        self._hw_lock = None                    # hwlock.HardwareLock while open
        # One serial line, many callers (the brain's setter and its poll
        # thread): a query's write and its read must not be interleaved with
        # another command, or answers get swapped.
        self._lock = threading.RLock()

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        try:
            import serial                       # pyserial, the `real` extra
        except ImportError as exc:
            raise RuntimeError(
                "pyserial is not installed: run `uv sync --extra gui --extra real`"
            ) from exc
        # ONE INSTRUMENT, ONE SERVICE (Lukas's rule): claim the COM port before
        # a single byte goes to the unit. If another service (a second dsphase,
        # or any module pointed at the same port) already holds it, this raises
        # HardwareBusy naming the holder and we never touch the box. "com5",
        # "COM5" and "ASRL5::INSTR" are the same port to the lock.
        self._hw_lock = hwlock.claim(self.port, "dsphase")
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout_s,
                                      write_timeout=self.timeout_s)
            time.sleep(0.2)                     # let the CDC port settle after open  # VERIFY
            pong = self._query("*PING?")        # VERIFY: reply is exactly "PONG!"
            if "PONG" not in pong.upper():
                raise RuntimeError(f"{self.port}: no PONG from the phase shifter (got {pong!r})")
            self._idn = self._query("*IDN?")    # VERIFY: reply format
        except BaseException:
            # A failed open must leave nothing behind: close the port WITHOUT
            # sending OUTP:STAT OFF (we never established that this is our
            # unit, so no "safe state" command goes to it) and give the
            # address back, so a retry -- or another service -- can have it.
            self._abandon()
            raise
        # NOTHING is written here (Lukas's rule, 2026-09-27): no *RST, no
        # OUTP:STAT OFF, no PHASE/ATT. The brain reads the unit back right after
        # open() and adopts what it holds, so a service restart leaves the RF
        # path exactly as it was. (close() still switches the output off.)

    def _abandon(self) -> None:
        """Drop the port and the address claim without talking to the unit."""
        ser, self._ser = self._ser, None
        try:
            if ser is not None:
                ser.close()
        except Exception:
            pass
        finally:
            self._release_lock()

    def _release_lock(self) -> None:
        lock, self._hw_lock = self._hw_lock, None
        if lock is not None:
            lock.release()

    def close(self) -> None:
        if self._ser is None:
            self._release_lock()                # harmless if nothing is held
            return
        try:
            self.set_output(False)              # RF off on the way out
        finally:
            try:
                self._ser.close()
            finally:
                self._ser = None
                # released LAST: the RF-off above must reach the unit while it
                # is still ours, before another service may claim the port.
                self._release_lock()

    # ---- phase -----------------------------------------------------------

    def set_phase(self, deg: float) -> None:
        # The brain already rounded to the device step. Send the number with
        # as many decimals as it needs and no more ("PHASE 90", "PHASE 44.5",
        # the manual's own style): a fixed one decimal would turn a 0.25 or
        # 5.625 deg step into a different number from the one we then expect
        # to read back, and the scan's echo check would never be satisfied.
        self._write(f"PHASE {_num(deg)}")       # VERIFY: accepts fractions and +180

    def read_phase(self) -> float:
        return parse_number(self._query("PHASE?"))    # VERIFY: reply format

    # ---- attenuator ------------------------------------------------------

    def set_attenuation(self, dB: float) -> None:
        self._write(f"ATT {_num(dB)}")          # VERIFY: 0..30 dB, 0.25 dB step (manual: "ATT 13.25")

    def read_attenuation(self) -> float:
        return parse_number(self._query("ATT?"))      # VERIFY: reply format

    # ---- output ----------------------------------------------------------

    def set_output(self, on: bool) -> None:
        self._write("OUTP:STAT ON" if on else "OUTP:STAT OFF")

    def read_output(self) -> bool:
        return parse_on(self._query("OUTP:STAT?"))    # VERIFY: "ON"/"OFF" or "1"/"0"

    # ---- frequency -------------------------------------------------------

    def set_frequency(self, mhz: float) -> None:
        # Not in the V3 command list: sent only when a template is configured.
        if self.freq_command:
            try:
                line = self.freq_command.format(mhz=float(mhz))
            except (KeyError, IndexError, ValueError) as exc:
                # a typo in the template (only {mhz} is defined) -> a clear error
                raise ValueError(f"bad device.freq_command {self.freq_command!r}: {exc}") from exc
            self._write(line)                   # VERIFY: command exists

    # ---- identity / diagnostics -----------------------------------------

    def idn(self) -> str:
        return self._idn

    def error(self) -> str:
        """Pending error text (`SYST:ERR?`), for debugging from a script."""
        return self._query("SYST:ERR?")         # VERIFY: reply format

    # ---- line I/O --------------------------------------------------------

    def _write(self, line: str) -> None:
        if self._ser is None:
            raise RuntimeError("phase shifter not open")
        with self._lock:
            self._ser.write((line + "\n").encode("ascii"))
            self._ser.flush()

    def _query(self, line: str) -> str:
        if self._ser is None:
            raise RuntimeError("phase shifter not open")
        with self._lock:
            # Drop anything left over (a set command that DID reply, a half
            # line after a timeout), so this answer is really to this question.
            self._ser.reset_input_buffer()
            self._ser.write((line + "\n").encode("ascii"))
            self._ser.flush()
            raw = self._ser.readline()
        if not raw:
            raise TimeoutError(f"no reply to {line!r} within {self.timeout_s} s")
        return raw.decode("ascii", errors="replace").strip()
