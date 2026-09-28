"""The field-vs-current calibration curve, B(I).

You measure this once by sweeping the magnet through a full cycle
(-limit -> +limit -> -limit), recording (current, field) pairs. Because iron
has hysteresis, the up-leg and down-leg disagree at the same current; we cancel
that by AVERAGING the two readings taken at each current. Optionally we subtract
"remanence" so that zero current maps to zero field.

Afterwards the curve does two jobs:
    field_for_current(I) -> B     (forward: what field will this current give?)
    current_for_field(B) -> I     (inverse: what current do I need for this field?)
Both are linear interpolation between measured points, clamped at the ends.

No numpy on purpose -- this keeps the dependency list tiny and the maths
visible. `bisect` from the standard library does the "which segment am I in?"
lookup in log time.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import List, Tuple

from .config import HallProbe


def _interp(xs: List[float], ys: List[float], x: float) -> float:
    """Linear interpolation of y at x over sorted xs, clamped at both ends."""
    if not xs:
        raise ValueError("empty calibration curve")
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, x)          # xs[i-1] <= x < xs[i]
    x0, x1 = xs[i - 1], xs[i]
    y0, y1 = ys[i - 1], ys[i]
    if x1 == x0:
        return y0
    frac = (x - x0) / (x1 - x0)
    return y0 + frac * (y1 - y0)


@dataclass
class FieldCalibration:
    """A monotonic B(I) table plus the Hall parameters used to build it."""

    currents_A: List[float] = field(default_factory=list)   # sorted ascending
    fields_mT: List[float] = field(default_factory=list)     # paired with currents
    hall: HallProbe = field(default_factory=HallProbe)

    # ---- construction -----------------------------------------------------

    @classmethod
    def from_sweep(
        cls,
        points: List[Tuple[float, float]],
        hall: HallProbe,
        current_resolution_A: float = 1e-4,
        subtract_remanence: bool = True,
    ) -> "FieldCalibration":
        """Build a calibration from raw (current, field) sweep points.

        Points at the "same" current (within current_resolution_A) from the up
        and down legs are averaged together, cancelling hysteresis.
        """
        # bucket by rounded current
        buckets: dict[float, List[float]] = {}
        for I, B in points:
            key = round(I / current_resolution_A) * current_resolution_A
            buckets.setdefault(key, []).append(B)

        currents = sorted(buckets)
        avg_fields = [sum(buckets[I]) / len(buckets[I]) for I in currents]

        cal = cls(currents_A=list(currents), fields_mT=avg_fields, hall=hall)
        if subtract_remanence:
            cal.remove_remanence()
        return cal

    def remove_remanence(self) -> None:
        """Shift the whole curve so that field at zero current is exactly zero."""
        if not self.currents_A:
            return
        b_at_zero = _interp(self.currents_A, self.fields_mT, 0.0)
        self.fields_mT = [b - b_at_zero for b in self.fields_mT]

    # ---- the two lookups --------------------------------------------------

    def field_for_current(self, current_A: float) -> float:
        return _interp(self.currents_A, self.fields_mT, current_A)

    def current_for_field(self, field_mT: float) -> float:
        """Inverse lookup. Assumes B(I) is monotonic increasing (it is for an
        electromagnet after remanence removal); we interpolate I over B."""
        # fields_mT is ascending because currents_A is ascending and B rises
        # with I. If a stray non-monotonic point appears we still clamp safely.
        return _interp(self.fields_mT, self.currents_A, field_mT)

    @property
    def range_mT(self) -> Tuple[float, float]:
        if not self.fields_mT:
            return (0.0, 0.0)
        return (min(self.fields_mT), max(self.fields_mT))

    # ---- persistence ------------------------------------------------------

    def save(self, path: str) -> None:
        """Human-readable text: Hall params in a header, then I,B rows."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# clMag-control field calibration\n")
            fh.write("# Hall probe parameters used to acquire this curve:\n")
            fh.write(f"sensitivity_mT_per_mV = {self.hall.sensitivity_mT_per_mV}\n")
            fh.write(f"correction = {self.hall.correction}\n")
            fh.write(f"offset_mV = {self.hall.offset_mV}\n")
            fh.write("# current_A, field_mT\n")
            for I, B in zip(self.currents_A, self.fields_mT):
                fh.write(f"{I:.6f}, {B:.6f}\n")

    @classmethod
    def load(cls, path: str) -> "FieldCalibration":
        hall = HallProbe()
        currents: List[float] = []
        fields_: List[float] = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip()
                    if hasattr(hall, key):
                        setattr(hall, key, float(val))
                    continue
                parts = line.split(",")
                if len(parts) == 2:
                    currents.append(float(parts[0]))
                    fields_.append(float(parts[1]))
        return cls(currents_A=currents, fields_mT=fields_, hall=hall)
