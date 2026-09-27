"""The hardware interface the brain may call (blueprint §3).

A ``typing.Protocol`` -- any object with these methods (the simulator OR the real
KCube driver) is a valid backend.  Voltage is the piezo drive level in volts.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ZBackend(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def idn(self) -> str: ...
    def set_voltage(self, volts: float) -> None: ...
    def read_voltage(self) -> float: ...
    def range(self) -> tuple: ...          # (v_min, v_max)
