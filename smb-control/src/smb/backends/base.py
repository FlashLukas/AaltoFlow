"""The hardware interface -- what the rest of the code is allowed to assume.

We use typing.Protocol, Python's "structural interface": any object that has
these methods counts as an RFSource, whether it is the real SMB100A or the
simulator. The Generator depends ONLY on this interface, so swapping real
hardware for the simulator changes nothing above this line. That is how you
develop and test the whole thing with no instrument plugged in.

Every setter commands the value directly; there is no ramping or sequencing at
this layer (an RF generator settles its own hardware). Clamping to the safety
limits is the Generator's job, not the backend's.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RFSource(Protocol):
    """A programmable RF signal generator (the R&S SMB100A)."""

    def open(self) -> None:
        """Connect and initialise. Should leave the RF OUTPUT OFF."""

    def close(self) -> None:
        """Turn RF off and disconnect. Safe to call on shutdown/crash."""

    # ---- RF output on/off ------------------------------------------------
    def set_output(self, on: bool) -> None:
        """Enable/disable the RF output (SCPI OUTP:STAT ON|OFF)."""

    def read_output(self) -> bool:
        """True if the RF output is on (OUTP:STAT?)."""

    # ---- level -----------------------------------------------------------
    def set_power(self, dBm: float) -> None:
        """Set the output level in dBm (POW <v>)."""

    def read_power(self) -> float:
        """Read back the output level in dBm (POW?)."""

    # ---- frequency -------------------------------------------------------
    def set_frequency(self, hz: float) -> None:
        """Set the CW frequency in Hz (FREQ <v>)."""

    def read_frequency(self) -> float:
        """Read back the CW frequency in Hz (FREQ?)."""

    # ---- phase -----------------------------------------------------------
    def set_phase(self, deg: float) -> None:
        """Set the phase in degrees (PHAS <v> DEG)."""

    def read_phase(self) -> float:
        """Read back the phase in degrees (PHAS?)."""

    # ---- identity --------------------------------------------------------
    def idn(self) -> str:
        """Instrument identification string (*IDN?). '' if unknown."""
