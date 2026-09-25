"""Configuration: every tunable number in one place.

Same pattern as smb-control -- grouped dataclasses with safe defaults, saved to
and loaded from a plain-text .ini so a setup survives a restart.

The two measurement channels use ONE dataclass, `Channel`, twice ([ch1] and
[ch2] in the .ini). A channel is a demodulator plus everything that feeds it:
which signal input, which oscillator, internal or external reference, and the
low-pass filter (time constant + order).

Units are explicit in every field name: seconds (s), hertz (Hz), volts (V),
degrees (deg).
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields

#: The two reference modes. "external" = a PLL locks the channel's oscillator to
#: a signal on a reference input, so the frequency is measured, not set.
REF_MODES = ("internal", "external")


@dataclass
class Channel:
    """One measurement channel = one HF2LI demodulator and its inputs.

    Indices are the instrument's own 0-based node indices, exactly as they
    appear in LabOne node paths (/devN/demods/0/...), so what you type here is
    what you see in LabOne.
    """

    demod: int = 0                  # demodulator index, 0..5
    signal_input: int = 0           # 0 = Signal Input 1, 1 = Signal Input 2
    oscillator: int = 0             # 0 or 1; give each channel its OWN oscillator
    # "internal" = we set the frequency from software (the lab's way of working,
    # confirmed by Lukas 2026-09-15); "external" = a PLL follows a reference input.
    reference: str = "internal"
    ref_input: int = 0              # external mode only: which input the PLL locks to (VERIFY mapping)
    frequency_Hz: float = 1000.0    # internal mode: the oscillator frequency
    time_constant_s: float = 0.01   # low-pass filter time constant
    order: int = 4                  # filter order 1..8 (4 = 24 dB/oct, a common default)
    harmonic: int = 1               # demodulate at n x the reference frequency
    phase_deg: float = 0.0          # demodulator phase shift
    input_range_V: float = 1.0      # signal input range (HF2: 0.01 .. 2 V)
    input_ac: bool = False          # AC coupling
    input_50ohm: bool = False       # 50 ohm input impedance (else 1 Mohm)
    input_diff: bool = False        # differential input


@dataclass
class Acquisition:
    """How the `acquire` verb produces a settled, fresh sample.

    settle_percent -- wait until a step at the input has reached this fraction
                      of its final value at the filter output. The wait is
                      COMPUTED from each channel's time constant and order
                      (99 % is 4.6 TC for order 1 but 10 TC for order 4).
    average_tc     -- after settling, average the output over this many time
                      constants (0 = take one sample). Samples closer together
                      than ~1 TC are strongly correlated, so averaging over a
                      window measured in TC is what actually reduces noise.
    extra_wait_s   -- added on top of the computed settle time.
    timeout_s      -- how long a coordinator should wait before giving up.
    """

    settle_percent: float = 99.0
    average_tc: float = 0.0
    extra_wait_s: float = 0.0
    timeout_s: float = 120.0


@dataclass
class Limits:
    """Safety / sanity envelope. Setpoints outside are clamped with a warning.

    Nothing on a lock-in input can hurt the sample, so these limits are about
    not asking the instrument for something it cannot do. The HF2LI reports
    back the time constant it actually applied, so a request inside this
    envelope that the hardware rounds is still visible in the status.
    """

    tc_min_s: float = 1e-5          # VERIFY against the HF2LI's real range
    tc_max_s: float = 500.0         # VERIFY
    freq_min_Hz: float = 1.0
    freq_max_Hz: float = 50e6       # the HF2LI's 50 MHz bandwidth
    order_min: int = 1
    order_max: int = 8


@dataclass
class Hardware:
    """Where the instrument lives. Used by the real backend only.

    The HF2 series talks to its OWN data server (ziServer, installed with
    LabOne) on port 8005 with API level 1 -- the newer instruments (MFLI, UHFLI)
    use port 8004 and level 6. Getting this wrong is the classic first error.
    """

    device_id: str = "dev0000"      # VERIFY: the HF2LI's id, as shown in LabOne ("dev1234")
    server_host: str = "localhost"
    server_port: int = 8005
    api_level: int = 1
    interface: str = "USB"          # HF2 connects over USB
    demod_rate_Sa_s: float = 1000.0 # demodulator output rate (hardware rounds it)
    poll_hz: float = 50.0           # how often the brain reads the demodulators


@dataclass
class UI:
    """GUI preferences. `theme` is a start-up setting (no live toggle)."""

    theme: str = "dark"             # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    ch1: Channel = None
    ch2: Channel = None
    acquisition: Acquisition = None
    limits: Limits = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        self.ch1 = self.ch1 or Channel()
        # ch2 defaults to the OTHER signal input, demodulator and oscillator, so
        # the two channels are independent out of the box. Demod 3 rather than
        # 1 follows the HF2's habit of grouping demods 0-2 and 3-5 per input.
        self.ch2 = self.ch2 or Channel(demod=3, signal_input=1, oscillator=1,
                                       ref_input=1)
        self.acquisition = self.acquisition or Acquisition()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    def channel(self, i: int) -> Channel:
        """The config of channel index i (0 or 1)."""
        return (self.ch1, self.ch2)[i]

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "ch1": Channel,
        "ch2": Channel,
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
            fh.write("# hf2-control configuration -- edit values, keep keys.\n")
            parser.write(fh)

    @classmethod
    def load(cls, path: str) -> "Config":
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        base = cls()          # start from defaults, so ch2 keeps ITS defaults
        for name, klass in cls._GROUPS.items():
            if name not in parser:
                continue
            section = parser[name]
            grp = getattr(base, name)
            for f in fields(klass):
                if f.name in section:
                    setattr(grp, f.name, _cast(section[f.name], f.type))
        return base


def _cast(raw: str, type_name):
    """Cast a string read from the .ini back to the field's declared type.

    The bool case is the classic trap: bool("False") is True, so the string
    has to be parsed, not converted.
    """
    if type_name in ("bool", bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name in ("int", int):
        return int(raw)
    if type_name in ("float", float):
        return float(raw)
    return raw
