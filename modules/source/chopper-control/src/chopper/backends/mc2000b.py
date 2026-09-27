"""The real Thorlabs MC2000B optical chopper, over its USB virtual COM port.

This is the ONLY file that touches `pyserial`, and it imports it LAZILY (inside
open()), so the package imports and the simulator runs on a PC without it. On
the lab PC: `uv sync --extra gui --extra real` (gotcha #29: name BOTH extras).

Sources used (and what could not be confirmed without the unit):
  * Thorlabs "MC2000B, MC2000B-EC Optical Chopper User Guide", TTN102010-D02
    Rev A (2016): section 7.2 (terminal settings 115200 8N1, no flow control,
    CR-terminated, "keyword=value" / "keyword?", prompt ">"), section 8.1
    (command list), 8.2 (blade indices 0..14), 8.3/8.4 (blade-dependent ref /
    output indices), 12.1 (chopping ranges, frequency resolution).
  * The open-source `thorlabs_mc2000b` Python package (readthedocs), which
    confirms the same command words, that the unit ECHOES the command, and
    that a reply ends with "\\r> ".

The protocol, as far as those sources go:
    send   b"freq=1000\\r"
    reply  b"freq=1000\\r> "              (echo, then the prompt)
    send   b"freq?\\r"
    reply  b"freq?\\r1000\\r> "           (echo, value, prompt)
An unknown or out-of-range command answers with an error text such as
"Command error CMD_NOT_DEFINED" (manual 7.2); we raise on any reply containing
"error". Every method below is marked # VERIFY where the exact reply format,
unit or number format is an assumption.

Rule from the manual (5.2) that the brain enforces before we get here: blade,
reference mode, output mode and harmonics can only be changed in STANDBY
(enable=0); only frequency and phase can change while the wheel runs.
"""

from __future__ import annotations

import re
import time

from ..hwlock import claim

# The name other services see when they find the COM port taken.
MODULE = "chopper"

_PROMPT = b"> "
_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


class MC2000BError(RuntimeError):
    """The controller answered with an error, or did not answer."""


class SerialMC2000B:
    """Drives a physical MC2000B. Implements the ChopperBackend interface."""

    def __init__(self, port: str = "COM5", baud: int = 115200, timeout_s: float = 0.5,
                 quiet_on_open: bool = False):
        self._port = port
        # Lukas's rule (2026-09-27): at start we only READ the controller. The
        # one write the old open() made, `verbose=0`, changes a setting of the
        # unit, so it is now opt-in (hardware.quiet_on_open, default False).
        # The reply parser below copes with verbose mode instead.
        self._quiet_on_open = bool(quiet_on_open)
        self._baud = int(baud)
        self._timeout = float(timeout_s)
        self._ser = None
        self._lock = None      # the hwlock claim on the COM port while open
        self._idn = ""

    # ---- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        # ONE INSTRUMENT, ONE SERVICE (Lukas's rule: "the same instrument has
        # to be defined by the same physical address"). The COM port IS this
        # chopper's physical address, so we claim it BEFORE a single byte goes
        # out. If another AaltoFlow service already holds it, claim() raises
        # HardwareBusy naming that service and we never open the port -- two
        # programs sending freq= to one wheel would fight without knowing.
        # "com5", "COM5" and "ASRL5::INSTR" are the same port (hwlock.normalize).
        self._lock = claim(self._port, MODULE)
        try:
            self._open_claimed()
        except BaseException:
            # A failed open must not leave the address claimed, or the next
            # attempt (after fixing the cable) would report "busy" against
            # ourselves. Close whatever got opened, then drop the claim.
            self.close()
            raise

    def _open_claimed(self) -> None:
        import serial                                    # lazy: only needed for real hw
        self._ser = serial.Serial(self._port, self._baud,          # VERIFY 115200 8N1
                                  bytesize=8, parity="N", stopbits=1,
                                  timeout=self._timeout, write_timeout=self._timeout)
        # Flush whatever the unit printed at power-up (a banner and a prompt).
        time.sleep(0.05)
        self._ser.reset_input_buffer()
        # QUERIES ONLY from here on (Lukas, 2026-09-27: "read the instrument
        # state on startup, not change anything"). The brain then reads blade,
        # modes, frequency, phase and run state and ADOPTS them.
        #
        # Verbose mode may add status lines to a reply. We do NOT switch it off
        # by default (that would change the unit's setting); `_query` takes the
        # LAST line before the prompt, which is the answer either way. Only if
        # that turns out not to be enough on the real unit, set
        # hardware.quiet_on_open = True to send `verbose=0` here.
        if self._quiet_on_open:
            try:
                self._command("verbose=0")                # VERIFY accepted, silences status lines
            except MC2000BError:
                pass
        self._idn = self._query("id?")                    # VERIFY reply "THORLABS MC2000B ..."

    def close(self) -> None:
        # Close the port, then give the address back (in that order: the claim
        # must outlive every byte we could still send). Safe to call twice.
        try:
            if self._ser is not None:
                try:
                    self._ser.close()
                finally:
                    self._ser = None
        finally:
            lock, self._lock = self._lock, None
            if lock is not None:
                lock.release()

    def idn(self) -> str:
        return self._idn

    # ---- the line protocol -------------------------------------------------

    def _transact(self, line: str) -> str:
        """Send one line, read up to the prompt, return what came between the
        echo and the prompt (stripped)."""
        if self._ser is None:
            raise MC2000BError("not connected")
        self._ser.reset_input_buffer()
        self._ser.write(line.encode("ascii") + b"\r")     # CR terminator (manual 7.2)
        raw = self._ser.read_until(_PROMPT)               # VERIFY prompt is "> " after CR
        if not raw.endswith(_PROMPT):
            raise MC2000BError(f"no prompt after {line!r} (got {raw!r})")
        text = raw[: -len(_PROMPT)].decode("ascii", "replace")
        # drop the echoed command (the unit echoes what it received, # VERIFY)
        text = text.replace("\r\n", "\r").strip("\r\n ")
        if text.startswith(line):
            text = text[len(line):]
        text = text.strip("\r\n ")
        if "error" in text.lower():                       # "Command error CMD_..." (7.2)
            raise MC2000BError(f"{line!r}: {text}")
        return text

    def _command(self, line: str) -> None:
        self._transact(line)

    def _query(self, line: str) -> str:
        # The answer is the last non-empty line before the prompt; anything
        # earlier would be a status message printed in verbose mode.
        # (# VERIFY: where verbose lines appear relative to the value.)
        lines = [ln.strip() for ln in self._transact(line).split("\r") if ln.strip()]
        return lines[-1] if lines else ""

    def _query_num(self, line: str) -> float:
        text = self._query(line)
        m = _NUM.search(text)                             # VERIFY bare number, no unit
        if not m:
            raise MC2000BError(f"{line!r}: no number in reply {text!r}")
        return float(m.group(0))

    def _query_int(self, line: str) -> int:
        return int(round(self._query_num(line)))

    # ---- configuration (standby only) --------------------------------------

    def get_blade(self) -> int:
        return self._query_int("blade?")                  # VERIFY index 0..14 (manual 8.2)

    def set_blade(self, index: int) -> None:
        self._command(f"blade={int(index)}")              # VERIFY

    def get_ref(self) -> int:
        return self._query_int("ref?")                    # VERIFY index per blade (8.3)

    def set_ref(self, index: int) -> None:
        self._command(f"ref={int(index)}")                # VERIFY

    def get_output(self) -> int:
        return self._query_int("output?")                 # VERIFY index per blade (8.4)

    def set_output(self, index: int) -> None:
        self._command(f"output={int(index)}")             # VERIFY

    def get_nharmonic(self) -> int:
        return self._query_int("nharmonic?")              # VERIFY 1..15

    def set_nharmonic(self, n: int) -> None:
        self._command(f"nharmonic={int(n)}")              # VERIFY

    def get_dharmonic(self) -> int:
        return self._query_int("dharmonic?")              # VERIFY 1..15

    def set_dharmonic(self, d: int) -> None:
        self._command(f"dharmonic={int(d)}")              # VERIFY

    # ---- run-time controls -------------------------------------------------

    def get_frequency(self) -> float:
        return self._query_num("freq?")                   # VERIFY Hz, which ring on 10/100

    def set_frequency(self, hz: float) -> None:
        # The manual's resolution is 1 Hz for most blades, 0.1 Hz for the
        # 10/100 blade and 0.01 Hz for the 2/100 blades; the brain has already
        # rounded to that grid. Whether `freq=` takes a decimal ("freq=150.5")
        # or only an integer is not documented; the Python package sends
        # integers. We send an integer when the value is whole, else the
        # shortest decimal (at most 2 places) -- so on a 1 Hz blade nothing but
        # integers ever goes out.
        hz = float(hz)
        if abs(hz - round(hz)) < 1e-9:
            arg = f"{int(round(hz))}"
        else:
            arg = f"{hz:.2f}".rstrip("0").rstrip(".")
        self._command(f"freq={arg}")                      # VERIFY decimals accepted

    def get_phase(self) -> float:
        return self._query_num("phase?")                  # VERIFY degrees 0..360

    def set_phase(self, deg: float) -> None:
        self._command(f"phase={int(round(float(deg)))}")  # VERIFY integer degrees only?

    def get_enable(self) -> bool:
        return self._query_int("enable?") == 1            # VERIFY reply 0 / 1

    def set_enable(self, on: bool) -> None:
        self._command(f"enable={1 if on else 0}")         # VERIFY

    # ---- measurements ------------------------------------------------------

    def read_refout_frequency(self) -> float:
        return self._query_num("refoutfreq?")             # VERIFY Hz; 0 when stopped?

    def read_input_frequency(self) -> float:
        return self._query_num("input?")                  # VERIFY Hz; 0 with no signal?
