"""Configuration: every tunable number in one place.

Same idea as the other modules -- dataclasses with sensible defaults, saved to /
loaded from a plain-text .ini file so nothing is lost across a restart.

Units are in every field name: times in s, wavelengths in nm. Intensities have
no physical unit: the CCS200 reports each pixel as a fraction of its FULL SCALE
(1.0 = the pixel is saturated), and this module keeps that convention, so
"0.6" means "60 % of the way to saturation" on the simulator and the real
instrument alike.

Groups:
  * `scan`        -- what the spectrometer does: integration time, how many
                     scans one acquisition averages, dark subtraction on/off,
                     continuous scanning for the front panel.
  * `analysis`    -- the wavelength window the scalar detectors (peak,
                     integrated intensity) look at.
  * `acquisition` -- how long a client waits for an acquisition, at least.
  * `sim`         -- the PRETEND light on the fibre: a halogen-like lamp plus a
                     few Hg/Ar emission lines, and the CCD's dark signal. Used
                     only by the simulator; changeable live, because "what does
                     a longer integration do to the dark" is a fair question to
                     ask a simulator.
  * `hardware`    -- the real CCS200 (used only with --real).
  * `limits`      -- the envelope every setpoint is clamped to.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Scan:
    """What the spectrometer does. Every one is changeable live over the wire."""

    # 10 ms, the driver's own default (TLCCS_DEF_INT_TIME). Since 2026-09-27
    # this is NOT pushed at start: the brain adopts whatever the instrument is
    # already set to and overwrites this field with it; it is sent only when
    # the user sets a time (setter, GUI, set_config).
    integration_time_s: float = 0.01
    averages: int = 1                  # scans averaged per acquisition
    dark_subtract: bool = False        # subtract the latched dark spectrum
    continuous: bool = True            # scan on its own between acquisitions (front panel)


@dataclass
class Analysis:
    """The window the scalar detectors look at. The spectrum itself is always
    the full 200-1000 nm; only peak / integrated intensity use the window, so a
    scan can follow ONE line while the rest of the spectrum is recorded too."""

    window_min_nm: float = 200.0
    window_max_nm: float = 1000.0


@dataclass
class Acquisition:
    """The scan-safe read (`acquire`): `averages` scans that all STARTED after
    the trigger, averaged and latched as the sample."""

    timeout_s: float = 30.0            # at least this; long integrations get more (describe)


@dataclass
class Sim:
    """The SIMULATED light on the input fibre and the CCD's dark signal.

    Intensity scales as rate x integration time, in full-scale units per second:
    with the defaults and 10 ms, the brightest emission line reaches ~0.6 of
    full scale and the lamp continuum ~0.15, and at 20 ms the line saturates.

    light_on          -- light reaches the fibre. Switch it OFF before `take_dark`
                         (on the real instrument: cap the fibre / close a shutter).
    lamp_level_per_s  -- the broad lamp (a blackbody at lamp_temperature_K,
                         seen through the CCD's response), at its maximum.
    lamp_temperature_K -- colour temperature of that lamp (tungsten ~2800-3200 K).
    line_level_per_s  -- the brightest emission line (Hg 546.07 nm) at its peak.
    line_fwhm_nm      -- instrument resolution. The CCS200 datasheet: < 2 nm FWHM
                         at 633 nm.
    dark_rate_per_s   -- dark current: grows LINEARLY with integration time,
                         with a fixed per-pixel pattern -- which is what makes a
                         dark taken at another integration time wrong.
    offset            -- electronic offset, present at any integration time.
    read_noise        -- rms noise per pixel per scan, full-scale units.
    """

    light_on: bool = True
    lamp_level_per_s: float = 15.0
    lamp_temperature_K: float = 2900.0
    line_level_per_s: float = 60.0
    line_fwhm_nm: float = 1.5
    dark_rate_per_s: float = 0.05
    offset: float = 0.004
    read_noise: float = 0.001


@dataclass
class Hardware:
    """The real CCS200 (used only with --real).

    resource    -- the VISA resource of the spectrometer,
                   "USB0::0x1313::0x8089::M<serial>::RAW". Empty = the first
                   CCS200 the VISA library can find.
    dll_path    -- "" = TLCCS_64.dll where Thorlabs' installer puts it
                   (C:\\Program Files\\IVI Foundation\\VISA\\Win64\\Bin).
    calibration -- "factory" or "user": which wavelength calibration stored in
                   the instrument maps pixels to nm.
    scan_timeout_s -- how long past the integration time to wait for a scan
                   before calling it a hardware error.
    """

    resource: str = ""
    dll_path: str = ""
    calibration: str = "factory"
    scan_timeout_s: float = 5.0


@dataclass
class Limits:
    """Hard envelope. Setpoints outside it are clamped and the clamp is announced
    as a warn event. The integration time range is the driver's own
    (TLCCS_MIN_INT_TIME .. TLCCS_MAX_INT_TIME = 10 us .. 60 s)."""

    integration_min_s: float = 1e-5
    integration_max_s: float = 60.0
    averages_min: int = 1
    averages_max: int = 1000
    min_window_nm: float = 1.0          # narrowest analysis window


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting (no live toggle)."""

    theme: str = "dark"               # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    scan: Scan = None
    analysis: Analysis = None
    acquisition: Acquisition = None
    sim: Sim = None
    hardware: Hardware = None
    limits: Limits = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.scan = self.scan or Scan()
        self.analysis = self.analysis or Analysis()
        self.acquisition = self.acquisition or Acquisition()
        self.sim = self.sim or Sim()
        self.hardware = self.hardware or Hardware()
        self.limits = self.limits or Limits()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "scan": Scan,
        "analysis": Analysis,
        "acquisition": Acquisition,
        "sim": Sim,
        "hardware": Hardware,
        "limits": Limits,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# ccs200-control configuration -- edit values, keep keys.\n")
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
