"""A pure-Python simulator for the 3-axis piezo-inertia stage (§3 of guide).

It implements every method of :class:`kim.backends.base.KimBackend`, so the
whole application -- GUI, service, client, tests -- runs with no hardware and no
Thorlabs drivers installed.  This is the DEFAULT backend.

"Just enough physics" to feel lifelike: each axis steps toward its target at a
constant step RATE (steps/s, time-based), reports ``is_moving`` while
travelling, and holds an integer step counter you can reset (``zero_counter``).
That gives the front panel a smoothly advancing read-out to display.

Everything here is in STEPS -- the simulator is deliberately dumb about
micrometres; that translation lives one layer up in the brain.
"""

from __future__ import annotations

import time

from ..config import (
    Config,
    axis_acceleration,
    axis_rate,
    axis_voltage,
)


class SimKim:
    """Simulated KIM101 + 3 PIA25 actuators (open-loop step counters)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.n = 3

        # Live state, per axis (steps).
        self._pos = [0.0, 0.0, 0.0]        # current step position
        self._target = [0.0, 0.0, 0.0]     # commanded target, steps
        self._start_pos = [0.0, 0.0, 0.0]  # position when the move began
        self._t0 = [0.0, 0.0, 0.0]         # monotonic time the move began
        self._moving = [False, False, False]

        # Drive parameters (seeded from config; the brain re-pushes on start).
        self._rate = [axis_rate(cfg, a) for a in range(3)]       # steps/s
        self._acc = [axis_acceleration(cfg, a) for a in range(3)]  # steps/s^2
        self._volt = [axis_voltage(cfg, a) for a in range(3)]     # V

        self._opened = False

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def idn(self) -> str:
        return "SIM KIM101 3-axis piezo-inertia stage (simulator)"

    # -- internal motion integrator --------------------------------------- #
    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _advance(self, axis: int) -> float:
        """Update and return the current step position of ``axis``.

        Called lazily by every read so the simulated motion advances in real
        time without needing a background thread.  Motion is modelled at a
        constant step rate (the acceleration ramp is ignored in the sim -- it is
        a real KIM101 parameter we still store and forward, but the arrival time
        it affects is short compared with a coarse move).
        """
        if not self._moving[axis]:
            return self._pos[axis]

        dt = self._now() - self._t0[axis]
        distance = self._target[axis] - self._start_pos[axis]
        travelled = max(0.0, self._rate[axis]) * dt  # steps = steps/s * s

        if travelled >= abs(distance):
            self._pos[axis] = self._target[axis]      # arrived
            self._moving[axis] = False
        else:
            direction = 1.0 if distance >= 0 else -1.0
            self._pos[axis] = self._start_pos[axis] + direction * travelled
        return self._pos[axis]

    # -- motion ------------------------------------------------------------ #
    def move_to(self, axis: int, position_steps: int) -> None:
        self._start_pos[axis] = self._advance(axis)  # start from where we are
        self._target[axis] = float(int(position_steps))
        self._t0[axis] = self._now()
        self._moving[axis] = abs(self._target[axis] - self._start_pos[axis]) >= 1.0
        if not self._moving[axis]:
            self._pos[axis] = self._target[axis]

    def move_by(self, axis: int, delta_steps: int) -> None:
        current = self._advance(axis)
        self.move_to(axis, int(round(current)) + int(delta_steps))

    def is_moving(self, axis: int) -> bool:
        self._advance(axis)
        return self._moving[axis]

    def stop(self, axis: int) -> None:
        self._advance(axis)  # freeze wherever we are right now
        self._moving[axis] = False

    def read_position(self, axis: int) -> int:
        return int(round(self._advance(axis)))

    def zero_counter(self, axis: int) -> None:
        """Define the current physical position as step 0 (reset the counter)."""
        self._advance(axis)
        self._pos[axis] = 0.0
        self._target[axis] = 0.0
        self._start_pos[axis] = 0.0
        self._moving[axis] = False

    # -- parameters -------------------------------------------------------- #
    def set_step_rate(self, axis: int, steps_per_sec: float) -> None:
        # If a move is in flight, RE-ANCHOR it so the new rate takes effect from
        # NOW rather than being multiplied by the whole elapsed time (which would
        # make the simulated position jump).  On real hardware a velocity change
        # mid-move just changes the speed going forward -- this mirrors that.
        if self._moving[axis]:
            self._start_pos[axis] = self._advance(axis)
            self._t0[axis] = self._now()
        self._rate[axis] = float(steps_per_sec)

    def read_step_rate(self, axis: int) -> float:
        return self._rate[axis]

    def set_acceleration(self, axis: int, steps_per_sec2: float) -> None:
        self._acc[axis] = float(steps_per_sec2)

    def read_acceleration(self, axis: int) -> float:
        return self._acc[axis]

    def set_voltage(self, axis: int, volts: float) -> None:
        self._volt[axis] = float(volts)

    def read_voltage(self, axis: int) -> float:
        return self._volt[axis]
