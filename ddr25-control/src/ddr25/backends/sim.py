"""A pure-Python simulator of the DDR25 on its K-Cube (section 3 of the guide).

It implements every method of :class:`ddr25.backends.base.RotatorBackend`, so
the whole application -- GUI, service, client, tests -- runs with no hardware
and no Thorlabs drivers. This is the DEFAULT backend.

"Just enough physics" to behave like the real thing where it matters to the
software above it:

* **Trapezoidal motion profile.** The servo accelerates at ``acceleration``,
  cruises at ``velocity`` and decelerates onto the target (a short move never
  reaches cruise speed: a triangle). So a big move takes visibly longer than
  distance/velocity, and a scan's settle wait has something real to wait for.
* **Power-up frame.** A K-Cube counts from 0 wherever the stage happened to be
  when it was switched on. The simulated shaft starts ``sim_start_deg`` away
  from the encoder INDEX mark, the counter reads 0, and it is NOT homed --
  unless ``sim_start_homed`` says the controller was homed in an earlier
  session. The stored velocity/acceleration are the controller's own
  (``sim_start_velocity`` / ``_acceleration``), not the config's, so the
  brain's adopt-on-start is really tested.
* **Homing** turns in the + direction (VERIFY the real home direction) at the
  profile velocity until the index mark passes, then re-zeroes the counter
  there. From 137 deg that is a 223 deg trip -- homing is not instantaneous.
* **Servo dither.** At rest a direct-drive servo holds position against the
  encoder to within a few counts; the readout jitters by about +-0.0003 deg
  (~1 count at 4000 counts/deg), so a display showing 4 decimals is honest.
* **Stop** is a profiled deceleration; ``immediate=True`` halts on the spot.

Motion is computed from the wall clock on every read (no thread of its own);
the brain's poll thread is what reads it.
"""

from __future__ import annotations

import math
import random
import time

from ..config import Config

#: Readout jitter of a servo holding position, deg (about one encoder count).
DITHER_DEG = 0.0003


class _Profile:
    """One trapezoidal move, as a function of time since its start.

    ``v0`` is the speed already present at the start (non-zero only for a
    deceleration-to-stop, which has ``decel_only``).
    """

    def __init__(self, start, target, vmax, acc, t0, v0=0.0, decel_only=False):
        self.start = float(start)
        self.t0 = t0
        self.a = max(float(acc), 1e-6)
        self.sign = 1.0 if target >= start else -1.0
        d = abs(float(target) - float(start))
        if decel_only:
            # brake from v0 to rest: d = v0^2 / 2a
            self.v0 = float(v0)
            self.duration = self.v0 / self.a
            self.target = self.start + self.sign * self.v0 ** 2 / (2 * self.a)
            self.kind = "decel"
            return
        self.target = float(target)
        v = max(float(vmax), 1e-6)
        if d < v * v / self.a:              # never reaches cruise: triangle
            v = math.sqrt(d * self.a)
        self.vp = v
        self.ta = v / self.a                 # time to accelerate (and to brake)
        self.tc = (d - v * v / self.a) / v if v > 0 else 0.0
        self.duration = 2 * self.ta + self.tc
        self.kind = "trap"

    def state(self, t: float) -> tuple[float, float, bool]:
        """(position, speed, finished) at wall time ``t``."""
        s = t - self.t0
        if s >= self.duration:
            return self.target, 0.0, True
        if self.kind == "decel":
            return self.start + self.sign * (self.v0 * s - 0.5 * self.a * s * s), \
                self.v0 - self.a * s, False
        a, ta, tc, vp = self.a, self.ta, self.tc, self.vp
        if s < ta:
            x, v = 0.5 * a * s * s, a * s
        elif s < ta + tc:
            x, v = 0.5 * a * ta * ta + vp * (s - ta), vp
        else:
            r = s - ta - tc
            x = 0.5 * a * ta * ta + vp * tc + vp * r - 0.5 * a * r * r
            v = vp - a * r
        return self.start + self.sign * x, v, False


class SimRotator:
    """Simulated K-Cube brushless controller + DDR25."""

    def __init__(self, cfg: Config, seed: int | None = None):
        self.cfg = cfg
        self._rng = random.Random(seed)
        # Physical shaft angle measured from the encoder index mark (deg,
        # continuous). The counter reads shaft - _counter_zero.
        hw = cfg.hardware
        self._shaft = float(hw.sim_start_deg)
        self._profile: _Profile | None = None
        self._homing = False
        if bool(hw.sim_start_homed):
            # Homed in an earlier session: the counter was zeroed at the index
            # mark (shaft 0), so it reads the shaft angle itself.
            self._homed = True
            self._counter_zero = 0.0
        else:
            self._homed = False
            self._counter_zero = self._shaft  # fresh power-up: counter reads 0 here
        # The controller's OWN stored profile -- not cfg.motion: the brain must
        # read it at start rather than find its own config echoed back.
        self._vel = float(hw.sim_start_velocity)
        self._acc = float(hw.sim_start_acceleration)
        #: Every state-changing call, in order (tests assert start() adds none).
        self.writes: list[tuple] = []
        self._opened = False

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def idn(self) -> str:
        return "SIM K-Cube brushless controller + DDR25 rotation stage (simulator)"

    # -- motion integrator ------------------------------------------------- #
    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _advance(self) -> float:
        """Bring the shaft up to date with the wall clock and return it."""
        if self._profile is not None:
            pos, _v, done = self._profile.state(self._now())
            self._shaft = pos
            if done:
                self._profile = None
                if self._homing:
                    # The index mark passed: this IS the reference now.
                    self._homing = False
                    self._homed = True
                    self._counter_zero = self._shaft
        return self._shaft

    def _speed(self) -> float:
        if self._profile is None:
            return 0.0
        return self._profile.state(self._now())[1]

    def _start(self, shaft_target: float) -> None:
        here = self._advance()
        self._profile = _Profile(here, shaft_target, self._vel, self._acc, self._now())
        if self._profile.duration <= 0:
            self._profile = None

    # -- motion ------------------------------------------------------------ #
    def move_to(self, position: float) -> None:
        self.writes.append(('move_to', float(position)))
        self._homing = False                    # a move aborts a home
        self._start(float(position) + self._counter_zero)

    def home(self) -> None:
        self.writes.append(('home',))
        # Turn + to the NEXT index mark (shaft = a multiple of 360). A stage
        # already sitting exactly on the mark finds it at once.
        here = self._advance()
        nxt = math.ceil(here / 360.0) * 360.0
        self._homed = False
        self._homing = True
        self._start(nxt)
        if self._profile is None:               # already on the mark
            self._homing = False
            self._homed = True
            self._counter_zero = self._shaft

    def is_moving(self) -> bool:
        self._advance()
        return self._profile is not None

    def is_homed(self) -> bool:
        self._advance()
        return self._homed

    def stop(self, immediate: bool = False) -> None:
        self.writes.append(('stop', bool(immediate)))
        here = self._advance()
        v = abs(self._speed())
        was_homing = self._homing
        self._homing = False
        if was_homing:
            self._homed = False                 # an interrupted home is no home
        if immediate or v <= 0 or self._profile is None:
            self._profile = None
            return
        sign = self._profile.sign
        self._profile = _Profile(here, here + sign, 0, self._acc, self._now(),
                                 v0=v, decel_only=True)

    def read_position(self) -> float:
        raw = self._advance() - self._counter_zero
        if self._profile is None:
            raw += self._rng.uniform(-DITHER_DEG, DITHER_DEG)
        return raw

    # -- parameters -------------------------------------------------------- #
    def set_velocity(self, velocity: float) -> None:
        self.writes.append(('set_velocity', float(velocity)))
        self._vel = float(velocity)

    def set_acceleration(self, acceleration: float) -> None:
        self.writes.append(('set_acceleration', float(acceleration)))
        self._acc = float(acceleration)

    def read_velocity_params(self) -> tuple[float, float]:
        return self._vel, self._acc
