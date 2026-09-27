"""A pure-Python simulator of an AG-UC2 driving two Agilis actuators.

It implements every method of :class:`agilis.backends.base.AgilisBackend`, so
the whole module -- GUI, service, client, tests -- runs with no hardware and no
drivers. This is the DEFAULT backend.

"Just enough physics" to make the micrometre language honest:

* Each axis keeps TWO positions: the controller's step COUNTER (what TP reports)
  and the TRUE position of the moving part in um (what the sample feels). They
  are different things on an open-loop stage, and the simulator keeps them
  apart so the brain cannot cheat.
* The step size depends on the step AMPLITUDE (1..50) non-linearly, with a
  threshold: below ~amplitude 5 the piezo does not overcome static friction and
  nothing moves at all (the manual warns exactly this). Forward and backward
  steps differ (~15 %), and every step scatters by a few percent.
* The travel has END STOPS (12 mm, an AG-LS25). At a stop the controller keeps
  counting while the part does not move -- the classic way an open-loop counter
  drifts from reality -- and the limit switch (PH) reports it.
* Motion happens at the controller's fixed rates: a PR move at PR_RATE, a jog at
  one of the four JA speeds; JA 2/3 use the maximum amplitude whatever SU says.
* Commands the controller would refuse in the current state raise
  RuntimeError with the same meaning as the controller's TE codes.

Everything is advanced lazily on each call (no thread), from the wall clock.
"""

from __future__ import annotations

import math
import random
import time

from ..config import AMPLITUDE_DEFAULT, AMPLITUDE_MAX, AMPLITUDE_MIN, Config
from .base import JOGGING, READY, STEPPING

#: Stepping rate of a PR move, steps/s. The manual gives no number for PR; the
#: sim assumes the same 666 steps/s as a JA 4 jog ("at defined step
#: amplitude"). # VERIFY on the controller (time a 2000-step PR).
PR_RATE = 666.0

#: JA speed table (manual, JA command): mode -> (steps/s, uses max amplitude?)
JOG_TABLE = {1: (5.0, False), 2: (100.0, True), 3: (1700.0, True), 4: (666.0, False)}

#: Travel of the simulated stage (AG-LS25: 12 mm), um.
TRAVEL_UM = 12000.0
#: Amplitude below which a step does not move the part at all.
THRESHOLD_AMP = 4.0


class SimAgilis:
    """Simulated AG-UC2 + two Agilis linear actuators."""

    def __init__(self, cfg: Config, seed: int | None = 1234):
        self.cfg = cfg
        self._rng = random.Random(seed)
        self.pr_rate = PR_RATE         # tests raise this to keep them short
        # per controller axis (index 0 = axis 1, 1 = axis 2)
        self._count = [0, 0]                   # the TP step counter
        self._true = [1200.0, -800.0]          # where the part really is, um
        self._amp = [[AMPLITUDE_DEFAULT, AMPLITUDE_DEFAULT] for _ in range(2)]  # [fwd, bwd]
        # Largest step size (um) at amplitude 50, forward / backward. Two
        # actuators are never identical, and backward is the weaker direction
        # here (the manual only says they differ).
        self._s_max = [[0.30, 0.26], [0.28, 0.25]]
        self._state = [READY, READY]
        self._rate = [0.0, 0.0]                # steps/s of the running motion
        self._dir = [0, 0]                     # +1 / -1
        self._left = [0, 0]                    # PR steps still to go (jog: unused)
        self._use_max = [False, False]         # JA 2/3: max amplitude
        self._t0 = [0.0, 0.0]                  # time the motion (re)started
        self._done = [0, 0]                    # steps issued since _t0
        self._opened = False
        self._remote = False

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        self._opened = True
        self._remote = True                    # the real driver sends MR here

    def close(self) -> None:
        for i in (0, 1):
            self._advance(i)
            self._state[i] = READY
        self._opened = False
        self._remote = False

    def idn(self) -> str:
        return "SIM AG-UC2 v0.0 (simulated Agilis controller, 2 axes)"

    # -- physics ----------------------------------------------------------- #
    def step_size_um(self, idx: int, direction: int, amplitude: float) -> float:
        """Mean distance of ONE step at an amplitude (0 below the threshold).

        A power law above a threshold: small amplitudes barely beat static
        friction, big ones saturate. Only the SHAPE matters -- the manual
        insists there is no fixed relation.
        """
        if amplitude <= THRESHOLD_AMP:
            return 0.0
        frac = (amplitude - THRESHOLD_AMP) / (AMPLITUDE_MAX - THRESHOLD_AMP)
        return self._s_max[idx][0 if direction > 0 else 1] * frac ** 1.3

    def true_um(self, hw_axis: int) -> float:
        """Where the part really is (tests only: the real stage cannot say)."""
        idx = hw_axis - 1
        self._advance(idx)
        return self._true[idx]

    def _apply_steps(self, idx: int, n: int) -> None:
        """Issue n steps in the current direction: count all, move if free."""
        if n <= 0:
            return
        d = self._dir[idx]
        amp = AMPLITUDE_MAX if self._use_max[idx] else self._amp[idx][0 if d > 0 else 1]
        mean = self.step_size_um(idx, d, amp)
        # n steps with ~4 % scatter each: the sum scatters by 4 % * sqrt(n)
        dist = n * mean + self._rng.gauss(0.0, 0.04 * mean * math.sqrt(n))
        self._count[idx] += d * n
        half = TRAVEL_UM / 2.0
        self._true[idx] = min(half, max(-half, self._true[idx] + d * max(dist, 0.0)))

    def _advance(self, idx: int) -> None:
        if self._state[idx] == READY:
            return
        due = int(self._rate[idx] * (time.monotonic() - self._t0[idx])) - self._done[idx]
        if self._state[idx] == STEPPING:
            due = min(due, self._left[idx])
        if due > 0:
            self._apply_steps(idx, due)
            self._done[idx] += due
            if self._state[idx] == STEPPING:
                self._left[idx] -= due
                if self._left[idx] <= 0:
                    self._state[idx] = READY

    def _start(self, idx: int, state: int, direction: int, rate: float, use_max: bool) -> None:
        self._state[idx] = state
        self._dir[idx] = direction
        self._rate[idx] = rate
        self._use_max[idx] = use_max
        self._t0[idx] = time.monotonic()
        self._done[idx] = 0

    def _check(self, hw_axis: int) -> int:
        if hw_axis not in (1, 2):
            raise RuntimeError("controller error -2: axis out of range (must be 1 or 2)")
        if not self._remote:
            raise RuntimeError("controller error -5: not allowed in local mode")
        idx = hw_axis - 1
        self._advance(idx)
        return idx

    # -- motion ------------------------------------------------------------ #
    def move_by(self, hw_axis: int, delta_steps: int) -> None:
        idx = self._check(hw_axis)
        if self._state[idx] != READY:
            raise RuntimeError("controller error -6: not allowed in current state")
        n = int(delta_steps)
        if n == 0:
            return
        self._left[idx] = abs(n)
        self._start(idx, STEPPING, 1 if n > 0 else -1, self.pr_rate, False)

    def jog(self, hw_axis: int, mode: int) -> None:
        idx = self._check(hw_axis)
        mode = int(mode)
        if self._state[idx] not in (READY, JOGGING):
            raise RuntimeError("controller error -6: not allowed in current state")
        if abs(mode) > 4:
            raise RuntimeError("controller error -4: parameter out of range")
        if mode == 0:
            self._state[idx] = READY
            return
        rate, use_max = JOG_TABLE[abs(mode)]
        self._start(idx, JOGGING, 1 if mode > 0 else -1, rate, use_max)

    def stop(self, hw_axis: int) -> None:
        idx = self._check(hw_axis)
        self._state[idx] = READY

    def read_position(self, hw_axis: int) -> int:
        idx = self._check(hw_axis)
        return int(self._count[idx])

    def axis_state(self, hw_axis: int) -> int:
        idx = hw_axis - 1           # TS works in local mode too
        self._advance(idx)
        return int(self._state[idx])

    def zero_counter(self, hw_axis: int) -> None:
        idx = self._check(hw_axis)
        if self._state[idx] != READY:
            raise RuntimeError("controller error -6: not allowed in current state")
        self._count[idx] = 0

    # -- drive ------------------------------------------------------------- #
    def set_amplitude(self, hw_axis: int, direction: int, amplitude: int) -> None:
        idx = self._check(hw_axis)
        if self._state[idx] != READY:
            raise RuntimeError("controller error -6: not allowed in current state")
        a = int(amplitude)
        if not AMPLITUDE_MIN <= a <= AMPLITUDE_MAX:
            raise RuntimeError("controller error -4: parameter out of range")
        self._amp[idx][0 if direction > 0 else 1] = a

    def read_amplitude(self, hw_axis: int, direction: int) -> int:
        idx = self._check(hw_axis)
        return int(self._amp[idx][0 if direction > 0 else 1])

    def limit_status(self) -> int:
        """PH: bit 0 = axis 1 at a limit switch, bit 1 = axis 2."""
        bits = 0
        for idx in (0, 1):
            self._advance(idx)
            if abs(self._true[idx]) >= TRAVEL_UM / 2.0 - 5.0:
                bits |= 1 << idx
        return bits
