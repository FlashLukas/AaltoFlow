"""Configuration: every tunable number in one place.

Python dataclasses with sensible defaults, saved to / loaded from a plain-text
.ini file so nothing is lost across a restart. A dataclass is just a class where
you list the fields and Python writes the boring __init__ for you.

Units are explicit in every field name: frequency in hertz (Hz), amplitude in
volts peak-to-peak (Vpp), offset / peak voltage in volts (V), phase in degrees
(deg), duty cycle and ramp symmetry in percent (pct), load in ohm.

A FUNCTION GENERATOR'S VOLTS ARE "INTO THE DECLARED LOAD". The AFG has a 50 ohm
source impedance and a "Load" setting (50 ohm, high-Z or a value). Amplitude
and offset are quoted for that load: 1 Vpp at "Load 50 ohm" is really 2 Vpp
on an open (high-Z) input such as a scope at 1 Mohm or an amplifier input.
This module never changes the load setting by itself (it is read at start and
shown); see README "Volts and the load setting".

Two output channels, CH1 and CH2, one config group each (`channel_1`,
`channel_2`) built from the same `Channel` dataclass, and one SAFETY group per
channel (`limits_1`, `limits_2`): CH1 may drive a magnet amplifier while CH2
only feeds a trigger input, so their ceilings differ.

Instrument numbers (Tektronix AFG1000 series datasheet, AFG1062 = 2 channels,
60 MHz, 300 MS/s) live in backends/tek_afg.py (`AFG1062_ENVELOPE`), not here:
this file holds what the LAB decides, the backend what the instrument can do.
The brain uses the narrower of the two.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields

#: The waveforms the module can SELECT, as they are called on the wire and in
#: the GUI. An arbitrary waveform the instrument was left playing is read as
#: "arb" and kept, but "arb" cannot be selected from here (which of the stored
#: ones would it be?).
WAVEFORMS = ("sine", "square", "pulse", "ramp", "noise", "dc")
SHAPES_READ = WAVEFORMS + ("arb",)

#: Channel names on the wire, in the GUI and in describe ids: "ch1", "ch2".
CHANNEL_NAMES = ("ch1", "ch2")


@dataclass
class Channel:
    """The setting of ONE output channel.

    NOT pushed at start (rule of 2026-09-27: every module reads the
    instrument at start and changes nothing). The service READS what the
    channel is doing and overwrites these fields with it, so get_config and a
    saved .ini show the truth. A value here reaches the instrument only when
    someone changes it: a setter, or set_config / the Settings dialog with a
    different value. The values below are what is shown before a connection.

    There is no "output on" field: the output state is read at start (and left
    alone), and switched only by an explicit set_output.

    duty_pct      -- pulse duty cycle (the AFG1062's square wave is fixed at
                     50 %; # VERIFY on the unit).
    symmetry_pct  -- ramp symmetry: 50 = triangle, 100 = rising saw.
    """

    waveform: str = "sine"
    frequency_Hz: float = 1000.0
    amplitude_Vpp: float = 0.1
    offset_V: float = 0.0
    phase_deg: float = 0.0
    duty_pct: float = 50.0
    symmetry_pct: float = 50.0


@dataclass
class Limits:
    """The LAB's safety ceiling for ONE channel. A request outside it is
    clamped (and said so in a warn event); the instrument's own range
    (backend envelope) narrows it further.

    amplitude_max_Vpp -- largest peak-to-peak, into the declared load.
    peak_max_V        -- largest |offset| + amplitude/2, i.e. the highest
                         voltage the output may ever reach (into the declared
                         load). THE number to lower when the channel drives a
                         magnet amplifier: it bounds the current whatever
                         waveform or offset someone picks.
    freq_max_Hz       -- highest frequency (e.g. what the coil + amplifier
                         can follow). The waveform's own maximum applies too.
    """

    amplitude_max_Vpp: float = 10.0
    peak_max_V: float = 5.0
    freq_max_Hz: float = 60e6


@dataclass
class Coupling:
    """CH2 locked to CH1 -- the use for which this module was written: CH1
    drives the experiment (e.g. a magnet at 30 Hz), CH2 makes a synchronous
    square for the trigger input of a scope.

    ch2_follows_ch1   -- CH2 always gets CH1's frequency, and after every
                         frequency change the two channels are phase-aligned
                         (the AFG's "align phase"), so the phase offset below
                         is meaningful.
    phase_offset_deg  -- CH2's phase = CH1's phase + this.

    A software rule of this module (the brain applies it), so it is in the
    config, not read from the instrument; it is NOT applied at start. It
    works the same for any two-channel generator backend.
    """

    ch2_follows_ch1: bool = False
    phase_offset_deg: float = 0.0


@dataclass
class Hardware:
    """How the real instrument is reached and read. The simulator ignores the
    address; poll_hz applies to both.

    visa       -- the VISA resource. The AFG1062 is a USB-TMC device:
                  "USB0::0x0699::0x0353::<serial>::INSTR" (0x0699 = Tektronix;
                  the product id # VERIFY). Mission Control's "Instruments on
                  this PC" offers the found ones; the service takes --visa.
    timeout_ms -- how long to wait for one reply.
    poll_hz    -- how often the worker READS the instrument back (each read is
                  ~20 queries per channel over USB). The readback is what makes
                  `settled` honest and notices a change made at the front panel.
    phase_unit -- what the instrument's phase command speaks: "rad" (the
                  AFG3000 family's default) or "deg". # VERIFY on the AFG1062:
                  set CH1 phase to 90 deg here and read the front panel.
    """

    visa: str = "USB0::0x0699::0x0353::SERIAL::INSTR"
    timeout_ms: int = 3000
    poll_hz: float = 2.0
    phase_unit: str = "rad"


@dataclass
class UI:
    """User-interface preferences. `theme` is a START-UP setting: it selects the
    light/dark palette when the GUI launches (there is no live toggle)."""

    theme: str = "dark"                       # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    channel_1: Channel = None
    channel_2: Channel = None
    limits_1: Limits = None
    limits_2: Limits = None
    coupling: Coupling = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.channel_1 = self.channel_1 or Channel()
        self.channel_2 = self.channel_2 or Channel(waveform="square")
        self.limits_1 = self.limits_1 or Limits()
        self.limits_2 = self.limits_2 or Limits()
        self.coupling = self.coupling or Coupling()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    def channel(self, ch: str) -> Channel:
        """The setting group of "ch1" or "ch2"."""
        return self.channel_1 if ch == "ch1" else self.channel_2

    def limits(self, ch: str) -> Limits:
        """The safety group of "ch1" or "ch2"."""
        return self.limits_1 if ch == "ch1" else self.limits_2

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "channel_1": Channel,
        "channel_2": Channel,
        "limits_1": Limits,
        "limits_2": Limits,
        "coupling": Coupling,
        "hardware": Hardware,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# afg-control configuration -- edit values, keep keys.\n")
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
        return int(float(raw))
    if type_name in ("float", float):
        return float(raw)
    return raw
