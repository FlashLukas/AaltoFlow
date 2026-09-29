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

PER-OBJECTIVE AUTOFOCUS DISTANCES (2026-09-29, Lukas: "suggest zcal_max_travel
8 um, but this differs between objective lenses"). The focal depth scales with
the NA (~ lambda / NA^2), so how far an autofocus or the Z step calibration may
walk, and how big its steps are, belong to the OBJECTIVE, not to one global
setting. A section may carry any of ``AF_KEYS`` (autofocus config names, in
the Z unit); a key that is absent keeps the autofocus config's own value:

    [63x]
    pixel_size_x_um = 0.04562
    pixel_size_y_um = 0.04562
    zcal_max_travel_v = 8
    max_travel_v = 12
    coarse_step_v = 2
    fine_step_v = 0.25

They are applied when the objective is SET (set_objective, and at start for
the current one); :func:`store_af_value` writes one into the file (from the
AutoFocus tab), keeping every other line and comment of the file as it was.
"""

from __future__ import annotations

import configparser
import math
import re
from dataclasses import dataclass, field

#: Autofocus settings an objective section may override (autofocus config
#: names; distances in the Z unit). Why these: they are the DISTANCES and step
#: sizes, which scale with the focal depth; everything else (tolerances,
#: averages, the metric) does not depend on the objective.
AF_KEYS = ("zcal_max_travel_v", "zcal_step_v", "zcal_start_offset_v",
           "max_travel_v", "coarse_step_v", "fine_step_v", "drive_amplitude_v")


@dataclass
class Objective:
    name: str
    pixel_size_x_um: float
    pixel_size_y_um: float
    magnification: float = 0.0
    na: float = 0.0
    # autofocus overrides of this objective ({AF_KEYS name: value}); empty =
    # the autofocus config applies unchanged
    af: dict = field(default_factory=dict)


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
            af=_af_values(sec),
        )
    return table or default_objectives()


def _af_values(sec) -> dict:
    """The AF_KEYS present in one section, as floats. A value that is not a
    finite number is IGNORED (the autofocus config applies), never guessed."""
    out = {}
    for k in AF_KEYS:
        if k in sec:
            try:
                v = float(sec[k])
            except ValueError:
                continue
            if math.isfinite(v):
                out[k] = v
    return out


def save_objectives(table: dict[str, Objective], path: str) -> None:
    """Write an objective table to ``path`` (handy to seed a real file)."""
    cp = configparser.ConfigParser()
    for name, obj in table.items():
        cp[name] = {
            "pixel_size_x_um": str(obj.pixel_size_x_um),
            "pixel_size_y_um": str(obj.pixel_size_y_um),
            "magnification": str(obj.magnification),
            "na": str(obj.na),
            **{k: repr(float(v)) for k, v in obj.af.items() if k in AF_KEYS},
        }
    with open(path, "w", encoding="utf-8") as fh:
        cp.write(fh)


def resolve(table: dict[str, Objective], name: str) -> Objective | None:
    """Look up an objective by exact name, else return None."""
    return table.get(name)


def store_af_value(path: str, objective: str, key: str, value: float | None) -> None:
    """Write ONE autofocus override into the objective's section of ``path``
    (``value`` None = remove it, i.e. back to the autofocus config's value).

    Edited LINE BY LINE on purpose, not through configparser: that would drop
    every comment in the file (objectives.ini explains itself in comments) and
    rewrite the whole lab file. Here only the one
    ``key = value`` line changes (or is added at the end of the section, or
    removed). A missing file or section is created.
    """
    if key not in AF_KEYS:
        raise ValueError(f"{key!r} is not a per-objective autofocus setting "
                         f"(allowed: {', '.join(AF_KEYS)})")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        lines = []
    head = re.compile(r"^\s*\[(.*)\]\s*$")
    keyline = re.compile(rf"^\s*{re.escape(key)}\s*[=:]", re.IGNORECASE)
    start = next((i for i, ln in enumerate(lines)
                  if (m := head.match(ln)) and m.group(1).strip() == objective), None)
    new = None if value is None else f"{key} = {float(value):g}"
    if start is None:
        if new is not None:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [f"[{objective}]", new]
    else:
        end = next((i for i in range(start + 1, len(lines)) if head.match(lines[i])),
                   len(lines))
        hit = next((i for i in range(start + 1, end) if keyline.match(lines[i])), None)
        if hit is not None:
            if new is None:
                del lines[hit]
            else:
                lines[hit] = new
        elif new is not None:
            # after the section's last non-blank line (before the blank ones
            # that separate it from the next section)
            last = end
            while last - 1 > start and not lines[last - 1].strip():
                last -= 1
            lines.insert(last, new)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
