"""Microscope-objective pixel-size table (LabVIEW: 'load objective details from
settings file' -> predefines X/Y step per pixel).

Each objective maps a friendly name to the physical size of one camera pixel in
the sample plane, in micrometres.  Selecting an objective is how the whole
system learns its pixel->um scale, which every distance (spot->point, scanning
array) depends on.

The table is a plain INI so you can add the objectives on your microscope by
hand.  One section per objective:

    [20x - Zeiss NA 0.7]
    pixel_size_x_um = 0.4130
    pixel_size_y_um = 0.4130
    magnification = 20
    na = 0.7

If the file does not exist, :func:`load_objectives` returns a small built-in
default set (including the 20x used in the LabVIEW panel) so the module still
runs out of the box.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass


@dataclass
class Objective:
    name: str
    pixel_size_x_um: float
    pixel_size_y_um: float
    magnification: float = 0.0
    na: float = 0.0


# Built-in fallback table.  These are reasonable placeholders; recalibrate on the
# real microscope (image a known grating/graticule and set the true values).
_DEFAULTS: list[Objective] = [
    Objective("5x - Zeiss NA 0.16", 1.6520, 1.6520, 5, 0.16),
    Objective("10x - Zeiss NA 0.3", 0.8260, 0.8260, 10, 0.30),
    Objective("20x - Zeiss NA 0.7", 0.4130, 0.4130, 20, 0.70),
    Objective("50x - Zeiss NA 0.8", 0.1652, 0.1652, 50, 0.80),
    Objective("100x - Zeiss NA 0.9", 0.0826, 0.0826, 100, 0.90),
]


def default_objectives() -> dict[str, Objective]:
    return {o.name: o for o in _DEFAULTS}


def load_objectives(path: str) -> dict[str, Objective]:
    """Load the objective table from ``path``; fall back to the defaults."""
    cp = configparser.ConfigParser()
    read = cp.read(path, encoding="utf-8")
    if not read:
        return default_objectives()
    table: dict[str, Objective] = {}
    for name in cp.sections():
        sec = cp[name]
        table[name] = Objective(
            name=name,
            pixel_size_x_um=sec.getfloat("pixel_size_x_um", fallback=1.0),
            pixel_size_y_um=sec.getfloat(
                "pixel_size_y_um", fallback=sec.getfloat("pixel_size_x_um", 1.0)
            ),
            magnification=sec.getfloat("magnification", fallback=0.0),
            na=sec.getfloat("na", fallback=0.0),
        )
    return table or default_objectives()


def save_objectives(table: dict[str, Objective], path: str) -> None:
    """Write an objective table to ``path`` (handy to seed a real file)."""
    cp = configparser.ConfigParser()
    for name, obj in table.items():
        cp[name] = {
            "pixel_size_x_um": str(obj.pixel_size_x_um),
            "pixel_size_y_um": str(obj.pixel_size_y_um),
            "magnification": str(obj.magnification),
            "na": str(obj.na),
        }
    with open(path, "w", encoding="utf-8") as fh:
        cp.write(fh)


def resolve(table: dict[str, Objective], name: str) -> Objective | None:
    """Look up an objective by exact name, else return None."""
    return table.get(name)
