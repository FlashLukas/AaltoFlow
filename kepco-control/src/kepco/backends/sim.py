"""Simulated hardware: a Kepco BOP driving a coil.

It implements BipolarSupplyBackend from `base`, so the brain cannot tell it
apart from the real instrument. The load is a resistor R in series with an
inductor L (a coil), because that is the case the software ramp exists for.

The physics, integrated in small time steps from the wall clock:

    Kirchhoff:        V = I R + L dI/dt

    current mode:     the BOP's own (fast, ~1 ms) loop drives I towards the
                      programmed current. The voltage that takes is
                      I R + L dI/dt -- and it is CLIPPED at +-voltage limit.
                      A current step into a coil therefore does not happen
                      instantly: the supply sits at its compliance and the
                      current slews at (V_lim - I R) / L. That is what the
                      "AT LIMIT" flag in status shows.
    voltage mode:     V is the programmed voltage, and the current follows
                      L dI/dt = V - I R, with time constant L/R. If |I| would
                      pass the current limit, the BOP crosses over and holds
                      the limit instead.
    output off:       the BOP programs 0 V and 0 A (manual B.20); the coil
                      current then decays through R.

Measurements are what the BIT 4886 reports: the average of its last 16
conversions (manual sec. 1.2.1), modelled as a low-pass with time constant
_TAU_MEAS, plus a little Gaussian noise (so an averaged acquisition is visibly
better than one reading). The low-pass matters: the software ramp is a
staircase, and each stair asks the BOP for a brief voltage spike of about
L * dI / tau_bop -- real, but not what the averaged readback shows.
"""

from __future__ import annotations

import random
import time

from ..config import Sim

_TAU_BOP = 1e-3          # s: bandwidth of the BOP's own regulation loop
_DT_MAX = 2e-4           # s: integration sub-step (stable for dt < tau)
_TAU_MEAS = 0.05         # s: the readback's 16-conversion average, as a low-pass


class SimulatedBOP:
    """Pretends to be a Kepco BOP 20-10 with a BIT 4886 GPIB card."""

    def __init__(self, load: Sim | None = None, clock=time.monotonic,
                 v_rating: float = 20.0, i_rating: float = 10.0, seed=None):
        self.load = load or Sim()
        self._clock = clock
        self._rng = random.Random(seed)
        self._v_rating = v_rating
        self._i_rating = i_rating
        self._open = False
        self._mode = "voltage"          # *RST default on the real unit
        self._prog_v = 0.0
        self._prog_i = 0.0
        self._output = False
        # the physical state of the load
        self._I = 0.0
        self._V = 0.0
        self._I_avg = 0.0                # what the averaged readback shows
        self._V_avg = 0.0
        self._t = self._clock()

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        self._advance()
        self._open = True
        self._output = False
        self._prog_v = self._prog_i = 0.0

    def close(self) -> None:
        self._advance()
        self._output = False
        self._open = False

    # ---- programming -----------------------------------------------------

    def set_mode(self, mode: str) -> None:
        self._advance()
        if mode not in ("voltage", "current"):
            raise ValueError(f"unknown mode {mode!r}")
        self._mode = mode

    def program_voltage(self, volts: float) -> None:
        self._advance()
        self._prog_v = max(-self._v_rating, min(self._v_rating, float(volts)))

    def program_current(self, amps: float) -> None:
        self._advance()
        self._prog_i = max(-self._i_rating, min(self._i_rating, float(amps)))

    def set_output(self, on: bool) -> None:
        self._advance()
        self._output = bool(on)

    # ---- measurement -----------------------------------------------------

    def measure_voltage(self) -> float:
        self._advance()
        return self._V_avg + self._rng.gauss(0.0, self.load.noise_V)

    def measure_current(self) -> float:
        self._advance()
        return self._I_avg + self._rng.gauss(0.0, self.load.noise_A)

    def idn(self) -> str:
        return "KEPCO,BIT 4886 SIMULATED,20,10,0,0,0.0-0.0" if self._open else ""

    # ---- for tests ---------------------------------------------------------

    @property
    def true_current(self) -> float:
        self._advance()
        return self._I

    @property
    def output_on(self) -> bool:
        return self._output

    @property
    def mode(self) -> str:
        return self._mode

    # ---- the physics -------------------------------------------------------

    def _advance(self) -> None:
        """Integrate the RL load from the last call up to now."""
        now = self._clock()
        dt_total = max(0.0, now - self._t)
        self._t = now
        # a long gap (a test sleeping, a debugger) needs no more resolution
        # than ~5 time constants; cap the work so a call never takes long
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
            # 0 V at the terminals: the coil discharges through R
            V = 0.0
            I_new = I + (V - I * R) / L * dt
        elif self._mode == "current":
            v_lim = abs(self._prog_v)
            # the voltage the BOP would like to apply to pull I to the setpoint
            V_want = I * R + L * (self._prog_i - I) / _TAU_BOP
            V = max(-v_lim, min(v_lim, V_want))
            I_new = I + (V - I * R) / L * dt
        else:
            i_lim = abs(self._prog_i)
            V = self._prog_v
            I_new = I + (V - I * R) / L * dt
            if abs(I_new) > i_lim:
                # crossover: the current limit takes over and holds |I| = i_lim
                I_new = i_lim if I_new > 0 else -i_lim
                V = I_new * R
        self._I = I_new
        self._V = V
        a = dt / _TAU_MEAS
        self._I_avg += (I_new - self._I_avg) * a
        self._V_avg += (V - self._V_avg) * a
