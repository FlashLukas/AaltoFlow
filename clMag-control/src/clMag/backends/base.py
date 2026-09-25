"""The hardware interface -- what the rest of the code is allowed to assume.

We use typing.Protocol, which is Python's "structural interface": any object
that has these methods counts as a CurrentSource / FieldSensor, whether it is
the real Kepco or the simulator. The control code depends ONLY on these
interfaces, so swapping real hardware for the simulator changes nothing above
this line. (This is how you develop and test the whole controller with no
instruments plugged in.)
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CurrentSource(Protocol):
    """A programmable current supply (the Kepco BOP in constant-current mode)."""

    def open(self) -> None:
        """Connect and initialise (e.g. FUNC:MODE CURR, OUTP ON)."""

    def close(self) -> None:
        """Ramp to zero, OUTP OFF, disconnect. Safe to call on shutdown/crash."""

    def set_current(self, amps: float) -> None:
        """Command an output current. This sets it directly; ramping is the
        controller's job, not the backend's."""

    def read_current(self) -> float:
        """Measured output current in amps (the Kepco CURR? query)."""


@runtime_checkable
class FieldSensor(Protocol):
    """A voltage input averaging N samples at a given rate (the NI Hall probe)."""

    def open(self) -> None:
        ...

    def close(self) -> None:
        ...

    def read_voltage(self, samples: int, rate_Hz: float) -> float:
        """Acquire `samples` at `rate_Hz` and return their MEAN in volts.
        Blocks for roughly samples / rate_Hz seconds."""


@runtime_checkable
class AuxIO(Protocol):
    """The general-purpose I/O on the DAQ: analog out, analog in, digital out.
    Used by the AUX panel; independent of the field control loop."""

    def open(self) -> None:
        ...

    def close(self) -> None:
        ...

    def set_ao(self, channel: str, volts: float) -> None:
        """Drive an analog output to `volts`."""

    def read_ao(self, channel: str) -> float:
        """The last commanded AO voltage (the 6259 cannot read AO back)."""

    def read_ai(self, channel: str) -> float:
        """A single analog-input sample, in volts."""

    def set_do(self, line: str, state: bool) -> None:
        """Set a digital output line high/low."""

    def read_do(self, line: str) -> bool:
        """The current digital-output state."""
