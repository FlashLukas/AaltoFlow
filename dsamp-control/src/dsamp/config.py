"""Configuration: every tunable number in one place.

Python dataclasses with sensible defaults, saved to / loaded from a plain-text
.ini so nothing is lost across a restart. Units are in every field name:
gain in dB, power in dBm, frequency in Hz, temperature in C.

Two different "gain ranges" live here on purpose:

  * `hardware.gain_min_dB .. gain_max_dB` is what the DEVICE accepts (the
    command list says 0 to 31 dB in 0.5 dB steps for the GB6000L family).
  * `limits.gain_min_dB .. gain_max_dB` is YOUR safety envelope. The default
    ceiling is deliberately low (10 dB): an amplifier at +31 dB with a 0 dBm
    input puts ~+20 dBm into whatever follows, and a mixer or a thin-film sample
    does not survive that. Raise it on purpose, in the .ini or in Settings.

The brain uses the INTERSECTION of the two. `describe` reports that
intersection as the live limits of the gain control.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Amp:
    """Start-up values and the operating point.

    There is deliberately NO "amplifier on at start-up" option: the amplifier is
    always switched off when the service starts (and when it stops). Turning it
    on is always an explicit act.

    `frequency_Hz` and `input_dBm` are BOOKKEEPING: the amplifier has no
    frequency or input-level command. You tell the module what goes through it,
    and it uses the datasheet roll-off to estimate the real gain and the output
    power (and warns before the output approaches compression).
    """

    startup_gain_dB: float = 0.0           # gain pushed at start (clamped to the envelope)
    frequency_Hz: float = 2_000_000_000.0  # signal frequency through the amp (2 GHz)
    input_dBm: float = -20.0               # expected input level


@dataclass
class Limits:
    """The safety envelope every request is clamped to (a clamp emits a warn event)."""

    gain_min_dB: float = 0.0
    gain_max_dB: float = 10.0              # SAFETY ceiling -- raise deliberately
    freq_min_Hz: float = 10_000_000.0      # 10 MHz, the GB6000L's lower band edge
    freq_max_Hz: float = 6_000_000_000.0   # 6 GHz
    input_min_dBm: float = -80.0
    input_max_dBm: float = 10.0            # the datasheet's absolute max input (+10 dBm)
    output_warn_dBm: float = 18.0          # warn when the ESTIMATED output exceeds this


@dataclass
class Hardware:
    """Where the amplifier lives and what it accepts. Only the real backend uses
    the port/baud; the simulator uses the gain range, step and P1dB too."""

    port: str = "COM5"                     # the USB virtual COM port (see Device Manager)
    baud: int = 115200                     # command list: 115200 8N1, no flow control
    timeout_s: float = 1.0                 # serial read timeout
    gain_min_dB: float = 0.0               # device range (command list: 0 to 31)
    gain_max_dB: float = 31.0
    gain_step_dB: float = 0.5              # GB6000L: 0.5 dB; the PA6000L uses 0.25 dB
    p1db_dBm: float = 22.0                 # output 1 dB compression point (GB6000L page: +22)
    poll_hz: float = 4.0                   # how often the poll thread reads the device
    buttons_on_exit: bool = True           # re-enable the front-panel buttons on close


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting (no live toggle)."""

    theme: str = "dark"                    # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    amp: Amp = None
    limits: Limits = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.amp = self.amp or Amp()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "amp": Amp,
        "limits": Limits,
        "hardware": Hardware,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# dsamp-control configuration -- edit values, keep keys.\n")
            parser.write(fh)

    @classmethod
    def load(cls, path: str) -> "Config":
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        kwargs = {}
        for name, klass in cls._GROUPS.items():
            if name not in parser:
                continue
            section = parser[name]
            values = {}
            for f in fields(klass):
                if f.name not in section:
                    continue
                # with `from __future__ import annotations`, f.type is a string
                # like "float"/"bool", so we always route through _cast.
                values[f.name] = _cast(section[f.name], f.type)
            kwargs[name] = klass(**values)
        return cls(**kwargs)


def _cast(raw: str, type_name):
    """Cast a string read from the .ini back to the field's declared type.

    The bool case is the classic trap: bool("False") is True in Python, so the
    string has to be PARSED (docs/DEVELOPER_NOTES.md gotcha #3).
    """
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(raw)
    if type_name in ("float", float):
        return float(raw)
    return raw
