"""Configuration: every tunable number in one place.

Same idea as the other modules -- dataclasses with sensible defaults, saved to /
loaded from a plain-text .ini file so nothing is lost across a restart.

Units are explicit in every field name. The module speaks ONE field unit on the
wire and in every file: millitesla (mT). The Lake Shore 455 itself can show
gauss, tesla, oersted or A/m on its front panel (`display_unit`); the backend
converts whatever the meter sends into mT, so a scan never has to care what
someone left on the display.

Useful numbers: 1 G = 0.1 mT, 1 kG = 100 mT, 35 kG = 3.5 T.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Meter:
    """What the gaussmeter is set to. Every one is changeable live over the
    wire, and the live value is written back here, so Save config stores what
    is actually in use.

    At start-up the meter's OWN settings ALWAYS win: they are read and written
    into this group, never the other way round (Lukas's rule, 2026-09-27 --
    starting a service must not change an instrument). The values here are
    applied to the meter only when a user asks: a setter, or set_config /
    Settings > Apply. (The old `hardware.push_on_start` option is gone; an
    .ini that still has it loads fine, the key is ignored.)

    mode         -- "dc" (static field; the normal case), "rms" (AC field,
                    wide band up to 20 kHz or narrow band up to 1 kHz) or
                    "peak" (the peak detector; its periodic/pulse and
                    positive/negative/both sub-settings stay as the front
                    panel has them).
    dc_digits    -- DC resolution 3, 4 or 5 digits. It IS the 455's filter:
                    3 digits = 100 Hz bandwidth, 30 rdg/s; 4 = 10 Hz, 30 rdg/s;
                    5 = 1 Hz, 10 rdg/s (manual section 4.6.2). More digits =
                    less noise but a slower answer to a field step.
    rms_band     -- "wide" or "narrow" (only used in rms mode).
    auto_range   -- let the meter pick its range (right for a static field).
    range_mT     -- manual full-scale range in mT; the meter snaps UP to its
                    next range, which depends on the probe type.
    display_unit -- what the FRONT PANEL shows: G, T, Oe or A/m. Readings on
                    the wire are always mT.
    relative     -- show/record B - rel_setpoint_mT as well (the 455's
                    relative mode; useful to watch a small change on a big field).
    """

    mode: str = "dc"
    dc_digits: int = 4
    rms_band: str = "wide"
    auto_range: bool = True
    range_mT: float = 350.0
    display_unit: str = "G"
    relative: bool = False
    rel_setpoint_mT: float = 0.0


@dataclass
class Acquisition:
    """The scan-safe read (`acquire`): average N readings that were all taken
    after the trigger AND after the meter's own filter had time to follow a
    field step, so nothing measured before the scan step can leak in.

    settle_time_constants -- how many filter time constants to wait before the
    first counted reading. The manual's time constant is 0.01 s at 3 digits,
    0.1 s at 4 and 1 s at 5 (specification table, DC measurement). After 7 of
    them an exponential step has settled to 0.1 % (e^-7), so an acquisition
    starts counting 0.07 s / 0.7 s / 7 s after the trigger. If a step test on
    the real meter shows its filter is a plain average (settled after ONE time
    constant), lower this to ~1.5.
    timeout_s -- a MARGIN: a client waits this long on top of the settling
    time and 0.5 s per reading before it calls the acquisition failed.
    """

    readings: int = 5                 # readings averaged per acquisition
    settle_time_constants: float = 7.0
    timeout_s: float = 30.0           # a client gives up waiting after this


@dataclass
class Limits:
    """Hard envelope. Setpoints outside it are clamped and the clamp is
    announced as a warn event. The range envelope is narrowed further by the
    ranges the connected PROBE actually has.

    range_*        -- 3.5 uT (UHS probe, 35 mG) ... 35 T (HST probe, 350 kG).
    rel_setpoint_* -- the 455 accepts a relative setpoint of +-350 kG.
    readings_max   -- one acquisition takes readings / reading rate seconds.
    """

    range_min_mT: float = 0.0035
    range_max_mT: float = 35000.0
    rel_setpoint_max_mT: float = 35000.0
    readings_min: int = 1
    readings_max: int = 1000


@dataclass
class Hardware:
    """Only used by the REAL backend (pyvisa); the simulator ignores these.

    resource    -- the VISA resource. The interface is not decided yet, both work:
                   GPIB:   "GPIB0::12::INSTR" (12 = the 455's factory address)
                   RS-232: "ASRL3::INSTR"     (COM3). The 455's serial format is
                   FIXED at 7 data bits, odd parity, 1 stop bit; only the baud
                   rate can be changed (manual table 6-6).
    baud_rate   -- RS-232 only: 9600 (factory), 19200, 38400 or 57600; must
                   match the meter's Interface menu.
    command_gap_s -- the manual asks for 50 ms of silence after a command and
                   no more than 20 messages per second (section 6.2.6).
    poll_hz     -- live readings per second. Each costs one query (two on auto
                   range), so 8 Hz stays well under the 20 messages/s limit.
    zero_time_s -- how long ZPROBE takes. The manual gives no completion query,
                   so readings pause for this long. VERIFY on the real meter.
    probe_geometry -- "axial" (the Hall element measures the field ALONG the
                   stem; the lab's probe) or "transverse" (across a flat blade).
                   The 455 reports the probe's type (HSE/HST/UHS), serial and
                   sensitivity but not its geometry, so it is stated here. It
                   changes only labels, never a reading.
    """

    resource: str = "GPIB0::12::INSTR"
    baud_rate: int = 9600
    timeout_ms: int = 2000
    command_gap_s: float = 0.05
    poll_hz: float = 8.0
    zero_time_s: float = 8.0
    probe_geometry: str = "axial"


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting (no live toggle)."""

    theme: str = "dark"               # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    meter: Meter = None
    acquisition: Acquisition = None
    limits: Limits = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.meter = self.meter or Meter()
        self.acquisition = self.acquisition or Acquisition()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "meter": Meter,
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
            fh.write("# ls455-control configuration -- edit values, keep keys.\n")
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
