"""Configuration for the 2D piezo stage (Jena d-Drive + PXY-200).

Follows the project blueprint (§4 of INSTRUMENT_MODULE_GUIDE.md): every tunable
number lives on a small ``@dataclass`` grouped by concern, and the whole thing
is persisted as a plain-text INI file via :mod:`configparser`.

WHY dataclasses + INI (and not, say, a big dict or JSON)?
  * dataclasses give you attribute access with editor autocomplete and a clear,
    typed "here is exactly what is configurable" contract.
  * INI is human-readable and hand-editable in any text editor, which is handy
    in the lab when you just want to bump a limit without opening Python.

The stage has TWO piezo axes (X, Y) driven by a piezosystem jena d-Drive
digital controller.  All positions are in MICROMETRES (um) and velocities in
um/s.

Two things make a piezo stage different from the coarse stepper stage:

  1. Loop mode.  Each axis can run CLOSED-LOOP (a strain-gauge sensor feeds a
     servo inside the controller, so the commanded position is the *true*
     position -- hysteresis-free but the usable travel is a bit smaller) or
     OPEN-LOOP (you command a drive level; motion is fast and high-resolution
     but subject to piezo hysteresis/creep, and the full travel is available).
     The PXY-200 gives ~200 um open-loop and ~160 um closed-loop per axis.

  2. Velocity.  A piezo normally jumps to a new setpoint as fast as it can.  To
     move at a controlled speed we either use the controller's native SLEW-RATE
     limiter or a SOFTWARE RAMP that walks the setpoint there in small steps
     (see :mod:`piezo.piezo`).  ``ramp_mode`` selects which.
"""

# `from __future__ import annotations` turns every annotation into a *string* at
# runtime.  That is exactly why the INI loader below routes each value through
# `_cast(raw, field.type)` using the type *name* -- see load_config().
from __future__ import annotations

import configparser
from dataclasses import asdict, dataclass, fields

# Axis order is fixed everywhere in the package: index 0=X, 1=Y.
AXES = ("X", "Y")

# The three ways to move at a controlled speed (``Motion.ramp_mode``).
RAMP_MODES = ("hardware", "software", "off")


# --------------------------------------------------------------------------- #
# Config groups (one @dataclass per concern)
# --------------------------------------------------------------------------- #
@dataclass
class Motion:
    """Default motion parameters + how velocity is applied."""

    # Target velocity per axis, um/s.  Used as the controller SLEW RATE in
    # "hardware" ramp mode and as the ramp speed in "software" mode.
    vel_x: float = 50.0
    vel_y: float = 50.0

    # How a move honours the velocity above:
    #   "hardware" -> push vel to the controller's slew-rate limiter, then write
    #                 the final setpoint ONCE; the hardware rate-limits.
    #   "software" -> the brain walks the setpoint from here to the target in
    #                 small steps at ``ramp_hz`` (works even if the hardware
    #                 slew-rate isn't trusted, and gives an identical feel in
    #                 the simulator).  This is the "come up with a ramp" path.
    #   "off"      -> jump straight to the setpoint as fast as the piezo allows.
    ramp_mode: str = "software"

    # Software-ramp update rate (steps per second).  50 Hz is smooth and light.
    ramp_hz: float = 50.0

    # Default jog step used by the GUI +/- buttons, um.
    jog_step: float = 1.0

    # Loop mode each axis starts in (True = closed loop, needs the SG sensor).
    closed_loop_x: bool = True
    closed_loop_y: bool = True


@dataclass
class Limits:
    """The SAFETY ENVELOPE.  The brain clamps every target/param to this.

    Travel depends on the loop mode: closed-loop travel is smaller than
    open-loop travel (the sensor eats some range), so we keep both ceilings and
    the brain picks the right one for each axis' current mode.
    """

    # Lowest commandable position, um (a piezo can't retract past 0).
    travel_min: float = 0.0
    # Full open-loop travel per axis, um (PXY-200: 200 um).
    travel_max_ol: float = 200.0
    # Usable closed-loop travel per axis, um (PXY-200 SG: 160 um).
    travel_max_cl: float = 160.0
    # Velocity ceiling, um/s, so a fat-fingered speed can't be commanded.
    max_velocity: float = 2000.0
    # Master switch: set False to disable clamping (e.g. for a special stage).
    enforce: bool = True


@dataclass
class Relative:
    """Per-axis RELATIVE ORIGIN (device um).

    The "zero here" working origin for relative moves -- like the zero button on
    a bench DRO.  ``move_relative(axis, V)`` targets ``rel_* + V``, and
    ``move_relative(axis, 0)`` returns to the zeroed point.
    """

    rel_x: float = 0.0
    rel_y: float = 0.0


@dataclass
class UI:
    """Front-panel appearance.  Startup-only (applied when the GUI launches)."""

    # "dark" (default) or "light".  See piezo.apps.theme for the palettes.
    theme: str = "dark"


@dataclass
class Hardware:
    """How to reach the d-Drive controller (USB virtual COM port)."""

    # Serial port the d-Drive shows up as.  Windows: "COM3"; Linux: "/dev/ttyUSB0".
    port: str = "COM3"
    # Serial line settings.  >>> VERIFY against your d-Drive (see ddrive.py). <<<
    baud: int = 115200
    # Which controller channel drives each logical axis (0-based here).
    ch_x: int = 0
    ch_y: int = 1
    # Convenience flag: physically swap X and Y without rewiring.
    swap_xy: bool = False
    units: str = "um"


@dataclass
class Config:
    """Top-level config: one of each group.

    dataclasses cannot have mutable defaults, so the groups are created in
    ``__post_init__`` rather than as field defaults.
    """

    motion: Motion = None
    limits: Limits = None
    relative: Relative = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        if self.motion is None:
            self.motion = Motion()
        if self.limits is None:
            self.limits = Limits()
        if self.relative is None:
            self.relative = Relative()
        if self.hardware is None:
            self.hardware = Hardware()
        if self.ui is None:
            self.ui = UI()


# --------------------------------------------------------------------------- #
# Small typed accessors (so the rest of the code never indexes by hand)
# --------------------------------------------------------------------------- #
def axis_velocity(cfg: Config, axis: int) -> float:
    return (cfg.motion.vel_x, cfg.motion.vel_y)[axis]


def set_axis_velocity(cfg: Config, axis: int, value: float) -> None:
    setattr(cfg.motion, ("vel_x", "vel_y")[axis], value)


def axis_closed_loop_default(cfg: Config, axis: int) -> bool:
    return (cfg.motion.closed_loop_x, cfg.motion.closed_loop_y)[axis]


def axis_channel(cfg: Config, axis: int) -> int:
    chans = [cfg.hardware.ch_x, cfg.hardware.ch_y]
    if cfg.hardware.swap_xy:
        chans[0], chans[1] = chans[1], chans[0]
    return chans[axis]


def axis_rel_origin(cfg: Config, axis: int) -> float:
    return (cfg.relative.rel_x, cfg.relative.rel_y)[axis]


def set_axis_rel_origin(cfg: Config, axis: int, value: float) -> None:
    setattr(cfg.relative, ("rel_x", "rel_y")[axis], value)


def travel_max(cfg: Config, closed_loop: bool) -> float:
    """The upper travel limit for the given loop mode (um)."""
    return cfg.limits.travel_max_cl if closed_loop else cfg.limits.travel_max_ol


# --------------------------------------------------------------------------- #
# INI persistence
# --------------------------------------------------------------------------- #
def _sections(cfg: Config) -> dict[str, object]:
    """Map INI section name -> the dataclass instance that fills it."""
    return {
        "Motion": cfg.motion,
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
    # ramp_mode is free text in the INI -- keep it valid.
    if cfg.motion.ramp_mode not in RAMP_MODES:
        cfg.motion.ramp_mode = "software"
    # theme is free text too -- keep it to a known palette name.
    if str(cfg.ui.theme).lower() not in ("dark", "light"):
        cfg.ui.theme = "dark"
    return cfg
