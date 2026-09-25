"""Configuration for the 3D piezo-inertia stage (Thorlabs KIM101 + 3x PIA25).

Follows the project blueprint (§4 of INSTRUMENT_MODULE_GUIDE.md): every tunable
number lives on a small ``@dataclass`` grouped by concern, and the whole thing
is persisted as a plain-text INI file via :mod:`configparser`.

WHY dataclasses + INI (and not, say, a big dict or JSON)?
  * dataclasses give you attribute access with editor autocomplete and a clear,
    typed "here is exactly what is configurable" contract.
  * INI is human-readable and hand-editable in any text editor, which is handy
    in the lab when you just want to bump a limit without opening Python.

------------------------------------------------------------------------------
The defining idea of THIS module: two languages, one calibration bridge
------------------------------------------------------------------------------
A KIM101 drives PIA25 *piezo-inertia* actuators.  These are "slip-stick" motors:
they move in tiny discrete STEPS, and the controller's native language is
therefore *steps* -- how many steps, at how many steps/second, at what step
acceleration.  There is no encoder, so "position" is simply the accumulated
step count the controller has issued (open-loop).

But you think about your sample in MICROMETRES.  So every axis carries a
calibration constant ``um_per_step`` (see :class:`Calibration`).  With it the
brain converts freely between the two languages:

    steps        = round(micrometres / um_per_step)
    micrometres  = steps * um_per_step
    step_rate    = round(velocity_um_per_s / um_per_step)    (steps/s)

The physical SIZE of one step is set by the piezo drive VOLTAGE (85-125 V on a
KIM101, which changes the step by up to ~30%); see :class:`Motion.voltage_*`.
When you change the voltage you have re-calibrated the actuator, so you should
re-measure ``um_per_step`` -- the two belong together.

Datasheet anchors (VERIFY against your own actuators -- every unit differs):
  * PIA25: 25 mm travel, typical step size ~20 nm (0.02 um), adjustable ~30% by
    voltage, max step rate 2000 steps/s, top speed ~2 mm/min.
  * KIM101: 4 channels (one/two at a time), 85-125 V, step rate 1-2000 steps/s,
    step acceleration 1-100000 steps/s^2.
"""

# `from __future__ import annotations` turns every annotation into a *string* at
# runtime.  That is exactly why the INI loader below routes each value through
# `_cast(raw, field.type)` using the type *name* -- see load_config().
from __future__ import annotations

import configparser
from dataclasses import asdict, dataclass, fields

# Axis order is fixed everywhere in the package: index 0=X, 1=Y, 2=Z.
AXES = ("X", "Y", "Z")


# --------------------------------------------------------------------------- #
# Config groups (one @dataclass per concern)
# --------------------------------------------------------------------------- #
@dataclass
class Motion:
    """Default drive parameters pushed to each KIM101 channel on start.

    All in the controller's NATIVE units (steps), plus the drive voltage that
    governs the physical size of a step.
    """

    # Step rate (a.k.a. velocity) per axis, steps/s.  KIM101 range 1..2000.
    # The defaults deliberately match the SLOW + SMALL preset below, so a fresh
    # config lines up with the front-panel toggles (Movement: Slow, Steps:
    # Small) -- the safe state to power on a coarse inertial stage in.
    rate_x: float = 300.0
    rate_y: float = 300.0
    rate_z: float = 300.0
    # Step acceleration per axis, steps/s^2.  KIM101 range 1..100000.
    acc_x: float = 5000.0
    acc_y: float = 5000.0
    acc_z: float = 5000.0
    # Piezo drive voltage per axis, V.  This sets the PHYSICAL step size
    # (bigger voltage -> bigger step, up to ~30%).  KIM101 range 85..125 V.
    # Default = min (small steps) to match the Steps: Small preset.
    voltage_x: float = 85.0
    voltage_y: float = 85.0
    voltage_z: float = 85.0
    # Default jog step used by the GUI +/- buttons, in STEPS.
    jog_steps: int = 100

    # Front-panel "Movement: Fast / Slow" presets (applied to ALL axes by the
    # toggle button).  Fast/slow are a step RATE (steps/s) + acceleration
    # (steps/s^2); tune them here in Settings to taste.
    fast_rate: float = 1500.0
    fast_accel: float = 20000.0
    slow_rate: float = 300.0
    slow_accel: float = 5000.0


@dataclass
class Calibration:
    """The bridge between STEPS (hardware) and MICROMETRES (your sample).

    ``um_per_step`` = how far one step physically moves the axis, in um.  Per
    axis, because piezo-inertia step size varies from unit to unit (and with the
    drive voltage).  Default 0.02 um = 20 nm from the PIA25 datasheet -- MEASURE
    yours and put the real number here.

    ``use_px_calibration`` (2026-09-16): when a camera px/step table has been
    measured, X and Y take their step size FROM IT -- per direction and at the
    current drive voltage -- instead of the numbers above, which are one
    datasheet figure for every axis and both directions. Z always uses the
    configured value (the camera never measured it). Set False to force the
    configured numbers back.
    """

    um_per_step_x: float = 0.02
    um_per_step_y: float = 0.02
    um_per_step_z: float = 0.02
    # BACKWARD step size, if you measured one. A slip-stick actuator does not
    # step equally both ways (this rig: Y+ 18.9 nm, Y- 31.9 nm), and with no
    # camera these are the only place to say so. 0 = same as forward, which is
    # what a datasheet gives you.
    um_per_step_x_bwd: float = 0.0
    um_per_step_y_bwd: float = 0.0
    um_per_step_z_bwd: float = 0.0
    use_px_calibration: bool = True
    # Camera-frame calibration of X/Y (image px per step, per voltage and
    # direction; see kim/pxcal.py). A relative path is resolved against the
    # kim-control project folder.
    px_file: str = "px_calibration.json"


@dataclass
class Limits:
    """The SAFETY ENVELOPE.  The brain clamps every target/param to this.

    Travel is bounded in STEPS because that is what the open-loop controller
    actually counts.  The default max (1,250,000 steps) is 25 mm of PIA25 travel
    at the 0.02 um/step default calibration -- re-derive it if you re-calibrate
    (max_steps ~= 25000 um / um_per_step).
    """

    # Travel range per axis, in STEPS.  SYMMETRIC about the datum by default
    # (+/- 25 mm of PIA25 travel at 0.02 um/step): the datum is arbitrary on an
    # open-loop stage, so you must be able to move either side of it.  The LEASH
    # is the tighter, datum-referenced runout guard; these are the coarse
    # backstop.
    min_steps_x: int = -1_250_000
    max_steps_x: int = 1_250_000
    min_steps_y: int = -1_250_000
    max_steps_y: int = 1_250_000
    min_steps_z: int = -1_250_000
    max_steps_z: int = 1_250_000
    # Parameter ceilings (KIM101 hardware limits) so a fat-fingered value can't
    # be pushed past what the controller accepts.
    max_step_rate: float = 2000.0      # steps/s
    max_acceleration: float = 100000.0  # steps/s^2
    min_voltage: float = 85.0           # V
    max_voltage: float = 125.0          # V
    # Master switch: set False to disable clamping (advanced/bench use).
    enforce: bool = True

    # --- LEASH: a symmetric travel box around the DATUM (runout guard) -------
    # When enabled, an axis may only move within +/- this many steps of the
    # datum (the counter's 0).  This is the recommended runout protection for an
    # open-loop inertial stage: after you press Datum somewhere mid-travel the
    # absolute step limits above no longer line up with the physical ends, so
    # the leash -- referenced to the datum you just set -- is what actually keeps
    # the stage from driving off the end.  X and Y share one range; Z has its
    # own.  When the leash is ON it REPLACES the min/max_steps clamp above.
    leash_enabled: bool = False
    leash_xy: int = 50000   # +/- steps from datum for X and Y (50000 ~ 1 mm @ 20 nm)
    leash_z: int = 50000    # +/- steps from datum for Z


@dataclass
class Relative:
    """Per-axis RELATIVE ORIGIN (in STEPS) -- a "zero here" display reference.

    This is the zero button on a bench DRO: it does NOT move anything and does
    NOT touch the controller's own step counter.  It only re-references the
    read-out so the panel can show how far you have travelled *from here*:

        relative_steps = position_steps - rel_*        (and *um via calibration)

    Kept separate from the hardware counter (see :meth:`Kim.zero_counter`) on
    purpose: this one is a throwaway reference you re-zero whenever you like.
    """

    rel_x: int = 0
    rel_y: int = 0
    rel_z: int = 0


@dataclass
class UI:
    """Front-panel appearance.  Startup-only (applied when the GUI launches)."""

    # "dark" or "light" -- selected once at startup (no live toggle).
    theme: str = "dark"


@dataclass
class Hardware:
    """How to reach the KIM101 and which channel drives each axis."""

    # Kinesis serial number of the KIM101 (starts with 97... on real hardware).
    # "" = use the ONE KIM101 connected to this PC (serials start with 97);
    # set it when several are plugged in. Not a lab serial here: this default
    # is public, and the rig has exactly one KIM101.
    serial: str = ""
    # Which controller channel (1..4) drives each logical axis.
    ch_x: int = 1
    ch_y: int = 2
    ch_z: int = 3
    # Convenience flag: physically swap X and Y channels without rewiring.
    swap_xy: bool = False
    units: str = "step"


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
        if self.motion is None:
            self.motion = Motion()
        if self.calibration is None:
            self.calibration = Calibration()
        if self.limits is None:
            self.limits = Limits()
        if self.relative is None:
            self.relative = Relative()
        if self.hardware is None:
            self.hardware = Hardware()
        self.ui = self.ui or UI()


# --------------------------------------------------------------------------- #
# Small typed accessors (so the rest of the code never indexes by hand)
# --------------------------------------------------------------------------- #
def axis_rate(cfg: Config, axis: int) -> float:
    return (cfg.motion.rate_x, cfg.motion.rate_y, cfg.motion.rate_z)[axis]


def axis_acceleration(cfg: Config, axis: int) -> float:
    return (cfg.motion.acc_x, cfg.motion.acc_y, cfg.motion.acc_z)[axis]


def axis_voltage(cfg: Config, axis: int) -> float:
    return (cfg.motion.voltage_x, cfg.motion.voltage_y, cfg.motion.voltage_z)[axis]


def axis_um_per_step(cfg: Config, axis: int, direction: int = 0) -> float:
    """Configured um-per-step: `direction` +1 forward, -1 backward, 0 = mean.

    The backward number is optional (0 = "same as forward"), so a setup that
    only knows the datasheet figure behaves exactly as before.
    """
    fwd = (
        cfg.calibration.um_per_step_x,
        cfg.calibration.um_per_step_y,
        cfg.calibration.um_per_step_z,
    )[axis]
    bwd = (
        cfg.calibration.um_per_step_x_bwd,
        cfg.calibration.um_per_step_y_bwd,
        cfg.calibration.um_per_step_z_bwd,
    )[axis]
    if bwd <= 0:
        return fwd
    if direction > 0:
        return fwd
    if direction < 0:
        return bwd
    return (fwd + bwd) / 2.0


def axis_step_limits(cfg: Config, axis: int) -> tuple[int, int]:
    lo = (cfg.limits.min_steps_x, cfg.limits.min_steps_y, cfg.limits.min_steps_z)[axis]
    hi = (cfg.limits.max_steps_x, cfg.limits.max_steps_y, cfg.limits.max_steps_z)[axis]
    return lo, hi


def axis_leash_half(cfg: Config, axis: int) -> int:
    """The +/- leash half-range (steps) for an axis: leash_xy for X/Y, leash_z for Z."""
    half = cfg.limits.leash_z if axis == 2 else cfg.limits.leash_xy
    return abs(int(half))


def axis_effective_limits(cfg: Config, axis: int) -> tuple[int, int]:
    """The step bounds the brain ACTUALLY clamps to for an axis.

    With the leash enabled this is a symmetric box around the datum
    ``(-half, +half)`` -- the leash REPLACES the absolute min/max_steps limits
    (which are in the wrong frame once you datum mid-travel).  With the leash
    off it is the plain absolute ``(min_steps, max_steps)``.
    """
    if cfg.limits.leash_enabled:
        half = axis_leash_half(cfg, axis)
        return (-half, half)
    return axis_step_limits(cfg, axis)


def axis_rel_origin(cfg: Config, axis: int) -> int:
    return (cfg.relative.rel_x, cfg.relative.rel_y, cfg.relative.rel_z)[axis]


def set_axis_rate(cfg: Config, axis: int, value: float) -> None:
    setattr(cfg.motion, ("rate_x", "rate_y", "rate_z")[axis], value)


def set_axis_acceleration(cfg: Config, axis: int, value: float) -> None:
    setattr(cfg.motion, ("acc_x", "acc_y", "acc_z")[axis], value)


def set_axis_voltage(cfg: Config, axis: int, value: float) -> None:
    setattr(cfg.motion, ("voltage_x", "voltage_y", "voltage_z")[axis], value)


def set_axis_um_per_step(cfg: Config, axis: int, value: float, direction: int = 0) -> None:
    """Store a measured step size. `direction` 0 sets BOTH (and clears the
    separate backward value), +1 forward only, -1 backward only."""
    fwd = ("um_per_step_x", "um_per_step_y", "um_per_step_z")[axis]
    bwd = ("um_per_step_x_bwd", "um_per_step_y_bwd", "um_per_step_z_bwd")[axis]
    if direction >= 0:
        setattr(cfg.calibration, fwd, value)
    if direction < 0:
        setattr(cfg.calibration, bwd, value)
    elif direction == 0:
        setattr(cfg.calibration, bwd, 0.0)      # one number again: both ways alike


def set_axis_rel_origin(cfg: Config, axis: int, value: int) -> None:
    setattr(cfg.relative, ("rel_x", "rel_y", "rel_z")[axis], value)


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

    Because of ``from __future__ import annotations`` the field type arrives as
    a NAME ("bool", "int", "float", "str"), not the type object.

    The bool case is the classic trap: ``bool("False")`` is True (any non-empty
    string is truthy), so we parse the text explicitly.
    """
    if type_name == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if type_name == "int":
        return int(raw)
    if type_name == "float":
        return float(raw)
    return raw  # str (or anything we don't specially handle)


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
                raw = cp[section][fld.name]
                setattr(obj, fld.name, _cast(raw, fld.type))
    return cfg
