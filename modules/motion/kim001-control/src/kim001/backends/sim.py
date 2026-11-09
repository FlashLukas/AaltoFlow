"""A pure-Python simulator for the KIM001 + one PIA13 (section 3 of the guide).

It implements every method of :class:`kim001.backends.base.Kim001Backend`, so the
whole application -- GUI, service, client, tests -- runs with no hardware and no
Thorlabs drivers installed.  This is the DEFAULT backend.

ONE actuator, like the real controller: the focus axis, which the brain calls
axis 2 (Z). Asking the simulator about axis 0 or 1 (X / Y) is a programming
error -- the brain refuses those axes before it gets here -- so it raises
instead of inventing a second actuator that the real KIM001 does not have.

"Just enough physics" to feel lifelike (the same as kim-control's sim): the
actuator steps toward its target at a constant step RATE (steps/s,
time-based), reports ``is_moving`` while travelling, and holds an integer step
counter you can reset (``zero_counter``).

Everything here is in STEPS -- the simulator is deliberately dumb about
micrometres; that translation lives one layer up in the brain.
"""

from __future__ import annotations

import time

from ..config import FITTED_AXES, Config, axis_acceleration, axis_rate, axis_voltage

#: The one axis this controller has (index 2 = Z).
AXIS = FITTED_AXES[0]


def _initial(value, default):
    """A start value from `state`: a number, or a 3-list [X, Y, Z] (as the
    3-axis kim tests write it), of which only Z counts."""
    if value is None:
        return float(default)
    if isinstance(value, (list, tuple)):
        return float(value[AXIS])
    return float(value)


class SimKim:
    """Simulated KIM001 + one PIA13 actuator (an open-loop step counter)."""

    def __init__(self, cfg: Config, state: dict | None = None):
        """`state` = what the "controller" holds BEFORE the software starts:
        optional keys ``position`` (steps), ``rate`` (steps/s), ``accel``
        (steps/s^2), ``voltage`` (V) -- each a number, or a 3-list of which
        Z (index 2) is used. Tests use it to start the sim somewhere the config
        would never put it, so they can prove the brain ADOPTS the controller's
        state instead of overwriting it (the adopt-on-start rule, 2026-09-27).
        Without it the sim starts at the config values, as a freshly configured
        controller would.
        """
        self.cfg = cfg
        self.n = 1
        state = state or {}

        pos0 = _initial(state.get("position"), 0.0)
        self._pos = pos0              # current step position
        self._target = pos0           # commanded target, steps
        self._start_pos = pos0        # position when the move began
        self._t0 = 0.0                # monotonic time the move began
        self._moving = False

        # Drive parameters, as the controller would remember them. The brain
        # READS these at start (it never pushes the config over them).
        self._rate = _initial(state.get("rate"), axis_rate(cfg, AXIS))
        self._acc = _initial(state.get("accel"), axis_acceleration(cfg, AXIS))
        self._volt = _initial(state.get("voltage"), axis_voltage(cfg, AXIS))

        # Every call that CHANGES the controller, in order -- so a test can
        # assert that start-up wrote nothing.
        self.writes: list[str] = []

        self._opened = False

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def idn(self) -> str:
        return "SIM KIM001 + PIA13 focus actuator (simulator)"

    @staticmethod
    def _check(axis: int) -> None:
        if axis != AXIS:
            raise ValueError(f"the simulated KIM001 has one actuator (axis {AXIS} = Z), "
                             f"not axis {axis}")

    # -- internal motion integrator --------------------------------------- #
    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _advance(self) -> float:
        """Update and return the current step position.

        Called lazily by every read so the simulated motion advances in real
        time without needing a background thread.  Motion is modelled at a
        constant step rate (the acceleration ramp is ignored in the sim -- it is
        a real controller parameter we still store and forward, but the arrival
        time it affects is short compared with a move).
        """
        if not self._moving:
            return self._pos

        dt = self._now() - self._t0
        distance = self._target - self._start_pos
        travelled = max(0.0, self._rate) * dt  # steps = steps/s * s

        if travelled >= abs(distance):
            self._pos = self._target      # arrived
            self._moving = False
        else:
            direction = 1.0 if distance >= 0 else -1.0
            self._pos = self._start_pos + direction * travelled
        return self._pos

    # -- motion ------------------------------------------------------------ #
    def move_to(self, axis: int, position_steps: int) -> None:
        self._check(axis)
        self.writes.append("move_to")
        self._start_pos = self._advance()  # start from where we are
        self._target = float(int(position_steps))
        self._t0 = self._now()
        self._moving = abs(self._target - self._start_pos) >= 1.0
        if not self._moving:
            self._pos = self._target

    def move_by(self, axis: int, delta_steps: int) -> None:
        self._check(axis)
        current = self._advance()
        self.move_to(axis, int(round(current)) + int(delta_steps))

    def is_moving(self, axis: int) -> bool:
        self._check(axis)
        self._advance()
        return self._moving

    def stop(self, axis: int) -> None:
        self._check(axis)
        self.writes.append("stop")
        self._advance()  # freeze wherever we are right now
        self._moving = False

    def read_position(self, axis: int) -> int:
        self._check(axis)
        return int(round(self._advance()))

    def zero_counter(self, axis: int) -> None:
        """Define the current physical position as step 0 (reset the counter)."""
        self._check(axis)
        self.writes.append("zero_counter")
        self._advance()
        self._pos = self._target = self._start_pos = 0.0
        self._moving = False

    # -- parameters -------------------------------------------------------- #
    def set_step_rate(self, axis: int, steps_per_sec: float) -> None:
        # If a move is in flight, RE-ANCHOR it so the new rate takes effect from
        # NOW rather than being multiplied by the whole elapsed time (which would
        # make the simulated position jump).  On real hardware a velocity change
        # mid-move just changes the speed going forward -- this mirrors that.
        self._check(axis)
        self.writes.append("set_step_rate")
        if self._moving:
            self._start_pos = self._advance()
            self._t0 = self._now()
        self._rate = float(steps_per_sec)

    def read_step_rate(self, axis: int) -> float:
        self._check(axis)
        return self._rate

    def set_acceleration(self, axis: int, steps_per_sec2: float) -> None:
        self._check(axis)
        self.writes.append("set_acceleration")
        self._acc = float(steps_per_sec2)

    def read_acceleration(self, axis: int) -> float:
        self._check(axis)
        return self._acc

    def set_voltage(self, axis: int, volts: float) -> None:
        self._check(axis)
        self.writes.append("set_voltage")
        self._volt = float(volts)

    def read_voltage(self, axis: int) -> float:
        self._check(axis)
        return self._volt
