"""Configuration for the Newport Agilis 2-axis stage on an AG-UC2 controller.

Follows the project blueprint (section 4 of INSTRUMENT_MODULE_GUIDE.md): every
tunable number lives on a small ``@dataclass`` grouped by concern, and the whole
thing is persisted as a plain-text INI file via :mod:`configparser`.

------------------------------------------------------------------------------
The defining idea of THIS module: steps are real, micrometres are an estimate
------------------------------------------------------------------------------
An Agilis actuator is a slip-stick (stick-slip) piezo motor. The AG-UC2 sends
it a sawtooth: the slow ramp drags the moving part along by friction ("stick"),
the fast edge lets the piezo snap back while the part stays put ("slip"). Every
tooth is one STEP. There is no encoder, so the only position the controller
knows is how many steps it has sent (the ``TP`` step counter).

How far one step goes is set by the STEP AMPLITUDE (``SU``, an integer 1..50,
power-up default 16). The manual is blunt about it: there is NO linear relation
between amplitude and step size, a low amplitude may not move at all, forward
and backward steps differ, and the size drifts with position, load and age. So
the micrometre language of this module rests on a MEASURED step size per axis
AND PER DIRECTION, and every measurement remembers the amplitude it was taken
at. Change the amplitude and the measurement no longer applies -- the brain says
so (``cal_valid`` in status) instead of silently reporting wrong micrometres.

    forward steps  x  um_per_step (forward)
  - backward steps x  um_per_step (backward)   = position estimate in um

THE STAGE ON THE RIG (Lukas, 2026-09-27): an AG-LS25 linear stage ("NPT-AG-LS25V"
-- Newport's "V6" suffix is the 1e-6 Torr VACUUM version, AG-LS25V6; the manual
lists no separate vertical model). Datasheet (Newport manual A824E, section 2.1):
  * travel 12 mm, mechanical hard stops AND a precision electrical limit switch
    (so PH, MV, MA, PA all work -- see the brain);
  * minimum incremental motion 0.05 um; "with default settings, the step size
    for the forward direction varies from the step size for the backward
    direction and may be larger than 50 nm";
  * absolute positioning accuracy (MA/PA) 100 um -- coarse, it is a
    limit-to-limit step count;
  * max speed > 0.5 mm/s with no axial load, > 0.2 mm/s with 1.7 N axial load;
    axial load capacity 2 N, normal load 3 N (40 mm cantilever), holding force 3 N.
If it is mounted VERTICALLY, gravity is an axial load: the step going UP is
smaller than the step going DOWN (the datasheet's speed halves at 1.7 N). That
is exactly what the per-direction calibration below already handles -- measure
both directions (``measure_step_size``) and the up/down difference is in the
numbers.
"""

# `from __future__ import annotations` turns every annotation into a *string* at
# runtime.  That is exactly why the INI loader below routes each value through
# `_cast(raw, field.type)` using the type *name* -- see load_config().
from __future__ import annotations

import configparser
from dataclasses import asdict, dataclass, fields

# Axis order is fixed everywhere in the package: index 0 = X, 1 = Y.
# The AG-UC2 drives exactly two actuators ("axis 1" and "axis 2" in its manual);
# which of them is X is a Hardware setting.
AXES = ("X", "Y")

#: SU accepts an integer 1..50 in each direction (manual, SU command).
AMPLITUDE_MIN = 1
AMPLITUDE_MAX = 50
#: The controller's power-up step amplitude (manual: "The default value after
#: power-up is 16").
AMPLITUDE_DEFAULT = 16


# --------------------------------------------------------------------------- #
# Config groups (one @dataclass per concern)
# --------------------------------------------------------------------------- #
@dataclass
class Motion:
    """Drive settings.

    ADOPTED, NOT PUSHED (rule of 2026-09-27): at start the brain READS the
    amplitudes the controller is using (SU+? / SU-?) and writes them into
    amp_*; the values from the .ini are then only what you get when you press
    "Apply" (set_amplitude / set_config) -- never written behind your back.

    The AG-UC2 has no velocity or acceleration setting for a relative move: a
    ``PR`` move always runs at the controller's own stepping rate. What you CAN
    choose is how big each step is (amplitude, per direction) and, for a jog,
    one of four fixed speeds.
    """

    # Step amplitude per axis and direction (SU), 1..50. It is NOT a length:
    # it is the height of the drive sawtooth. The power-up default is 16.
    amp_fwd_x: int = AMPLITUDE_DEFAULT
    amp_bwd_x: int = AMPLITUDE_DEFAULT
    amp_fwd_y: int = AMPLITUDE_DEFAULT
    amp_bwd_y: int = AMPLITUDE_DEFAULT
    # The "Steps: Small / Large" front-panel preset sets every amplitude to one
    # of these two values. Small = the power-up default, large = the maximum.
    small_amplitude: int = AMPLITUDE_DEFAULT
    large_amplitude: int = AMPLITUDE_MAX
    # Default jog step of the GUI +/- buttons (a relative PR move), in STEPS.
    jog_steps: int = 100
    # Speed of a CONTINUOUS jog (JA), 1..4 -- the controller's four fixed rates:
    #   1 = 5 steps/s  at the set amplitude     (fine creep)
    #   2 = 100 steps/s at MAXIMUM amplitude
    #   3 = 1700 steps/s at MAXIMUM amplitude    (coarse, fast)
    #   4 = 666 steps/s at the set amplitude
    # 2 and 3 ignore your amplitude (the controller uses 50), so their step
    # size is not the calibrated one; the step COUNT is still right.
    jog_speed: int = 1
    # Dead-man for a continuous jog: a jog ends by itself this long after the
    # last jog command, so a client that dies mid-jog cannot leave the stage
    # driving into its end stop. The GUI re-sends the jog while you hold the
    # button.
    jog_timeout_s: float = 1.5


@dataclass
class Calibration:
    """The bridge between STEPS (hardware) and MICROMETRES (your sample).

    Per axis and per direction, because a slip-stick actuator does not step
    equally both ways. ``*_bwd = 0`` means "same as forward" (all a datasheet
    tells you). ``amp_*`` records the step amplitude each number was measured
    at: a step size is only meaningful together with its amplitude. The brain
    fills these in automatically when you store a new step size.

    Default 0.05 um = the AG-LS25 datasheet's minimum incremental motion, at the
    power-up amplitude 16 (the datasheet: "may be larger than 50 nm" there).
    For scale at amplitude 50: > 0.5 mm/s at jog speed 3 (1700 steps/s, always
    amplitude 50) means >= ~0.3 um per step. # VERIFY -- MEASURE yours with the
    ``measure_step_size`` routine (limit to limit, both directions); on a
    vertical mount up and down WILL differ.
    """

    um_per_step_x: float = 0.05
    um_per_step_x_bwd: float = 0.0
    um_per_step_y: float = 0.05
    um_per_step_y_bwd: float = 0.0
    # Amplitude (SU) at which each number above was measured. 0 = unknown (the
    # brain then cannot tell whether the number still applies; it says so).
    amp_x_fwd: int = AMPLITUDE_DEFAULT
    amp_x_bwd: int = AMPLITUDE_DEFAULT
    amp_y_fwd: int = AMPLITUDE_DEFAULT
    amp_y_bwd: int = AMPLITUDE_DEFAULT


@dataclass
class Limits:
    """The SAFETY ENVELOPE.  The brain clamps every target to this.

    Travel is bounded in STEPS because that is what the open-loop controller
    actually counts. The AG-LS25 travels 12 mm = 240 000 steps at the
    datasheet's 50 nm; the datum can sit anywhere along it, so the counter can
    legitimately reach +/-240 000 from it. The stage's own limit switches and
    hard stops are the real ends; this box only stops nonsense targets.
    The LEASH (below) is the tighter, datum-referenced guard.
    """

    min_steps_x: int = -240_000
    max_steps_x: int = 240_000
    min_steps_y: int = -240_000
    max_steps_y: int = 240_000
    # Master switch: set False to disable clamping (advanced/bench use).
    enforce: bool = True
    # --- LEASH: a symmetric travel box around the DATUM (counter 0) ---------
    # When enabled it REPLACES the min/max_steps clamp above: after a Datum
    # somewhere mid-travel, the absolute numbers no longer line up with the
    # physical ends, but a box around the datum you just set does. X and Y
    # share one half-width.
    leash_enabled: bool = False
    leash_steps: int = 20000   # +/- steps from the datum (20000 ~ 1 mm at 50 nm)


@dataclass
class Relative:
    """Per-axis RELATIVE ORIGIN (in STEPS) -- a "zero here" display reference.

    The zero button on a bench DRO: it does NOT move anything and does NOT
    touch the controller's step counter (that is the Datum, ``ZP``).
    """

    rel_x: int = 0
    rel_y: int = 0


@dataclass
class UI:
    """Front-panel appearance.  Startup-only (applied when the GUI launches)."""

    theme: str = "dark"


@dataclass
class Hardware:
    """How to reach the AG-UC2 and which of its two axes is X."""

    # The virtual COM port the AG-UC2's USB shows up as, e.g. "COM5". Empty =
    # not configured; the real backend refuses to guess (the lab PC has other
    # USB-serial devices).
    port: str = ""
    # Manual section 4.5: 921600 baud, 8 data bits, no parity, 1 stop bit, no
    # flow control, commands terminated by CR LF.
    baud: int = 921600
    timeout_s: float = 0.5
    # AG-UC8 only: which channel (actuator pair 1..4) to select with CC. The
    # AG-UC2 does not know CC (it answers "unknown command"), so 0 = never send.
    channel: int = 0
    # Controller axis (1 or 2) that drives logical X / Y.
    axis_x: int = 1
    axis_y: int = 2
    # Convenience flag: swap X and Y without re-plugging the actuators.
    swap_xy: bool = False
    # Status poll rate: every cycle reads TP, TS for both axes and PH.
    poll_hz: float = 20.0
    # Hand the push buttons back (ML, local mode) when the service stops, so
    # the controller is usable by hand again.
    local_on_close: bool = True
    # --- the stage on the controller (AG-LS25, datasheet in the module doc) --
    # Travel between the limits, um. The step-size measurement divides this by
    # the counted steps, and MA/PA report in 1/1000 of it. # VERIFY: the
    # switch-to-switch distance may be a little shorter than the nominal 12 mm.
    travel_um: float = 12000.0
    # The AG-LS25 has an electrical limit switch; MA, PA and MV need one (a
    # stage without one would never stop an MV). Set False for a mirror mount.
    has_limit_switch: bool = True
    # At start, a jog found running (a crashed session's JA: no dead-man any
    # more) is given this long to end by itself before it is stopped.
    start_wait_s: float = 2.0
    # MA / PA interrupt the USB link "up to 2 minutes" (manual); a finite move
    # found running at start is also waited for this long.
    limit_op_timeout_s: float = 150.0


@dataclass
class Config:
    """Top-level config: one of each group.

    dataclasses cannot have mutable defaults, so the groups are created in
    ``__post_init__`` rather than as field defaults.
    """

    motion: Motion = None
    calibration: Calibration = None
    limits: Limits = None
    relative: Relative = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        self.motion = self.motion or Motion()
        self.calibration = self.calibration or Calibration()
        self.limits = self.limits or Limits()
        self.relative = self.relative or Relative()
        self.hardware = self.hardware or Hardware()
        self.ui = self.ui or UI()


# --------------------------------------------------------------------------- #
# Small typed accessors (so the rest of the code never indexes by hand)
# --------------------------------------------------------------------------- #
def _dir_name(direction: int) -> str:
    return "fwd" if direction > 0 else "bwd"


def axis_amplitude(cfg: Config, axis: int, direction: int) -> int:
    """Configured step amplitude for an axis and direction (+1 fwd, -1 bwd)."""
    return int(getattr(cfg.motion, f"amp_{_dir_name(direction)}_{AXES[axis].lower()}"))


def set_axis_amplitude(cfg: Config, axis: int, direction: int, value: int) -> None:
    setattr(cfg.motion, f"amp_{_dir_name(direction)}_{AXES[axis].lower()}", int(value))


def axis_um_per_step(cfg: Config, axis: int, direction: int = 0) -> float:
    """Configured um-per-step: `direction` +1 forward, -1 backward, 0 = mean.

    The backward number is optional (0 = "same as forward").
    """
    a = AXES[axis].lower()
    fwd = getattr(cfg.calibration, f"um_per_step_{a}")
    bwd = getattr(cfg.calibration, f"um_per_step_{a}_bwd")
    if bwd <= 0:
        return fwd
    if direction > 0:
        return fwd
    if direction < 0:
        return bwd
    return (fwd + bwd) / 2.0


def set_axis_um_per_step(cfg: Config, axis: int, value: float, direction: int = 0) -> None:
    """Store a measured step size. `direction` 0 sets BOTH (and clears the
    separate backward value), +1 forward only, -1 backward only."""
    a = AXES[axis].lower()
    if direction >= 0:
        setattr(cfg.calibration, f"um_per_step_{a}", value)
    if direction < 0:
        setattr(cfg.calibration, f"um_per_step_{a}_bwd", value)
    elif direction == 0:
        setattr(cfg.calibration, f"um_per_step_{a}_bwd", 0.0)   # one number again


def calibration_amplitude(cfg: Config, axis: int, direction: int) -> int:
    """The amplitude the step size of this axis/direction was measured at."""
    return int(getattr(cfg.calibration, f"amp_{AXES[axis].lower()}_{_dir_name(direction)}"))


def set_calibration_amplitude(cfg: Config, axis: int, direction: int, value: int) -> None:
    setattr(cfg.calibration, f"amp_{AXES[axis].lower()}_{_dir_name(direction)}", int(value))


def axis_step_limits(cfg: Config, axis: int) -> tuple[int, int]:
    a = AXES[axis].lower()
    return (int(getattr(cfg.limits, f"min_steps_{a}")),
            int(getattr(cfg.limits, f"max_steps_{a}")))


def axis_effective_limits(cfg: Config, axis: int) -> tuple[int, int]:
    """The step bounds the brain ACTUALLY clamps to for an axis.

    With the leash enabled this is a symmetric box around the datum -- the
    leash REPLACES the absolute min/max_steps limits. With the leash off it is
    the plain absolute ``(min_steps, max_steps)``.
    """
    if cfg.limits.leash_enabled:
        half = abs(int(cfg.limits.leash_steps))
        return (-half, half)
    return axis_step_limits(cfg, axis)


def axis_rel_origin(cfg: Config, axis: int) -> int:
    return int(getattr(cfg.relative, f"rel_{AXES[axis].lower()}"))


def set_axis_rel_origin(cfg: Config, axis: int, value: int) -> None:
    setattr(cfg.relative, f"rel_{AXES[axis].lower()}", int(value))


def hardware_axis(cfg: Config, axis: int) -> int:
    """Controller axis number (1 or 2) for logical axis 0 (X) / 1 (Y)."""
    hw = cfg.hardware
    if hw.swap_xy:
        axis = 1 - axis
    return int(hw.axis_x if axis == 0 else hw.axis_y)


# --------------------------------------------------------------------------- #
# INI persistence
# --------------------------------------------------------------------------- #
def _sections(cfg: Config) -> dict[str, object]:
    """Map INI section name -> the dataclass instance that fills it."""
    return {
        "Motion": cfg.motion,
        "Calibration": cfg.calibration,
        "Limits": cfg.limits,
        "Relative": cfg.relative,
        "Hardware": cfg.hardware,
        "UI": cfg.ui,
    }


def _cast(raw: str, type_name: str):
    """Turn an INI string back into the right Python type.

    The bool case is the classic trap (gotcha #3): ``bool("False")`` is True
    (any non-empty string is truthy), so we parse the text explicitly.
    """
    if type_name == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if type_name == "int":
        return int(float(raw))
    if type_name == "float":
        return float(raw)
    return raw


def save_config(cfg: Config, path: str) -> None:
    """Write the config to ``path`` as INI (one section per group)."""
    cp = configparser.ConfigParser()
    for section, obj in _sections(cfg).items():
        cp[section] = {key: str(val) for key, val in asdict(obj).items()}
    with open(path, "w", encoding="utf-8") as fh:
        cp.write(fh)


def load_config(path: str) -> Config:
    """Read an INI file written by :func:`save_config` back into a Config.

    Unknown/missing keys are ignored so an older file still loads after you add
    a new field (it just keeps that field's default).
    """
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    cfg = Config()
    for section, obj in _sections(cfg).items():
        if section not in cp:
            continue
        for fld in fields(obj):
            if fld.name in cp[section]:
                setattr(obj, fld.name, _cast(cp[section][fld.name], fld.type))
    return cfg
