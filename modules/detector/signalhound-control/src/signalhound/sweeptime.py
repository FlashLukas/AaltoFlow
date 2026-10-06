"""The sweep-time estimate that LEARNS from the sweeps this analyser really took.

Why (lab PC, 2026-10-06): an SA124B swept 0.9-12 GHz at RBW 6 MHz (11101
points) in 4.65 s, but the estimate said 82 s -- it used constants measured on
the SA44B, which is ~18x slower over a wide span. A fitted model for every
model, RBW and span would need a timing series nobody has measured (Lukas
declined one for now). The instrument itself, though, tells us how long each
sweep took: the brain times every sweep it finishes. So:

  * same settings as a sweep already timed -> report THAT duration (the last
    one; a sweep's duration does not drift, and the last one is the truth for
    the settings now on the instrument);
  * other settings -> take the NEAREST timed sweep of the same model and mode
    and scale it: by the span (spectrum mode -- the analyser steps its LO
    across the span, so time ~ span at one RBW) and by the number of points
    (time ~ points in a TG sweep, one point per step; a narrow RBW in
    spectrum mode shows up as more points for the same span);
  * nothing timed yet for this model and mode -> the backend's a-priori
    estimate (per-model constants in backends/sa_api.py).

The estimate only feeds the `sweep_time_s` readout, the progress bar and a
scan's ETA. The DATA never depend on it: a sweep ends when the instrument
returns it.

Not thread-safe on its own: the brain calls it with its _lock held.
"""

from __future__ import annotations

import math
from collections import OrderedDict

from .instruments import SweepSettings

#: how many different settings are remembered (oldest forgotten first). A
#: scan that steps the span would otherwise grow the table without bound.
MAX_ENTRIES = 64


def setting_key(model: str, s: SweepSettings, points: int) -> tuple:
    """What makes two sweeps take the same time: the model, the mode (spectrum
    or TG), start, stop, RBW and the number of points. Rounded to 1 Hz so a
    float round-trip through a setter does not make a new key."""
    start = s.center_Hz - s.span_Hz / 2
    stop = s.center_Hz + s.span_Hz / 2
    return (model or "", bool(s.tg_on), round(start), round(stop), round(s.rbw_Hz),
            int(points))


class SweepTimeLearner:
    def __init__(self) -> None:
        # key -> (span_Hz, points, rbw_Hz, seconds)
        self._timed: OrderedDict[tuple, tuple[float, int, float, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._timed)

    def record(self, model: str, s: SweepSettings, points: int, seconds: float) -> None:
        """Remember how long one finished sweep took."""
        if not (seconds > 0 and math.isfinite(seconds)) or points < 1:
            return
        key = setting_key(model, s, points)
        self._timed.pop(key, None)
        self._timed[key] = (float(s.span_Hz), int(points), float(s.rbw_Hz), float(seconds))
        while len(self._timed) > MAX_ENTRIES:
            self._timed.popitem(last=False)

    def estimate(self, model: str, s: SweepSettings, points: int) -> float | None:
        """The learned duration for these settings, or None if this model and
        mode has never been timed (the caller then uses its a-priori value)."""
        key = setting_key(model, s, points)
        hit = self._timed.get(key)
        if hit is not None:
            return hit[3]
        same = [v for k, v in self._timed.items() if k[0] == key[0] and k[1] == key[1]]
        if not same:
            return None
        span, pts, rbw = max(float(s.span_Hz), 1.0), max(int(points), 1), max(s.rbw_Hz, 1e-3)

        def distance(v):
            # log distances: 2x the span is as far as 2x the points
            return (abs(math.log(span / max(v[0], 1.0)))
                    + abs(math.log(pts / max(v[1], 1)))
                    + abs(math.log(rbw / max(v[2], 1e-3))))
        span0, pts0, _, t0 = min(same, key=distance)
        if s.tg_on:
            factor = pts / max(pts0, 1)
        else:
            # geometric mean of the span and point ratios: at one RBW the two
            # are equal (points ~ span) and this is plain span scaling; when
            # only the RBW changed it grows with the extra points, half-way
            # in log -- the honest middle of what we do not know (# VERIFY).
            factor = math.sqrt((span / max(span0, 1.0)) * (pts / max(pts0, 1)))
        return float(min(t0 * factor, 600.0))
