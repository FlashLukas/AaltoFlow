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
