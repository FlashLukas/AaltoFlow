"""Configuration for the DDR25/M rotation stage.

Follows the project blueprint (section 4 of INSTRUMENT_MODULE_GUIDE.md): every
tunable number lives on a small ``@dataclass`` grouped by concern, and the
whole thing is persisted as a plain-text INI file via :mod:`configparser`.

ONE axis, in DEGREES. The DDR25 is a direct-drive (brushless servo) rotation
stage with an optical encoder and no end stops: it can turn forever in either
direction. The K-Cube counts encoder ticks without wrapping (4000 counts per
degree with the DDR25 scale, so the int32 counter covers ~1500 turns), which
means the controller's own position is a CONTINUOUS angle: 370 deg is one turn
plus 10 deg, not the same as 10 deg. What a user means by "go to 10 deg" is a
policy decision, and it is the ``wrap`` setting below:

  literal   the angle is a plain linear coordinate inside [min_deg, max_deg].
            350 -> 10 turns BACK by 340 deg. Predictable, safe when cables or
            fibres are attached to whatever sits on the stage, and the only
            mode in which a fly scan across 0/360 makes sense. DEFAULT.
  shortest  angles are taken modulo 360 and the stage takes the shorter way
            round: 350 -> 10 turns +20 deg. For a polariser or a free sample.
  positive  modulo 360, always turning in the + direction (approach every
            angle from the same side -- a habit from geared stages; the DDR25
            has no backlash, so this is a preference, not a need).
  negative  modulo 360, always turning in the - direction.
"""

# `from __future__ import annotations` turns every annotation into a *string* at
# runtime.  That is exactly why the INI loader below routes each value through
# `_cast(raw, field.type)` using the type *name* -- see load_config().
from __future__ import annotations

import configparser
from dataclasses import asdict, dataclass, fields

#: The allowed values of ``Motion.wrap`` (see the module docstring).
WRAP_POLICIES = ("literal", "shortest", "positive", "negative")


# --------------------------------------------------------------------------- #
# Config groups (one @dataclass per concern)
# --------------------------------------------------------------------------- #
@dataclass
class Motion:
    """Motion defaults pushed to the controller on start."""

    # Profile velocity, deg/s. Gentle default: a sample holder spinning at the
    # stage's 1800 deg/s would throw anything loosely mounted.
    velocity: float = 30.0
    # Profile acceleration, deg/s^2.
    acceleration: float = 60.0
    # Default step of the GUI's jog buttons, deg.
    jog_step: float = 5.0
    # How an absolute angle becomes a controller position (module docstring).
    wrap: str = "literal"
    # Absolute moves are REFUSED until the stage has been homed since power-up:
    # before that the encoder's zero is wherever the stage happened to be when
    # the controller was switched on, and "45 deg" means nothing.
    require_home: bool = True
    # Home automatically when the service starts. Off by default: homing turns
    # the stage up to a full revolution, which nobody should get unannounced.
    home_on_start: bool = False


@dataclass
class Limits:
    """The SAFETY ENVELOPE. The brain clamps every request to this."""

    # Travel of the ANGLE (after the display zero) in literal mode, deg. The
    # stage itself has no end stops; this box is for whatever is attached to
    # it (cables, fibres). In the modulo-360 modes these bounds are not used.
    min_deg: float = -360.0
    max_deg: float = 720.0
    # Parameter ceilings. The DDR25 datasheet quotes up to 1800 deg/s (5 rev/s)
    # and 7200 deg/s^2 with a light load (VERIFY with the mounted load).
    max_velocity: float = 720.0
    max_acceleration: float = 3600.0
    # Master switch: False disables clamping (you get what you type).
    enforce: bool = True


@dataclass
class Frame:
    """The display zero ("zero here"), in controller degrees.

    angle = controller_position - zero_deg. Kept in config so it survives a
    restart -- which also means a coordinator's set_config that pushes this
    group overwrites it (gotcha #5); push only the groups you mean to change.
    """

    zero_deg: float = 0.0


@dataclass
class Hardware:
    """How to reach the K-Cube and how to interpret its units."""

    # Kinesis serial number of the K-Cube brushless controller (KBD101 serials
    # start with 28... -- VERIFY). A placeholder; the lab PC's real one goes in its .ini.
    serial: str = "28000001"
    # pylablib unit scaling. The KBD101 cannot report which stage is attached,
    # so "stage" (autodetect) does NOT work for it: name the stage. "DDR25"
    # makes pylablib use 1 440 000 counts per 360 deg and report degrees.
    scale: str = "DDR25"
    # Rate of the poll thread that reads position / moving / homed, Hz.
    poll_hz: float = 20.0
    # A move counts as finished when the controller says "not moving" AND the
    # position is within this of the target (deg) -- or the grace time below
    # has passed (a stop, or a target the servo cannot reach exactly).
    in_position_tol_deg: float = 0.01
    # For this long after a move command a "not moving" reading is not
    # believed: the controller may not have STARTED yet (VERIFY on hardware).
    start_grace_s: float = 0.15
    # Simulator only: the angle the stage "powered up" at (un-homed).
    sim_start_deg: float = 137.0


@dataclass
class UI:
    """GUI preferences. ``theme`` ("dark" or "light") is applied at launch;
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
    frame: Frame = None
    hardware: Hardware = None
    ui: UI = None

    def __post_init__(self):
        if self.motion is None:
            self.motion = Motion()
        if self.limits is None:
            self.limits = Limits()
        if self.frame is None:
            self.frame = Frame()
        if self.hardware is None:
            self.hardware = Hardware()
        if self.ui is None:
            self.ui = UI()


def wrap_policy(cfg: Config) -> str:
    """The active wrap policy, falling back to literal on a typo in the INI."""
    w = str(cfg.motion.wrap).strip().lower()
    return w if w in WRAP_POLICIES else "literal"


# --------------------------------------------------------------------------- #
# INI persistence
# --------------------------------------------------------------------------- #
def _sections(cfg: Config) -> dict[str, object]:
    """Map INI section name -> the dataclass instance that fills it.

    A NEW GROUP must be added here, in Config.__post_init__, and in
    net/protocol.py (config_to_dict + apply_config_dict) -- gotcha #4.
    """
    return {
        "Motion": cfg.motion,
        "Limits": cfg.limits,
        "Frame": cfg.frame,
        "Hardware": cfg.hardware,
        "UI": cfg.ui,
    }


def _cast(raw, type_name: str):
    """Turn an INI string (or a JSON value) back into the right Python type.

    Because of ``from __future__ import annotations`` the field type arrives as
    a NAME ("bool", "int", "float", "str"), not the type object.

    The bool case is the classic trap (gotcha #3): ``bool("False")`` is True
    (any non-empty string is truthy), so we parse the text explicitly.
    """
    if type_name == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_name == "int":
        return int(float(raw))
    if type_name == "float":
        return float(raw)
    return str(raw)


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
