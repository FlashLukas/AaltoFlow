# COPIED from afg-control (src/afg/config.py) -- the suite copies shared code
# instead of importing across modules. Channels renamed ch1/ch2 -> w1/w2
# (the Analog Discovery generator outputs W1/W2). Keep the two in step by
# hand when the original changes.
"""The GENERATOR's configuration (W1 / W2 of an Analog Discovery): the copy of
afg-control's config, adapted. Saved in its own file next to scope.ini
(scope-generator.ini), because these are a second instrument's settings.

Units are explicit in every field name: frequency in hertz (Hz), amplitude in
volts peak-to-peak (Vpp), offset / peak voltage in volts (V), phase in degrees
(deg), duty cycle and ramp symmetry in percent (pct).

The Analog Discovery's outputs have NO load setting: the volts are what the
output drives into a high-impedance input (the AD2's W1/W2 are low-impedance
outputs, +-5 V). One config group per output (`channel_1` = W1, `channel_2`
= W2) and one SAFETY group per output (`limits_1`, `limits_2`).

The instrument's own range comes from the backend (dwf: FDwfAnalogOut*Info),
not from here: this file holds what the LAB decides.
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

#: Channel names on the wire, in the GUI and in describe ids: "w1", "w2".
CHANNEL_NAMES = ("w1", "w2")


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

    DEFAULTS = THE FULL RANGE (as for the AFG, Lukas 2026-10-06): the
    Analog Discovery's widest numbers (+-5 V: 10 Vpp, 5 V peak; 20 MHz, above
    the AD2's ~12 MHz analog bandwidth), so out of the box only the
    instrument's own range applies. Lower them (scope-generator.ini, or
    gen_set_config) when a setup needs a ceiling.
    """

    amplitude_max_Vpp: float = 10.0
    peak_max_V: float = 5.0
    freq_max_Hz: float = 20e6


@dataclass
class Coupling:
    """W2 locked to W1 -- the use for which this module was written: W1
    drives the experiment (e.g. a magnet at 30 Hz), W2 makes a synchronous
    square for the trigger input of a scope.

    ch2_follows_ch1   -- W2's FREQUENCY is always W1's, and after every
                         frequency change the channels are phase-aligned
                         (the AFG's "align phase").
    ch2_phase_follows -- while the frequency follows: W2's PHASE is W1's
                         + phase_offset_deg as well. Off = W2's phase is its
                         own setting (counted from W1's after the alignment).
                         Lukas 2026-10-07: "select if also the phase follows".
    phase_offset_deg  -- W2's phase = W1's phase + this.

    A software rule of this module (the brain applies it), so it is in the
    config, not read from the instrument; it is NOT applied at start. It
    works the same for any two-channel generator backend.
    """

    ch2_follows_ch1: bool = False
    ch2_phase_follows: bool = True
    phase_offset_deg: float = 0.0


@dataclass
class Hardware:
    """poll_hz -- how often the worker READS the outputs back (what makes
    `settled` honest). The device itself is chosen by the scope's hardware
    group (it is ONE instrument: scope and generator share it)."""

    poll_hz: float = 2.0


@dataclass
class GenConfig:
    """The whole configuration, one object to pass around."""

    channel_1: Channel = None
    channel_2: Channel = None
    limits_1: Limits = None
    limits_2: Limits = None
    coupling: Coupling = None
    hardware: Hardware = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.channel_1 = self.channel_1 or Channel()
        self.channel_2 = self.channel_2 or Channel()
        self.limits_1 = self.limits_1 or Limits()
        self.limits_2 = self.limits_2 or Limits()
        self.coupling = self.coupling or Coupling()
        self.hardware = self.hardware or Hardware()

    def channel(self, ch: str) -> Channel:
        """The setting group of "w1" or "w2"."""
        return self.channel_1 if ch == "w1" else self.channel_2

    def limits(self, ch: str) -> Limits:
        """The safety group of "w1" or "w2"."""
        return self.limits_1 if ch == "w1" else self.limits_2

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "channel_1": Channel,
        "channel_2": Channel,
        "limits_1": Limits,
        "limits_2": Limits,
        "coupling": Coupling,
        "hardware": Hardware,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# scope-control GENERATOR configuration (W1/W2) -- edit values, keep keys.\n")
            parser.write(fh)

    @classmethod
    def load(cls, path: str) -> "GenConfig":
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
