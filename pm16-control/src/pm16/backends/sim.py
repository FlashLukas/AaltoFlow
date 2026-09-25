"""A simulated PM16-120, so everything runs with nothing plugged in.

Enough physics that the controls visibly DO something:

  * A laser at `laser_nm` shines `incident_W` onto a Si photodiode. The meter
    turns photocurrent into watts with the responsivity at the wavelength it is
    TOLD, so setting the wrong wavelength gives the wrong reading -- exactly
    what happens on the bench (800 nm light read at 1064 nm reads high).
  * Ranges come in steps of 100x from 0.174 mW, as measured on the lab's
    PM16-121; a manual range smaller than the signal saturates and flags
    `overrange`.
  * The dark offset adds a small bias until you zero the sensor; zeroing takes
    `zero_time_s` and no readings are possible meanwhile.

`measure_power` does not sleep by default, so tests stay fast; pass
`sample_period_s=0.06` (the real PM16's reading time) for a realistic feel.
"""

from __future__ import annotations

import math
import random
import threading
import time

from .base import FLAG_OK, FLAG_OVERRANGE

# Approximate Si photodiode responsivity (A/W), roughly the shape of Thorlabs'
# S12x curves: rising with wavelength, peaking near 960 nm, falling to 1100 nm.
_RESPONSIVITY = [(400, 0.12), (500, 0.24), (600, 0.33), (700, 0.42),
                 (800, 0.50), (900, 0.58), (960, 0.61), (1000, 0.58),
                 (1064, 0.40), (1100, 0.25)]

# the ranges the real PM16-121 offered: 0.174 mW, 17.4 mW, 1.74 W
_RANGES_W = [1.73668e-4, 1.736957e-2, 1.743936]


def responsivity(nm: float) -> float:
    """Piecewise-linear interpolation of the table above, clamped at the ends."""
    pts = _RESPONSIVITY
    if nm <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if nm <= x1:
            return y0 + (y1 - y0) * (nm - x0) / (x1 - x0)
    return pts[-1][1]


class SimulatedPM16:
    def __init__(self, laser_nm: float = 800.0, incident_W: float = 1.2e-3,
                 sample_period_s: float = 0.0, zero_time_s: float = 1.0,
                 seed: int | None = None, clock=time.monotonic):
        self.laser_nm = float(laser_nm)
        self.incident_W = float(incident_W)
        self.sample_period_s = float(sample_period_s)
        self.zero_time_s = float(zero_time_s)
        self._clock = clock
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

        self._open = False
        self._wl = 633.0              # a fresh meter remembers SOME wavelength
        self._auto = True
        self._range = _RANGES_W[0]
        self._dark_A = 2e-9           # un-zeroed dark current
        self._zero_until = None
        self._t0 = clock()

    # ---- lifecycle -------------------------------------------------------
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def idn(self) -> str:
        return "Thorlabs PM16-121 (simulated) S/N SIM0001 fw 1.5.0"

    def sensor_name(self) -> str:
        return "PM16-121 (sim)"

    # ---- wavelength ------------------------------------------------------
    def set_wavelength(self, nm: float) -> None:
        self._wl = float(nm)

    def get_wavelength(self) -> float:
        return self._wl

    def wavelength_range(self) -> tuple[float, float]:
        return (400.0, 1100.0)

    # ---- range -----------------------------------------------------------
    def set_auto_range(self, on: bool) -> None:
        if self._auto and not on:
            # like the real PM16: leaving auto keeps the range auto had chosen
            self._range = self.get_range()
        self._auto = bool(on)

    def get_auto_range(self) -> bool:
        return self._auto

    def set_range(self, watts: float) -> None:
        # a real meter snaps to its next range up; so does this one
        self._range = next((r for r in _RANGES_W if r >= watts * 0.999), _RANGES_W[-1])

    def get_range(self) -> float:
        if self._auto:
            p = self._reading_W()
            return next((r for r in _RANGES_W if r >= p), _RANGES_W[-1])
        return self._range

    def range_limits(self) -> tuple[float, float]:
        return (_RANGES_W[0], _RANGES_W[-1])

    # ---- averaging -------------------------------------------------------
    def average_time_s(self) -> float:
        return 0.06024096385542168          # what the real PM16-121 reports

    # ---- measurement -----------------------------------------------------
    def _reading_W(self) -> float:
        """The noiseless value the meter would display right now."""
        t = self._clock() - self._t0
        drift = 1.0 + 0.01 * math.sin(2 * math.pi * t / 20.0)     # slow 1 % laser drift
        current_A = self.incident_W * drift * responsivity(self.laser_nm) + self._dark_A
        return current_A / responsivity(self._wl)

    def measure_power(self) -> tuple[float, str]:
        if not self._open:
            raise RuntimeError("simulated PM16 is not open")
        if self.zero_running():
            raise RuntimeError("zero adjustment in progress")
        if self.sample_period_s > 0:
            time.sleep(self.sample_period_s)
        p = self._reading_W()
        sigma = 0.002 * p + 2e-9                      # laser noise + a noise floor (nW scale, as measured)
        p += self._rng.gauss(0.0, sigma)
        if not self._auto and p > self._range * 1.1:
            return self._range * 1.1, FLAG_OVERRANGE
        return p, FLAG_OK

    # ---- zero ------------------------------------------------------------
    def start_zero(self) -> None:
        with self._lock:
            self._zero_until = self._clock() + self.zero_time_s
            self._dark_A = 0.0

    def cancel_zero(self) -> None:
        with self._lock:
            self._zero_until = None

    def zero_running(self) -> bool:
        with self._lock:
            if self._zero_until is None:
                return False
            if self._clock() >= self._zero_until:
                self._zero_until = None
                return False
            return True

    def dark_offset(self) -> float:
        return 2e-9 if self._dark_A == 0.0 else 0.0
