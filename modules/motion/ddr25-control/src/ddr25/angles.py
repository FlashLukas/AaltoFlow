"""Angle arithmetic and the stored-angle list -- pure Python, no hardware.

Two frames, one number each:

  controller position ("raw")  what the K-Cube counts, in degrees, continuous
                               over many turns (370 is not 10). Zero is the
                               encoder index found by homing.
  angle                        what the user sees and commands:
                               raw - zero_deg, and in the modulo-360 wrap
                               modes additionally reduced to [0, 360).

Everything that turns an angle into a raw target lives in :func:`raw_target`,
so the wrap policy is implemented (and tested) in exactly one place.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

#: Number of stored-angle slots.
N_SLOTS = 10


def wrap360(a: float) -> float:
    """Reduce an angle to [0, 360). (-10 -> 350, 370 -> 10, 360 -> 0.)"""
    r = float(a) % 360.0
    # float modulo can return 360.0 for tiny negative inputs (-1e-15 % 360)
    return 0.0 if r >= 360.0 else r


def display_angle(raw: float, zero: float, policy: str) -> float:
    """The angle the user sees for a controller position."""
    a = float(raw) - float(zero)
    return a if policy == "literal" else wrap360(a)


def raw_target(policy: str, raw_now: float, zero: float, angle: float) -> float:
    """Controller position to drive to so that the stage shows ``angle``.

    literal   : a linear coordinate, raw = zero + angle.
    shortest  : the nearer way round (a tie of exactly 180 deg goes negative).
    positive  : always turn +, between 0 and just under a full turn.
    negative  : always turn -, likewise.

    The modulo modes add the turn DIFFERENCE to where the stage is now, so the
    controller's continuous count stays continuous (no jump back by 360).
    """
    if policy == "literal":
        return float(zero) + float(angle)
    here = wrap360(float(raw_now) - float(zero))
    d = wrap360(angle) - here               # in (-360, 360)
    if policy == "shortest":
        d = ((d + 180.0) % 360.0) - 180.0   # -> [-180, 180)
    elif policy == "positive":
        d = d % 360.0                       # -> [0, 360)
    elif policy == "negative":
        d = -((-d) % 360.0)                 # -> (-360, 0]
    else:
        raise ValueError(f"unknown wrap policy {policy!r}")
    return float(raw_now) + d


# --------------------------------------------------------------------------- #
# stored angles
# --------------------------------------------------------------------------- #
@dataclass
class StoredAngle:
    """One slot. ``raw`` is the CONTROLLER position, so moving the display zero
    never moves a stored physical orientation. ``used=False`` = empty."""

    name: str = ""
    raw: float = 0.0
    used: bool = False


class AngleList:
    """A fixed-length list of named orientations with JSON save/load."""

    def __init__(self, n_slots: int = N_SLOTS):
        self.slots: list[StoredAngle] = [StoredAngle() for _ in range(n_slots)]

    def store(self, index: int, raw: float, name: str = "") -> StoredAngle:
        self._check(index)
        self.slots[index] = StoredAngle(name=name or f"A{index}", raw=float(raw), used=True)
        return self.slots[index]

    def clear(self, index: int) -> None:
        self._check(index)
        self.slots[index] = StoredAngle()

    def get(self, index: int) -> StoredAngle:
        self._check(index)
        return self.slots[index]

    def to_list(self) -> list[dict]:
        return [asdict(s) for s in self.slots]

    def from_list(self, data: list[dict]) -> None:
        for i in range(len(self.slots)):
            d = data[i] if i < len(data) and isinstance(data[i], dict) else {}
            self.slots[i] = StoredAngle(
                name=str(d.get("name", "")), raw=float(d.get("raw", 0.0)),
                used=bool(d.get("used", False)))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_list(), fh, indent=2)

    def load(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            self.from_list(json.load(fh))

    def _check(self, index: int) -> None:
        if not 0 <= int(index) < len(self.slots):
            raise IndexError(f"angle slot {index} out of range 0..{len(self.slots) - 1}")
