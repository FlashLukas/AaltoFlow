"""Configuration: every tunable number in one place.

Same idea as clMag's config -- Python dataclasses with sensible defaults, saved
to / loaded from a plain-text .ini file so nothing is lost across a restart. A
dataclass is just a class where you list the fields and Python writes the boring
__init__ for you.

Units are explicit in every field name:
    frequencies in hertz (Hz), power in dBm, phase in degrees (deg).

Note on frequency/power ranges: the SMB100A's real limits depend on which
options are fitted (frequency goes 9 kHz up to 1.1 / 3.2 / 6 / 12.75 / 20 / 40
GHz; max level depends on the level option). The defaults below are a safe,
conservative envelope -- widen them in the .ini to match your actual unit.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Signal:
    """The signal the generator should produce at start-up. These are just the
    power-on defaults; everything is changeable live over the wire."""

    frequency_Hz: float = 1_000_000_000.0   # 1 GHz
    power_dBm: float = -30.0                 # a quiet, safe default level
    phase_deg: float = 0.0
    rf_on: bool = False                      # start with the RF OUTPUT OFF (safe)


@dataclass
class Limits:
    """Hard safety envelope. Setpoints outside these are clamped and the event
    is reported (in red on a status bar / as an event over the wire). Protects
    the sample and the downstream chain from an accidental full-power command.

    freq_*  -- the fitted frequency range of your SMB100A.
    power_* -- keep power_max_dBm conservative; raise it deliberately.
    phase_* -- the SMB100A accepts a wide phase range; 0..360 is the useful part.
    """

    freq_min_Hz: float = 9_000.0             # SMB100A RF-path minimum (9 kHz)
    freq_max_Hz: float = 6_000_000_000.0     # 6 GHz -- widen if your unit goes higher
    power_min_dBm: float = -145.0
    power_max_dBm: float = 18.0              # base-model max level; raise for high-power option
    phase_min_deg: float = -360.0
    phase_max_deg: float = 360.0


@dataclass
class Hardware:
    """Where the instrument lives. Only used by the REAL backend; the simulator
    ignores these. GPIB, same transport as the Kepco -- 28 is the SMB100A's
    factory-default GPIB address."""

    smb_visa: str = "GPIB0::28::INSTR"
    visa_timeout_ms: int = 5000
    settle_s: float = 0.05                    # small pause after a SCPI write before reading back


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting: it selects the
    light/dark palette when the GUI launches (there is no live toggle). Persisted
    in the .ini and carried over the wire so a remote GUI matches the service."""

    theme: str = "dark"                       # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    signal: Signal = None
    limits: Limits = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.signal = self.signal or Signal()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "signal": Signal,
        "limits": Limits,
        "hardware": Hardware,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# smb-control configuration -- edit values, keep keys.\n")
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
    """Cast a string read from the .ini back to the field's declared type."""
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(raw)
    if type_name in ("float", float):
        return float(raw)
    return raw
