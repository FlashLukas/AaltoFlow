"""Configuration for the Z focus piezo (blueprint §4).

A single-axis piezo driven by a Thorlabs KCube: you command a DRIVE VOLTAGE and
it holds it.  Just two config groups -- the safety envelope (voltage range) and
how to reach the hardware.
"""

from __future__ import annotations

import configparser
from dataclasses import asdict, dataclass, fields


@dataclass
class Limits:
    """The SAFETY ENVELOPE -- the brain clamps every commanded voltage to this."""

    v_min: float = 0.0
    v_max: float = 75.0       # KPZ101 default full-scale (matches the LabVIEW panel)
    enforce: bool = True


@dataclass
class Hardware:
    """How to reach the KCube (and GUI/jog conveniences)."""

    serial: str = ""          # Thorlabs KCube serial number
    step_v: float = 0.25      # jog step, volts
    step_time_ms: float = 25.0  # settle time per step
    units: str = "V"


@dataclass
class Config:
    limits: Limits = None
    hardware: Hardware = None

    def __post_init__(self):
        self.limits = self.limits or Limits()
        self.hardware = self.hardware or Hardware()


# --------------------------------------------------------------------------- #
# INI persistence
# --------------------------------------------------------------------------- #
def _sections(cfg: Config) -> dict[str, object]:
    return {"Limits": cfg.limits, "Hardware": cfg.hardware}


def _cast(raw: str, type_name: str):
    if type_name == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if type_name == "int":
        return int(float(raw))
    if type_name == "float":
        return float(raw)
    return raw


def save_config(cfg: Config, path: str) -> None:
    cp = configparser.ConfigParser()
    for section, obj in _sections(cfg).items():
        cp[section] = {k: str(v) for k, v in asdict(obj).items()}
    with open(path, "w", encoding="utf-8") as fh:
        cp.write(fh)


def load_config(path: str) -> Config:
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


# --------------------------------------------------------------------------- #
# Applying a config dict that came over the wire (set_config)
# --------------------------------------------------------------------------- #
def _coerce(value, type_name: str):
    """Cast a wire value to a field's type.

    JSON normally brings the right type, but a hand-typed console command or
    a GUI text box can send "50" or "False".  Stored raw, "50" made every later
    set_voltage fail (float <= str), and "False" is truthy (gotcha #3).
    """
    if isinstance(value, str):
        return _cast(value, type_name)
    if type_name == "float":
        return float(value)
    if type_name == "int":
        return int(value)
    if type_name == "bool":
        return bool(value)
    if type_name == "str":
        return str(value)
    return value


def apply_config_dict(cfg: Config, data: dict) -> None:
    """Apply ``{"limits": {...}, "hardware": {...}}`` to ``cfg`` IN PLACE.

    All or nothing: every value is cast and the resulting voltage envelope is
    checked first, and only then is anything written.  Unknown groups and keys
    are ignored (a newer client may know more fields than we do).

    Why the envelope check: v_min >= v_max used to be accepted, and the clamp
    min(max(v, v_min), v_max) then answers v_max to EVERY request -- a typo in
    v_min drove the focus to full scale.
    """
    groups = {"limits": cfg.limits, "hardware": cfg.hardware}
    staged = []
    for gname, values in (data or {}).items():
        obj = groups.get(gname)
        if obj is None or not isinstance(values, dict):
            continue
        types = {f.name: f.type for f in fields(obj)}
        for key, val in values.items():
            if key in types:
                staged.append((obj, key, _coerce(val, types[key])))
    env = {"v_min": cfg.limits.v_min, "v_max": cfg.limits.v_max}
    for obj, key, val in staged:
        if obj is cfg.limits and key in env:
            env[key] = val
    if not env["v_min"] < env["v_max"]:        # also catches NaN
        raise ValueError(f"voltage limits need v_min < v_max, got "
                         f"v_min={env['v_min']!r}, v_max={env['v_max']!r}")
    for obj, key, val in staged:
        setattr(obj, key, val)
