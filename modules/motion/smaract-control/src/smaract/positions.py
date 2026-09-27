"""The stored-position list (20 named positions on the rail).

Capture the current position into a slot, give it a name ("sample A",
"load"), and jump back to it later. The whole list saves to / loads from one
JSON file.

WHY JSON here (when config uses INI)? The list is a collection of records
(name + position), which maps to JSON far more cleanly than to INI sections,
and it is exactly the shape already sent over the ZeroMQ wire.

Positions are stored on the ABSOLUTE (referenced) scale, in mm. That is only
meaningful once the encoder is referenced, which is why the brain refuses to
store or go to a slot before that: a slot captured on an unreferenced counter
would point somewhere else after the next power cycle.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

#: Number of slots in the list.
N_SLOTS = 20


@dataclass
class Position:
    """One slot. ``used=False`` marks an empty slot."""

    name: str = ""
    position_mm: float = 0.0
    used: bool = False


class PositionList:
    """A fixed-length list of :class:`Position` slots with file save/load."""

    def __init__(self, n_slots: int = N_SLOTS):
        self.slots: list[Position] = [Position() for _ in range(n_slots)]

    def store(self, index: int, position_mm: float, name: str = "") -> Position:
        self._check(index)
        self.slots[index] = Position(name=name or f"P{index:02d}",
                                     position_mm=float(position_mm), used=True)
        return self.slots[index]

    def clear(self, index: int) -> None:
        self._check(index)
        self.slots[index] = Position()

    def get(self, index: int) -> Position:
        self._check(index)
        return self.slots[index]

    def to_list(self) -> list[dict]:
        """Plain list-of-dicts -- what travels over the wire and into JSON."""
        return [asdict(p) for p in self.slots]

    def from_list(self, data: list[dict]) -> None:
        """Replace slots from a list-of-dicts (extras ignored, missing = empty)."""
        for i in range(len(self.slots)):
            if i < len(data):
                d = data[i]
                self.slots[i] = Position(
                    name=str(d.get("name", "")),
                    position_mm=float(d.get("position_mm", 0.0)),
                    used=bool(d.get("used", False)),
                )
            else:
                self.slots[i] = Position()

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_list(), fh, indent=2)

    def load(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            self.from_list(json.load(fh))

    def _check(self, index: int) -> None:
        if not 0 <= index < len(self.slots):
            raise IndexError(f"position slot {index} out of range 0..{len(self.slots) - 1}")
