"""Configuration: every tunable number in one place.

Same pattern as every module -- grouped dataclasses with safe defaults, saved to
and loaded from a plain-text .ini so a setup survives a restart.

The SR830 has ONE demodulator, so there are no per-channel groups; its settings
are split the way the instrument's front panel splits them:

    [reference]   where the reference comes from, frequency, harmonic, phase,
                  the external trigger shape, and the SINE OUT amplitude
    [input]       A / A-B / current, shield grounding, coupling, line notches
    [demod]       sensitivity, dynamic reserve, time constant, slope, sync filter
    [aux_out]     the four rear-panel AUX OUT voltages
    [acquisition] how `acquire` waits and averages (same as hf2)
    [limits]      the envelope every setter clamps to
    [safety]      what the service does to the outputs when it stops
    [hardware]    GPIB address, timeouts, polling rate
    [ui]          the GUI's colour scheme

Discrete settings (time constant, sensitivity) are stored as their LABELS,
"30 ms" or "10 mV", exactly as printed on the front panel, so the .ini reads
like the instrument. tables.py converts them to GPIB indices.

Units are explicit in every numeric field name: seconds (s), hertz (Hz),
volts (V), degrees (deg). X / Y / R / sine amplitudes are RMS volts, as on the
SR830.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields

from . import tables


@dataclass
class Reference:
    """The reference oscillator and the SINE OUT connector."""

    # "internal": the SR830's own oscillator at frequency_Hz (scannable).
    # "external": it locks to the REF IN connector and MEASURES the frequency.
    source: str = "internal"
    frequency_Hz: float = 1000.0        # internal mode only (0.001 Hz .. 102 kHz)
    harmonic: int = 1                   # detect at n x reference (n x f <= 102 kHz)
    phase_deg: float = 0.0              # reference phase shift, -180 .. +180
    trigger: str = "sine"               # external mode: sine / ttl_rising / ttl_falling
    # SINE OUT amplitude, Vrms. It can NOT be switched off on an SR830; 4 mV is
    # its minimum, so that is the default -- nothing downstream gets driven
    # harder than the instrument allows it to be driven softly.
    sine_out_V: float = 0.004


@dataclass
class Input:
    """The signal input stage (manual 5-5)."""

    source: str = "A"                   # A, A-B, I1M (1 Mohm current), I100M
    ground: str = "float"               # shield: float or ground
    coupling: str = "AC"                # AC or DC
    line_filter: str = "off"            # off, line, 2xline, both (notch filters)


@dataclass
class Demod:
    """Gain and the low-pass filter (manual 5-6)."""

    sensitivity: str = "10 mV"          # full scale; one of tables.SENS_LABELS_V
    reserve: str = "normal"             # high, normal, low_noise
    time_constant: str = "30 ms"        # one of tables.TC_LABELS
    slope: str = "24 dB/oct"            # 6/12/18/24 dB/oct = filter order 1..4
    sync_filter: bool = False           # synchronous filter (only below 200 Hz)


@dataclass
class AuxOut:
    """The four rear-panel AUX OUT voltages, pushed at start (manual 5-9)."""

    out1_V: float = 0.0
    out2_V: float = 0.0
    out3_V: float = 0.0
    out4_V: float = 0.0

    def get(self, k: int) -> float:
        return getattr(self, f"out{k + 1}_V")

    def set(self, k: int, v: float) -> None:
        setattr(self, f"out{k + 1}_V", float(v))


@dataclass
class Acquisition:
    """How the `acquire` verb produces a settled, fresh sample.

    settle_percent -- wait until a step at the input has reached this fraction
                      of its final value at the filter output. The wait is
                      COMPUTED from the time constant and slope (99 % is
                      4.6 tau at 6 dB/oct, 10 tau at 24 dB/oct; the manual's
                      ENBW / "wait time" table rounds these to 5 / 7 / 9 / 10 tau).
    average_tc     -- after settling, average over this many time constants
                      (0 = take one reading).
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

    The defaults are the SR830's own ranges (manual chapter 5), so they only
    stop requests the instrument would reject anyway -- narrow them here to
    protect whatever is connected to SINE OUT or AUX OUT.
    """

    freq_min_Hz: float = 0.001
    freq_max_Hz: float = 102000.0       # also the limit for harmonic x frequency
    harmonic_max: int = 19999
    sine_min_V: float = 0.004
    sine_max_V: float = 5.0
    aux_out_min_V: float = -10.5
    aux_out_max_V: float = 10.5
    # the longest time constant offered (index into tables.TC_LABELS); 30 ks
    # is legal but a scan point would then take the better part of a week
    tc_max: str = "30 ks"


@dataclass
class Safety:
    """What happens to the OUTPUTS when the service stops (cleanly).

    A lock-in input cannot hurt anything, but SINE OUT and AUX OUT drive
    whatever is plugged into them -- a modulation coil, a piezo, a laser
    driver. A hard kill (Task Manager, power cut) can not do any of this.
    """

    sine_min_on_stop: bool = True       # SINE OUT to its 4 mV minimum
    aux_out_zero_on_stop: bool = True   # every AUX OUT to 0 V


@dataclass
class Hardware:
    """Where the instrument lives. Used by the real backend only."""

    resource: str = "GPIB0::8::INSTR"   # the SR830 ships at GPIB address 8 (manual 5-24)
    timeout_ms: int = 3000
    # OVRM 1 keeps the front panel usable while the SR830 is under remote control.
    # Without it every GPIB command locks the knobs out.
    front_panel_override: bool = True
    poll_hz: float = 20.0               # readings per second (each costs 3 GPIB queries)


@dataclass
class UI:
    """GUI preferences. `theme` is a start-up setting (no live toggle)."""

    theme: str = "dark"                 # "dark" or "light"


#: Which string fields are enums, and their allowed values. The Settings pane
#: turns these into drop-downs, and the brain validates against them.
CHOICES = {
    ("reference", "source"): tables.REF_SOURCES,
    ("reference", "trigger"): tables.TRIGGERS,
    ("input", "source"): tables.INPUT_SOURCES,
    ("input", "ground"): tables.GROUNDS,
    ("input", "coupling"): tables.COUPLINGS,
    ("input", "line_filter"): tables.LINE_FILTERS,
    ("demod", "sensitivity"): tables.SENS_LABELS_V,
    ("demod", "reserve"): tables.RESERVES,
    ("demod", "time_constant"): tables.TC_LABELS,
    ("demod", "slope"): tables.SLOPES,
    ("limits", "tc_max"): tables.TC_LABELS,
    ("ui", "theme"): ("dark", "light"),
}


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    reference: Reference = None
    input: Input = None
    demod: Demod = None
    aux_out: AuxOut = None
    acquisition: Acquisition = None
    limits: Limits = None
    safety: Safety = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        self.reference = self.reference or Reference()
        self.input = self.input or Input()
        self.demod = self.demod or Demod()
        self.aux_out = self.aux_out or AuxOut()
        self.acquisition = self.acquisition or Acquisition()
        self.limits = self.limits or Limits()
        self.safety = self.safety or Safety()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "reference": Reference,
        "input": Input,
        "demod": Demod,
        "aux_out": AuxOut,
        "acquisition": Acquisition,
        "limits": Limits,
        "safety": Safety,
        "hardware": Hardware,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# sr830-control configuration -- edit values, keep keys.\n")
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
        return int(raw)
    if type_name in ("float", float):
        return float(raw)
    return raw
