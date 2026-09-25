"""Configuration: every tunable number in one place.

Same idea as the other modules -- dataclasses with sensible defaults, saved to /
loaded from a plain-text .ini file so nothing is lost across a restart.

Units are explicit in every field name: wavelength in nanometres (nm), power in
watts (W), times in seconds (s).

The PM16 series are USB power meters with a built-in sensor (the lab's unit is
a PM16-121, Si photodiode, 400-1100 nm). The limits below are a wide envelope;
when the real meter is connected the brain ALSO asks it for its own wavelength
and range limits and uses the narrower of the two, so the module never offers a
setting the head does not have.

Learned on the real PM16-121 (2026-09-15): its averaging is FIXED (60 ms per
reading, setAvgCnt and setAvgTime are both refused), so there is no averaging
setting here -- extra averaging happens in software, in `acquire`.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Sensor:
    """What the meter is set to. Every one is changeable live over the wire,
    and the live value is written back here, so Save config stores what is
    actually in use.

    At start-up the meter's OWN stored settings win (it keeps its wavelength
    across power cycles, and someone may have set it in Thorlabs OPM), unless
    hardware.push_on_start is True -- then these values are pushed instead.
    """

    wavelength_nm: float = 800.0      # sets the responsivity used to turn A into W
    auto_range: bool = True           # let the meter pick its range (right for CW light)
    range_W: float = 0.005            # manual range, used only when auto_range is off


@dataclass
class Acquisition:
    """The scan-safe read (`acquire`): average N readings that were all STARTED
    after the trigger, so nothing measured before the scan step can leak in."""

    readings: int = 5                 # readings averaged per acquisition (60 ms each on a PM16)
    timeout_s: float = 30.0           # a client gives up waiting after this


@dataclass
class Limits:
    """Hard envelope. Setpoints outside it are clamped and the clamp is
    announced as a warn event. Narrowed further by what the meter reports.

    wavelength_* -- Si photodiode heads: 400-1100 nm.
    range_*      -- wide on purpose; the PM16-121 itself reported ranges
                    0.174 mW ... 1.74 W, and that is what is actually offered.
    readings_max -- one acquisition takes readings x 60 ms.
    """

    wavelength_min_nm: float = 400.0
    wavelength_max_nm: float = 1100.0
    range_min_W: float = 1e-9
    range_max_W: float = 2.0
    readings_min: int = 1
    readings_max: int = 1000


@dataclass
class Hardware:
    """Only used by the REAL backend (TLPMX); the simulator ignores these.

    resource  -- the TLPMX resource name, e.g. "USB0::0x1313::0x807B::...::INSTR".
                 Empty = the first Thorlabs power meter found. List what is
                 plugged in with `scripts/list_devices.py`.
    dll_path  -- empty = the standard install location of TLPMX_64.dll
                 (comes with Thorlabs Optical Parameter Monitor / OPM).
    """

    resource: str = ""
    dll_path: str = ""
    timeout_ms: int = 5000
    poll_hz: float = 20.0             # upper bound; the PM16 itself gives ~17 readings/s
    push_on_start: bool = False       # False = adopt the meter's stored settings at start


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting (no live toggle)."""

    theme: str = "dark"               # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    sensor: Sensor = None
    acquisition: Acquisition = None
    limits: Limits = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.sensor = self.sensor or Sensor()
        self.acquisition = self.acquisition or Acquisition()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "sensor": Sensor,
        "acquisition": Acquisition,
        "limits": Limits,
        "hardware": Hardware,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# pm16-control configuration -- edit values, keep keys.\n")
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

    The bool case matters: bool("False") is True, so parse the text instead.
    """
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(float(raw))
    if type_name in ("float", float):
        return float(raw)
    return raw
