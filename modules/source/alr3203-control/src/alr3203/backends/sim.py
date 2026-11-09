"""Simulated hardware: an ELC ALR3203 driving a coil through a polarity relay.

It implements SupplyBackend (and SimRelay implements PolarityRelay) from
`base`, so the brain cannot tell them apart from the real instruments. The load
is a resistor R in series with an inductor L (a coil), because that is the case
the software ramp exists for.

The physics, integrated in small time steps from the wall clock (all values are
the SUPPLY's magnitudes; the relay only decides which way the coil sees them):

    Kirchhoff:        V = I R + L dI/dt          (I >= 0: the supply cannot sink)

    The supply regulates whichever setpoint it reaches first (CV/CC):
      it wants to apply V_set; if the current would exceed I_set it lowers the
      voltage so the current stays at I_set (CC); and it never delivers more
      than 96 W: V <= P_max / I.
    output off:       0 V at the terminals; the coil current decays through R
                      (a real supply's output capacitor and protection diode
                      give it that path).

Measurements: a low-pass with time constant _TAU_MEAS plus a little Gaussian
noise, so an averaged acquisition is visibly better than one reading.

SimRelay counts HOT SWITCHES: a flip while the coil still carries more than a
few milliamps. On the real relay that is an arc in the magnet leads; the tests
assert that the brain never does it.
"""

from __future__ import annotations

import math
import random
import time

from ..config import RATED_A, RATED_V, RATED_W, Sim

_TAU_REG = 1e-3          # s: bandwidth of the supply's own CC loop
_DT_MAX = 2e-4           # s: integration sub-step (stable for dt < tau)
_TAU_MEAS = 0.05         # s: the readback, as a low-pass


class SimulatedALR3203:
    """Pretends to be an ELC ALR3203 (32 V / 6 A / 96 W) with a coil on it."""

    def __init__(self, load: Sim | None = None, clock=time.monotonic, seed=None):
        self.load = load or Sim()
        self._clock = clock
        self._rng = random.Random(seed)
        self._open = False
        ld = self.load
        # The state the unit is FOUND in (cfg.sim.found_*): a real supply keeps
        # its setpoints and output across our connections.
        self._prog_v = min(RATED_V, max(0.0, float(ld.found_voltage_V)))
        self._prog_i = min(RATED_A, max(0.0, float(ld.found_current_A)))
        self._output = bool(ld.found_output)
        self._I = 0.0
        self._V = 0.0
        if self._output:
            # found live: the coil has long settled at its steady state
            R = max(1e-6, float(ld.load_R_ohm))
            i = min(self._prog_v / R, self._prog_i, math.sqrt(RATED_W / R))
            self._I, self._V = i, i * R
        self._I_avg, self._V_avg = self._I, self._V
        self._t = self._clock()
        # every write the brain makes, for tests ("start writes nothing")
        self.writes: list[tuple] = []

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        self._advance()
        self._open = True

    def read_state(self) -> dict:
        self._advance()
        return {"output": self._output, "voltage_V": self._prog_v,
                "current_A": self._prog_i, "regulation": self.read_regulation()}

    def close(self, output_off: bool = True) -> None:
        self._advance()
        if output_off:
            self._output = False
        self._open = False

    def disconnect(self) -> None:
        self._open = False

    # ---- programming -----------------------------------------------------

    def program_voltage(self, volts: float) -> None:
        self._advance()
        v = float(volts)
        if v < 0:
            raise ValueError("the ALR3203 takes magnitudes only")
        self.writes.append(("program_voltage", v))
        self._prog_v = min(RATED_V, v)

    def program_current(self, amps: float) -> None:
        self._advance()
        a = float(amps)
        if a < 0:
            raise ValueError("the ALR3203 takes magnitudes only")
        self.writes.append(("program_current", a))
        self._prog_i = min(RATED_A, a)

    def set_output(self, on: bool) -> None:
        self._advance()
        self.writes.append(("set_output", bool(on)))
        self._output = bool(on)

    # ---- measurement -----------------------------------------------------

    def measure_voltage(self) -> float:
        self._advance()
        return max(0.0, self._V_avg + self._rng.gauss(0.0, self.load.noise_V))

    def measure_current(self) -> float:
        self._advance()
        return max(0.0, self._I_avg + self._rng.gauss(0.0, self.load.noise_A))

    def read_regulation(self) -> str:
        self._advance()
        if not self._output:
            return "off"
        # CC when the current setpoint (or the power limit) holds the output
        # below the voltage setpoint
        return "current" if self._V < self._prog_v - 1e-3 else "voltage"

    def idn(self) -> str:
        return "ELC ALR3203 SIMULATED" if self._open else ""

    # ---- for tests ---------------------------------------------------------

    @property
    def true_current(self) -> float:
        """The coil current (magnitude), without readback lag or noise."""
        self._advance()
        return self._I

    @property
    def output_on(self) -> bool:
        return self._output

    # ---- the physics -------------------------------------------------------

    def _advance(self) -> None:
        now = self._clock()
        dt_total = max(0.0, now - self._t)
        self._t = now
        R = max(1e-6, float(self.load.load_R_ohm))
        L = max(1e-9, float(self.load.load_L_H))
        dt_total = min(dt_total, max(0.05, 8.0 * L / R))
        while dt_total > 0.0:
            dt = min(_DT_MAX, dt_total)
            dt_total -= dt
            self._step(dt, R, L)

    def _step(self, dt: float, R: float, L: float) -> None:
        I = self._I
        if not self._output:
            V = 0.0
        else:
            # CV: apply the voltage setpoint -- unless that would push the
            # current past its setpoint within this step; then CC: the
            # voltage that lands the current exactly on the setpoint
            # (clipped to 0 .. V_set: the supply can neither sink nor exceed
            # its voltage setpoint)
            V = self._prog_v
            if I + (V - I * R) / L * dt > self._prog_i:
                V = min(self._prog_v, max(0.0, I * R + L * (self._prog_i - I) / _TAU_REG))
            # and never more than the 96 W envelope
            if I > 1e-9:
                V = min(V, RATED_W / I)
        I_new = max(0.0, I + (V - I * R) / L * dt)    # the supply cannot sink
        self._I = I_new
        self._V = V
        a = dt / _TAU_MEAS
        self._I_avg += (I_new - self._I_avg) * a
        self._V_avg += (self._V - self._V_avg) * a


class SimRelay:
    """A simulated polarity relay. Knows the supply, so it can tell a HOT
    switch (a flip under current) -- which the brain must never do."""

    def __init__(self, supply: SimulatedALR3203 | None = None,
                 inverted: bool = False, hot_threshold_A: float = 0.01):
        self.supply = supply
        self.inverted = bool(inverted)
        self.hot_threshold_A = float(hot_threshold_A)
        self.flips: list[bool] = []          # every set(), in order
        self.hot_switches = 0                # flips with current flowing
        self.fail = False                    # True: set() raises (DAQ gone)
        self.readable = True                 # False: read() -> None

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def read(self):
        return self.inverted if self.readable else None

    def set(self, inverted: bool) -> None:
        if self.fail:
            raise ConnectionError("simulated relay: the DAQ service does not answer")
        inverted = bool(inverted)
        if self.supply is not None and inverted != self.inverted \
                and self.supply.true_current > self.hot_threshold_A:
            self.hot_switches += 1
        self.flips.append(inverted)
        self.inverted = inverted

    def describe(self) -> str:
        return "simulated relay"


class NoRelay:
    """No relay fitted: the output is unipolar. read() says normal; set(True)
    is refused, so the brain never offers a negative value."""

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def read(self):
        return False

    def set(self, inverted: bool) -> None:
        if inverted:
            raise ValueError("no polarity relay configured (polarity.driver = none)")

    def describe(self) -> str:
        return "no relay (unipolar)"
