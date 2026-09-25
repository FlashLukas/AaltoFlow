"""A simulated DynaCool, so everything runs with no cryostat and no MultiVu.

Kept deliberately plain -- it has to behave like the real system in the ways
the brain and a scan depend on, not model a superconducting magnet:

  * a new setpoint STARTS a ramp at the commanded rate and returns at once
    (fire-and-forget, exactly like MultiVu);
  * while moving the field status is "Ramping", and once there it is
    "Holding (driven)" -- the DynaCool's word for "at field" (it has no
    persistent mode);
  * the temperature sweeps at its rate, reports "Chasing" on the way, "Near"
    for a short settling time after it arrives, then "Stable";
  * readings carry a little noise, so a tolerance check is a real check.

Position is computed from the (injectable) clock rather than stepped by a
thread: a ramp re-anchors where it is whenever a new command arrives, so a
setpoint change mid-ramp never teleports the field.
"""

from __future__ import annotations

import random
import time


class _Ramp:
    """A value that moves linearly towards a target at a rate (units per s)."""

    def __init__(self, value: float, clock):
        self._clock = clock
        self.start = float(value)
        self.target = float(value)
        self.rate = 1.0
        self.t0 = clock()

    def value(self) -> float:
        span = self.target - self.start
        travelled = self.rate * max(0.0, self._clock() - self.t0)
        if travelled >= abs(span):
            return self.target
        return self.start + travelled * (1 if span > 0 else -1)

    def arrived_at(self) -> float:
        """Clock time at which the ramp reaches (or reached) its target."""
        return self.t0 + abs(self.target - self.start) / max(self.rate, 1e-12)

    def go(self, target: float, rate: float) -> None:
        self.start = self.value()                 # re-anchor where we ARE now
        self.t0 = self._clock()
        self.target = float(target)
        self.rate = max(float(rate), 1e-9)


class SimulatedDynaCool:
    simulated = True

    #: how long the temperature sits in "Near" after arriving, before "Stable"
    NEAR_S = 2.0

    def __init__(self, field_mT: float = 0.0, temperature_K: float = 300.0,
                 clock=time.monotonic, noise: bool = True, seed: int | None = None):
        self._clock = clock
        self._rng = random.Random(seed)
        self._noise = noise
        self._field = _Ramp(field_mT, clock)
        self._field.rate = 10.0
        self._temp = _Ramp(temperature_K, clock)
        self._temp.rate = 20.0 / 60.0
        self._field_approach = "linear"
        self._temp_approach = "fast_settle"
        self._open = False
        self.chamber = "Purged and Sealed"

    # ---- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def idn(self) -> str:
        return "DYNACOOL (simulated)"

    # ---- field ------------------------------------------------------------------

    def read_field(self) -> tuple[float, str]:
        v = self._field.value()
        at = self._clock() >= self._field.arrived_at()
        noise = self._rng.gauss(0.0, 0.003) if self._noise else 0.0
        return v + noise, ("Holding (driven)" if at else "Ramping")

    def read_field_setpoint(self) -> tuple[float, float, str]:
        return self._field.target, self._field.rate, self._field_approach

    def set_field(self, field_mT: float, rate_mT_per_s: float, approach: str) -> None:
        self._field_approach = approach
        self._field.go(field_mT, rate_mT_per_s)

    # ---- temperature ------------------------------------------------------------

    def read_temperature(self) -> tuple[float, str]:
        v = self._temp.value()
        now = self._clock()
        arrived = self._temp.arrived_at()
        if now < arrived:
            status = "Chasing"
        elif now < arrived + self.NEAR_S:
            status = "Near"
        else:
            status = "Stable"
        noise = self._rng.gauss(0.0, 0.002) if self._noise else 0.0
        return v + noise, status

    def read_temperature_setpoint(self) -> tuple[float, float, str]:
        return self._temp.target, self._temp.rate * 60.0, self._temp_approach

    def set_temperature(self, temperature_K: float, rate_K_per_min: float,
                        approach: str) -> None:
        self._temp_approach = approach
        self._temp.go(temperature_K, rate_K_per_min / 60.0)

    # ---- chamber ------------------------------------------------------------------

    def read_chamber(self) -> str:
        return self.chamber
