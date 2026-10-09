"""Configuration: every tunable number in one place.

In LabVIEW these lived as front-panel controls whose values you saved to disk.
Here they are Python dataclasses -- a dataclass is just a class where you list
the fields and Python writes the boring __init__ for you. Each group carries
sensible defaults (your calibrated values), and the whole set saves to / loads
from a plain-text .ini file so nothing is lost across a restart.

Units are SI-ish and explicit in every field name:
    currents in amperes (A), fields in millitesla (mT), times in seconds (s),
    voltages in millivolts (mV) at the conversion boundary.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class HallProbe:
    """Turns a measured Hall voltage into a field.

        B[mT] = (V[mV] - offset_mV) * sensitivity_mT_per_mV * correction

    `sensitivity` is the probe's own parameter; `correction` is the geometry
    factor from tilting the probe in the field to extend range beyond ~30 mT;
    `offset_mV` is the zero-field reading.
    """

    sensitivity_mT_per_mV: float = 0.0196
    correction: float = 2.602
    offset_mV: float = 2510.0

    def volts_to_field(self, volts: float) -> float:
        """Convert a DAQ reading in VOLTS to field in mT."""
        millivolts = volts * 1000.0
        return (millivolts - self.offset_mV) * self.sensitivity_mT_per_mV * self.correction

    def field_to_volts(self, field_mT: float) -> float:
        """Inverse of volts_to_field -- handy for the simulator."""
        millivolts = field_mT / (self.sensitivity_mT_per_mV * self.correction) + self.offset_mV
        return millivolts / 1000.0


@dataclass
class Ramp:
    """How the current is allowed to move: never abruptly, always stepped.

    A requested change is applied in steps of `increment_A` spaced `delay_s`
    apart. A change SMALLER than one increment is applied directly (no ramp) --
    this is what lets small PID corrections take effect immediately.
    """

    increment_A: float = 0.05
    delay_s: float = 0.010


@dataclass
class PID:
    """Parallel-form PI controller (Td = 0, so no derivative term).

    Output is a current correction in amperes for a field error in mT, so Kc
    has units A/mT. Ti is the integral time in seconds.
    """

    Kc_A_per_mT: float = 0.002
    Ti_s: float = 5.0
    Td_s: float = 0.0


@dataclass
class Limits:
    """Hard safety envelope. Setpoints outside these are clamped silently and
    the event is reported in red on the status bar.

    `field_tolerance_mT` and `stable_time_s` work both ways: field_stable is
    raised after the field has been within tolerance for stable_time, and
    dropped again (in STABLE) after it has been OUTSIDE for stable_time -- the
    time filter keeps one noisy reading from flickering the flag."""

    current_max_A: float = 3.0
    field_tolerance_mT: float = 0.1   # full tolerance band for "on target"
    field_step_mT: float = 2.0        # deliberate undershoot before the PI seek
    stable_time_s: float = 0.2        # must hold within tolerance this long
    # The field SWEEP (ramp_field, for fly scans, 2026-10-09): the pace a
    # sweep may be asked for. The upper end is what the PI follows on the
    # SIMULATED magnet with room to spare; the real coil's inductance and the
    # Kepco's voltage compliance decide the true limit (VERIFY on the rig:
    # sweep at increasing rates and watch the field error in status).
    sweep_rate_min_mT_per_s: float = 0.01
    sweep_rate_max_mT_per_s: float = 20.0


@dataclass
class Stabilizer:
    """Long-term watchdog for a field that has been reached (STABLE or HOLD).

    It INTEGRATES (Lukas, 2026-09-28): every `period_s` it averages the field
    measured since its last move, and if that average is off by more than
    tolerance/2 it adds a correction to a running current trim. The trim is
    kept, so a slow drift is removed completely instead of leaving the standing
    error a proportional nudge leaves. How big a correction is:

        dI = fraction * error / (dB/dI)

    with dB/dI the calibration's local slope -- i.e. `fraction` of the error is
    removed per correction, whatever the operating point on the saturating
    B(I) curve (0.7: converges in 2-3 corrections, cannot overshoot unless the
    true slope is less than a third of the calibrated one). `gain_A_per_mT` is
    used instead of 1/(dB/dI) only when there is no calibration to take a
    slope from.

    Limits: at most `max_step_A` per correction, at most `max_trim_A` in total
    (the anti-windup: at the limit the trim stops accumulating and a warning
    says the drift is bigger than the stabilizer may fix).

    Hysteresis (gotcha #11): a correction AGAINST the direction the field was
    approached from would flip the iron onto the other branch (a 2h jump the
    trim knows nothing about -> the next correction reverses again -> limit
    cycle). So such a correction steps back `backlash_A` FURTHER and then comes
    forward: every correction ends moving in the approach direction, the way
    the seek approaches from one side. `settle_s` is how long after a move the
    readings are ignored (eddy currents, the coil's L/R -- # VERIFY on the real
    magnet: a few time constants)."""

    gain_A_per_mT: float = 0.001    # fallback 1/(dB/dI) when no calibration
    period_s: float = 1.0           # averaging window between corrections
    settle_s: float = 0.3           # readings ignored after a move
    fraction: float = 0.7           # share of the averaged error removed per step
    max_step_A: float = 0.01        # cap per correction (~0.4 mT near 0 A)
    max_trim_A: float = 0.1         # total authority (~4 mT near 0 A)
    backlash_A: float = 0.005       # extra back-step for a correction against the branch


@dataclass
class Acquisition:
    """Two profiles on the USB-6259 (1.25 MS/s single channel):
        precise -- 1000 samples @ 10 kHz  (~100 ms), used when idle/holding/calibrating
        fast    --  200 samples @ 50 kHz  (~4 ms),   used while seeking a new field
    """

    precise_samples: int = 1000
    precise_rate_Hz: float = 10_000.0
    fast_samples: int = 200
    fast_rate_Hz: float = 50_000.0


@dataclass
class Hardware:
    """Where the instruments live. Only used by the REAL backends; the
    simulator ignores these."""

    kepco_visa: str = "GPIB0::6::INSTR"
    daq_channel: str = "Dev1/ai0"
    daq_terminal: str = "RSE"          # referenced single-ended
    daq_v_min: float = -5.0
    daq_v_max: float = 5.0


@dataclass
class Aux:
    """The 'other' BNC connectors on the USB-6259: general-purpose analog outputs,
    single-value analog inputs, and digital outputs. All ±10 V, RSE.

    Channel lists are stored as comma-separated strings so the whole config still
    round-trips through the plain-text .ini file. Use the *_list() helpers to get
    them as Python lists. ai0 is deliberately absent -- it is the Hall probe.
    """

    ao_channels: str = "Dev1/ao0,Dev1/ao1,Dev1/ao2,Dev1/ao3"
    ai_channels: str = "Dev1/ai1,Dev1/ai2,Dev1/ai3"
    do_lines: str = "Dev1/port0/line0,Dev1/port0/line1,Dev1/port0/line2"
    v_min: float = -10.0
    v_max: float = 10.0

    @staticmethod
    def _split(s: str):
        return [c.strip() for c in s.split(",") if c.strip()]

    def ao_list(self):
        return self._split(self.ao_channels)

    def ai_list(self):
        return self._split(self.ai_channels)

    def do_list(self):
        return self._split(self.do_lines)


@dataclass
class UI:
    """GUI preferences. `theme` is applied at launch (dark or light)."""

    theme: str = "dark"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    hall: HallProbe = None
    ramp: Ramp = None
    pid: PID = None
    limits: Limits = None
    stabilizer: Stabilizer = None
    acquisition: Acquisition = None
    hardware: Hardware = None
    aux: Aux = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.hall = self.hall or HallProbe()
        self.ramp = self.ramp or Ramp()
        self.pid = self.pid or PID()
        self.limits = self.limits or Limits()
        self.stabilizer = self.stabilizer or Stabilizer()
        self.acquisition = self.acquisition or Acquisition()
        self.hardware = self.hardware or Hardware()
        self.aux = self.aux or Aux()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------

    _GROUPS = {
        "hall": HallProbe,
        "ramp": Ramp,
        "pid": PID,
        "limits": Limits,
        "stabilizer": Stabilizer,
        "acquisition": Acquisition,
        "hardware": Hardware,
        "aux": Aux,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# clMag-control configuration -- edit values, keep keys.\n")
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
                raw = section[f.name]
                # cast back to the field's declared type
                values[f.name] = f.type(raw) if not isinstance(f.type, str) else _cast(raw, f.type)
            kwargs[name] = klass(**values)
        return cls(**kwargs)


def _cast(raw: str, type_name):
    """Cast a string to the type named in the dataclass annotation."""
    if type_name in ("float", float):
        return float(raw)
    if type_name in ("int", int):
        return int(raw)
    return raw
