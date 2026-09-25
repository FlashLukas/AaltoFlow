"""Configuration for the 3D coarse stage.

Follows the project blueprint (§4 of INSTRUMENT_MODULE_GUIDE.md): every tunable
number lives on a small ``@dataclass`` grouped by concern, and the whole thing
is persisted as a plain-text INI file via :mod:`configparser`.

WHY dataclasses + INI (and not, say, a big dict or JSON)?
  * dataclasses give you attribute access with editor autocomplete and a clear,
    typed "here is exactly what is configurable" contract.
  * INI is human-readable and hand-editable in any text editor, which is handy
    in the lab when you just want to bump a limit without opening Python.

The stage has THREE motors (X, Y, Z) driven by a Thorlabs BSC203 benchtop
stepper controller.  All positions/velocities are in *physical* units (mm,
mm/s, mm/s^2) assuming the pylablib backend is opened with ``scale="stage"``.

Coordinate model (see also stage.py):
  * DEVICE coordinates  = raw motor positions the controller actually uses.
  * LOGICAL coordinates = a user frame obtained from the device frame by
    subtracting a per-axis OFFSET and applying a 2x2 TRANSFORM to the XY plane.
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
    """Default motion parameters pushed to each motor on start."""

    # Max (target) velocity per axis, mm/s.
    vel_x: float = 2.0
    vel_y: float = 2.0
    vel_z: float = 2.0
    # Acceleration per axis, mm/s^2.
    acc_x: float = 2.0
    acc_y: float = 2.0
    acc_z: float = 2.0
    # Default jog step used by the GUI +/- buttons, mm.
    jog_step: float = 0.5
    # If True, every axis is homed automatically when the service starts.
    home_on_start: bool = False


@dataclass
class Limits:
    """The SAFETY ENVELOPE.  The brain clamps every target/param to this."""

    # Travel range per axis, mm (BSC203 + typical 25 mm actuator shown).
    min_x: float = 0.0
    max_x: float = 25.0
    min_y: float = 0.0
    max_y: float = 25.0
    min_z: float = 0.0
    max_z: float = 25.0
    # Parameter ceilings so a fat-fingered velocity can't slam the stage.
    max_velocity: float = 5.0
    max_acceleration: float = 10.0
    # Master switch: set False to disable clamping (e.g. for a special stage).
    enforce: bool = True


@dataclass
class Offsets:
    """User-set per-axis offset (mm) subtracted from the device position to
    reach the logical frame.  Think "where is my sample origin"."""

    off_x: float = 0.0
    off_y: float = 0.0
    off_z: float = 0.0


@dataclass
class Transform:
    """2x2 matrix applied to the XY plane (logical -> device).

    Layout:  [ m00  m01 ]      device_x = m00*u + m01*v + off_x
             [ m10  m11 ]      device_y = m10*u + m11*v + off_y

    Identity (the default) means logical == device (minus offsets).  Use it for
    rotating/scaling/skewing the sample coordinate system relative to the stage.
    Z is intentionally NOT transformed -- it stays a plain offset axis.
    """

    m00: float = 1.0
    m01: float = 0.0
    m10: float = 0.0
    m11: float = 1.0


@dataclass
class Relative:
    """Per-axis RELATIVE ORIGIN (device mm).

    This is the "zero here" working origin for relative-movement mode -- like
    the zero button on a bench DRO.  It is kept SEPARATE from :class:`Offsets`
    /:class:`Transform` (your calibrated sample frame) on purpose: this one is a
    quick, throwaway reference you re-zero whenever you like.

        relative_position = device_position - rel_*
        a relative move to value V  ->  device target = rel_* + V
    """

    rel_x: float = 0.0
    rel_y: float = 0.0
    rel_z: float = 0.0


@dataclass
class Hardware:
    """How to reach the BSC203 and how to interpret its units."""

    # Kinesis serial number of the BSC203 (starts with 70... on real hardware).
    serial: str = "70000001"
    # Which controller channel (bay) drives each logical axis.
    ch_x: int = 1
    ch_y: int = 2
    ch_z: int = 3
    # pylablib unit scaling: "stage" auto-picks the calibration for the mounted
    # actuator so positions come back in mm.  Override with a number if needed.
    scale: str = "stage"
    units: str = "mm"
    # Convenience flag: physically swap the X and Y channels without rewiring.
    swap_xy: bool = False


@dataclass
class UI:
    """GUI preferences.  ``theme`` ("dark" or "light") is applied at launch;
    there is no live toggle -- it is read once when the window is built."""

    theme: str = "dark"


@dataclass
class Config:
    """Top-level config: one of each group.

    dataclasses cannot have mutable defaults, so the groups are created in
    ``__post_init__`` rather than as field defaults.
    """

    motion: Motion = None
    limits: Limits = None
    offsets: Offsets = None
    transform: Transform = None
    relative: Relative = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        if self.motion is None:
            self.motion = Motion()
        if self.limits is None:
            self.limits = Limits()
        if self.offsets is None:
            self.offsets = Offsets()
        if self.transform is None:
            self.transform = Transform()
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
    return (cfg.motion.vel_x, cfg.motion.vel_y, cfg.motion.vel_z)[axis]


def axis_acceleration(cfg: Config, axis: int) -> float:
    return (cfg.motion.acc_x, cfg.motion.acc_y, cfg.motion.acc_z)[axis]


def axis_offset(cfg: Config, axis: int) -> float:
    return (cfg.offsets.off_x, cfg.offsets.off_y, cfg.offsets.off_z)[axis]


def axis_limits(cfg: Config, axis: int) -> tuple[float, float]:
    lo = (cfg.limits.min_x, cfg.limits.min_y, cfg.limits.min_z)[axis]
    hi = (cfg.limits.max_x, cfg.limits.max_y, cfg.limits.max_z)[axis]
    return lo, hi


def set_axis_velocity(cfg: Config, axis: int, value: float) -> None:
    setattr(cfg.motion, ("vel_x", "vel_y", "vel_z")[axis], value)


def set_axis_acceleration(cfg: Config, axis: int, value: float) -> None:
    setattr(cfg.motion, ("acc_x", "acc_y", "acc_z")[axis], value)


def set_axis_offset(cfg: Config, axis: int, value: float) -> None:
    setattr(cfg.offsets, ("off_x", "off_y", "off_z")[axis], value)


def axis_rel_origin(cfg: Config, axis: int) -> float:
    return (cfg.relative.rel_x, cfg.relative.rel_y, cfg.relative.rel_z)[axis]


def set_axis_rel_origin(cfg: Config, axis: int, value: float) -> None:
    setattr(cfg.relative, ("rel_x", "rel_y", "rel_z")[axis], value)


def matrix_tuple(cfg: Config) -> tuple[float, float, float, float]:
    t = cfg.transform
    return (t.m00, t.m01, t.m10, t.m11)


# Relative determinant floor for "is this 2x2 safely invertible?".
# We compare |det| against the matrix's own scale (sum of squares of entries),
# which is dimensionless, so a legitimate small-scale matrix like diag(1e-3,1e-3)
# (det = 1e-6, but perfectly invertible) passes, while a truly singular matrix
# like [[1,1],[1,1]] (det = 0) fails.  For a pure rotation the ratio is 0.5.
MATRIX_MIN_REL_DET = 1e-9


def matrix_determinant(m00: float, m01: float, m10: float, m11: float) -> float:
    return m00 * m11 - m01 * m10


def matrix_is_invertible(
    m00: float, m01: float, m10: float, m11: float, rel_tol: float = MATRIX_MIN_REL_DET
) -> tuple[bool, float]:
    """Return (ok, determinant).

    ``ok`` is False for an exactly-singular OR numerically-unusable matrix, so
    callers can refuse it before any inversion divides by (near) zero.
    """
    det = matrix_determinant(m00, m01, m10, m11)
    norm2 = m00 * m00 + m01 * m01 + m10 * m10 + m11 * m11
    if norm2 <= 0.0:  # the all-zero matrix
        return False, 0.0
    return (abs(det) / norm2) >= rel_tol, det


# --------------------------------------------------------------------------- #
# INI persistence
# --------------------------------------------------------------------------- #
def _sections(cfg: Config) -> dict[str, object]:
    """Map INI section name -> the dataclass instance that fills it."""
    return {
        "Motion": cfg.motion,
        "Limits": cfg.limits,
        "Offsets": cfg.offsets,
        "Transform": cfg.transform,
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
