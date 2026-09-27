"""The hardware interface -- what the rest of the code is allowed to assume.

We use typing.Protocol, Python's "structural interface": any object that has
these methods counts as a BipolarSupplyBackend, whether it is the real Kepco BOP
or the simulator. The brain (`supply.py`) depends ONLY on this interface, so
swapping real hardware for the simulator changes nothing above this line.

This layer is deliberately dumb: every setter programs the value AT ONCE. The
ramp, the clamping to the safety envelope and the "ramp to zero before output
off" rule all live in the brain, so they are identical for sim and real.

Vocabulary (BIT 4886 manual sec. 4.1.1.1): the MAIN channel is what the mode
regulates; the LIMIT channel is the complementary quantity. On the wire both
are programmed with the same two commands -- VOLT and CURR -- and which one is
"main" depends on FUNC:MODE. The interface keeps that literal shape
(`program_voltage`, `program_current`), so the backend never has to know which
one is the limit.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class BipolarSupplyBackend(Protocol):
    """A four-quadrant bipolar power supply (Kepco BOP 20-10)."""

    def open(self) -> None:
        """Connect and initialise. Must leave the OUTPUT OFF and both channels
        programmed to 0."""

    def close(self) -> None:
        """Output off and disconnect. Safe to call on shutdown/crash. The brain
        ramps to zero BEFORE calling this; close() itself does not ramp."""

    def set_mode(self, mode: str) -> None:
        """'voltage' or 'current' (SCPI FUNC:MODE VOLT|CURR)."""

    def program_voltage(self, volts: float) -> None:
        """VOLT <v>: the output voltage in voltage mode, the voltage LIMIT in
        current mode (absolute value)."""

    def program_current(self, amps: float) -> None:
        """CURR <a>: the output current in current mode, the current LIMIT in
        voltage mode (absolute value)."""

    def set_output(self, on: bool) -> None:
        """OUTP ON|OFF. On the BOP, OUTP OFF programs 0 V / 0 A in one step --
        which is why the brain ramps first."""

    def measure_voltage(self) -> float:
        """Measured voltage at the output terminals (MEAS:VOLT?)."""

    def measure_current(self) -> float:
        """Measured current at the output terminals (MEAS:CURR?)."""

    def idn(self) -> str:
        """Instrument identification (*IDN?). '' if unknown."""
