"""Simulated hardware: a fake Kepco and a fake Hall probe.

These implement the CurrentSource / FieldSensor interfaces from `base`, so the
controller cannot tell them apart from the real instruments. The point is to
develop and test everything -- calibration, ramping, PID, the state machine,
the GUI -- with nothing plugged in.

The physics is deliberately simple but not trivial:
  * The magnet saturates:      B = B_sat * tanh(I / I_scale)
  * The iron has hysteresis:   an offset that flips sign with sweep direction,
                               which the calibration's up/down averaging cancels.
  * The probe is noisy:        Gaussian noise per sample, so averaging more
                               samples (the "precise" profile) gives a cleaner
                               reading than the "fast" profile -- exactly the
                               trade-off the real controller makes while seeking.
"""

from __future__ import annotations

import math
import random
import time

from ..config import HallProbe


class SimulatedKepco:
    """Pretends to be a Kepco BOP in constant-current mode."""

    def __init__(self, current_max_A: float = 3.0):
        self._current = 0.0
        self._direction = 1       # +1 if last move raised current, -1 if lowered
        self._max = current_max_A
        self._open = False

    def open(self) -> None:
        self._open = True
        self._current = 0.0

    def close(self) -> None:
        # mirror the real shutdown: ramp to zero, output off
        self._current = 0.0
        self._open = False

    def set_current(self, amps: float) -> None:
        # clamp to the supply's hard limit (the controller also clamps, but a
        # real supply would refuse to exceed its rating too)
        amps = max(-self._max, min(self._max, amps))
        if amps > self._current:
            self._direction = 1
        elif amps < self._current:
            self._direction = -1
        self._current = amps

    def read_current(self) -> float:
        return self._current

    # exposed so the simulated probe can see the magnet state
    @property
    def direction(self) -> int:
        return self._direction


class SimulatedHallProbe:
    """Pretends to be the NI Hall-probe input. Reads the magnet current from a
    SimulatedKepco and returns the corresponding (noisy) voltage."""

    def __init__(
        self,
        kepco: SimulatedKepco,
        hall: HallProbe | None = None,
        B_sat_mT: float = 125.0,
        I_scale_A: float = 3.0,
        hysteresis_mT: float = 0.08,
        noise_mV_per_sample: float = 5.0,
        seed: int = 0,
        emulate_timing: bool = True,
    ):
        self._kepco = kepco
        self._hall = hall or HallProbe()
        self._B_sat = B_sat_mT
        self._I_scale = I_scale_A
        self._hyst = hysteresis_mT
        self._noise = noise_mV_per_sample
        self._rng = random.Random(seed)
        self._open = False
        # When True, read_voltage blocks for samples/rate seconds, like a real
        # DAQ acquisition. Tests/calibration set this False so they run instantly.
        self.emulate_timing = emulate_timing

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def _true_field_mT(self) -> float:
        I = self._kepco.read_current()
        anhysteretic = self._B_sat * math.tanh(I / self._I_scale)
        return anhysteretic + self._hyst * self._kepco.direction

    def read_voltage(self, samples: int, rate_Hz: float) -> float:
        """Return the mean of `samples` noisy readings, in volts. Averaging more
        samples shrinks the noise as 1/sqrt(samples)."""
        if self.emulate_timing and rate_Hz > 0:
            time.sleep(samples / rate_Hz)     # a real DAQ read takes this long
        B = self._true_field_mT()
        ideal_V = self._hall.field_to_volts(B)
        # per-sample noise in mV -> volts; mean of `samples` draws
        noise_sum = sum(self._rng.gauss(0.0, self._noise) for _ in range(samples))
        mean_noise_mV = noise_sum / samples
        return ideal_V + mean_noise_mV / 1000.0


class SimulatedAux:
    """Fake general-purpose DAQ I/O for the AUX panel.

    Analog and digital OUTPUTS just remember what they were set to. Analog INPUTS
    return a gentle, per-channel wandering signal plus a little noise, so the AUX
    panel shows live values you can watch move (in the lab these would be whatever
    you have plugged into those BNCs)."""

    def __init__(self, seed: int = 1, noise_V: float = 0.01, v_min: float = -10.0, v_max: float = 10.0):
        self._ao: dict[str, float] = {}
        self._do: dict[str, bool] = {}
        self._rng = random.Random(seed)
        self._noise = noise_V
        self._v_min, self._v_max = v_min, v_max
        self._n = 0
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def set_ao(self, channel: str, volts: float) -> None:
        self._ao[channel] = max(self._v_min, min(self._v_max, volts))

    def read_ao(self, channel: str) -> float:
        return self._ao.get(channel, 0.0)

    def read_ai(self, channel: str) -> float:
        # a slow sine (phase fixed per channel name) + noise -> looks "live"
        self._n += 1
        phase = (abs(hash(channel)) % 1000) / 1000.0 * 6.28318
        return 2.0 * math.sin(self._n * 0.05 + phase) + self._rng.gauss(0.0, self._noise)

    def set_do(self, line: str, state: bool) -> None:
        self._do[line] = bool(state)

    def read_do(self, line: str) -> bool:
        return self._do.get(line, False)
