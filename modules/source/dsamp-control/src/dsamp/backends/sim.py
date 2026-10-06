"""Simulated hardware: a fake GB6000L smart amplifier.

It implements AmpBackend, so the brain cannot tell it apart from the real one.
What it models, and why:

  * the gain setting is QUANTISED to the device step and clipped to the device
    range, as the firmware does -- so a readback of 10.0 after asking for 10.2
    is exercised here, not first discovered on the bench;
  * it can start in ANY state (`initial_gain_dB`, `initial_on`), because the
    real amplifier keeps whatever its front panel or a previous session left --
    open() does not touch it, and the brain has to ADOPT it. The default is a
    plausible leftover (6 dB, stage off), not the device minimum, so adoption
    is exercised every time the simulator runs; close() switches it off;
  * the case warms up with the stage on (first-order, ~60 s time constant, a
    +12 C rise), because DS Instruments note hot amplifiers lose gain and the
    GUI's temperature readout should move like the real one;
  * the USB rail sags a little under the ~0.45 A the stage draws.

The gain-vs-frequency roll-off is NOT in here: the device does not know the
frequency either. The brain applies `model.py` to whatever the backend reports.

`fail_reads = True` makes every read raise, to test how the brain survives a
pulled USB cable.
"""

from __future__ import annotations

import math
import random
import time


class SimulatedGB6000L:
    """Pretends to be a DS Instruments GB6000L variable-gain amplifier."""

    AMBIENT_C = 27.0
    RISE_ON_C = 12.0
    TAU_S = 60.0

    def __init__(self, gain_min_dB: float = 0.0, gain_max_dB: float = 31.0,
                 gain_step_dB: float = 0.5, initial_gain_dB: float = 6.0,
                 initial_on: bool = False):
        self._gmin = float(gain_min_dB)
        self._gmax = float(gain_max_dB)
        self._step = float(gain_step_dB) if gain_step_dB > 0 else 0.5
        self._gain = self._gmin
        self.set_gain(initial_gain_dB)                  # snapped like any other setting
        self._output = bool(initial_on)
        self._open = False
        self.fail_reads = False
        # thermal state: temperature at the last update, and when (an amplifier
        # found ON has been on for a while: start it warm)
        self._temp = self.AMBIENT_C + (self.RISE_ON_C if self._output else 0.0)
        self._t_last = time.monotonic()

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        self._open = True             # connect only: output and gain stay as found

    def close(self, output_off: bool = True) -> None:
        self._update_temp()
        if output_off:                # OFF on the way out (not on a restart)
            self._output = False
        self._open = False

    # ---- output ----------------------------------------------------------

    def set_output(self, on: bool) -> None:
        self._update_temp()           # integrate the old state before switching
        self._output = bool(on)

    def read_output(self) -> bool:
        self._check()
        return self._output

    # ---- gain ------------------------------------------------------------

    def set_gain(self, dB: float) -> None:
        # the firmware snaps to its step and its range
        q = round(float(dB) / self._step) * self._step
        self._gain = min(self._gmax, max(self._gmin, q))

    def read_gain(self) -> float:
        self._check()
        return self._gain

    # ---- health ----------------------------------------------------------

    def read_temperature(self) -> float:
        self._check()
        self._update_temp()
        return round(self._temp + random.gauss(0.0, 0.05), 1)

    def read_supply(self) -> float:
        self._check()
        sag = 0.12 if self._output else 0.02
        return round(5.08 - sag + random.gauss(0.0, 0.005), 3)

    def idn(self) -> str:
        return "DS Instruments,GB6000L,SIMULATED,0.0" if self._open else ""

    # ---- internals -------------------------------------------------------

    def _check(self) -> None:
        if self.fail_reads:
            raise IOError("simulated read failure (USB unplugged?)")

    def _update_temp(self) -> None:
        """Advance the first-order thermal model to 'now'."""
        now = time.monotonic()
        dt = max(0.0, now - self._t_last)
        self._t_last = now
        target = self.AMBIENT_C + (self.RISE_ON_C if self._output else 0.0)
        self._temp = target + (self._temp - target) * math.exp(-dt / self.TAU_S)
