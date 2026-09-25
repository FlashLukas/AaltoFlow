"""Configuration: every tunable number of the 2D vector magnet in one place.

Same pattern as every module in the suite: small dataclasses grouped by concern,
with defaults, saved to / loaded from a plain-text .ini file.

Units are explicit in every field name: field in millitesla (mT), angle in
degrees (deg), voltages in volts (V), times in seconds (s).

Where the defaults come from: the old LabVIEW program "RotSampleInVNA" (Rasmus).
Numbers copied from its front panel are marked "from the VI"; numbers that are
uncertain until someone checks them on the real magnet are marked VERIFY. The
control gains were tuned on the SIMULATOR and must be retuned on the magnet.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict, fields


@dataclass
class Hardware:
    """Where the signals are wired on the NI DAQ. Only used by the REAL backend
    (backends/nidaq.py); the simulator ignores this group.

    Channel names are DAQmx physical channel names, the same strings NI MAX shows.
    """

    ao_x: str = "Dev1/ao0"            # X coil drive (from the VI)
    ao_y: str = "Dev1/ao1"            # Y coil drive (from the VI)
    ao_min_V: float = -10.0
    ao_max_V: float = 10.0
    # +1 if a positive AO voltage gives a positive field on that axis, -1 if the
    # coil (or the amplifier) is wired the other way. The VI assumes +1. VERIFY:
    # with the wrong sign the PI pushes the field AWAY from the setpoint.
    ao_sign_x: float = 1.0
    ao_sign_y: float = 1.0
    ai_hall_x: str = "Dev1/ai0"       # Hall probe X (from the VI)
    ai_hall_y: str = "Dev1/ai1"       # Hall probe Y (from the VI)
    ai_temp1: str = "Dev1/ai2"        # temperature 1 (from the VI)
    ai_temp2: str = "Dev1/ai3"        # temperature 2 (from the VI)
    ai_terminal: str = "default"      # "default" | "RSE" | "NRSE" | "DIFF" (VI: default)
    ai_min_V: float = 0.0             # the VI reads 0..5 V
    ai_max_V: float = 5.0
    hall_samples: int = 100           # averaged per loop tick (10 ms at 10 kHz)
    hall_rate_Hz: float = 10000.0
    di_water: str = "Dev1/port0/line1"    # True = cooling water flowing (from the VI)
    do_enable: str = "Dev1/port0/line0"   # output enable, True while running (from the VI)
    timeout_s: float = 2.0            # DAQmx read timeout


@dataclass
class Hall:
    """Hall probe calibration, per axis:   V = offset + k * B

    so the brain computes  B[mT] = (V - offset_V) / (k[mV/mT] / 1000).
    Values from the VI. Note the X probe has a NEGATIVE slope -- that is what the
    VI says, and it is why reading the calibration from config matters.
    """

    x_offset_V: float = 2.4915
    x_mV_per_mT: float = -13.38466
    y_offset_V: float = 2.4973
    y_mV_per_mT: float = 13.15434

    def volts_to_mT(self, vx: float, vy: float) -> tuple[float, float]:
        return ((vx - self.x_offset_V) / (self.x_mV_per_mT / 1000.0),
                (vy - self.y_offset_V) / (self.y_mV_per_mT / 1000.0))

    def mT_to_volts(self, bx: float, by: float) -> tuple[float, float]:
        """The inverse, for the simulator."""
        return (self.x_offset_V + bx * self.x_mV_per_mT / 1000.0,
                self.y_offset_V + by * self.y_mV_per_mT / 1000.0)


@dataclass
class Temperature:
    """Two temperature sensors:  T[C] = C_per_V * V + offset_C.

    The VI multiplies by 10e3 (= 10000). VERIFY on the hardware: an LM35-type
    sensor would be 100 C/V, and 10000 C/V would put 25 C at 2.5 mV. Whatever the
    truth, it is a config value, so fixing it is an edit here, not in the code.
    """

    t1_C_per_V: float = 10000.0
    t1_offset_C: float = 0.0
    t2_C_per_V: float = 10000.0
    t2_offset_C: float = 0.0


@dataclass
class Control:
    """The field loop: one PI per axis, running CONTINUOUSLY while energized.

    Output volts per axis = feed-forward + Kp*e + Ki*integral(e dt), where e is
    the field error in mT.

    ff_mT_per_V   feed-forward: the output starts at B_set / ff_mT_per_V, the volts
                  the magnet needs in an ideal world, and the PI only trims what is
                  left (hysteresis, cross-talk, gain error). 0 = no feed-forward.
                  20 mT/V is the SIMULATOR's gain -- MEASURE it on the magnet
                  (set 1 V, read the field): with a wrong value the PI still gets
                  there, just slower.
    kp, ki        SIM-TUNED. Must be retuned on the real magnet (a real iron yoke
                  has more hysteresis and eddy-current lag than the sim).
    slew_V_per_s  the old VI stepped 0.1 V every 50 ms = 2 V/s. Also used for every
                  ramp down (output off, fault, shutdown).
    tolerance_mT  per axis; the VI's "accuracy" was 0.5 mT.
    stable_time_s every axis must stay inside tolerance this long for field_stable.
    settle_timeout_s  how long a scan waits for field_stable before giving up
                  (published in describe; the VI waited at most 10 s).
    """

    kp_V_per_mT: float = 0.02
    ki_V_per_mT_s: float = 0.4
    ff_mT_per_V: float = 20.0
    slew_V_per_s: float = 2.0
    tolerance_mT: float = 0.5
    stable_time_s: float = 0.3
    settle_timeout_s: float = 30.0
    loop_hz: float = 50.0
    energize_on_start: bool = True


@dataclass
class Interlock:
    """Safety: cooling water and (optionally) coil temperature.

    water_bypass  run without the water flow switch. DANGER: the coils can
                  overheat. Also settable per launch with run_service.py --bypass-water.
    temp_monitor  fault when any temperature exceeds max_temp_C (off in the VI).
    """

    water_bypass: bool = False
    temp_monitor: bool = False
    max_temp_C: float = 40.0          # "too hot" in the VI


@dataclass
class Limits:
    """Hard envelope. Setpoints outside are clamped and a warn event says so."""

    field_max_mT: float = 180.0       # |B|; the Hall probes read about +-186 mT
    angle_min_deg: float = -360.0
    angle_max_deg: float = 360.0
    ao_limit_V: float = 10.0          # the PI output is clamped to +-this


@dataclass
class Sim:
    """The simulated magnet (backends/sim.py). Ignored by the real backend.

    Numbers chosen to look like a small air-cooled-ish vector magnet: a coil
    current that lags the drive, a little hysteresis, a little X/Y cross-talk,
    and slightly different gains on the two axes so the feed-forward is not
    perfect and the PI has real work to do.
    """

    tau_s: float = 0.08               # coil L/R lag
    gain_x_mT_per_V: float = 20.0
    gain_y_mT_per_V: float = 19.4
    hysteresis_mT: float = 0.15       # half-width of the direction-dependent offset
    crosstalk: float = 0.02           # fraction of the other axis seen by each probe
    noise_mT: float = 0.05            # rms, per averaged Hall read
    water_ok: bool = True
    ambient_C: float = 25.0
    heating_C_per_V2: float = 0.15    # steady-state rise per volt squared of drive
    thermal_tau_s: float = 60.0


@dataclass
class UI:
    """GUI preferences. `theme` is applied at launch (dark or light)."""

    theme: str = "dark"


@dataclass
class Config:
    """The whole configuration, one object to pass around."""

    hardware: Hardware = None
    hall: Hall = None
    temperature: Temperature = None
    control: Control = None
    interlock: Interlock = None
    limits: Limits = None
    sim: Sim = None
    ui: UI = None

    def __post_init__(self):
        # dataclasses can't use a mutable default directly, so fill in here.
        self.hardware = self.hardware or Hardware()
        self.hall = self.hall or Hall()
        self.temperature = self.temperature or Temperature()
        self.control = self.control or Control()
        self.interlock = self.interlock or Interlock()
        self.limits = self.limits or Limits()
        self.sim = self.sim or Sim()
        self.ui = self.ui or UI()

    # ---- plain-text persistence (INI format, human-editable) --------------
    # A NEW GROUP must be added here AND in __post_init__ (docs/DEVELOPER_NOTES.md
    # gotcha #4). protocol.config_to_dict uses asdict(), so the wire follows.

    _GROUPS = {
        "hardware": Hardware,
        "hall": Hall,
        "temperature": Temperature,
        "control": Control,
        "interlock": Interlock,
        "limits": Limits,
        "sim": Sim,
        "ui": UI,
    }

    def save(self, path: str) -> None:
        parser = configparser.ConfigParser()
        for name in self._GROUPS:
            parser[name] = {k: str(v) for k, v in asdict(getattr(self, name)).items()}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# mag2d-control configuration -- edit values, keep keys.\n")
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
                if f.name in section:
                    # `from __future__ import annotations` makes f.type the
                    # STRING "float"/"bool", so everything goes through _cast.
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
