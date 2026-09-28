"""The saved position list (up to 20 named positions).

This is a small, self-contained store the user fills from the front panel:
capture the current stage position into a slot, give it a name, and later jump
back to it.  The whole list saves to / loads from a single JSON file.

WHY JSON here (when config uses INI)?
  * The list is a homogeneous collection of records (name + x,y,z), which maps
    to JSON far more cleanly than to INI sections, and it is exactly the shape
    already sent over the ZeroMQ wire, so one format serves both.
  * It is a separate, on-demand user action ("Save positions as..."), distinct
    from the instrument config, so keeping it in its own file is natural.

IMPORTANT: positions are stored in DEVICE coordinates (raw motor mm).  That
makes "go to position" deterministic -- it commands the exact motor targets
regardless of the current offsets/transform, so changing your logical frame
never moves a saved physical point.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

# Number of slots in the list.  Fixed at 20 per the requirement.
N_SLOTS = 20


@dataclass
class Position:
    """One slot in the list.  ``used=False`` marks an empty slot."""

    name: str = ""
    x: float = 0.0  # device coordinate, mm
    y: float = 0.0
    z: float = 0.0
    used: bool = False


class PositionList:
    """A fixed-length list of :class:`Position` slots with file save/load."""

    def __init__(self, n_slots: int = N_SLOTS):
        self.slots: list[Position] = [Position() for _ in range(n_slots)]

    # -- editing ----------------------------------------------------------- #
    def store(self, index: int, x: float, y: float, z: float, name: str = "") -> Position:
        """Put device coordinates into slot ``index`` and mark it used."""
        self._check(index)
        self.slots[index] = Position(
            name=name or f"P{index:02d}", x=x, y=y, z=z, used=True
        )
        return self.slots[index]

    def clear(self, index: int) -> None:
        """Empty a slot."""
        self._check(index)
        self.slots[index] = Position()

    def get(self, index: int) -> Position:
        self._check(index)
        return self.slots[index]

    def rename(self, index: int, name: str) -> None:
        self._check(index)
        self.slots[index].name = name

    # -- (de)serialisation ------------------------------------------------- #
    def to_list(self) -> list[dict]:
        """Plain list-of-dicts -- what travels over the wire and into JSON."""
        return [asdict(p) for p in self.slots]

    def from_list(self, data: list[dict]) -> None:
        """Replace slots from a list-of-dicts (extra entries ignored,
        missing ones left empty)."""
        for i in range(len(self.slots)):
            if i < len(data):
                d = data[i]
                self.slots[i] = Position(
                    name=d.get("name", ""),
                    x=float(d.get("x", 0.0)),
                    y=float(d.get("y", 0.0)),
                    z=float(d.get("z", 0.0)),
                    used=bool(d.get("used", False)),
                )
            else:
                self.slots[i] = Position()

    # -- files ------------------------------------------------------------- #
    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_list(), fh, indent=2)

    def load(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            self.from_list(json.load(fh))

    # -- internal ---------------------------------------------------------- #
    def _check(self, index: int) -> None:
        if not 0 <= index < len(self.slots):
            raise IndexError(
                f"position slot {index} out of range 0..{len(self.slots) - 1}"
            )
