"""The ramper: current is never allowed to jump, it moves in steps.

In LabVIEW this was the little loop that nudged the setpoint by one increment
every few milliseconds. Here it is a tiny state machine with ONE rule worth
remembering:

    A requested change SMALLER than one increment is applied directly.

That rule is what lets a small PID correction (a few mA) take effect on the very
next control tick instead of waiting for a ramp. Larger moves are stepped.

The ramper is clock-free on purpose: it does not sleep. It just answers "given
where the setpoint is now, where should it be after one step?" The control loop
owns the timing and calls step() once per `delay_s`.
"""

from __future__ import annotations

import math


class Ramper:
    def __init__(self, increment_A: float, delay_s: float):
        self.increment = abs(increment_A)
        self.delay_s = delay_s
        self._setpoint = 0.0     # where the output is right now
        self._target = 0.0       # where we are heading

    def sync_to(self, current_A: float) -> None:
        """Tell the ramper the true present output (e.g. read from the supply),
        so the next move starts from reality, not from a stale internal value."""
        self._setpoint = current_A
        self._target = current_A

    def go_to(self, target_A: float) -> None:
        self._target = target_A

    @property
    def setpoint(self) -> float:
        return self._setpoint

    @property
    def target(self) -> float:
        return self._target

    @property
    def done(self) -> bool:
        return abs(self._target - self._setpoint) < 1e-12

    @property
    def is_small_move(self) -> bool:
        """True when the remaining distance is within one increment, i.e. the
        next step will jump straight to target with no further ramping."""
        return abs(self._target - self._setpoint) <= self.increment

    def step(self) -> float:
        """Advance the setpoint one increment toward target (or snap to it if
        the remainder is within one increment) and return the new setpoint."""
        delta = self._target - self._setpoint
        if abs(delta) <= self.increment:
            self._setpoint = self._target          # the direct-set rule
        else:
            self._setpoint += math.copysign(self.increment, delta)
        return self._setpoint
