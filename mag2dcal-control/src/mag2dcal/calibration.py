"""The measured calibration: field versus drive voltage, per axis, per sweep leg.

WHY THIS FILE EXISTS. The plain PI of `mag2d-control` reaches a field by pushing
until the error is small. This module instead does what clMag-control does with
the 1-axis magnet: it MEASURES the magnet once (B against the drive voltage),
and afterwards a new setpoint is reached mostly by a single calibrated JUMP, with
the PI only trimming the last couple of millitesla. A measurement beats a
feedback loop here because iron is slow and the loop's own hunting is what makes
it dither.

WHY TWO LEGS PER AXIS. Iron has hysteresis: at the same drive voltage the field
is higher when you arrived from above than from below. A single averaged curve
(clMag averages its up and down legs) therefore has a built-in error of half the
hysteresis width. Here we keep the two legs SEPARATE:

    up leg    measured while the drive voltage was INCREASING
    down leg  measured while the drive voltage was DECREASING

and the seek picks the leg that matches the direction it is about to approach
from. That is the whole point of the undershoot in the controller: by always
arriving from one side we know which branch of the hysteresis loop we are on, so
the calibration can be right instead of average.

The tables are plain lists of (volts, millitesla) pairs and the lookups are
linear interpolation, clamped at both ends -- no fitting, no numpy. `bisect`
from the standard library finds the segment in log time.

FILE FORMAT. One JSON file per calibration in the project's `Calibrations`
folder, written with encoding="utf-8" (suite gotcha #27):

    {
      "module": "mag2dcal",
      "created": "2026-09-19T14:03:11",
      "note": "measured with the 63x objective in place",
      "hall": {...},                       # the probe constants it was taken with
      "axes": [
        {"axis": "x",
         "up":   [[-5.0, -99.1], ..., [5.0, 100.4]],
         "down": [[-5.0, -100.3], ..., [5.0, 99.2]]},
        {"axis": "y", ...}
      ]
    }

JSON, not clMag's .txt, because there are four tables instead of one and a
header of key = value lines would need inventing a section syntax.
"""

from __future__ import annotations

import bisect
import datetime as _dt
import json
import os
from dataclasses import dataclass, field, asdict, fields as dataclass_fields
from pathlib import Path
from typing import List, Sequence, Tuple

from .config import Hall

#: The two hysteresis branches. `UP` is the one to use when the field on that
#: axis is RISING toward the target, `DOWN` when it is falling.
UP, DOWN = "up", "down"

AXIS_NAMES = ("x", "y")


def leg_name(direction) -> str:
    """Normalise an approach direction to a leg name.

    Accepts the names "up"/"down" themselves, or a sign: +1 means the field on
    that axis is rising toward the target, so we arrive with the drive voltage
    increasing -- the UP leg.
    """
    if isinstance(direction, str):
        return DOWN if direction.lower() == DOWN else UP
    return UP if direction >= 0 else DOWN


def _interp(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    """Linear interpolation of y at x over ascending xs, CLAMPED at both ends.

    Clamping (rather than extrapolating) is deliberate: past the last measured
    point we have no idea what the magnet does, and a straight line carried on
    into saturation would ask for a voltage that does not exist. The caller
    checks `range_mT` when it wants to warn the user.
    """
    if not xs:
        raise ValueError("empty calibration leg")
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, x)          # xs[i-1] <= x < xs[i]
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    if x1 == x0:
        return y0
    return y0 + (x - x0) / (x1 - x0) * (y1 - y0)


def _strictly_increasing(pairs: List[Tuple[float, float]], key: int):
    """Sort (a, b) pairs on element `key` and drop any that do not advance it.

    An interpolation table needs a strictly increasing x column. The measured
    field is monotone in the drive voltage for a real magnet, but noise on a
    nearly flat piece of the curve (or a repeated point at the sweep's turning
    point) can produce two samples with the same x, and bisect would then divide
    by zero. Dropping the duplicate is the honest fix: we cannot invert a curve
    that does not move.
    """
    out: List[Tuple[float, float]] = []
    for p in sorted(pairs, key=lambda q: q[key]):
        if out and p[key] <= out[-1][key]:
            continue
        out.append(p)
    return out


@dataclass
class AxisCalibration:
    """One axis: two measured legs of (drive volts, field mT)."""

    up: List[Tuple[float, float]] = field(default_factory=list)
    down: List[Tuple[float, float]] = field(default_factory=list)

    # ---- access ----------------------------------------------------------

    def leg(self, direction) -> List[Tuple[float, float]]:
        """The leg for an approach direction (+1 rising, -1 falling) or the name
        UP/DOWN. Falls back to the other leg if only one of them was measured."""
        want = self.up if leg_name(direction) == UP else self.down
        other = self.down if want is self.up else self.up
        return want if want else other

    @property
    def is_empty(self) -> bool:
        return not (self.up or self.down)

    def field_for_volts(self, volts: float, direction=UP) -> float:
        pairs = _strictly_increasing(self.leg(direction), 0)
        return _interp([v for v, _ in pairs], [b for _, b in pairs], volts)

    def volts_for_field(self, field_mT: float, direction=UP) -> float:
        """The inverse lookup: which drive voltage gives this field, on this leg."""
        pairs = _strictly_increasing(self.leg(direction), 1)
        return _interp([b for _, b in pairs], [v for v, _ in pairs], field_mT)

    @property
    def range_mT(self) -> Tuple[float, float]:
        """The field range BOTH legs cover, so either branch can be used.

        The two legs are offset from each other by the hysteresis, so their
        ranges are not identical; the overlap is what we can honestly promise.
        """
        spans = []
        for leg in (self.up, self.down):
            if leg:
                bs = [b for _, b in leg]
                spans.append((min(bs), max(bs)))
        if not spans:
            return (0.0, 0.0)
        return (max(s[0] for s in spans), min(s[1] for s in spans))

    @property
    def n_points(self) -> int:
        return len(self.up) + len(self.down)


@dataclass
class Calibration:
    """The whole magnet: one AxisCalibration per axis, plus provenance."""

    axes: List[AxisCalibration] = field(default_factory=lambda: [AxisCalibration(),
                                                                 AxisCalibration()])
    created: str = ""
    note: str = ""
    hall: Hall = field(default_factory=Hall)

    # ---- access ----------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return all(a.is_empty for a in self.axes)

    @property
    def n_points(self) -> int:
        return sum(a.n_points for a in self.axes)

    def volts_for_field(self, axis: int, field_mT: float, direction=UP) -> float:
        return self.axes[axis].volts_for_field(field_mT, direction)

    def field_for_volts(self, axis: int, volts: float, direction=UP) -> float:
        return self.axes[axis].field_for_volts(volts, direction)

    def range_mT(self, axis: int) -> Tuple[float, float]:
        return self.axes[axis].range_mT

    def field_max_mT(self) -> float:
        """The largest |B| this calibration supports at ANY angle.

        A vector setpoint asks for Bx = B cos a and By = B sin a, so the
        magnitude we can promise in every direction is limited by the WEAKEST
        of the four half-ranges (both axes, both signs).
        """
        best = None
        for a in self.axes:
            if a.is_empty:
                continue
            lo, hi = a.range_mT
            half = min(abs(lo), abs(hi))
            best = half if best is None else min(best, half)
        return 0.0 if best is None else best

    # ---- the wire / disk format -----------------------------------------

    def to_dict(self) -> dict:
        return {
            "module": "mag2dcal",
            "created": self.created,
            "note": self.note,
            "hall": asdict(self.hall),
            "axes": [
                {"axis": AXIS_NAMES[i] if i < len(AXIS_NAMES) else str(i),
                 "up": [[float(v), float(b)] for v, b in a.up],
                 "down": [[float(v), float(b)] for v, b in a.down]}
                for i, a in enumerate(self.axes)
            ],
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "Calibration | None":
        if not d:
            return None
        axes = []
        for entry in d.get("axes", []):
            axes.append(AxisCalibration(
                up=[(float(v), float(b)) for v, b in entry.get("up", [])],
                down=[(float(v), float(b)) for v, b in entry.get("down", [])]))
        while len(axes) < 2:
            axes.append(AxisCalibration())
        hall_d = d.get("hall") or {}
        known = {f.name for f in dataclass_fields(Hall)}
        hall = Hall(**{k: float(v) for k, v in hall_d.items() if k in known})
        return cls(axes=axes, created=d.get("created", ""), note=d.get("note", ""),
                   hall=hall)

    def save(self, path: str | os.PathLike) -> None:
        # encoding="utf-8" everywhere: the Windows default is cp1252 and a note
        # with a degree sign in it would come back as mojibake (gotcha #27).
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Calibration":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def summary(self) -> str:
        """One ASCII line for a log message or a GUI label."""
        if self.is_empty:
            return "empty calibration"
        lo = min(self.range_mT(i)[0] for i in range(len(self.axes)))
        hi = max(self.range_mT(i)[1] for i in range(len(self.axes)))
        when = f", {self.created}" if self.created else ""
        return (f"{self.n_points} points, {lo:.1f} .. {hi:.1f} mT per axis, "
                f"|B| up to {self.field_max_mT():.1f} mT at any angle{when}")


# ------------------------------------------------------------------ the sweep

@dataclass(frozen=True)
class CalStep:
    """One step of a calibration sweep: drive `axis` to `volts` and, if
    `record`, note the field once it has settled. `leg` says which table the
    sample belongs to ("" for steps that only position the magnet)."""

    axis: int
    volts: float
    leg: str = ""
    record: bool = False


def sweep_plan(n_per_leg: int, v_max_V: float, limit_V: float | None = None,
               n_axes: int = 2) -> List[CalStep]:
    """The whole measurement, as a flat list of steps the controller executes.

    Per axis (the other axis is held at 0 V throughout, so its cross-talk
    contributes nothing that would change between points):

        1. PRIME: drive a little PAST +v_max. This puts the iron at one end of
           its hysteresis loop, so the following leg starts from a KNOWN
           magnetic history instead of from whatever the magnet was doing.
        2. DOWN leg: n_per_leg points from +v_max down to -v_max, recorded.
           Every sample on it was reached with the voltage decreasing.
        3. TURN: a little past -v_max, for the same reason.
        4. UP leg: n_per_leg points from -v_max back up to +v_max, recorded.
        5. Return to 0 V before moving on to the other axis.

    WHY THE OVER-TRAVEL. Without steps 1 and 3 going past the ends, the FIRST
    sample of each leg would sit exactly at the turning point -- reached with
    the drive still moving the other way, so recorded on the wrong branch. One
    extra step's worth of over-travel (clamped to the amplifier limit) costs two
    moves and makes both endpoints honest. If v_max is already the limit there
    is no room, and those two endpoints are worth half the hysteresis less than
    they claim.

    Each leg is then measured over the FULL range in one direction, which is
    exactly the shape the seek needs. Total steps = n_axes * (2*n_per_leg + 3).
    """
    n = max(2, int(n_per_leg))
    v = abs(float(v_max_V))
    span = 2 * v / (n - 1)                                    # one leg step
    over = v + span if limit_V is None else min(abs(limit_V), v + span)
    steps: List[CalStep] = []
    for axis in range(n_axes):
        steps.append(CalStep(axis, +over))                    # prime
        for i in range(n):                                    # +v -> -v
            steps.append(CalStep(axis, v - span * i, DOWN, True))
        steps.append(CalStep(axis, -over))                    # turn
        for i in range(n):                                    # -v -> +v
            steps.append(CalStep(axis, -v + span * i, UP, True))
        steps.append(CalStep(axis, 0.0))                      # park
    return steps


def build_calibration(points, hall: Hall | None = None, note: str = "",
                      n_axes: int = 2) -> Calibration:
    """Turn the recorded samples into a Calibration.

    `points` is a list of (axis, leg, volts, field_mT) in the order they were
    measured; each leg is sorted by voltage here, because the interpolation
    tables want an ascending x column and the down leg was measured descending.
    """
    axes = [AxisCalibration() for _ in range(n_axes)]
    for axis, leg, volts, field_mT in points:
        table = axes[axis].up if leg == UP else axes[axis].down
        table.append((float(volts), float(field_mT)))
    for a in axes:
        a.up.sort(key=lambda p: p[0])
        a.down.sort(key=lambda p: p[0])
    return Calibration(axes=axes, note=note, hall=hall or Hall(),
                       created=_dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))


# ------------------------------------------------------------------ the folder

def project_root() -> Path:
    """`<...>/mag2dcal-control`, found from this file (src/mag2dcal/calibration.py)."""
    return Path(__file__).resolve().parents[2]


def calibration_dir(directory: str) -> Path:
    """Resolve the configured folder; a relative name is relative to the project
    root, so the default "Calibrations" is the same folder whatever the current
    working directory happens to be when the service is launched."""
    p = Path(directory).expanduser()
    return p if p.is_absolute() else project_root() / p


def newest_calibration(directory: str) -> Path | None:
    """The most recently modified *.json in the folder, or None."""
    d = calibration_dir(directory)
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def default_filename() -> str:
    return _dt.datetime.now().strftime("mag2dcal_%Y%m%d_%H%M%S.json")
