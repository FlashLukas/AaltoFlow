"""Simulated Z piezo -- remembers the commanded voltage (blueprint §3)."""

from __future__ import annotations


class SimZ:
    def __init__(self, v0: float = 0.0, v_min: float = 0.0, v_max: float = 75.0):
        self._v = float(v0)
        self._vmin = float(v_min)
        self._vmax = float(v_max)
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def idn(self) -> str:
        return "SimZ (simulated KCube piezo)"

    def set_voltage(self, volts: float) -> None:
        self._v = float(volts)

    def read_voltage(self) -> float:
        return self._v

    def range(self) -> tuple:
        return (self._vmin, self._vmax)
