"""A simulated TC200 with a heated block on it, so everything runs offline.

The physics is the simplest model that behaves like the real thing in the ways
the brain and a scan depend on -- a FIRST-ORDER thermal plant:

    C dT/dt = P - (T - T_ambient) / R

  C  heat capacity of block + sample (J/K)
  R  thermal resistance to the room (K/W)
  so tau = R*C is the time constant and P*R the temperature rise a steady
  power P gives. Defaults: R = 9 K/W, C = 10 J/K -> tau = 90 s, and 18 W (the
  TC200's maximum) could lift the block ~160 K above the room.

The controller is a PID on the error e = TSET - T, output clipped to 0..PMAX
(a heater cannot cool: below the setpoint the block only drifts down towards
the room, which is why a heater's cooling is slow and why a setpoint below
room temperature is never reached). The box's unitless gains are mapped to
physical ones by fixed factors chosen so the manual's recipe (P 125, I small,
D 0) gives a sensible response on this plant; the real mapping is Thorlabs'
secret and does not matter here -- the sim has to be plausible, not identical.

The box's own behaviour is imitated too, because the brain depends on it:
  * `ens` TOGGLES the output;
  * at T >= TMAX the relay opens (no power) and "TMAX ERROR" is latched; it
    closes again once T is back down to TSET; the third trip disables the
    output (manual, chapter 4 item 9);
  * a sensor alarm (inject with `sensor_alarm = True`) disables the output at
    once and blocks enabling (chapter 4 item 8);
  * TMAX set below TSET drags TSET down with it.

Time comes from an injectable clock; the plant is integrated in small steps up
to "now" whenever it is read or commanded, so a test can jump the clock by an
hour and get the right answer without waiting.
"""

from __future__ import annotations

import random
import time

from ..config import SENSORS
from .base import StatusBits

#: integration step, seconds (tau is ~90 s, so 0.1 s is far inside stability)
_DT = 0.1


class SimulatedTC200:
    simulated = True

    #: mapping of the box's unitless gains to physical ones (see docstring):
    #: P -> fraction of PMAX per degC, I -> per degC*s, D -> per (degC/s)
    KP_PER_UNIT = 1.0 / 1000.0
    KI_PER_UNIT = 1.0 / 2000.0
    KD_PER_UNIT = 1.0 / 100.0

    def __init__(self, temperature_C: float = 22.0, setpoint_C: float = 25.0,
                 enabled: bool = False, ambient_C: float = 22.0,
                 r_K_per_W: float = 9.0, c_J_per_K: float = 10.0,
                 sensor: str = "ptc100", p_gain: int = 125, i_gain: int = 5,
                 d_gain: int = 0, pmax_W: float = 10.0, tmax_C: float = 120.0,
                 clock=time.monotonic, noise: bool = True, seed: int | None = None):
        self._clock = clock
        self._rng = random.Random(seed)
        self._noise = noise
        self.T = float(temperature_C)
        self.ambient = float(ambient_C)
        self.R = float(r_K_per_W)
        self.C = float(c_J_per_K)
        self.tset = float(setpoint_C)
        self.enabled = bool(enabled)
        self.cycle_mode = False
        self.sensor = sensor
        self.p, self.i, self.d = int(p_gain), int(i_gain), int(d_gain)
        self.pmax = float(pmax_W)
        self.tmax = float(tmax_C)
        self.sensor_alarm = False       # tests inject a broken sensor here
        self.tmax_alarm = False
        self.relay_open = False
        self.tmax_trips = 0
        self.power_W = 0.0              # the sim's own truth; the real box has no readback
        self._integral = 0.0
        self._t = clock()
        self._open = False

    # ---- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        self._advance()
        self._open = True

    def close(self) -> None:
        self._open = False

    def idn(self) -> str:
        return "THORLABS TC200 (simulated)"

    # ---- the plant ---------------------------------------------------------------

    def _advance(self) -> None:
        """Integrate the plant and the controller from the last call up to now."""
        now = self._clock()
        span = now - self._t
        self._t = now
        if span <= 0:
            return
        # A very long gap (a test jumping the clock) would need millions of
        # 0.1 s steps; widen the step instead -- still stable up to ~tau/2.
        n = int(span / _DT) + 1
        if n > 20000:
            n = 20000
        dt = span / n
        for _ in range(n):
            self._step(dt)

    def _step(self, dt: float) -> None:
        if self.sensor_alarm:
            self.enabled = False
        # over-temperature trip: open at TMAX, close again once down at TSET
        if self.T >= self.tmax and not self.relay_open:
            self.relay_open = True
            self.tmax_alarm = True
            self.tmax_trips += 1
            if self.tmax_trips >= 3:
                self.enabled = False
        elif self.relay_open and self.T <= self.tset:
            self.relay_open = False

        power = 0.0
        if self.enabled and not self.relay_open:
            e = self.tset - self.T
            kp = self.p * self.KP_PER_UNIT
            ki = self.i * self.KI_PER_UNIT
            kd = self.d * self.KD_PER_UNIT
            dTdt = (self._power_prev() - (self.T - self.ambient) / self.R) / self.C
            u = kp * e + self._integral - kd * dTdt
            # anti-windup: integrate only while the output is not pinned, and
            # keep the integral itself inside the output range
            if 0.0 < u < 1.0 or (u >= 1.0 and e < 0) or (u <= 0.0 and e > 0):
                self._integral = min(1.0, max(0.0, self._integral + ki * e * dt))
            power = self.pmax * min(1.0, max(0.0, u))
        else:
            self._integral = 0.0
        self.power_W = power
        self.T += dt * (power - (self.T - self.ambient) / self.R) / self.C

    def _power_prev(self) -> float:
        return self.power_W

    # ---- temperature ---------------------------------------------------------------

    def read_temperature(self) -> float:
        self._advance()
        noise = self._rng.gauss(0.0, 0.01) if self._noise else 0.0
        return round(self.T + noise, 2)

    def read_setpoint(self) -> float:
        self._advance()
        return self.tset

    def set_setpoint(self, temperature_C: float) -> None:
        self._advance()
        # the box keeps one decimal and refuses outside 20..min(200, TMAX)
        v = round(float(temperature_C), 1)
        if not 20.0 <= v <= min(200.0, self.tmax):
            raise ValueError(f"tset={v:.1f} out of range (20.0 .. {min(200.0, self.tmax):.1f})")
        self.tset = v

    # ---- output ----------------------------------------------------------------------

    def read_status(self) -> StatusBits:
        self._advance()
        raw = ((1 if self.enabled else 0) | (2 if self.cycle_mode else 0)
               | {"ptc100": 4, "ptc1000": 8}.get(self.sensor, 0) | 16
               | (64 if self.sensor_alarm else 0))
        return StatusBits(enabled=self.enabled, cycle_mode=self.cycle_mode,
                          sensor_alarm=self.sensor_alarm, tmax_alarm=self.tmax_alarm,
                          raw=raw)

    def toggle_enable(self) -> None:
        self._advance()
        if self.enabled:
            self.enabled = False
        elif self.sensor_alarm:
            return                       # the box will not enable on a broken sensor
        else:
            self.enabled = True
        # pressing ENABLE with a TMAX alarm clears it (manual, TMAX function)
        self.tmax_alarm = False
        self.tmax_trips = 0
        self.relay_open = False

    # ---- stored settings ----------------------------------------------------------------

    def read_sensor(self) -> str:
        return self.sensor

    def set_sensor(self, sensor: str) -> None:
        if sensor not in SENSORS:
            raise ValueError(f"unknown sensor {sensor!r}")
        self._advance()
        # manual 5.6.3: "Any attempt to change the sensor while the heater is
        # enabled will immediately disable the heater." (The brain refuses
        # before it gets here; the sim still behaves like the box.)
        if self.enabled:
            self.enabled = False
        self.sensor = sensor

    def read_pid(self) -> tuple[int, int, int]:
        return self.p, self.i, self.d

    def set_p_gain(self, p: int) -> None:
        self._advance(); self.p = int(p)

    def set_i_gain(self, i: int) -> None:
        self._advance(); self.i = int(i)

    def set_d_gain(self, d: int) -> None:
        self._advance(); self.d = int(d)

    def read_pmax(self) -> float:
        return self.pmax

    def set_pmax(self, watts: float) -> None:
        self._advance(); self.pmax = round(float(watts), 1)

    def read_tmax(self) -> float:
        return self.tmax

    def set_tmax(self, temperature_C: float) -> None:
        self._advance()
        self.tmax = round(float(temperature_C), 1)
        if self.tset > self.tmax:        # the box lowers TSET with TMAX
            self.tset = self.tmax
