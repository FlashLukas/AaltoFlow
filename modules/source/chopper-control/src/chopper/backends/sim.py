"""Simulated hardware: a fake MC2000B with a wheel that takes time to spin up.

It implements the ChopperBackend interface, so the brain cannot tell it apart
from the real controller. What makes it worth having is the MOTOR:

    The wheel's rotation rate (revolutions per second) follows the rate the
    reference asks for with a first-order lag,
        d(rps)/dt = (rps_target - rps) / tau,
    with tau = `spinup_tau_s` while enabled and the longer `coast_tau_s` after
    disable (the motor is not braked, the wheel just coasts down). That is a
    fair cartoon of the PLL + motor: a new frequency is reached "within a few
    seconds" (manual 5.1), exponentially, never with a jump.

The chopping frequency seen through a ring of N slots is rps * N, so a 10/100
blade chops its inner ring at one tenth of its outer ring -- the same wheel.
When the wheel is close to its target a little rms jitter is added to what the
sensors report, so the lock detector upstairs has something real to judge.

The state is advanced LAZILY, whenever it is read or changed, with the exact
solution of the first-order equation over the elapsed time -- no thread of its
own, and the result does not depend on how often it is polled.

It also enforces the rule the manual states (section 5.2): blade, reference
and output modes and the harmonics change only in STANDBY. A set while the
motor runs raises, exactly where the real controller would answer an error.
"""

from __future__ import annotations

import math
import random
import time

from ..blades import blade_by_name
from ..config import Sim


class SimulatedMC2000B:
    """Pretends to be a Thorlabs MC2000B optical chopper controller."""

    def __init__(self, sim: Sim | None = None, clock=time.monotonic, seed=None):
        s = sim or Sim()
        self._clock = clock
        self._rng = random.Random(seed)
        self._p = s                                    # live: tau / jitter / input read each step
        blade = blade_by_name(s.blade)
        self._blade = blade.index
        self._ref = blade.ref_modes.index(s.ref_mode) if s.ref_mode in blade.ref_modes else 0
        self._output = (blade.output_modes.index(s.output_mode)
                        if s.output_mode in blade.output_modes else 0)
        self._freq = float(s.frequency_Hz)
        self._phase = float(s.phase_deg)
        self._enable = bool(s.enabled)
        self._nh = 1
        self._dh = 1
        self._open = False
        # a chopper found running is found SPINNING at its set speed
        self._rps = self._target_rps() if self._enable else 0.0
        self._t = self._clock()

    # ---- the motor ---------------------------------------------------------

    def _bl(self):
        from ..blades import blade_by_index
        return blade_by_index(self._blade)

    def _ref_name(self) -> str:
        return self._bl().ref_modes[self._ref]

    def _target_Hz(self) -> float:
        """The chopping frequency the reference asks for, on the referenced ring."""
        if self._bl().is_external(self._ref_name()):
            return float(self._p.external_input_Hz) * self._nh / self._dh
        return self._freq

    def _target_rps(self) -> float:
        b = self._bl()
        return self._target_Hz() / b.slots(b.ring_of(self._ref_name()))

    def _advance(self) -> None:
        now = self._clock()
        dt = max(0.0, now - self._t)
        self._t = now
        goal = self._target_rps() if self._enable else 0.0
        tau = self._p.spinup_tau_s if self._enable else self._p.coast_tau_s
        tau = max(float(tau), 1e-3)
        self._rps = goal + (self._rps - goal) * math.exp(-dt / tau)

    def _ring_Hz(self, ring: str) -> float:
        """What a slot sensor on `ring` sees: the wheel, plus jitter once locked."""
        b = self._bl()
        f = self._rps * b.slots(ring)
        if f > 0:
            f *= 1.0 + self._rng.gauss(0.0, float(self._p.jitter_rel))
        return max(f, 0.0)

    def _standby_only(self, what: str) -> None:
        if not self._open:
            raise RuntimeError("not connected")
        if self._enable:
            raise RuntimeError(f"{what} can only be changed in standby (disable first)")

    # ---- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        self._advance()
        self._open = True

    def close(self) -> None:
        self._open = False

    def idn(self) -> str:
        return "THORLABS MC2000B (simulated) v0.0" if self._open else ""

    # ---- configuration (standby only) --------------------------------------

    def get_blade(self) -> int:
        return self._blade

    def set_blade(self, index: int) -> None:
        self._standby_only("the blade")
        from ..blades import blade_by_index
        new = blade_by_index(index)
        self._advance()
        self._blade = new.index
        # a different blade has a different mode table: fall back to index 0
        # where the old index does not exist (# VERIFY what the unit does)
        if self._ref >= len(new.ref_modes):
            self._ref = 0
        if self._output >= len(new.output_modes):
            self._output = 0

    def get_ref(self) -> int:
        return self._ref

    def set_ref(self, index: int) -> None:
        self._standby_only("the reference mode")
        if not 0 <= int(index) < len(self._bl().ref_modes):
            raise ValueError(f"ref={index} out of range for {self._bl().name}")
        self._advance()
        self._ref = int(index)

    def get_output(self) -> int:
        return self._output

    def set_output(self, index: int) -> None:
        self._standby_only("the reference output")
        if not 0 <= int(index) < len(self._bl().output_modes):
            raise ValueError(f"output={index} out of range for {self._bl().name}")
        self._output = int(index)

    def get_nharmonic(self) -> int:
        return self._nh

    def set_nharmonic(self, n: int) -> None:
        self._standby_only("the harmonic multiplier")
        self._advance()
        self._nh = int(n)

    def get_dharmonic(self) -> int:
        return self._dh

    def set_dharmonic(self, d: int) -> None:
        self._standby_only("the harmonic divider")
        self._advance()
        self._dh = int(d)

    # ---- run-time controls -------------------------------------------------

    def get_frequency(self) -> float:
        return self._freq

    def set_frequency(self, hz: float) -> None:
        self._advance()
        # the synthesiser has a finite step (1 Hz, 0.1 Hz on the 10/100 blade)
        res = self._bl().resolution_Hz
        self._freq = round(float(hz) / res) * res

    def get_phase(self) -> float:
        return self._phase

    def set_phase(self, deg: float) -> None:
        self._phase = float(deg)

    def get_enable(self) -> bool:
        return self._enable

    def set_enable(self, on: bool) -> None:
        self._advance()
        self._enable = bool(on)

    # ---- measurements ------------------------------------------------------

    def read_refout_frequency(self) -> float:
        self._advance()
        b = self._bl()
        ring = b.output_ring(b.output_modes[self._output], self._ref_name())
        if ring is None:                       # "target": the synthesiser itself
            return self._target_Hz() if self._enable else 0.0
        return self._ring_Hz(ring)

    def read_input_frequency(self) -> float:
        return float(self._p.external_input_Hz)
