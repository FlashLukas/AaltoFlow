"""The real Thorlabs TC200, over its USB virtual COM port.

This is the ONLY file that touches the serial port, and it imports pyserial
inside `open()`, so the package (and every test) works on a PC without it.
Install on the lab PC with  `uv sync --extra gui --extra real`.

Sources (read side by side with this file):
  * Thorlabs "TC200, TC200-EC Temperature Controller User Guide", Rev P,
    2019-02-21 -- chapter 6.3 "The Command Line Interface": 115200 8N1, no
    flow control; commands and queries end in CR; "All commands and queries are
    in lower case letters"; temperatures always degC over serial; the command
    table (6.3.2) and "The Status Byte".
  * InstrumentKit's open-source TC200 driver and its tests
    (instruments/thorlabs/tc200.py), for what the unit actually sends back:
    the command is ECHOED, then the answer, then a "> " prompt, e.g.
        sent  "stat?\\r"   received  "stat?\\r54\\r> "
    and `sns?` answers in a "key = value, ..." form.

How a transaction works here: write "<cmd>\\r", read until the '>' prompt,
drop the echoed command and the prompt, and what is left is the answer. A
reply containing "Command error" (the unit's CMD_NOT_DEFINED /
CMD_ARG_RANGE_ERR) becomes a RuntimeError.

UNTESTED ON THE INSTRUMENT. Every exchange that could not be confirmed against
a transcript from a real unit is marked `# VERIFY`. The parsing is written to
be forgiving (take the first number in the answer) because the exact reply
formats ("24.3 C"? "Tset = 24.3 C"? "24.3 Celsius"?) differ between the
manual's examples, InstrumentKit's code and firmware revisions.
"""

from __future__ import annotations

import re

from ..config import SENSORS
from .base import StatusBits

_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")
PROMPT = b">"


# ---- pure parsing helpers (unit-tested against InstrumentKit-style transcripts) ----

def strip_reply(command: str, raw: str) -> str:
    """Remove the echoed command and the '>' prompt from a raw reply.

    "tact?\\r24.3 C\\r> " -> "24.3 C". The echo may be missing (a firmware
    that does not echo) -- then there is nothing to strip.
    """
    text = raw.replace("\r", "\n")
    text = text.strip()
    if text.endswith(">"):
        text = text[:-1]
    text = text.strip()
    if text.startswith(command):                 # the echo  # VERIFY
        text = text[len(command):]
    return text.strip()


def first_number(text: str) -> float:
    """The first number in an answer: "Tset = 54.3 C" -> 54.3."""
    m = _NUM.search(text)
    if m is None:
        raise RuntimeError(f"TC200: expected a number, got {text!r}")
    return float(m.group(0))


def all_ints(text: str) -> list[int]:
    return [int(float(x)) for x in _NUM.findall(text)]


def parse_stat(text: str, base: int = 16) -> StatusBits:
    """Decode the status byte. The manual calls it hexadecimal; InstrumentKit
    reads decimal. Bit 0 (enabled) comes out the same either way, because the
    last digit's parity does not depend on the base; the other bits do.
    # VERIFY `base` on the unit: enable it and look for bit 2 (PTC100).

    The number is taken as the first WHOLE token that is a valid number, after
    removing the "TMAX ERROR" text: a plain "first run of hex digits" search
    would read the "A" in "TMAX" (or the "E" in "ERROR") as the status byte
    whenever the manual's alarm text came BEFORE the number -- and a latched
    TMAX alarm would then look like "disabled, CYCLE mode". A "0x" prefix is
    accepted too."""
    tmax = "TMAX" in text.upper()                   # "TMAX ERROR" text  # VERIFY
    cleaned = re.sub(r"(?i)tmax|error", " ", text)
    raw = None
    for tok in re.split(r"[\s,;:=]+", cleaned):
        tok = tok.strip()
        if tok.lower().startswith("0x"):
            tok, tb = tok[2:], 16
        else:
            tb = base
        if not tok or not re.fullmatch(r"[0-9A-Fa-f]+", tok):
            continue
        try:
            raw = int(tok, tb)
        except ValueError:                          # e.g. "5C" read in base 10
            raw = int(tok, 16)
        break
    if raw is None:
        raise RuntimeError(f"TC200: bad status byte {text!r}")
    return StatusBits(
        enabled=bool(raw & 0x01),
        cycle_mode=bool(raw & 0x02),
        sensor_alarm=bool(raw & 0x40),                  # VERIFY bit 6
        tmax_alarm=tmax,
        raw=raw,
    )


def parse_sensor(text: str) -> str:
    """'Sensor = PTC100, Beta = 3970' (or just 'PTC100') -> 'ptc100'.
    PTC1000 is tested first because 'ptc100' is a prefix of it."""
    low = text.lower().replace(" ", "")
    for name in ("ptc1000", "ptc100", "th10k"):
        if name in low:
            return name
    raise RuntimeError(f"TC200: unknown sensor reply {text!r}")


class SerialTC200:
    simulated = False

    def __init__(self, cfg, serial_factory=None):
        """`cfg` = the module Config. `serial_factory(port, baud, timeout)` builds
        the port; tests pass a FAKE one, normally it is None and `open()` imports
        pyserial."""
        self.cfg = cfg
        self._factory = serial_factory
        self._ser = None
        self._idn = ""

    # ---- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        hw = self.cfg.hardware
        factory = self._factory
        if factory is None:
            try:
                import serial                       # lazy: only the real backend needs it
            except ImportError as exc:
                raise RuntimeError(
                    "pyserial is not installed. In tc200-control run: "
                    "uv sync --extra gui --extra real") from exc

            def factory(port, baud, timeout):
                return serial.Serial(port=port, baudrate=baud, bytesize=8, parity="N",
                                     stopbits=1, timeout=timeout, xonxoff=False,
                                     rtscts=False)
        try:
            self._ser = factory(hw.port, int(hw.baud), float(hw.timeout_s))
        except Exception as exc:
            raise RuntimeError(f"could not open {hw.port}: {exc}") from exc
        # A CR on its own makes the unit answer "Command error CMD_NOT_DEFINED"
        # and a prompt (manual 6.3.1 step 3) -- a harmless way to flush any
        # half-typed line and get into step.  # VERIFY
        try:
            self._ser.reset_input_buffer()
            self._ser.write(b"\r")
            self._ser.read_until(PROMPT)
            self._ser.reset_input_buffer()
        except Exception:
            pass
        try:
            self._idn = self._query("*idn?")        # VERIFY (the manual also lists id?)
        except Exception:
            self._idn = "THORLABS TC200"
        # prove the link with a real reading before claiming "connected"
        self.read_temperature()

    def close(self) -> None:
        """Close the port WITHOUT touching the heater (the brain decides that)."""
        s, self._ser = self._ser, None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    def idn(self) -> str:
        return self._idn

    # ---- temperature -------------------------------------------------------------

    def read_temperature(self) -> float:
        return first_number(self._query("tact?"))          # VERIFY reply "24.3 C"

    def read_setpoint(self) -> float:
        return first_number(self._query("tset?"))          # VERIFY reply format

    def set_setpoint(self, temperature_C: float) -> None:
        # one decimal: the set-point resolution is 0.1 degC (manual, specs)
        self._command(f"tset={float(temperature_C):.1f}")  # VERIFY

    # ---- output ------------------------------------------------------------------

    def read_status(self) -> StatusBits:
        return parse_stat(self._query("stat?"), int(self.cfg.hardware.stat_base))

    def toggle_enable(self) -> None:
        self._command("ens")                               # VERIFY: toggles, empty reply

    # ---- stored settings -----------------------------------------------------------

    def read_sensor(self) -> str:
        return parse_sensor(self._query("sns?"))           # VERIFY reply format

    def set_sensor(self, sensor: str) -> None:
        if sensor not in SENSORS:
            raise ValueError(f"unknown sensor {sensor!r}")
        self._command(f"sns={sensor}")                     # VERIFY

    def read_pid(self) -> tuple[int, int, int]:
        vals = all_ints(self._query("pid?"))               # VERIFY "126 0 0"
        if len(vals) < 3:
            raise RuntimeError(f"TC200: bad pid? reply {vals!r}")
        return vals[0], vals[1], vals[2]

    def set_p_gain(self, p: int) -> None:
        self._command(f"pgain={int(p)}")                   # VERIFY

    def set_i_gain(self, i: int) -> None:
        self._command(f"igain={int(i)}")                   # VERIFY

    def set_d_gain(self, d: int) -> None:
        self._command(f"dgain={int(d)}")                   # VERIFY

    # The manual's table spells these PMAX / TMAX in capitals while saying all
    # commands are lower case; InstrumentKit uses lower case.
    def read_pmax(self) -> float:
        return first_number(self._query("pmax?"))          # VERIFY case + "15.9 Watts"

    def set_pmax(self, watts: float) -> None:
        self._command(f"pmax={float(watts):.1f}")          # VERIFY case

    def read_tmax(self) -> float:
        return first_number(self._query("tmax?"))          # VERIFY case + "200.0 C"

    def set_tmax(self, temperature_C: float) -> None:
        self._command(f"tmax={float(temperature_C):.1f}")  # VERIFY case

    # ---- transport -------------------------------------------------------------------

    def _exchange(self, command: str) -> str:
        if self._ser is None:
            raise RuntimeError("TC200 is not connected")
        self._ser.write((command + "\r").encode("ascii"))
        raw = self._ser.read_until(PROMPT)
        if not raw.rstrip().endswith(PROMPT):
            # no prompt within the timeout: the unit is off, unplugged or busy
            raise RuntimeError(f"TC200: no reply to {command!r} (timeout)")
        text = strip_reply(command, raw.decode("ascii", errors="replace"))
        if "command error" in text.lower():
            raise RuntimeError(f"TC200 refused {command!r}: {text}")
        return text

    def _query(self, command: str) -> str:
        return self._exchange(command)

    def _command(self, command: str) -> None:
        self._exchange(command)
