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
    """The field loop: a calibrated JUMP, then a PI trim, then a FREEZE.

    This is clMag-control's philosophy on two axes, and it is the one thing that
    makes this module different from mag2d-control. Per axis, on a new setpoint:

      1. look up the drive voltage for a field `field_step_mT` SHORT of the
         target, on the hysteresis leg that matches the direction of approach,
         and ramp there (slew-limited). The calibration does the coarse work.
      2. trim the remaining couple of millitesla with a PI whose correction may
         only push in the direction of approach, so the field never reverses and
         never swaps hysteresis branch mid-approach.
      3. the moment the error is inside tolerance_mT / 2, FREEZE the output and
         leave it alone. Holding the drive perfectly still is what stops the
         limit cycle: a PI that keeps nudging keeps flipping the direction of
         travel, and each flip moves the field by the hysteresis width.

    ff_mT_per_V   the UNCALIBRATED fallback: with no calibration loaded the jump
                  uses B / ff_mT_per_V instead of a measured curve. 20 mT/V is
                  the SIMULATOR's gain -- MEASURE it on the magnet (set 1 V, read
                  the field). Run a calibration and this stops being used.
    kp, ki        the trim gains, SIM-TUNED. Small on purpose: the jump has
                  already done ~95 % of the work. Retune on the real magnet.
    field_step_mT how far the jump deliberately UNDERSHOOTS (clMag uses 2 mT).
                  Bigger = a longer trim but a more certain hysteresis branch.
    jump_settle_s the LONGEST the seek will hold the jump voltage before the
                  trim may look at the field. Normally it ends earlier, as soon
                  as the field has stopped moving (settle_rate_mT_per_s): a wait
                  long enough for a 100 mT jump is pure waste after a 1 mT one.
                  Keep it comfortably above the coil's arrival (~4 tau) so it
                  really is only a backstop.
    settle_rate_mT_per_s, settle_window_s
                  the field counts as ARRIVED when it moves by less than
                  settle_rate over a window of settle_window_s. The window is
                  what makes it usable: differentiating a 0.05 mT rms probe tick
                  by tick at 50 Hz gives ~2.5 mT/s of pure noise, over 0.1 s only
                  ~0.7 mT/s. Set the rate to a few times that noise floor.
    jump_slew_V_per_s
                  the rate of the calibrated JUMP -- a hardware limit (what the
                  amplifier and coils tolerate), not a control choice. Lukas:
                  10 V/s on this magnet. It is used ONLY for the open-loop jump
                  of a CALIBRATED seek, where the destination voltage is known
                  and the jump deliberately stops short. Everything else (ramp
                  down, faults, the calibration sweep, and the jump when there is
                  no calibration) keeps the conservative slew_V_per_s, because a
                  fast ramp toward a GUESSED voltage is how you overshoot: at
                  10 V/s the always-on PI of mag2d-control overshoots a 100 mT
                  step by 3.7 mT, the calibrated jump by 0.24 mT.
    trim_slew_V_per_s  the trim's OWN rate limit, far slower than slew_V_per_s.
                  The coils lag the drive, so when the freeze latches the field
                  is still coasting by about rate * tau; at the jump's 2 V/s
                  (40 mT/s) that coast is several millitesla and the trim skids
                  past the target. 0.08 V/s is 1.6 mT/s, a coast of about a
                  tenth of a millitesla. It buys accuracy with about a second
                  per point.
    seek_margin_V, seek_margin_frac
                  how far past the calibration's own answer the trim may push:
                  seek_margin_V + seek_margin_frac * |target volts|. That bounds
                  any overshoot whatever the gains do. A measured curve is good
                  to well under a percent and never reaches the cap; the
                  uncalibrated straight line can be several percent out, which
                  is what the fraction is there for.
    freeze_enabled  the freeze itself. Turn it OFF and this module regulates
                  exactly like mag2d-control -- a PI that never stops. On a
                  magnet with real hysteresis that dithers; tests/test_freeze.py
                  measures both and is the reason the freeze exists.
    slew_V_per_s  the old VI stepped 0.1 V every 50 ms = 2 V/s. Also used for
                  every ramp down (output off, fault, shutdown) and for the
                  calibration sweep.
    tolerance_mT  per axis; the VI's "accuracy" was 0.5 mT.
    stable_time_s every axis must stay inside tolerance this long for field_stable.
    settle_timeout_s  how long a scan waits for field_stable before giving up
                  (published in describe; the VI waited at most 10 s).
    """

    # Swept on the simulator 2026-09-20 (kp 0.01-0.08 x ki 0.2-1.5 x trim slew
    # 0.04-4 V/s). kp 0.04 / ki 0.8 / trim 2 V/s settles ~0.2 s faster, but a
    # trim that fast is no longer "the slow one": it changes what the no-freeze
    # comparison in tests/test_freeze.py is comparing, and on a magnet with a
    # longer tau it would coast past the band. Left conservative on purpose;
    # revisit with the real tau in hand.
    kp_V_per_mT: float = 0.02
    ki_V_per_mT_s: float = 0.4
    ff_mT_per_V: float = 20.0
    field_step_mT: float = 2.0
    jump_settle_s: float = 0.60          # a CAP now; the field usually arrives first
    settle_rate_mT_per_s: float = 1.5
    settle_window_s: float = 0.10
    jump_slew_V_per_s: float = 10.0      # measured on the magnet: what it tolerates
    # The coast when the trim stops is about trim_slew * tau, so this number is
    # really "how much overshoot will you accept": 0.08 V/s = 1.6 mT/s, and with
    # the simulator's tau of 0.08 s that coast is ~0.13 mT, half the freeze band.
    # On the real magnet, measure tau and scale this with it -- a slower coil
    # needs a slower trim for the same landing.
    trim_slew_V_per_s: float = 0.08
    seek_margin_V: float = 0.10
    seek_margin_frac: float = 0.05
    freeze_enabled: bool = True
    slew_V_per_s: float = 2.0
    tolerance_mT: float = 0.5
    stable_time_s: float = 0.3
    settle_timeout_s: float = 30.0
    loop_hz: float = 50.0
    energize_on_start: bool = True


@dataclass
class Stabilizer:
    """The slow long-term trim, active only while the field is held (STABLE/HOLD).

    The freeze deliberately stops correcting, so nothing else would answer a slow
    drift (the coils warm up, the Hall probes drift). This puts one small nudge
    on the output every `period_s`, and only when the error has grown past
    `deadband_mT` -- inside the deadband it does nothing at all, because a
    correction that is not needed is just another direction flip.

    Sized so it can never turn into a second, fighting control loop but can
    still outrun a drift: with the defaults one nudge moves the field by
    `gain * ff` = 0.6 mT per millitesla of error, at most once a second, and
    never more than max_step_V (1 mT) at a time.

    KNOWN PROBLEM, measured on the simulator 2026-09-20 -- do not trust this
    on a magnet with a square hysteresis loop until it is fixed:

      * the deadband equals the freeze's own parking error (tolerance/2), so the
        stabilizer fires on where the trim happened to stop, not on any drift;
      * its nudge REVERSES the drive, which drags the hysteresis branch by up to
        2h (0.8 mT on the sim) -- far more than the ~0.2 mT it meant to correct;
      * that pushes the field out of the band, and the re-seek that follows backs
        off by field_step and costs a ~5 mT excursion (1 re-seek and 2.28 V of
        drive travel in a 30 s hold).

    Raising the deadband to 0.40 removes THAT, but only by waiting longer: the
    next nudge flips the branch just the same, and a drift trial then ends WORSE
    with the stabilizer on than off (0.68 vs 0.40 mT). The real fix is to correct
    only in the approach direction and let a (cheap) re-seek handle drift the
    other way. Until then the numbers below are the tested ones.
    """

    enabled: bool = True
    period_s: float = 1.0
    gain_V_per_mT: float = 0.03
    deadband_mT: float = 0.25
    max_step_V: float = 0.05


@dataclass
class CalibrationCfg:
    """Where calibrations live and what a fresh sweep looks like.

    directory   relative names are relative to the PROJECT root, so
                "Calibrations" means the same folder wherever the service was
                launched from.
    load_newest_on_start  pick up the most recent *.json at start. Without a
                calibration the module still runs, on ff_mT_per_V, and says so.
    auto_save   write every freshly measured calibration to `directory` at once.
                A measurement that takes a minute of magnet time should not be
                lost because nobody pressed Save.
    n_per_leg / dwell_s / v_max_V  the defaults offered for `calibrate`.
    """

    directory: str = "Calibrations"
    load_newest_on_start: bool = True
    auto_save: bool = True
    n_per_leg: int = 21
    # The dwell must be SEVERAL TIME CONSTANTS of the magnet, or every point is
    # recorded while the coil is still arriving -- which biases the up leg low
    # and the down leg high and quietly shrinks the measured hysteresis. 0.5 s
    # is about six times the simulator's 0.08 s; measure the real one.
    dwell_s: float = 0.5
    v_max_V: float = 5.0


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
    """Hard envelope. Setpoints outside are clamped and a warn event says so.

    NOTE: once a calibration is loaded the FIELD limit is the narrower of this
    number and what the calibration actually covers -- a promise the magnet has
    been measured to keep. See Controller.field_envelope_mT().
    """

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
    # HYSTERESIS: half-width of the play operator, i.e. the field lags the drive
    # by this much until the drive has pushed past the backlash. 0.4 mT sits at
    # the edge of the 0.5 mT tolerance band ON PURPOSE -- that is the regime
    # where an always-on PI limit-cycles and a frozen output does not, which is
    # what tests/test_freeze.py measures. mag2d's sim uses a gentler 0.15 mT.
    hysteresis_mT: float = 0.4
    # How far the COIL CURRENT (in volts-equivalent) must travel after a
    # reversal for the hysteresis branch to swap over. 1 mV is 0.02 mT of
    # anhysteretic field, so the default loop is nearly SQUARE: any reversal at
    # all moves the field by close to the full 2 * hysteresis_mT. That is the
    # caricature that reproduces what the 1-axis magnet did to clMag's PI, and
    # what tests/test_freeze.py measures. Raise it for a rounder, lazier loop
    # that barely notices small wiggles (and then an always-on PI copes fine).
    hysteresis_reversal_V: float = 0.001
    # A slow zero drift added to both probes (coils warming, probe offset
    # creeping). It is what the long-term stabilizer exists to remove; 0 by
    # default so it does not confuse the other tests.
    drift_mT_per_s: float = 0.0
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
    stabilizer: Stabilizer = None
    calibration: CalibrationCfg = None
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
        self.stabilizer = self.stabilizer or Stabilizer()
        self.calibration = self.calibration or CalibrationCfg()
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
        "stabilizer": Stabilizer,
        "calibration": CalibrationCfg,
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
            fh.write("# mag2dcal-control configuration -- edit values, keep keys.\n")
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
