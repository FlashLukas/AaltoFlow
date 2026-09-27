"""Configuration for the Elliptec rotation mount module.

Follows the project blueprint (section 4 of INSTRUMENT_MODULE_GUIDE.md): every
tunable number lives on a small ``@dataclass`` grouped by concern, and the
whole thing is persisted as a plain-text INI file via :mod:`configparser`.

WHY dataclasses + INI (and not a big dict or JSON)?
  * dataclasses give attribute access with editor autocomplete and a clear,
    typed "this is exactly what is configurable" contract;
  * INI is human-readable and hand-editable in any text editor.

The instrument: one or more Thorlabs ELL14 rotation mounts (the ELL14K kit =
mount + USB interface board).  Several Elliptec devices can share ONE serial
bus; each answers to its own address, a single hex digit 0-F.  This module
turns every configured address into one rotary AXIS, so "axis 0" is the first
address in ``axes.addresses``, "axis 1" the second, and so on.

Angles are in DEGREES.  Two frames:
  * DEVICE angle = what the mount's encoder reports, relative to its home mark.
  * USER angle   = device angle minus a per-axis offset, wrapped to [0, 360).
    The offset is how you say "0 deg is where my waveplate's fast axis is
    vertical" without re-homing anything.
"""

# `from __future__ import annotations` turns every annotation into a *string* at
# runtime.  That is exactly why the INI loader routes each value through
# `_cast(raw, field.type)` using the type *name* -- see load_config().
from __future__ import annotations

import configparser
from dataclasses import asdict, dataclass, fields

#: The bus addresses an Elliptec device can have: one hex digit.
VALID_ADDRESSES = "0123456789ABCDEF"


# --------------------------------------------------------------------------- #
# Config groups (one @dataclass per concern)
# --------------------------------------------------------------------------- #
@dataclass
class Axes:
    """Which devices on the bus this module drives.

    Lists are stored as comma-separated strings so they survive the INI file
    (the same convention as clMag's AUX channel lists).
    """

    # Bus addresses, one per mount, e.g. "0" or "0,1,2".  A factory-fresh
    # ELL14 answers on address 0; give each mount its own address (Thorlabs'
    # Elliptec software, or the `ca` command) BEFORE putting two on one bus.
    addresses: str = "0"
    # Optional human names, same order, e.g. "HWP,polarizer".  Empty -> "Axis 0".
    names: str = ""


@dataclass
class Motion:
    """Motion defaults.  NOTHING here is pushed at start (adopt-on-start rule,
    2026-09-27): the service reads each mount's speed and angle and shows them."""

    # Drive speed as a percentage of the mount's maximum (the ELL14's own unit:
    # the `sv` command takes a percentage, not deg/s).  A DEFAULT, sent to the
    # mounts only when the user changes it (Settings / set_config); at start
    # each mount keeps the speed it has.
    velocity_pct: int = 100
    # Home every axis when the service starts.  Off by default and should stay
    # off: homing spins the optic, which breaks the rule that starting a
    # service never changes the instrument.  The ELL14 keeps its encoder
    # position while powered anyway.
    home_on_start: bool = False
    # Homing direction for a rotary mount: "cw" or "ccw" (the `ho0` / `ho1`
    # argument).  Only matters if something limits which way it may turn.
    home_direction: str = "cw"
    # Default step of the GUI's -/+ buttons, degrees.
    jog_step_deg: float = 5.0


@dataclass
class Limits:
    """The SAFETY ENVELOPE.  The brain clamps every request to this."""

    # Allowed USER angle window, degrees.  The default is the whole circle.
    # Narrow it when a cable or a fibre on the mount must not wind up: then
    # every move becomes a step along the user frame to a point inside the
    # window (worked out from the measured angle), so it can never turn the
    # long way round -- not even when the offset puts the home mark inside
    # the window.  Homing still turns to the home mark: do it by hand there.
    min_angle_deg: float = 0.0
    max_angle_deg: float = 360.0
    # Largest single relative move, degrees (one full turn).
    max_relative_deg: float = 360.0
    # Velocity window in percent.  The ELL14 is a resonant piezo motor and
    # stalls if driven too slowly, hence a floor.            # VERIFY the floor
    min_velocity_pct: int = 30
    max_velocity_pct: int = 100
    # Master switch for clamping (the angle wrap to [0, 360) always applies).
    enforce: bool = True


@dataclass
class Offsets:
    """Per-axis user zero, degrees: user angle = device angle - offset.

    Comma-separated, one entry per address (missing entries are 0).  This is
    config, so a coordinator's `set_config` push of this group overwrites a
    "Zero here" done on the panel (gotcha #5) -- push only the groups you mean.
    """

    offsets_deg: str = ""


@dataclass
class Hardware:
    """How to reach the bus."""

    # Virtual COM port of the ELL14K interface board (FTDI).  Look it up in
    # the Windows Device Manager ("Ports (COM & LPT)").
    port: str = "COM5"
    # Elliptec protocol: 9600 baud, 8 data bits, no parity, 1 stop bit.
    baudrate: int = 9600
    # Serial read timeout for ONE poll, seconds (kept short: the poll thread
    # must never sit on the port while a stop is waiting).
    read_timeout_s: float = 0.05
    # Pulses per full turn.  0 = use what the mount reports in its `in` reply
    # (preferred: then a different Elliptec rotator just works).  Set it only
    # if a firmware reports nonsense.
    pulses_per_rev_override: int = 0
    # Give up on a move that has not replied after this long, seconds.  A full
    # turn at the lowest speed should fit comfortably.
    move_timeout_s: float = 8.0
    # How often the worker thread polls the bus (and rebuilds the status), Hz.
    poll_hz: float = 20.0


@dataclass
class Sim:
    """Knobs of the simulator only (ignored by the real backend)."""

    # Top speed at 100 %, deg/s.  Thorlabs quotes ~430 deg/s for the ELL14.
    max_speed_deg_s: float = 430.0
    # Encoder resolution of the simulated mount, pulses per turn (the value a
    # real ELL14 reports in its `in` reply).
    pulses_per_rev: int = 143360
    # Where the simulated mounts sit at power-up (device degrees): not 0, so
    # the panel shows that homing actually does something.
    start_deg: float = 37.0
    # The speed the simulated mounts run at at power-up, percent.  Not 100 on
    # purpose: the service must ADOPT it, and a test proves it does.
    start_velocity_pct: int = 60


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

    axes: Axes = None
    motion: Motion = None
    limits: Limits = None
    offsets: Offsets = None
    hardware: Hardware = None
    sim: Sim = None
    ui: UI = None

    def __post_init__(self):
        for name, cls in _GROUP_CLASSES.items():
            if getattr(self, name) is None:
                setattr(self, name, cls())


#: Every config group, in one place: INI sections, the wire dict and the
#: settings dialog all iterate over this, so a new group is added HERE (and
#: gets its dataclass above) -- gotcha #4.
_GROUP_CLASSES = {
    "axes": Axes,
    "motion": Motion,
    "limits": Limits,
    "offsets": Offsets,
    "hardware": Hardware,
    "sim": Sim,
    "ui": UI,
}
GROUPS = tuple(_GROUP_CLASSES)


# --------------------------------------------------------------------------- #
# Small typed accessors (so the rest of the code never splits strings by hand)
# --------------------------------------------------------------------------- #
def parse_addresses(text: str) -> list[str]:
    """"0, 1,a" -> ["0", "1", "A"].  Raises ValueError on a bad or repeated one.

    Refusing a duplicate matters: two axes on one address would be one mount
    receiving two sets of commands.
    """
    out = []
    for part in str(text).split(","):
        a = part.strip().upper()
        if not a:
            continue
        if len(a) != 1 or a not in VALID_ADDRESSES:
            raise ValueError(f"bad Elliptec address {part!r} (use one hex digit 0-F)")
        if a in out:
            raise ValueError(f"address {a} is listed twice")
        out.append(a)
    if not out:
        raise ValueError("no Elliptec address configured")
    return out


def axis_names(cfg: Config) -> list[str]:
    addrs = parse_addresses(cfg.axes.addresses)
    given = [n.strip() for n in str(cfg.axes.names).split(",")]
    return [given[i] if i < len(given) and given[i] else f"Axis {i}"
            for i in range(len(addrs))]


def get_offsets(cfg: Config, n: int) -> list[float]:
    vals = []
    for part in str(cfg.offsets.offsets_deg).split(","):
        part = part.strip()
        try:
            vals.append(float(part) if part else 0.0)
        except ValueError:
            vals.append(0.0)
    vals = (vals + [0.0] * n)[:n]
    return vals


def set_offsets(cfg: Config, values: list[float]) -> None:
    cfg.offsets.offsets_deg = ",".join(f"{v:.6g}" for v in values)


# --------------------------------------------------------------------------- #
# INI persistence
# --------------------------------------------------------------------------- #
def _sections(cfg: Config) -> dict[str, object]:
    """Map INI section name -> the dataclass instance that fills it."""
    return {name.capitalize() if name != "ui" else "UI": getattr(cfg, name)
            for name in GROUPS}


def _cast(raw: str, type_name: str):
    """Turn an INI string back into the right Python type.

    Because of ``from __future__ import annotations`` the field type arrives as
    a NAME ("bool", "int", "float", "str"), not the type object.

    The bool case is the classic trap: ``bool("False")`` is True (any non-empty
    string is truthy), so we parse the text explicitly (gotcha #3).
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
