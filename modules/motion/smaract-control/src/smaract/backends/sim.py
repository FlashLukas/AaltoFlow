"""A pure-Python simulator of the CLL42 positioner on its SCU (section 3).

It implements every method of :class:`smaract.backends.base.SmaractBackend`, so
the whole application -- GUI, service, client, tests -- runs with no hardware
and no SmarAct library installed. This is the DEFAULT backend.

"Just enough physics" to behave like the real thing where it matters:

* **A rail with end stops.** The carriage lives on an absolute scale (mm) and
  cannot pass the end stops at +-``rail_stop_mm``. A move aimed beyond one
  stalls there -- a stick-slip drive simply slips on the spot -- and stops
  short of its target, which the brain notices ("not on target").
* **An unreferenced encoder.** At power-on the counter reads 0 wherever the
  carriage stands (``power_on_mm`` on the real scale). Until referenced, every
  reported position is off by that amount.
* **Distance-coded reference marks.** Marks sit every ``MARK_PITCH_MM``, with
  every other one shifted by a few tens of um -- the spacing between two
  neighbours is a code for where they are. ``find_reference`` therefore needs
  to cross TWO marks: it drives forward (backwards if the end stop is closer)
  to the second mark ahead, and from then on the scale is absolute.
* **Closed-loop stepping.** Speed = step frequency x step length. The last few
  um are approached slowly (the loop reduces the step amplitude), and the
  carriage parks within about one encoder count of the target -- never
  exactly on it. Positions are quantised to the encoder count.
* **Hold.** With a hold time the channel reports "holding" for that long after
  arrival, then "stopped".

Time is integrated lazily in 1 ms slices whenever something is read, so no
thread is needed (the brain's poll thread reads often enough).
"""

from __future__ import annotations

import math
import random
import time

from ..config import Config

#: Nominal spacing of the reference marks, mm; odd marks are shifted by the
#: code offset so that each neighbouring pair has a unique spacing.
MARK_PITCH_MM = 10.0
MARK_CODE_MM = 0.02


class SimScu:
    """Simulated SCU channel + CLL42 carriage."""

    def __init__(self, cfg: Config, *, power_on_mm: float = 23.4567,
                 rail_stop_mm: float = 118.0, seed: int = 7,
                 freq_hz: int = 1000, referenced: bool = False):
        """``freq_hz`` and ``referenced`` describe the state the controller is
        ALREADY in when the service connects (left there by an earlier session
        or another program). The brain must adopt them, never reset them --
        tests start the sim in a non-default state to prove that."""
        self.cfg = cfg
        self.rail_stop_mm = float(rail_stop_mm)
        self._rng = random.Random(seed)

        self._x = float(power_on_mm)       # true carriage position, absolute mm
        # Where the COUNTER reads zero: the power-on spot, or the scale's own
        # zero if an earlier session already referenced the axis (the SCU
        # keeps "position known" for as long as it stays powered).
        self._origin = 0.0 if referenced else float(power_on_mm)
        self._known = bool(referenced)     # physical position known?

        self._state = "stopped"
        self._target = self._x             # absolute mm the loop is chasing
        self._hold_ms = 0
        self._hold_until = 0.0
        self._ref_pending = False          # the current move is a reference search
        self._freq_hz = int(freq_hz)
        self._t_last = time.monotonic()
        self._opened = False

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        self._opened = True
        self._t_last = time.monotonic()

    def close(self) -> None:
        self._opened = False

    def idn(self) -> str:
        return "SIM SmarAct SCU + CLL42 linear positioner (simulator)"

    def sensor_present(self) -> bool:
        return True

    # -- physics ----------------------------------------------------------- #
    def _count_mm(self) -> float:
        return self.cfg.hardware.nm_per_count * 1e-6

    def _speed_mm_s(self) -> float:
        return self._freq_hz * self.cfg.hardware.um_per_step * 1e-3

    def _advance(self) -> None:
        """Integrate the motion up to now, in 1 ms slices."""
        now = time.monotonic()
        dt_total = now - self._t_last
        self._t_last = now
        if self._state == "holding" and now >= self._hold_until:
            self._state = "stopped"
        if self._state not in ("targeting", "moving_to_reference"):
            return
        count = self._count_mm()
        v = self._speed_mm_s()
        steps = max(1, int(math.ceil(dt_total / 0.001)))
        dt = dt_total / steps
        for _ in range(steps):
            dist = self._target - self._x
            if abs(dist) <= count:
                self._arrive()
                return
            # The last ~20 um are approached at a speed proportional to the
            # distance left (the loop turns the step amplitude down), which is
            # what makes a closed-loop stick-slip drive stop without overshoot.
            v_eff = min(v, max(abs(dist) / 0.02 * v, 0.05))
            step = math.copysign(min(abs(dist), v_eff * dt), dist)
            x_new = self._x + step
            if abs(x_new) >= self.rail_stop_mm:
                # End stop: the piezo keeps slipping, the carriage does not
                # move. The SCU gives up and stops (short of the target).
                self._x = math.copysign(self.rail_stop_mm, x_new)
                self._state = "stopped"
                self._ref_pending = False
                return
            self._x = x_new

    def _arrive(self) -> None:
        # The loop parks within about one count, never exactly on the target.
        self._x = self._target + self._rng.uniform(-0.6, 0.6) * self._count_mm()
        if self._ref_pending:
            # Two marks crossed: the scale is now absolute.
            self._known = True
            self._origin = 0.0
            self._ref_pending = False
        if self._hold_ms > 0:
            self._state = "holding"
            self._hold_until = time.monotonic() + self._hold_ms / 1000.0
        else:
            self._state = "stopped"

    def _start(self, target_abs: float, hold_ms: int, ref: bool = False) -> None:
        self._advance()
        self._target = float(target_abs)
        self._hold_ms = int(hold_ms)
        self._ref_pending = ref
        self._state = "moving_to_reference" if ref else "targeting"
        self._t_last = time.monotonic()

    # -- motion ------------------------------------------------------------ #
    def move_absolute(self, position_mm: float, hold_ms: int) -> None:
        # The controller works in COUNTER units: target = counter + origin.
        self._start(float(position_mm) + self._origin, hold_ms)

    def move_relative(self, delta_mm: float, hold_ms: int) -> None:
        self._advance()
        self._start(self._x + float(delta_mm), hold_ms)

    def find_reference(self, hold_ms: int) -> None:
        self._advance()
        self._start(self._second_mark_ahead(), hold_ms, ref=True)

    def _marks(self):
        n = int(self.rail_stop_mm // MARK_PITCH_MM)
        return [k * MARK_PITCH_MM + (MARK_CODE_MM if k % 2 else 0.0)
                for k in range(-n, n + 1)]

    def _second_mark_ahead(self) -> float:
        """Where a reference search ends: the second mark in the search
        direction (forward first; backward if the end stop comes first)."""
        marks = self._marks()
        ahead = [m for m in marks if m > self._x + 1e-3]
        if len(ahead) >= 2:
            return ahead[1]
        behind = [m for m in reversed(marks) if m < self._x - 1e-3]
        return behind[1] if len(behind) >= 2 else self._x

    def stop(self) -> None:
        self._advance()
        self._state = "stopped"
        self._ref_pending = False
        self._target = self._x

    # -- read-back --------------------------------------------------------- #
    def read_position_mm(self) -> float:
        self._advance()
        count = self._count_mm()
        reading = self._x - self._origin
        # While stepping, each slip jolts the carriage by a few tens of nm.
        if self._state in ("targeting", "moving_to_reference"):
            reading += self._rng.uniform(-0.3, 0.3) * count
        return round(reading / count) * count

    def channel_state(self) -> str:
        self._advance()
        return self._state

    def physical_position_known(self) -> bool:
        self._advance()
        return self._known

    # -- speed ------------------------------------------------------------- #
    def set_max_frequency(self, hz: int) -> None:
        self._advance()
        self._freq_hz = int(hz)

    def get_max_frequency(self) -> int:
        return self._freq_hz
