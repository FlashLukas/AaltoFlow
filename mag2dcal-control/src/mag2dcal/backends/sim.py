"""A simulated 2-axis vector magnet, good enough that the PI has to do real work.

Per axis, from drive voltage to Hall voltage:

    AO volts --(amplifier enable)--> coil lag (first order, tau)
             --> gain (mT/V, slightly different on X and Y)
             --> hysteresis (a "play" / backlash operator of half-width h)
             --> cross-talk (each probe sees a fraction of the other axis)
             --> Hall probe: V = offset + k*B, plus noise, clipped to the AI range

Why each piece is there:
  * the LAG is what makes a PI necessary at all and sets how fast it can be;
  * the GAIN MISMATCH means a straight-line guess at the volts is a few percent
    wrong, so a measured calibration is worth having;
  * the HYSTERESIS is the reason this module exists, so it gets its own note
    below;
  * the DRIFT (off by default) is a slow zero shift, the thing the long-term
    stabilizer is for;
  * the CROSS-TALK couples the axes, so setting only the angle still disturbs
    both loops;
  * the CLIP reproduces the real probes' +-186 mT range on a 0..5 V input.

THE HYSTERESIS MODEL, and why it is this one. The field carries an OFFSET that
depends on which way the coil current was last moving:

    B = gain * current + offset,     offset relaxes toward +h when the current
                                     rises and toward -h when it falls, over a
                                     travel of `hysteresis_reversal_V`.

So after a long sweep upward the offset sits at +h and after a sweep downward at
-h: the two branches of the loop are 2h apart, which is exactly what the two
legs of a calibration measure. A controller that keeps REVERSING its output near
the setpoint drags the offset back and forth across those 2h, and the field
swings by up to 2h no matter how good the gains are. With h = 0.4 mT and a
tolerance of 0.5 mT that swing is bigger than the band, so an always-on PI can
never settle -- and an output held perfectly still cannot move the offset at
all, so a frozen one settles immediately. tests/test_freeze.py measures both.

This is clMag-control's model (field + h * direction of travel), softened so the
branch moves over a finite travel instead of snapping: a millivolt-sized nudge
from the long-term stabilizer then moves the branch by a proportionally small
amount rather than flipping it wholesale. It is phenomenological, not a Preisach
model -- it is here to reproduce ONE failure mode faithfully, the one the real
magnet showed and the freeze exists to prevent.

TIME. The plant is advanced to `clock()` whenever it is touched, integrating the
lag EXACTLY over the elapsed time (the drive is constant between writes, so the
first-order response is an exponential). That makes the simulator correct at any
loop rate, and lets tests drive it with a fake clock: a 5-second settle then
runs in milliseconds and gives the same numbers every time.
"""

from __future__ import annotations

import math
import random
import time

from ..config import Hall, Sim, Temperature


class SimVectorMagnet:
    def __init__(self, sim: Sim | None = None, hall: Hall | None = None,
                 temperature: Temperature | None = None,
                 ai_range=(0.0, 5.0), clock=time.monotonic, seed: int | None = None):
        # The sim keeps REFERENCES to the config groups, so a test (or the
        # Settings dialog) that edits cfg.sim.water_ok is seen immediately.
        self.p = sim or Sim()
        self.hall = hall or Hall()
        self.temp_cal = temperature or Temperature()
        self.ai_range = ai_range
        self._clock = clock
        self._rng = random.Random(seed)

        self.is_open = False
        self.enable = False
        self.ao = [0.0, 0.0]            # last written drive (volts)
        self._lag = [0.0, 0.0]          # coil response, volts-equivalent
        self._hyst = [0.0, 0.0]         # hysteresis offset, mT, lives in [-h, +h]
        self._mag = [0.0, 0.0]          # field after hysteresis, mT (per coil)
        self._drift = 0.0               # slow zero shift, mT (see Sim.drift_mT_per_s)
        self._temp = [self.p.ambient_C, self.p.ambient_C]
        self._t = clock()
        # a log of every AO write, for tests that check the output never jumps
        self.ao_log: list[tuple[float, float, float]] = []

    # ---- Protocol -------------------------------------------------------------

    def open(self) -> None:
        self._advance()
        self.is_open = True
        self.enable = False
        self.ao = [0.0, 0.0]

    def close(self) -> None:
        self._advance()
        self.ao = [0.0, 0.0]
        self.enable = False
        self.is_open = False

    def write_ao(self, x_V: float, y_V: float) -> None:
        self._advance()
        self.ao = [float(x_V), float(y_V)]
        self.ao_log.append((self._t, self.ao[0], self.ao[1]))
        if len(self.ao_log) > 20000:
            del self.ao_log[:10000]

    def read_hall(self) -> tuple[float, float]:
        self._advance()
        bx, by = self.true_field()
        n = self.p.noise_mT
        bx += self._rng.gauss(0.0, n)
        by += self._rng.gauss(0.0, n)
        vx, vy = self.hall.mT_to_volts(bx, by)
        lo, hi = self.ai_range
        return (min(hi, max(lo, vx)), min(hi, max(lo, vy)))

    def read_temps(self) -> tuple[float, float]:
        self._advance()
        cal = self.temp_cal
        # invert T = C_per_V * V + offset, so the brain's conversion gets T back
        v1 = (self._temp[0] + self._rng.gauss(0.0, 0.05) - cal.t1_offset_C) / cal.t1_C_per_V
        v2 = (self._temp[1] + self._rng.gauss(0.0, 0.05) - cal.t2_offset_C) / cal.t2_C_per_V
        return (v1, v2)

    def read_water(self) -> bool:
        return bool(self.p.water_ok)

    def set_enable(self, on: bool) -> None:
        self._advance()
        self.enable = bool(on)

    # ---- the physics ------------------------------------------------------------

    def true_field(self) -> tuple[float, float]:
        """The field at the sample, without probe noise (tests use this)."""
        c = self.p.crosstalk
        d = self._drift
        return (self._mag[0] + c * self._mag[1] + d,
                self._mag[1] + c * self._mag[0] + d)

    def _advance(self) -> None:
        now = self._clock()
        dt = now - self._t
        if dt <= 0.0:
            return
        self._t = now
        p = self.p
        gains = (p.gain_x_mT_per_V, p.gain_y_mT_per_V)
        # A disabled amplifier delivers no current, whatever the AO says.
        drive = self.ao if self.enable else (0.0, 0.0)
        decay = math.exp(-dt / max(1e-6, p.tau_s))
        tdecay = math.exp(-dt / max(1e-6, p.thermal_tau_s))
        h = abs(p.hysteresis_mT)
        self._drift += p.drift_mT_per_s * dt
        for a in (0, 1):
            # exact first-order step response over dt (drive held constant)
            new_lag = drive[a] + (self._lag[a] - drive[a]) * decay
            d_lag = new_lag - self._lag[a]
            self._lag[a] = new_lag
            # The hysteresis branch follows the COIL CURRENT, not the commanded
            # voltage, so the field never moves before the current does. It
            # relaxes toward +h / -h over a travel of 2h/gain volts, which makes
            # the up and down branches 2h apart once a full sweep has been done.
            if h > 0.0 and d_lag != 0.0:
                reversal_V = max(1e-9, abs(p.hysteresis_reversal_V))
                frac = min(1.0, abs(d_lag) / reversal_V)
                want = h if d_lag > 0 else -h
                self._hyst[a] += (want - self._hyst[a]) * frac
            self._mag[a] = gains[a] * self._lag[a] + self._hyst[a]
            t_ss = p.ambient_C + p.heating_C_per_V2 * drive[a] ** 2
            self._temp[a] = t_ss + (self._temp[a] - t_ss) * tdecay


class FakeClock:
    """A clock you advance by hand. `sleep` advances it instead of waiting.

    Tests give the SAME FakeClock to the simulator and the brain, then call
    `brain.tick()` in a loop -- seconds of simulated magnet in milliseconds.
    """

    def __init__(self, t0: float = 1000.0):
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt

    def sleep(self, dt: float) -> None:
        self.t += max(0.0, dt)
