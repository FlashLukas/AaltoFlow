"""Configuration: every tunable number in one place.

Same pattern as every module in the suite -- grouped dataclasses with safe
defaults, saved to and loaded from a plain-text .ini so a setup survives a
restart.

The Model 7230 is a SINGLE-reference DSP lock-in: one reference channel, one
demodulator pair (X, Y), one output filter. So there is one `Reference`, one
`Signal` input and one `Filter` group, not a list of channels.

Several settings on the 7230 are DISCRETE (the instrument only knows a fixed
list of time constants and sensitivities). The tables live in `tables.py`; here
a time constant is stored in seconds (snapped to the table when it is sent) and
a sensitivity by its TABLE INDEX, exactly the number the `SEN n` command takes,
because the same index means 100 mV in voltage mode and 100 nA in current mode.

Units are explicit in every field name: seconds (s), hertz (Hz), volts (V),
degrees (deg).
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields

#: Reference sources, in the order of the instrument's `IE n` command
#: (0 = internal oscillator, 1 = external TTL, 2 = external analog).
REF_SOURCES = ("internal", "ext_ttl", "ext_analog")

#: Signal input configurations. One list for what the instrument splits over
#: two commands (IMODE for current mode, VMODE for the voltage inputs), because
#: to the person at the bench it is ONE choice: "where is my signal plugged in".
INPUT_MODES = ("A", "-B", "A-B", "ground", "I high-BW", "I low-noise")

#: Output filter slopes in dB/octave, index = the `SLOPE n` value. Each 6 dB is
#: one RC stage, so the slope is also the filter ORDER (6 -> 1 ... 24 -> 4).
SLOPES_DB = (6, 12, 18, 24)


@dataclass
class Reference:
    """The reference channel and the internal oscillator.

    The oscillator (OSC OUT on the front panel) always runs at `frequency_Hz`
    with `amplitude_V`. With an INTERNAL reference it is also what the lock-in
    demodulates at; with an EXTERNAL one the reference follows the REF IN signal
    and the oscillator just keeps running on its own.
    """

    source: str = "internal"        # internal | ext_ttl | ext_analog
    frequency_Hz: float = 1000.0    # oscillator frequency (OF.)
    # 0 V on purpose: OSC OUT may be wired to a sample or a coil driver, and a
    # service that starts must not start driving it. Raise it deliberately.
    amplitude_V: float = 0.0        # oscillator amplitude, V rms (OA.)
    phase_deg: float = 0.0          # reference phase shift (REFP.)
    harmonic: int = 1               # demodulate at n x the reference (REFN), 1..127


@dataclass
class Signal:
    """The signal channel: which input, how it is coupled, how sensitive."""

    input: str = "A"                # one of INPUT_MODES
    ac_coupled: bool = True         # DCCOUPLE 0 = AC. VERIFY the power-up default.
    fet: bool = False               # FET input device (else bipolar); high source impedance
    float_shield: bool = False      # input connector shells floating (1 kohm to ground)
    # Full-scale sensitivity as the SEN table index: 24 = 100 mV (voltage) /
    # 100 nA (high-BW current). Range 3..27 (7..27 in low-noise current mode).
    sensitivity_index: int = 24
    auto_ac_gain: bool = True       # AUTOMATIC 1: the AC gain follows the sensitivity
    line_filter: str = "off"        # off | 1f | 2f | both  (LF n1: mains notch)
    line_freq_Hz: int = 50          # 50 or 60 (LF n2)


@dataclass
class Filter:
    """The output low-pass filter."""

    time_constant_s: float = 0.1    # snapped to the nearest 1-2-5 table value
    slope_db: int = 12              # 6 / 12 / 18 / 24 dB/octave
    # FASTMODE. Off: time constants >= 5 ms, all four slopes. On: down to 10 us,
    # but only 6 or 12 dB/oct. The manual (section 6.6.04) states the trade.
    fast_mode: bool = False


@dataclass
class Acquisition:
    """How the `acquire` verb produces a settled, fresh sample.

    settle_percent -- wait until a step at the input has reached this fraction
                      of its final value at the filter output. The wait is
                      COMPUTED from the time constant and the slope (99 % is
                      4.6 TC at 6 dB/oct but 10 TC at 24 dB/oct).
    average_tc     -- after settling, average the output over this many time
                      constants (0 = take one reading).
    extra_wait_s   -- added on top of the computed settle time.
    timeout_s      -- the least a coordinator should wait before giving up; the
                      manifest raises it automatically for long time constants.
    """

    settle_percent: float = 99.0
    average_tc: float = 0.0
    extra_wait_s: float = 0.0
    timeout_s: float = 120.0


@dataclass
class Limits:
    """Safety / sanity envelope. Setpoints outside are clamped with a warning.

    The instrument's own ranges are tighter still for the frequency (120 kHz, or
    250 kHz with the 7230/99 option -- see Hardware) and the time constant
    (5 ms minimum unless fast mode is on); the brain applies both.
    """

    tc_min_s: float = 10e-6
    tc_max_s: float = 1000.0        # the table goes to 100 ks; nobody waits that long in a scan
    freq_min_Hz: float = 0.001
    freq_max_Hz: float = 250e3
    # The oscillator can deliver 5 V rms. 1 V is the envelope until someone
    # raises it on purpose, because OSC OUT often drives something real.
    amplitude_max_V: float = 1.0
    harmonic_max: int = 127


@dataclass
class Hardware:
    """How to reach the instrument. Used by the real backend only.

    The 7230 has Ethernet, USB and RS-232. This module talks Ethernet (TCP):
    port 50000 answers every command with the reply, a NUL byte and then the
    status and overload bytes, which gives the overload state for free with
    every reading (manual section 6.5.05). RS-232 is the documented fallback
    and not implemented yet (see the backend's docstring).
    """

    interface: str = "tcp"          # tcp (serial: not implemented yet)
    # The instrument's IP address, as shown on its web panel (manual section
    # 5.2). Empty until someone types it in: --real then refuses with a clear
    # message instead of trying a made-up address.
    host: str = ""
    port: int = 50000
    timeout_s: float = 2.0          # per command; auto operations get longer
    serial_port: str = "COM1"       # for a future RS-232 transport
    option_250kHz: bool = False     # fitted with the 7230/99 option (250 kHz instead of 120 kHz)
    read_adc: bool = True           # read the rear ADC1/ADC2 inputs with every poll
    poll_hz: float = 20.0           # how often the brain reads the outputs
    # OSC OUT safety. At start: the amplitude goes to 0 V whatever the .ini
    # says, so (re)starting the service never starts driving a coil or a
    # sample by itself. At shutdown: back to 0 V, so a Stop in the launcher
    # does not leave it driven either. Switch off only if the oscillator must
    # keep running across restarts (e.g. it IS the experiment's modulation).
    osc_zero_on_start: bool = True
    osc_off_on_shutdown: bool = True


@dataclass
class UI:
    """GUI preferences. `theme` is a start-up setting (no live toggle)."""

    theme: str = "dark"             # "dark" or "light"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    reference: Reference = None
    signal: Signal = None
    filter: Filter = None
    acquisition: Acquisition = None
    limits: Limits = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        self.reference = self.reference or Reference()
        self.signal = self.signal or Signal()
        self.filter = self.filter or Filter()
        self.acquisition = self.acquisition or Acquisition()
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "reference": Reference,
        "signal": Signal,
        "filter": Filter,
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
            fh.write("# sr7230-control configuration -- edit values, keep keys.\n")
            parser.write(fh)

    @classmethod
    def load(cls, path: str) -> "Config":
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        base = cls()
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
        return int(float(raw))
    if type_name in ("float", float):
        return float(raw)
    return raw
