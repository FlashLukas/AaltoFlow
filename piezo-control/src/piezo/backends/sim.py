"""A pure-Python simulator for the 2D piezo stage (§3 of the guide).

It implements every method of :class:`piezo.backends.base.PiezoBackend`, so the
whole application -- GUI, service, client, tests -- runs with no hardware and no
serial port.  This is the DEFAULT backend.

"Just enough physics" to feel lifelike and to make the two loop modes visibly
different:

  * Slew-rate limiting: each axis moves toward its setpoint at ``slew_rate``
    um/s (time-based, integrated lazily on every read).  A slew rate of 0 means
    "jump instantly".  This is what makes the controller's *native* velocity
    limiting real in the simulator -- set a slew rate, write one setpoint, and
    the read-out ramps.
  * Open loop vs closed loop: in CLOSED loop the measured position tracks the
    setpoint accurately (the servo cancels hysteresis) with only tiny noise.
    In OPEN loop we add a small hysteresis/creep-like error so the read-out
    lands slightly off the command -- exactly the effect closed loop removes.
"""

from __future__ import annotations

import time

from ..config import Config, axis_closed_loop_default, axis_velocity


class SimPiezo:
    """Simulated d-Drive + PXY-200 (2 axes)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.n = 2

        # Live state, per axis.
        self._setpoint = [0.0, 0.0]     # last commanded target, um
        self._pos = [0.0, 0.0]          # current true actuator position, um
        self._start_pos = [0.0, 0.0]    # position when the latest move began
        self._t0 = [0.0, 0.0]           # monotonic time the latest move began
        self._closed = [axis_closed_loop_default(cfg, a) for a in range(2)]
        # Native slew rate per axis, um/s (0 = instant).  Seeded from config vel.
        self._slew = [axis_velocity(cfg, a) for a in range(2)]

        # A tiny, deterministic open-loop error model so OL != CL visibly.
        # (Signed fraction of setpoint + a small constant, no randomness so
        #  tests stay reproducible.)
        self._ol_gain_err = [0.015, -0.012]   # ~1.5% scale error per axis
        self._ol_offset = [0.20, -0.15]       # um constant creep-like offset

        self._opened = False

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def idn(self) -> str:
        return "SIM piezosystem jena d-Drive + PXY-200 (simulator)"

    # -- internal motion integrator --------------------------------------- #
    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _advance(self, axis: int) -> float:
        """Update and return the true actuator position of ``axis``.

        Called lazily by every read so simulated motion advances in real time
        without a background thread.
        """
        target = self._setpoint[axis]
        cur = self._pos[axis]
        if cur == target:
            return cur

        rate = max(0.0, self._slew[axis])
        if rate <= 0.0:
            # No slew limit -> the piezo snaps to the setpoint immediately.
            self._pos[axis] = target
            return target

        dt = self._now() - self._t0[axis]
        distance = target - self._start_pos[axis]
        travelled = rate * dt
        if travelled >= abs(distance):
            self._pos[axis] = target
        else:
            direction = 1.0 if distance >= 0 else -1.0
            self._pos[axis] = self._start_pos[axis] + direction * travelled
        return self._pos[axis]

    def _measured(self, axis: int) -> float:
        """Sensor read-out: accurate in CL, hysteresis-biased in OL."""
        true_pos = self._advance(axis)
        if self._closed[axis]:
            return true_pos  # servo makes measured == true (== setpoint at rest)
        # Open loop: what you *read back* is derived from the drive command, so
        # it carries the gain error + creep offset the closed loop would remove.
        return true_pos * (1.0 + self._ol_gain_err[axis]) + self._ol_offset[axis]

    # -- position ---------------------------------------------------------- #
    def set_setpoint(self, axis: int, position: float) -> None:
        self._start_pos[axis] = self._advance(axis)  # start from where we are
        self._setpoint[axis] = float(position)
        self._t0[axis] = self._now()

    def read_position(self, axis: int) -> float:
        return self._measured(axis)

    # -- loop mode --------------------------------------------------------- #
    def set_closed_loop(self, axis: int, enabled: bool) -> None:
        self._closed[axis] = bool(enabled)

    def get_closed_loop(self, axis: int) -> bool:
        return self._closed[axis]

    # -- native slew rate -------------------------------------------------- #
    def set_slew_rate(self, axis: int, rate: float) -> None:
        # Re-anchor the integrator so changing the rate mid-move is smooth.
        self._start_pos[axis] = self._advance(axis)
        self._t0[axis] = self._now()
        self._slew[axis] = max(0.0, float(rate))

    def read_slew_rate(self, axis: int) -> float:
        return self._slew[axis]
