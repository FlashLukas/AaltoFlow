"""Configuration: every tunable number in one place.

Python dataclasses with sensible defaults, saved to / loaded from a plain-text
.ini file so nothing is lost across a restart. A dataclass is just a class where
you list the fields and Python writes the boring __init__ for you.

Units are explicit in every field name: phase in degrees (deg), attenuation in
dB, frequency in MHz.

The instrument is a DS Instruments PS6000L: an ACTIVE phase shifter (an I/Q
modulator core with an amplifier and a 30 dB output step attenuator), 400 to
6000 MHz, phase -180..+180 deg in 0.5 deg steps (datasheet V3.1). The numbers
below default to that model; a PS6000P (5.625 deg steps, 3.5-6 GHz, passive)
would need a different `device` group.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Signal:
    """The state pushed to the shifter at start-up. Everything is changeable
    live over the wire; these are only the power-on defaults."""

    phase_deg: float = 0.0
    attenuation_dB: float = 10.0        # 10 dB down from the ~+10 dBm full output: gentle
    frequency_MHz: float = 2400.0       # the carrier you feed in (see Device.freq_command)
    output_on: bool = False             # start with the RF OUTPUT OFF (safe)


@dataclass
class Limits:
    """Hard safety envelope. Setpoints outside it are clamped and a warn event
    says so.

    phase_*: the device itself covers -180..+180. The envelope is WIDER on
    purpose (-360..+360): phase is periodic, so a scan from 0 to 360 deg is a
    perfectly sensible request -- the service wraps the value into the
    device's range before sending it, and reports it back in YOUR branch.
    att_*: the output step attenuator. 0 dB = full output (~+10 dBm with 0 dBm
    in). Raise att_min_dB to put a ceiling on the power going downstream.
    freq_*: the datasheet band.
    """

    phase_min_deg: float = -360.0
    phase_max_deg: float = 360.0
    att_min_dB: float = 0.0
    att_max_dB: float = 30.0
    freq_min_MHz: float = 400.0
    freq_max_MHz: float = 6000.0


@dataclass
class Device:
    """What the particular unit can do. Changing any of these changes the
    describe manifest (the step sizes are its `step` and settle tolerance), so a
    client notices through describe_rev.

    phase_step_deg: the device's resolution -- every phase is rounded to it
      BEFORE it is sent, so the value we report is the value the unit holds.
    freq_command: the PS6000L R3 command list (V3) has NO frequency command,
      so by default the carrier frequency is only bookkeeping (and selects the
      datasheet accuracy band). If Lukas's firmware does take one, put its
      template here, e.g. "FREQ {mhz:.3f}MHZ" -- `{mhz}` is replaced by the
      value. Empty = never sent.
    """

    model: str = "PS6000L"
    phase_step_deg: float = 0.5
    att_step_dB: float = 0.25
    freq_command: str = ""


@dataclass
class Hardware:
    """Where the instrument lives. Only the REAL backend uses these; the
    simulator ignores them.

    The PS6000L is a USB virtual COM port at 115200 baud, lines ended by "\\n"
    (command list V3). The COM number is whatever Windows assigned -- see
    Device Manager > Ports.
    """

    port: str = "COM5"
    baud: int = 115200
    timeout_s: float = 1.0
    poll_hz: float = 5.0                # how often the worker reads the unit back


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting: it selects the
    light/dark palette when the GUI launches (there is no live toggle)."""

    theme: str = "dark"                 # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    signal: Signal = None
    limits: Limits = None
    device: Device = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.signal = self.signal or Signal()
        self.limits = self.limits or Limits()
        self.device = self.device or Device()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    # EVERY group listed here (gotcha #4). protocol.config_to_dict uses asdict
    # and apply_config_dict walks whatever it is given, so the wire follows.
    _GROUPS = {
        "signal": Signal,
        "limits": Limits,
        "device": Device,
        "hardware": Hardware,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        # interpolation=None: freq_command contains braces and may contain '%'.
        parser = configparser.ConfigParser(interpolation=None)
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# dsphase-control configuration -- edit values, keep keys.\n")
            parser.write(fh)

    @classmethod
    def load(cls, path: str) -> "Config":
        parser = configparser.ConfigParser(interpolation=None)
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

    The bool case is the classic trap (gotcha #3): bool("False") is True, so the
    text has to be PARSED, not converted.
    """
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(raw)
    if type_name in ("float", float):
        return float(raw)
    return raw
