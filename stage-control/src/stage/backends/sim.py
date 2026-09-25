"""A pure-Python simulator for the 3-axis stage (§3 of the guide).

It implements every method of :class:`stage.backends.base.StageBackend`, so the
whole application -- GUI, service, client, tests -- runs with no hardware and no
Thorlabs drivers installed.  This is the DEFAULT backend.

"Just enough physics" to feel lifelike: each axis ramps toward its target at a
constant velocity (time-based), reports ``is_moving`` while travelling, and sets
its ``homed`` flag when a home move completes.  That gives the front panel a
smoothly moving read-out to display.
"""

from __future__ import annotations

import time

from ..config import Config, axis_acceleration, axis_velocity


class SimStage:
    """Simulated BSC203 + 3 actuators."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.n = 3

        # Live state, per axis.
        self._pos = [0.0, 0.0, 0.0]        # current position, mm
        self._target = [0.0, 0.0, 0.0]     # commanded target, mm
        self._start_pos = [0.0, 0.0, 0.0]  # position when the move began
        self._t0 = [0.0, 0.0, 0.0]         # monotonic time the move began
        self._moving = [False, False, False]
        self._homing = [False, False, False]
        self._homed = [False, False, False]

        # Motion parameters (seeded from config; the brain re-pushes on start).
        self._vel = [axis_velocity(cfg, a) for a in range(3)]
        self._acc = [axis_acceleration(cfg, a) for a in range(3)]

        self._opened = False

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def idn(self) -> str:
        return "SIM BSC203 3-axis coarse stage (simulator)"

    # -- internal motion integrator --------------------------------------- #
    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _advance(self, axis: int) -> float:
        """Update and return the current position of ``axis``.

        Called lazily by every read so the simulated motion advances in real
        time without needing a background thread.
        """
        if not self._moving[axis]:
            return self._pos[axis]

        dt = self._now() - self._t0[axis]
        distance = self._target[axis] - self._start_pos[axis]
        travelled = max(0.0, self._vel[axis]) * dt

        if travelled >= abs(distance):
            # Arrived.
            self._pos[axis] = self._target[axis]
            self._moving[axis] = False
            if self._homing[axis]:
                self._homing[axis] = False
                self._homed[axis] = True
        else:
            direction = 1.0 if distance >= 0 else -1.0
            self._pos[axis] = self._start_pos[axis] + direction * travelled
        return self._pos[axis]

    # -- motion ------------------------------------------------------------ #
    def move_to(self, axis: int, position: float) -> None:
        self._start_pos[axis] = self._advance(axis)  # start from where we are
        self._target[axis] = float(position)
        self._t0[axis] = self._now()
        self._moving[axis] = abs(self._target[axis] - self._start_pos[axis]) > 1e-9
        if not self._moving[axis]:
            self._pos[axis] = self._target[axis]

    def home(self, axis: int) -> None:
        # Homing is modelled as a move to 0 that sets the homed flag on arrival.
        self._homed[axis] = False
        self._homing[axis] = True
        self.move_to(axis, 0.0)
        if not self._moving[axis]:
            # Already at 0 -> home completes instantly.
            self._homing[axis] = False
            self._homed[axis] = True

    def is_moving(self, axis: int) -> bool:
        self._advance(axis)
        return self._moving[axis]

    def is_homed(self, axis: int) -> bool:
        self._advance(axis)
        return self._homed[axis]

    def stop(self, axis: int) -> None:
        self._advance(axis)  # freeze wherever we are right now
        self._moving[axis] = False
        self._homing[axis] = False

    def read_position(self, axis: int) -> float:
        return self._advance(axis)

    # -- parameters -------------------------------------------------------- #
    def set_velocity(self, axis: int, velocity: float) -> None:
        self._vel[axis] = float(velocity)

    def read_velocity(self, axis: int) -> float:
        return self._vel[axis]

    def set_acceleration(self, axis: int, acceleration: float) -> None:
        self._acc[axis] = float(acceleration)

    def read_acceleration(self, axis: int) -> float:
        return self._acc[axis]
