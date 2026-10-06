"""The hardware interface -- what the rest of the code is allowed to assume.

We use typing.Protocol, which is Python's "structural interface": any object
that has these methods counts as a CurrentSource / FieldSensor, whether it is
the real Kepco or the simulator. The control code depends ONLY on these
interfaces, so swapping real hardware for the simulator changes nothing above
this line. (This is how you develop and test the whole controller with no
instruments plugged in.)

ONE INSTRUMENT, ONE SERVICE: a REAL backend must never send a byte to an
instrument whose address is not claimed. Because the Hall probe and the AUX
I/O share one DAQ card, the claims are taken once for the whole module, before
any backend is opened -- see backends/claims.py (HardwareClaims,
physical_addresses). Simulators claim nothing.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class CurrentSource(Protocol):
    """A programmable current supply (the Kepco BOP in constant-current mode).

    ADOPT-ON-START RULE (Lukas, 2026-09-27): connecting to the supply must not
    change what it is doing. open() only QUERIES (e.g. `FUNC:MODE?`, `OUTP?`,
    `MEAS:CURR?`); it never sends `*RST`, `FUNC:MODE CURR`, `OUTP ON` or a
    `CURR` value. The controller reads the state back and adopts it. Only when a
    user command first needs to move the current does the controller call
    `enable_output()` -- after programming the present output current, so the
    magnet does not jump to some stale programmed value.
    """

    def open(self) -> None:
        """Connect. Queries only -- no write that changes the supply's state."""

    def close(self, output_off: bool = True) -> None:
        """OUTP OFF and disconnect. Called on shutdown/crash, after the
        controller has ramped the current to zero (shutdown is unchanged).
        output_off=False (a restart, shutdown{keep_outputs: true}): disconnect
        only -- no OUTP OFF, no CURR; the next start adopts the supply."""

    def set_current(self, amps: float) -> None:
        """Command an output current. This sets it directly; ramping is the
        controller's job, not the backend's."""

    def read_current(self) -> float:
        """The current actually flowing, in amps (0 while the output is off).
        On the Kepco this is `MEAS:CURR?`, not `CURR?`, which would return the
        PROGRAMMED value even with the output off.  # VERIFY on the BOP"""

    def read_output(self) -> bool:
        """Is the output switched on? (`OUTP?`)  # VERIFY reply format"""

    def enable_output(self) -> None:
        """Make the supply ready to drive current: `FUNC:MODE CURR` + `OUTP ON`.
        Called by the controller only when a user command starts to move the
        current, never at start."""


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
        """Create the DAQ tasks. Must NOT write any AO or DO value: whatever
        the BNCs are driving when the service starts keeps being driven."""

    def close(self) -> None:
        ...

    def set_ao(self, channel: str, volts: float) -> None:
        """Drive an analog output to `volts`."""

    def read_ao(self, channel: str) -> Optional[float]:
        """The last AO voltage commanded in THIS session, or None if it has not
        been commanded since start: the 6259 cannot read AO back, and reporting
        0 V for an output that may be driving 3 V would be a lie."""

    def read_ai(self, channel: str) -> float:
        """A single analog-input sample, in volts."""

    def set_do(self, line: str, state: bool) -> None:
        """Set a digital output line high/low."""

    def read_do(self, line: str) -> bool:
        """The digital-output state, read from the line itself, so a state set
        before the service started is adopted (the 6259's port0 lines can be
        read back while driven -- # VERIFY with nidaqmx)."""
