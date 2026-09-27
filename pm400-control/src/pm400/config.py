"""Configuration: every tunable number in one place.

Same idea as the other modules -- dataclasses with sensible defaults, saved to /
loaded from a plain-text .ini file so nothing is lost across a restart.

Units are explicit in every field name: wavelength in nanometres (nm), power in
watts (W), energy in joules (J), times in seconds (s).

The PM400 is a CONSOLE: what it measures depends on the sensor head plugged
into it (Thorlabs C-series connector).

  * photodiode heads (S12xC ...)  -> power, W. Fast, wavelength-dependent.
  * thermal heads    (S30xC ...)  -> power, W. Flat spectrum, but SLOW: the
                                     absorber takes about a second to warm up.
  * pyroelectric     (ES1xxC ...) -> energy per pulse, J. No auto range, no zero.

The limits below are a wide envelope. The brain ALSO asks the console for the
head's own wavelength / range / averaging limits and uses the narrower of the
two, so the module never offers a setting the head does not have. When a head
is swapped, those limits change and so does `describe` (its revision moves).
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Sensor:
    """What the console is set to. Every one is changeable live over the wire,
    and the live value is written back here, so Save config stores what is
    actually in use.

    At start-up (and whenever a head is plugged in) the console's OWN settings
    are adopted, unless hardware.push_on_start is True -- then these values are
    pushed at start instead.
    """

    wavelength_nm: float = 800.0      # the correction wavelength (responsivity / absorption)
    auto_range: bool = True           # power heads only; right for CW light
    range_W: float = 0.005            # manual power range, used when auto_range is off
    range_J: float = 0.001            # energy range of a pyroelectric head (always manual)
    avg_time_s: float = 0.1           # the console averages its samples over this time per reading


@dataclass
class Acquisition:
    """The scan-safe read (`acquire`): average N readings that were all STARTED
    after the trigger + settle_s, so nothing measured before the scan step (or
    while a slow thermal head was still warming up) can leak in."""

    readings: int = 5                 # readings averaged per acquisition
    settle_s: float = 0.0             # ignore readings that start earlier than this after the trigger
    timeout_s: float = 60.0           # a client gives up waiting after this


@dataclass
class Limits:
    """Hard envelope. Setpoints outside it are clamped and the clamp is
    announced as a warn event. Narrowed further by what the head reports.

    wavelength_* -- wide on purpose: thermal and pyro heads reach 25 um.
    avg_time_max_s -- kept at 1 s ON PURPOSE: a reading blocks the console for
                    its whole averaging time, and every setter has to wait for
                    the reading in progress. Longer averages come from
                    `acquire` (N readings), which blocks nobody.
    """

    wavelength_min_nm: float = 150.0
    wavelength_max_nm: float = 30000.0
    range_min_W: float = 1e-10
    range_max_W: float = 250.0
    range_min_J: float = 1e-8
    range_max_J: float = 20.0
    avg_time_min_s: float = 0.001
    avg_time_max_s: float = 1.0
    readings_min: int = 1
    readings_max: int = 1000
    settle_max_s: float = 60.0


@dataclass
class Hardware:
    """Only used by the REAL backend (TLPMX); the simulator ignores these.

    resource  -- the TLPMX resource name, e.g. "USB0::0x1313::0x807D::...::INSTR".
                 Empty = the first Thorlabs power meter found. List what is
                 plugged in with `scripts/list_devices.py`.
    dll_path  -- empty = the standard install location of TLPMX_64.dll
                 (comes with Thorlabs Optical Power Monitor / OPM).
    channel   -- the TLPMX sensor channel. 1 on every single-channel console,
                 which the PM400 is.
    head_check_s -- how often the console is asked which head is plugged in.
    """

    resource: str = ""
    dll_path: str = ""
    timeout_ms: int = 5000
    channel: int = 1
    poll_hz: float = 20.0             # upper bound; the averaging time sets the real rate
    head_check_s: float = 2.0
    push_on_start: bool = False       # False = adopt the console's settings at start


@dataclass
class Sim:
    """The SIMULATED bench (ignored with --real). Read live by the simulator, so
    changing `head` in Settings is like plugging in a different head.

    head -- "photodiode" (S121C-like, Si 400-1100 nm), "thermal" (S302C-like,
            190 nm - 25 um, ~1 s response), "pyro" (ES111C-like energy head)
            or "none" (nothing plugged in).
    """

    head: str = "photodiode"
    laser_nm: float = 800.0           # wavelength of the simulated light
    incident_W: float = 1.2e-3        # CW power on the head (photodiode / thermal)
    pulse_energy_J: float = 2e-4      # energy per pulse (pyro)
    rep_rate_Hz: float = 10.0         # pulse repetition rate (pyro)


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
    sim: Sim = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.sensor = self.sensor or Sensor()
        self.acquisition = self.acquisition or Acquisition()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.sim = self.sim or Sim()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "sensor": Sensor,
        "acquisition": Acquisition,
        "limits": Limits,
        "hardware": Hardware,
        "sim": Sim,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# pm400-control configuration -- edit values, keep keys.\n")
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
