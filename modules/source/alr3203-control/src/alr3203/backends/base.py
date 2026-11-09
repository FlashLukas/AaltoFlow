"""The hardware interfaces -- what the rest of the code is allowed to assume.

We use typing.Protocol, Python's "structural interface": any object that has
these methods counts as a backend, whether it is the real ALR3203 or the
simulator. The brain (`supply.py`) depends ONLY on these interfaces, so swapping
real hardware for the simulator changes nothing above this line.

TWO pieces of hardware make the bipolar magnet supply:

  SupplyBackend   the ELC ALR3203 itself: a UNIPOLAR supply with a voltage
                  setpoint and a current setpoint (it regulates whichever it
                  reaches first). Values are magnitudes (>= 0).
  PolarityRelay   the relay in the magnet leads, on a DAQ line. `inverted`
                  True = the current flows the other way through the magnet.

This layer is deliberately dumb: every setter programs the value AT ONCE. The
ramp, the clamping, the "stop at zero, wait for no current, then flip the
relay" rule and the "ramp to zero before output off" rule all live in the
brain, so they are identical for sim and real.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SupplyBackend(Protocol):
    """A single-output CV/CC laboratory supply (ELC ALR3203)."""

    def open(self) -> None:
        """Connect. Must NOT change what the supply outputs (Lukas,
        2026-09-27): no output switching, no programming."""

    def read_state(self) -> dict:
        """The supply's present state, from queries only:
        {"output": bool, "voltage_V": VOLT setpoint, "current_A": CURR setpoint,
         "regulation": "voltage" | "current" | "off"}  (all magnitudes)."""

    def close(self, output_off: bool = True) -> None:
        """Output off and disconnect. The brain ramps to zero BEFORE calling
        this; close() itself does not ramp. output_off=False: disconnect and
        release the address, writing nothing to the output."""

    def disconnect(self) -> None:
        """Let go WITHOUT writing anything (close the port, release the claim)."""

    def program_voltage(self, volts: float) -> None:
        """The VOLTAGE setpoint (>= 0)."""

    def program_current(self, amps: float) -> None:
        """The CURRENT setpoint (>= 0)."""

    def set_output(self, on: bool) -> None:
        """Output ON / OFF."""

    def measure_voltage(self) -> float:
        """Measured output voltage (>= 0)."""

    def measure_current(self) -> float:
        """Measured output current (>= 0)."""

    def read_regulation(self) -> str:
        """What the supply regulates NOW: "voltage" (CV), "current" (CC) or
        "off" (output off)."""

    def idn(self) -> str:
        """Instrument identification. '' if unknown."""


@runtime_checkable
class PolarityRelay(Protocol):
    """The relay that reverses the magnet current."""

    def open(self) -> None:
        """Connect. Writes nothing (the relay stays as it is)."""

    def close(self) -> None:
        """Disconnect. Writes nothing (the relay stays as it is)."""

    def read(self) -> bool | None:
        """True = inverted, False = normal, None = unknown (not reachable, or
        the line's level cannot be read back yet)."""

    def set(self, inverted: bool) -> None:
        """Switch the relay. Raises if it could not be switched. The brain
        calls it only with (measured) zero current."""

    def describe(self) -> str:
        """One line for the log: what drives the relay."""
