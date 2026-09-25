"""The hardware interface -- what the brain is allowed to assume.

`typing.Protocol` is Python's structural interface: any object with these
methods counts as a VectorMagnetBackend, whether it is the NI DAQ driver or the
simulator. The brain depends ONLY on this, which is how the whole control loop
is developed and tested with nothing plugged in.

Everything here is in RAW VOLTS. Turning volts into millitesla (Hall
calibration) and into degrees Celsius is the brain's job, because those numbers
live in config and must be the same whichever backend is in use.

Threading: only the brain's control thread calls these methods (plus start and
shutdown, before/after that thread runs), so a backend needs no locking.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class VectorMagnetBackend(Protocol):
    """Two coil drives, two Hall probes, two thermometers, a flow switch and an
    output-enable line."""

    def open(self) -> None:
        """Connect. Must leave the output SAFE: AO at 0 V, enable False."""

    def close(self) -> None:
        """Disconnect. A last-resort backstop sets AO 0 V and enable False first;
        the brain has normally ramped the output down before calling this."""

    def write_ao(self, x_V: float, y_V: float) -> None:
        """Drive the X and Y coils. Sets directly -- slew limiting is the brain's job."""

    def read_hall(self) -> tuple[float, float]:
        """Hall probe voltages (Vx, Vy), each the MEAN of n samples."""

    def read_temps(self) -> tuple[float, float]:
        """Temperature sensor voltages (V1, V2)."""

    def read_water(self) -> bool:
        """True while cooling water flows."""

    def set_enable(self, on: bool) -> None:
        """The output-enable digital line: True while running, False when off."""
