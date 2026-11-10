"""Configuration for the objective-focus actuator (Thorlabs KIM001 + PIA13).

Follows the project blueprint (section 4 of INSTRUMENT_MODULE_GUIDE.md): every
tunable number lives on a small ``@dataclass`` grouped by concern, and the
whole thing is persisted as a plain-text INI file via :mod:`configparser`.

------------------------------------------------------------------------------
Why this file still talks about THREE axes (X, Y, Z) on a ONE-axis controller
------------------------------------------------------------------------------
This module is a copy of kim-control (KIM101 + 3x PIA25, the sample stage)
cut down to the single-channel KIM001. The camera already drives a "kim" Z
axis through a fixed wire contract (camera-control backends/remote_kim.py,
KimZFocus): status lists are 3 wide [X, Y, Z] and every verb takes
axis="Z". Keeping that shape means the camera can drive this objective with
the code it already has -- only the module key and the ports differ.

So inside, the axis index stays 0=X, 1=Y, 2=Z, and ONE axis is real:
``FITTED_AXES = (2,)``. X and Y are "not fitted": they read as a constant
0 at rest with limits 0..0, and every command for them is REFUSED with an
error saying so (see :func:`check_fitted`). Refusing at the edges, rather than
rewriting the 3-axis brain, was the least risky way to reuse a module that is
verified on hardware.

------------------------------------------------------------------------------
Two languages, one calibration bridge (inherited from kim-control)
------------------------------------------------------------------------------
A KIM001 drives a PIA13 *piezo-inertia* actuator: a "slip-stick" motor that
moves in tiny discrete STEPS. The controller's native language is therefore
steps; there is no encoder, so "position" is the accumulated step count the
controller has issued (open-loop). You think in MICROMETRES, so the axis
carries ``um_per_step`` (see :class:`Calibration`):

    steps        = round(micrometres / um_per_step)
    micrometres  = steps * um_per_step
    step_rate    = round(velocity_um_per_s / um_per_step)    (steps/s)

The physical SIZE of one step is set by the piezo drive VOLTAGE (85-125 V on a
K-Cube inertial controller, changing the step by up to ~30 %), so re-measure
``um_per_step`` after changing the voltage.

Datasheet anchors (VERIFY against your own actuator -- every unit differs):
  * PIA13: 13 mm travel, typical step size ~20 nm (0.02 um), max step rate
    2000 steps/s.
  * KIM001: ONE channel, 85-125 V, step rate 1-2000 steps/s, step
    acceleration 1-100000 steps/s^2 (assumed the same as the KIM101 --
    # VERIFY in the KIM001 manual).
"""

# `from __future__ import annotations` turns every annotation into a *string* at
# runtime.  That is exactly why the INI loader below routes each value through
# `_cast(raw, field.type)` using the type *name* -- see load_config().
from __future__ import annotations

import configparser
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

# Axis order is fixed everywhere in the package: index 0=X, 1=Y, 2=Z.
AXES = ("X", "Y", "Z")

#: The axes a KIM001 really has. ONE channel = ONE actuator = the objective's
#: focus, published as Z (index 2) so the camera's kim Z client works as is.
FITTED_AXES = (2,)

#: Kinesis serial-number prefix of a KIM001, used ONLY when no serial is
#: configured and the backend has to pick "the one KIM001 on this PC".
#: Thorlabs serials start with a product code: the KIM101's is 97 (verified
#: 2026-10-03), the KIM001's is 74 (verified on the Blue Box unit 2026-11-10;
#: Kinesis lists it as "Piezo Motor Controller", the same text as a KIM101, so
#: the prefix -- and the model_no check after opening -- tell them apart).
#: hardware.serial, when set, always wins.
KIM001_SERIAL_PREFIX = "74"


class NotFitted(ValueError):
    """A command for an axis this KIM001 does not have (X or Y)."""


def is_fitted(axis: int) -> bool:
    return axis in FITTED_AXES


def check_fitted(axis: int) -> None:
    """Raise NotFitted for X / Y, with a message that says why.

    The service turns it into {"ok": false, "error": "NotFitted: ..."}; the
    text names the KIM001 so a client pointed at the wrong module (expecting
    the 3-axis kim) is told so instead of seeing a silent no-op.
    """
    if not is_fitted(axis):
        name = AXES[axis] if 0 <= axis < len(AXES) else str(axis)
        raise NotFitted(f"axis {name} is not fitted on a KIM001 (single-channel "
                        f"controller: only Z, the objective focus)")


# --------------------------------------------------------------------------- #
# Config groups (one @dataclass per concern)
# --------------------------------------------------------------------------- #
@dataclass
class Motion:
    """Drive parameters per axis (only the Z fields reach the KIM001).

    NOT pushed on start (adopt-on-start rule, 2026-09-27): at start the brain
    READS the controller's rate / acceleration / voltage and overwrites these
    fields with them, so they always describe what the controller is doing.
    The .ini values reach the controller only when the user applies them
    (Settings OK, set_config, a preset button, a set_* verb).

    All in the controller's NATIVE units (steps), plus the drive voltage that
    governs the physical size of a step.
    """

    # Step rate (a.k.a. velocity) per axis, steps/s.  Range 1..2000.
    # The X / Y fields exist only so the 3-wide shape is kept; nothing reads them.
    # The defaults deliberately match the SLOW + SMALL preset below, so a fresh
    # config lines up with the front-panel toggles (Movement: Slow, Steps:
    # Small) -- the safe state to power on a coarse inertial stage in.
    rate_x: float = 300.0
    rate_y: float = 300.0
    rate_z: float = 300.0
    # Step acceleration per axis, steps/s^2.  Range 1..100000.
    acc_x: float = 5000.0
    acc_y: float = 5000.0
    acc_z: float = 5000.0
    # Piezo drive voltage per axis, V.  This sets the PHYSICAL step size
    # (bigger voltage -> bigger step, up to ~30%).  Range 85..125 V.
    # Default = min (small steps) to match the Steps: Small preset.
    voltage_x: float = 85.0
    voltage_y: float = 85.0
    voltage_z: float = 85.0
    # Default jog step used by the GUI +/- buttons, in STEPS.
    jog_steps: int = 100

    # Front-panel "Movement: Fast / Slow" presets (applied to the fitted axis
    # by the toggle button).  Fast/slow are a step RATE (steps/s) + acceleration
    # (steps/s^2); tune them here in Settings to taste.
    fast_rate: float = 1500.0
    fast_accel: float = 20000.0
    slow_rate: float = 300.0
    slow_accel: float = 5000.0


@dataclass
class Calibration:
    """The bridge between STEPS (hardware) and MICROMETRES (the objective).

    ``um_per_step_z`` = how far one step moves the objective, in um. Default
    0.02 um = 20 nm, the PIA13 datasheet's nominal step -- MEASURE yours (the
    camera's "Calibrate Z steps" does it from the image and writes both
    directions here with set_calibration, then save_calibration).

    There is no camera px/step table in this module (kim-control has one for
    its X / Y): that calibration measures how a SAMPLE stage moves the image
    sideways, which a focus actuator does not do.
    """

    um_per_step_x: float = 0.02     # not fitted: kept for the 3-wide shape
    um_per_step_y: float = 0.02     # not fitted: kept for the 3-wide shape
    um_per_step_z: float = 0.02
    # BACKWARD step size, if you measured one. A slip-stick actuator does not
    # step equally both ways (the KIM101's PIA25 Z on the rig: up ~0.75-0.8 x
    # down). 0 = same as forward, which is what a datasheet gives you.
    um_per_step_x_bwd: float = 0.0
    um_per_step_y_bwd: float = 0.0
    um_per_step_z_bwd: float = 0.0


@dataclass
class Limits:
    """The SAFETY ENVELOPE.  The brain clamps every target/param to this.

    Travel is bounded in STEPS because that is what the open-loop controller
    actually counts. Z: +/- 650,000 steps = +/- 13 mm at the 0.02 um/step
    nominal calibration, i.e. the PIA13's whole 13 mm travel EITHER side of the
    datum. Symmetric because the datum is arbitrary (wherever you pressed
    Datum, usually at a focus); the actuator's real end stops are somewhere in
    that box, and the LEASH below is the guard that actually protects the
    objective. Re-derive after re-calibrating: max_steps ~= 13000 um / um_per_step.

    X / Y are not fitted: 0..0 (the brain refuses those axes before it clamps).
    """

    min_steps_x: int = 0
    max_steps_x: int = 0
    min_steps_y: int = 0
    max_steps_y: int = 0
    min_steps_z: int = -650_000
    max_steps_z: int = 650_000
    # Parameter ceilings (K-Cube inertial controller limits) so a fat-fingered
    # value can't be pushed past what the controller accepts.
    max_step_rate: float = 2000.0      # steps/s
    max_acceleration: float = 100000.0  # steps/s^2
    min_voltage: float = 85.0           # V
    max_voltage: float = 125.0          # V
    # Master switch: set False to disable clamping (advanced/bench use).
    enforce: bool = True

    # --- LEASH: a symmetric travel box around the DATUM (runout guard) -------
    # When enabled, Z may only move within +/- leash_z steps of the datum (the
    # counter's 0). For an OBJECTIVE this is the guard that matters: datum at a
    # focus you trust, and the objective cannot be driven more than
    # leash_z x um_per_step away from it -- e.g. into the sample. When the
    # leash is ON it REPLACES the min/max_steps clamp above. Off by default,
    # exactly as in kim-control: arm it (GUI LEASH card or set_leash) once the
    # datum is set. leash_xy is kept for the wire shape (set_leash takes it)
    # but has no effect here.
    leash_enabled: bool = False
    leash_xy: int = 0       # not fitted
    leash_z: int = 50000    # +/- steps from datum for Z (50000 ~ 1 mm @ 20 nm)


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
    """How to reach the KIM001 and which channel drives the focus axis."""

    # Kinesis serial number of the KIM001. "" = use the ONE device found on
    # this PC whose serial starts with `serial_prefix` (and which then reports
    # itself as a KIM001); set it when several are plugged in. Not a lab serial
    # here: this default is public.
    serial: str = ""
    # The auto-pick prefix (see config.KIM001_SERIAL_PREFIX, # VERIFY).
    serial_prefix: str = KIM001_SERIAL_PREFIX
    # Which controller channel drives each logical axis. A KIM001 has ONE
    # channel, so Z = channel 1; X / Y = 0 = "no channel" (not fitted).
    ch_x: int = 0
    ch_y: int = 0
    ch_z: int = 1
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
    """The +/- leash half-range (steps) for an axis: leash_z for Z, 0 for the
    axes that are not fitted (X / Y)."""
    if not is_fitted(axis):
        return 0
    return abs(int(cfg.limits.leash_z))


def axis_effective_limits(cfg: Config, axis: int) -> tuple[int, int]:
    """The step bounds the brain ACTUALLY clamps to for an axis.

    With the leash enabled this is a symmetric box around the datum
    ``(-half, +half)`` -- the leash REPLACES the absolute min/max_steps limits
    (which are in the wrong frame once you datum mid-travel).  With the leash
    off it is the plain absolute ``(min_steps, max_steps)``. An axis that is
    not fitted (X / Y on a KIM001) is always (0, 0).
    """
    if not is_fitted(axis):
        return (0, 0)
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
    if direction > 0 and getattr(cfg.calibration, bwd) <= 0:
        # Backward was "same as forward" (0). Setting forward ONLY must not drag
        # the backward step along with it, so pin backward to the value it had
        # until now -- the old forward number -- before forward changes.
        setattr(cfg.calibration, bwd, getattr(cfg.calibration, fwd))
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


# --------------------------------------------------------------------------- #
# The service's own settings file: kim001.ini in the project folder
# --------------------------------------------------------------------------- #
#: File name of the lab's saved kim001 settings. Gitignored: it holds the step
#: sizes measured on THIS rig (lab data, like the camera's camera.ini).
DEFAULT_INI_NAME = "kim001.ini"


def default_config_path() -> Path:
    """<kim001-control>/kim001.ini (this file is src/kim001/config.py -> parents[2]).

    A function, not a constant, so the tests can point it at a temp folder and
    never write into the project."""
    return Path(__file__).resolve().parents[2] / DEFAULT_INI_NAME


def load_startup_config(config_arg: str | None = None,
                        default_path: Path | None = None) -> tuple[Config, Path, bool]:
    """The config a service / local GUI starts with: (cfg, file, loaded).

    ``--config FILE`` wins; otherwise kim001.ini in the project folder IF it
    exists; otherwise the built-in defaults. ``file`` is where a later
    save_calibration / save_config writes -- the file we read, or kim001.ini.

    WHY this matters (learned on kim-control, 2026-09-29): without a file,
    step sizes the camera measured and wrote with set_calibration were gone
    after a restart. Loading a file only fills cfg; it sends NOTHING
    to the KIM001 -- Kim.start() then READS rate / acceleration / voltage from
    the controller and overwrites cfg.motion with them (adopt-on-start rule).
    So from the file what counts is the calibration, limits, leash and UI.
    """
    if config_arg:
        path = Path(config_arg)
        return load_config(str(path)), path, True
    path = Path(default_path) if default_path is not None else default_config_path()
    if path.is_file():
        return load_config(str(path)), path, True
    return Config(), path, False


def save_sections(cfg: Config, path, sections=None) -> Path:
    """Write the named INI sections of ``cfg`` into ``path``, MERGING.

    ``sections`` = e.g. ["Calibration"]; None = every group. Sections already
    in the file that we were not asked to write are kept as they are -- saving
    the step sizes must not wipe a [Limits] or [UI] someone set up by hand.
    (configparser drops comments, so hand-written comments are not kept.)

    Atomic: written to a temp file in the same folder, then os.replace()d over
    the old one. A crash or a full disk mid-write leaves the old file intact
    instead of a half-written one that would load as defaults next start.
    """
    path = Path(path)
    groups = _sections(cfg)
    names = list(groups) if sections is None else list(sections)
    for name in names:
        if name not in groups:
            raise ValueError(f"unknown config section {name!r}")
    cp = configparser.ConfigParser()
    if path.is_file():
        cp.read(path, encoding="utf-8")
    for name in names:
        cp[name] = {key: str(val) for key, val in asdict(groups[name]).items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            cp.write(fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


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
