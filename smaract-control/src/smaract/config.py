"""Configuration for the SmarAct linear positioner (CLL42 on an SCU controller).

Follows the project blueprint (section 4 of INSTRUMENT_MODULE_GUIDE.md): every
tunable number lives on a small ``@dataclass`` grouped by concern, and the whole
thing is persisted as a plain-text INI file via :mod:`configparser`.

WHY dataclasses + INI (and not a big dict or JSON)?
  * dataclasses give attribute access with editor autocomplete and one clear
    "here is exactly what is configurable" list.
  * INI is readable and hand-editable in any text editor, which is handy in the
    lab when you just want to widen a limit without opening Python.

The instrument is ONE closed-loop linear axis:

  * a stick-slip piezo positioner (SmarAct CLL42, a carriage on a 360 mm rail),
  * an integrated optical encoder ("LC") with DISTANCE-CODED reference marks:
    after power-up the encoder counts from wherever the carriage happened to
    be; driving over two neighbouring marks tells the controller where it is on
    the absolute scale ("physical position known" = referenced),
  * an SCU controller that closes the loop itself: we send a target, it steps
    the piezo until the encoder says "there".

All positions are in millimetres (mm), velocities in mm/s. The controller
itself thinks in integer encoder counts and in a step FREQUENCY (Hz); the
conversions live in the backends, driven by the Hardware group below.
"""

# `from __future__ import annotations` turns every annotation into a *string* at
# runtime. That is exactly why the INI loader routes each value through
# `_cast(raw, field.type)` using the type NAME -- see load_config().
from __future__ import annotations

import configparser
from dataclasses import asdict, dataclass, fields


# --------------------------------------------------------------------------- #
# Config groups (one @dataclass per concern)
# --------------------------------------------------------------------------- #
@dataclass
class Motion:
    """How moves are made."""

    # Travel speed, mm/s. The SCU has no velocity setting as such: it limits
    # the STEP FREQUENCY of the closed loop ("closed-loop max frequency"). We
    # turn mm/s into Hz with hardware.um_per_step, so this is a NOMINAL speed.
    velocity_mm_s: float = 2.0
    # How long the controller keeps actively holding the target after it got
    # there, ms. 0 = let go at once (the piezo stops dithering, no vibration,
    # the carriage stays put by friction). The SCU manual's "infinite" value is
    # to be confirmed (# VERIFY), so the GUI offers only 0..60000.
    hold_time_ms: int = 0
    # Default step used by the GUI jog buttons, mm.
    jog_step_mm: float = 0.1
    # Run find_reference automatically when the service starts. OFF by default:
    # referencing MOVES the carriage (a few mm), and nothing should move just
    # because a program was started.
    reference_on_start: bool = False
    # Refuse ABSOLUTE moves until the encoder is referenced. Before referencing
    # the position is counted from the power-on spot, so "go to 50 mm" would
    # mean something different after every power cycle -- and the soft limits
    # would not protect anything.
    require_reference: bool = True
    # A relative step made BEFORE referencing is limited to this, mm. The limits
    # cannot protect an unreferenced axis (they are absolute), so small steps
    # are the only safe thing. The rail's end stops stall a stick-slip drive
    # harmlessly, but a sample holder might not like it.
    max_unreferenced_step_mm: float = 5.0
    # A move counts as "on target" when it stopped within this distance, um.
    # The encoder resolves ~0.1 um; the closed loop typically parks within a
    # count or two.
    on_target_tol_um: float = 1.0


@dataclass
class Limits:
    """The SAFETY ENVELOPE. The brain clamps every target and speed to this.

    The travel numbers are the SOFT limits on the ABSOLUTE (referenced) scale.
    # VERIFY on the real stage: reference once, drive gently to each end stop
    # and set these a little inside what you read. The defaults assume a
    # 360 mm rail minus a 120 mm carriage = 240 mm of stroke centred on zero.
    """

    min_mm: float = -115.0
    max_mm: float = 115.0
    # Speed ceiling (mm/s). A CLL42 can go faster, but a sample flying 20 mm
    # in one second on a table-top is rarely what anyone wants.
    max_velocity_mm_s: float = 10.0
    # Lower speed floor, so a typo cannot make a move take a day.
    min_velocity_mm_s: float = 0.01
    # Master switch: set False to disable clamping (never on a loaded stage).
    enforce: bool = True


@dataclass
class Relative:
    """The "zero here" working origin (absolute mm), like the zero button on
    a bench readout. Display and move_from_zero only; the absolute scale is
    untouched.

        relative_position = position - rel_origin_mm
    """

    rel_origin_mm: float = 0.0


@dataclass
class Hardware:
    """How to reach the SCU and how to translate its units."""

    # Path of the SmarAct SCU library. The bare name is found on PATH or next
    # to the Python executable. # VERIFY which DLL the SCU-C-0006 ships with.
    dll_path: str = "SCU3DControl.dll"
    # Which SCU on the USB bus (0 = the first one found) and which channel on
    # it drives the positioner (0-based; the SCU-C-0006 has ONE channel).
    device_index: int = 0
    channel: int = 0
    # One encoder count in nanometres. The SCU reports integer positions; the
    # sensor resolves 0.1 um, so 100 nm per count is the assumption.
    # # VERIFY: move by a known amount and compare, or read the SCU manual.
    nm_per_count: float = 100.0
    # Nominal distance per piezo step at full amplitude, um. Used ONLY to turn
    # a speed (mm/s) into the closed-loop max step frequency (Hz):
    #     f = v / step.  # VERIFY by timing a long move.
    um_per_step: float = 1.0
    # Allowed range of the closed-loop max frequency, Hz. # VERIFY the SCU's
    # own range (the MCS family takes 50..18500 Hz).
    min_frequency_hz: int = 50
    max_frequency_hz: int = 18500
    # Sensor type code to program at open (SA_SetSensorType_S). 0 = leave the
    # controller as it was commissioned (recommended: SmarAct sets it).
    sensor_type: int = 0
    # Flip the sign of the axis (positive = the other end of the rail).
    invert: bool = False
    # How often the poll thread reads the controller, Hz. It is also the
    # sample rate of the fly-scan position stream. # VERIFY the USB cost of
    # one position + one status read on the real SCU.
    poll_hz: float = 50.0


@dataclass
class UI:
    """GUI preferences. ``theme`` ("dark" or "light") is applied at launch."""

    theme: str = "dark"


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
# INI persistence
# --------------------------------------------------------------------------- #
# The ONE list of groups. protocol.py's config_to_dict / apply_config_dict and
# the settings dialog all read it, so a new group is added here once
# (gotcha #4: a group missing from one of those lists silently never travels).
GROUPS = ("motion", "limits", "relative", "hardware", "ui")


def _sections(cfg: Config) -> dict[str, object]:
    """Map INI section name -> the dataclass instance that fills it."""
    return {name.capitalize() if name != "ui" else "UI": getattr(cfg, name)
            for name in GROUPS}


def _cast(raw: str, type_name: str):
    """Turn an INI string back into the right Python type.

    Because of ``from __future__ import annotations`` the field type arrives as
    a NAME ("bool", "int", "float", "str"), not the type object.

    The bool case is the classic trap (gotcha #3): ``bool("False")`` is True
    (any non-empty string is truthy), so the text is parsed explicitly.
    """
    if type_name == "bool":
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

    Unknown/missing keys are ignored so an older file still loads after a new
    field is added (it just keeps that field's default).
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
