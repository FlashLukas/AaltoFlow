"""Configuration: every tunable number in one place.

Python dataclasses with sensible defaults, saved to / loaded from a plain-text
.ini file so nothing is lost across a restart. A dataclass is just a class where
you list the fields and Python writes the boring __init__ for you.

Units are explicit in every field name: frequencies in hertz (Hz) -- except
the external reference, which the instrument itself speaks in MHz -- power in
dBm, phase in degrees (deg), temperature in degC.

The SynthHD PRO v2 has TWO independent RF outputs, RFoutA and RFoutB. They get
one config group each (`channel_a`, `channel_b`) built from the same `Channel`
dataclass, so the two can never drift apart in what they offer.

Numbers below come from the Windfreak "SynthHD PRO v2 Preliminary Data Sheet
v0.2d" (2019): 10 MHz - 24 GHz (calibrated only up to 20 GHz), output power up
to +20 dBm (frequency dependent, typically +17), about -40 dBm minimum leveled,
0.01 dB and 0.01 deg resolution, external reference 10 - 100 MHz.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Channel:
    """Frequency / power / phase of ONE output channel.

    NOT pushed at start (rule of 2026-09-27: every module reads the
    instrument at start and changes nothing). The service READS what the
    channel is doing and overwrites these fields with it, so get_config and a
    saved .ini show the truth. A value here reaches the instrument only when
    someone changes it: a setter, or set_config / the Settings dialog with a
    different value. The values below are what is shown before a connection.

    There is no "RF on" field: the RF state is simply read at start (and
    left alone), and switched only by an explicit set_rf.
    """

    frequency_Hz: float = 1_000_000_000.0   # 1 GHz
    power_dBm: float = -10.0                 # a quiet, safe level
    phase_deg: float = 0.0


@dataclass
class Reference:
    """Where the synthesizer's PLLs take their clock from.

    source      -- "internal_10MHz", "internal_27MHz" or "external". Both PLLs
                   share it, so a missing external reference unlocks BOTH
                   channels (the lock lamps turn red, and a scan waiting on a
                   channel will time out rather than measure a wrong frequency).
    ext_MHz     -- the frequency of the external reference, 10..100 MHz. Only
                   used when source = "external". Must match what is really on
                   the REF IN connector, or the output frequency is scaled
                   wrongly (or the PLL does not lock at all).
    """

    source: str = "internal_27MHz"
    ext_MHz: float = 10.0


#: The three reference choices, in the order the instrument numbers them
#: ("x" command: 0 = external, 1 = internal 27 MHz, 2 = internal 10 MHz).
REFERENCE_SOURCES = ("external", "internal_27MHz", "internal_10MHz")


@dataclass
class Limits:
    """Hard safety envelope, shared by both channels. Setpoints outside it are
    clamped and reported as a warn event.

    freq_*   -- the instrument range (datasheet: 10 MHz - 24 GHz; above 20 GHz
                it is uncalibrated, so power is not leveled there).
    power_*  -- the instrument accepts about -50..+20 dBm, but it can only
                LEVEL between roughly -40 dBm and a frequency-dependent maximum
                (+20 at low frequency, falling to about +6 dBm at 24 GHz). A
                request outside that is set "as close as it can" and the
                channel's `leveled` flag goes false. Lower power_max_dBm if
                something fragile sits downstream.
    phase_*  -- 0..360 deg is the full circle; the instrument only takes
                phase STEPS, the brain turns absolute phases into steps.
    ext_ref_* -- what the REF IN connector accepts (datasheet: 10 - 100 MHz).
    """

    freq_min_Hz: float = 10_000_000.0          # 10 MHz
    freq_max_Hz: float = 24_000_000_000.0      # 24 GHz
    power_min_dBm: float = -50.0
    power_max_dBm: float = 20.0
    phase_min_deg: float = 0.0
    phase_max_deg: float = 360.0
    ext_ref_min_MHz: float = 10.0
    ext_ref_max_MHz: float = 100.0


@dataclass
class Hardware:
    """How the real instrument is reached and driven. The simulator ignores
    the port settings but honours the behavioural ones (off_mode, poll_hz).

    port               -- the USB virtual COM port Windows assigned (Device
                          Manager; the SynthHD may show up as "Teensy").
    timeout_s          -- how long to wait for one reply line.
    poll_hz            -- how often the worker reads lock / level / temperature.
    pll_off_when_rf_off -- False: "RF off" mutes the output and powers down the
                          output amplifier but keeps the PLL running, so a
                          frequency set with RF off still locks (and a scan can
                          verify it), and switching on is instant. True: also
                          power the PLL down -- the quietest possible off
                          (Windfreak's "E0r0 = full quiet"), at the cost of a
                          ~20 ms relock when switching on.
    phase_command      -- "relative": the "~" command ADDS a phase step (what
                          the v1.4 API guide says). "absolute": "~" sets the
                          phase directly. VERIFY on the v2 unit which is true.
    channel_spacing_Hz -- the PLL's frequency grid (= frequency resolution).
                          NEVER written at start (read-only start rule); sent
                          only when you CHANGE it (set_config / Settings), and
                          only if > 0. 0 = leave the instrument's own setting
                          (v2 factory default 100 Hz). The simulator uses it
                          as its grid. Smaller spacing means finer frequency
                          AND slower phase tuning.
    temp_warn_C        -- warn when the internal sensor goes above this (the
                          datasheet says keep it below 75 C).
    """

    port: str = "COM4"
    timeout_s: float = 1.0
    poll_hz: float = 5.0
    pll_off_when_rf_off: bool = False
    phase_command: str = "relative"
    channel_spacing_Hz: float = 0.0
    temp_warn_C: float = 70.0


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting: it selects the
    light/dark palette when the GUI launches (there is no live toggle)."""

    theme: str = "dark"                       # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    channel_a: Channel = None
    channel_b: Channel = None
    reference: Reference = None
    limits: Limits = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.channel_a = self.channel_a or Channel()
        self.channel_b = self.channel_b or Channel(phase_deg=0.0)
        self.reference = self.reference or Reference()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    def channel(self, ch: str) -> Channel:
        """The config group of channel "a" or "b"."""
        return self.channel_a if ch == "a" else self.channel_b

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "channel_a": Channel,
        "channel_b": Channel,
        "reference": Reference,
        "limits": Limits,
        "hardware": Hardware,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# windfreak-control configuration -- edit values, keep keys.\n")
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

    The bool case is the classic trap (gotcha #3): bool("False") is True in
    Python, because any non-empty string is truthy. So we PARSE the text.
    """
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(raw)
    if type_name in ("float", float):
        return float(raw)
    return raw
