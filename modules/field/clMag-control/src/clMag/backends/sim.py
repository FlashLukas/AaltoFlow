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
    """Pretends to be a Kepco BOP in constant-current mode.

    It has a state of its own BEFORE anyone connects (`initial_current_A`,
    `output_on`), like a real supply someone left running, so the tests can
    check that the controller ADOPTS that state instead of resetting it.
    It keeps the programmed current apart from the current actually flowing:
    with the output off the programmed value is remembered but 0 A flows --
    which is why the controller must reprogram the present current before it
    switches the output on.
    """

    def __init__(self, current_max_A: float = 3.0, initial_current_A: float = 0.0,
                 output_on: bool = True):
        self._current = float(initial_current_A)   # PROGRAMMED current
        self._output_on = bool(output_on)
        self._direction = 1       # +1 if last move raised current, -1 if lowered
        self._max = current_max_A
        self._open = False

    def open(self) -> None:
        # Connecting changes nothing (adopt-on-start rule): the old version
        # zeroed the current here, which a real supply would never do by itself.
        self._open = True

    def close(self, output_off: bool = True) -> None:
        # mirror the real shutdown: the controller has ramped to zero, output off.
        # output_off=False is a restart: the supply keeps driving what it drives.
        if output_off:
            self._current = 0.0
            self._output_on = False
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
        # the current actually FLOWING (MEAS:CURR?): nothing with the output off
        return self._current if self._output_on else 0.0

    def read_output(self) -> bool:
        return self._output_on

    def enable_output(self) -> None:
        self._output_on = True

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
        # Slow DRIFT (2026-09-28), for testing the long-term stabilizer: an
        # extra field that grows linearly at `rate` mT/s from the moment
        # set_drift() is called, and stops growing at `total` mT. Physically
        # it stands for anything slow the calibration does not know about --
        # a warming coil or pole piece, a Hall offset creeping with the room
        # temperature. Default: no drift, so nothing else changes.
        self._drift_rate = 0.0
        self._drift_total = 0.0
        self._drift_t0 = 0.0

    def set_drift(self, rate_mT_per_s: float, total_mT: float) -> None:
        """Start a drift of `rate` mT/s that stops after `total` mT (the sign
        of `total` gives the direction). set_drift(0, 0) removes it; a huge
        rate makes it a step."""
        self._drift_rate = abs(float(rate_mT_per_s))
        self._drift_total = float(total_mT)
        self._drift_t0 = time.monotonic()

    def drift_mT(self) -> float:
        """The drift field right now (mT)."""
        grown = self._drift_rate * (time.monotonic() - self._drift_t0)
        return math.copysign(min(grown, abs(self._drift_total)), self._drift_total)

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def _true_field_mT(self) -> float:
        I = self._kepco.read_current()
        anhysteretic = self._B_sat * math.tanh(I / self._I_scale)
        return anhysteretic + self._hyst * self._kepco.direction + self.drift_mT()

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

    def __init__(self, seed: int = 1, noise_V: float = 0.01, v_min: float = -10.0, v_max: float = 10.0,
                 initial_do: dict | None = None):
        # _ao holds only what was COMMANDED this session: like the real 6259,
        # the sim cannot tell what an AO was driving before we connected.
        self._ao: dict[str, float] = {}
        # DO lines CAN be read back, so a state set before start is adopted.
        self._do: dict[str, bool] = {k: bool(v) for k, v in (initial_do or {}).items()}
        self._rng = random.Random(seed)
        self._noise = noise_V
        self._v_min, self._v_max = v_min, v_max
        self._n = 0
        self._open = False

    def open(self) -> None:
        self._open = True      # writes nothing: outputs keep what they drive

    def close(self) -> None:
        self._open = False

    def set_ao(self, channel: str, volts: float) -> None:
        self._ao[channel] = max(self._v_min, min(self._v_max, volts))

    def read_ao(self, channel: str):
        return self._ao.get(channel)      # None = not commanded since start

    def read_ai(self, channel: str) -> float:
        # a slow sine (phase fixed per channel name) + noise -> looks "live"
        self._n += 1
        phase = (abs(hash(channel)) % 1000) / 1000.0 * 6.28318
        return 2.0 * math.sin(self._n * 0.05 + phase) + self._rng.gauss(0.0, self._noise)

    def set_do(self, line: str, state: bool) -> None:
        self._do[line] = bool(state)

    def read_do(self, line: str) -> bool:
        return self._do.get(line, False)
