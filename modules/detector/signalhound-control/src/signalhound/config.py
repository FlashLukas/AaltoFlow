"""Configuration: every tunable number in one place.

Same idea as the other modules -- dataclasses with sensible defaults, saved to /
loaded from a plain-text .ini file so nothing is lost across a restart.

Units are explicit in every field name: frequencies in Hz, levels in dBm,
ratios in dB, times in s.

The module drives a Signal Hound SA44B or SA124B spectrum analyser (with an
optional USB-TG44A tracking generator) through Signal Hound's sa_api.dll
(`--real`), or SIMULATES one. Next to the usual instrument groups (sweep,
tracking, acquisition, hardware, limits) there is:
  * `scene` -- the PRETEND world on the analyser's input, used only by the
               simulator: a signal generator with harmonics (spectrum mode) and
               a band-pass filter behind a cable (tracking-generator mode).
               They are real settings, changeable live, because "what does a
               narrower filter look like on the analyser" is a fair question to
               ask a simulator.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Sweep:
    """What the analyser sweeps. Every one is changeable live over the wire.

    The analyser -- not we -- decides how many frequency bins a sweep has: it
    follows from span and RBW (spectrum mode) or from `tracking.points`
    (tracking-generator mode). The grid actually in use is in status
    (`points`, `bin_Hz`) and comes from `get_frequencies`.
    """

    center_Hz: float = 1.0e9
    span_Hz: float = 200e6
    ref_level_dBm: float = -20.0      # top of the screen; the API picks gain/attenuation from it
    rbw_Hz: float = 100e3             # resolution bandwidth: narrower = lower noise floor, slower
    vbw_Hz: float = 100e3             # video bandwidth (<= RBW): smooths the noise, not the floor
    reject: bool = True               # software image rejection (right for steady signals)
    detector: str = "average"         # "average" or "peak" (the API's AVERAGE / MIN_MAX max)
    averages: int = 1                 # sweeps averaged per acquisition (in POWER, not in dB)


@dataclass
class Tracking:
    """The USB-TG44A tracking generator: a source that follows the sweep, so
    the analyser measures the TRANSMISSION of whatever sits between the two.

    on          -- sweep with the TG tracking (the API's TG sweep mode). Off =
                   an ordinary spectrum sweep with the TG idle.
    level_dBm   -- TG output level; the TG44A covers -30 ... -10 dBm.
    points      -- requested bins per TG sweep. The API treats it as a
                   suggestion (within a factor of 2); the grid in use is reported.
    high_dynamic_range, passive_device -- the two flags of the API's
                   saConfigTgSweep; both True gives the most dynamic range. Set
                   passive_device False when the device under test AMPLIFIES.
    """

    on: bool = False
    level_dBm: float = -20.0
    points: int = 401
    high_dynamic_range: bool = True
    passive_device: bool = True


@dataclass
class Acquisition:
    """The scan-safe read (`acquire`): `sweep.averages` sweeps that all STARTED
    after the trigger, averaged and latched as the sample."""

    timeout_s: float = 600.0          # a client gives up after this (narrow RBW sweeps are slow)
    continuous: bool = True           # sweep on its own between acquisitions (front-panel mode)
    # Lukas's rule (2026-09-27): starting the software must not change the
    # instrument. Sweeping means CONFIGURING the analyser with this file's
    # settings, so at start the analyser is left exactly as saOpenDevice left
    # it (idle, nothing configured) and `continuous` is switched off -- unless
    # this is True. Turning continuous on, a setter, or an acquire configures
    # it; that is a deliberate act. (The in-process simulator GUI sets it True:
    # there is no instrument to disturb, and an empty screen explains nothing.)
    sweep_on_start: bool = False


@dataclass
class Scene:
    """The SIMULATED world on the analyser input (simulator only).

    Spectrum mode: a signal generator (`tone_*`) with 2nd and 3rd harmonics,
    in front of a noise floor set by the analyser's DANL and the RBW.
    Tracking mode: TG -> cable -> band-pass filter (the device under test) ->
    analyser. `dut_inserted` False is the THRU: what a reference is taken with.
    The TG's own output is not flat (`tg_ripple_dB`) -- which is exactly why
    transmission is measured against a stored thru.
    """

    tone_on: bool = True
    tone_Hz: float = 1.005e9
    tone_dBm: float = -35.0
    harmonic_dBc: float = -45.0       # 2nd harmonic; the 3rd is 10 dB lower
    danl_dBm_per_Hz: float = -150.0   # displayed average noise level at low ref level
    dut_inserted: bool = True
    dut_center_Hz: float = 1.0e9
    dut_bandwidth_Hz: float = 60e6    # -3 dB bandwidth of the filter
    dut_order: int = 3                # Butterworth order: skirts fall 20*order dB/decade
    dut_loss_dB: float = 1.5          # insertion loss in the pass band
    cable_loss_dB_at_1GHz: float = 1.0
    tg_ripple_dB: float = 0.6
    tg_attached: bool = True          # does the simulated TG44A exist?


@dataclass
class Hardware:
    """The real analyser (used only with --real).

    model       -- "auto" (whatever is plugged in), "SA44B" or "SA124B". A
                   specific model makes the service REFUSE a different one,
                   so a mixed-up USB cable is noticed instead of measured with.
    serial      -- 0 = the first analyser found, else open the one with this
                   serial number (two analysers on one PC).
    dll_path    -- "" = load sa_api.dll from the PATH / the working directory;
                   otherwise the full path to it (Signal Hound's SDK folder).
    attach_tg   -- look for a USB-TG44A when opening. None found is not an
                   error: tracking mode is then refused.
    atten, gain -- -1 = automatic (the API chooses from the reference level;
                   Signal Hound recommends it). Otherwise atten 0..3, gain 0..2.
    preamp      -- the input pre-amplifier (only matters with manual gain).
    """

    model: str = "auto"
    serial: int = 0
    dll_path: str = ""
    attach_tg: bool = True
    atten: int = -1
    gain: int = -1
    preamp: bool = False


@dataclass
class Limits:
    """Hard envelope. Setpoints outside it are clamped and the clamp is
    announced as a warn event. The frequency range is further narrowed by the
    MODEL that is connected (SA44B: 1 Hz - 4.4 GHz, SA124B: 100 kHz - 12.4 GHz)
    and, in tracking mode, by the TG44A (10 Hz - 4.4 GHz)."""

    freq_min_Hz: float = 1.0
    freq_max_Hz: float = 13e9
    min_span_Hz: float = 100.0
    ref_min_dBm: float = -100.0
    ref_max_dBm: float = 20.0         # SA_MAX_REF in the API header
    rbw_min_Hz: float = 0.1
    rbw_max_Hz: float = 6e6
    averages_min: int = 1
    averages_max: int = 1000
    tg_level_min_dBm: float = -30.0
    tg_level_max_dBm: float = -10.0
    tg_points_min: int = 11
    tg_points_max: int = 20001
    max_bins: int = 100001            # simulator: coarser bins beyond this (keeps traces sane)


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting (no live toggle)."""

    theme: str = "dark"               # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    sweep: Sweep = None
    tracking: Tracking = None
    acquisition: Acquisition = None
    scene: Scene = None
    hardware: Hardware = None
    limits: Limits = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.sweep = self.sweep or Sweep()
        self.tracking = self.tracking or Tracking()
        self.acquisition = self.acquisition or Acquisition()
        self.scene = self.scene or Scene()
        self.hardware = self.hardware or Hardware()
        self.limits = self.limits or Limits()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "sweep": Sweep,
        "tracking": Tracking,
        "acquisition": Acquisition,
        "scene": Scene,
        "hardware": Hardware,
        "limits": Limits,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# signalhound-control configuration -- edit values, keep keys.\n")
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
