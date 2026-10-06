"""The hardware interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object that has these
methods counts as a PhaseShifterBackend, whether it is the real PS6000L or the
simulator. The brain depends ONLY on this interface, so swapping real hardware
for the simulator changes nothing above this line.

Backends speak the DEVICE's language: phase already wrapped into -180..+180 and
already rounded to the step. Clamping, rounding and wrapping are the brain's job
(one place, testable without hardware).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PhaseShifterBackend(Protocol):
    """A programmable RF phase shifter with an output attenuator."""

    def open(self) -> None:
        """Connect. QUERIES ONLY: must not change the unit's state (no reset,
        no output off, no phase/attenuation write) -- the brain reads the
        state back and adopts it (the suite's adopt-on-start rule)."""

    def close(self, rf_off: bool = True) -> None:
        """Turn the RF output off and disconnect. Safe to call twice / on a crash.
        rf_off=False: a restart (shutdown{keep_outputs}) -- disconnect and
        release the port, but leave the RF output as it is."""

    # ---- phase -----------------------------------------------------------
    def set_phase(self, deg: float) -> None:
        """Set the phase shift in degrees, -180..+180 (SCPI `PHASE <deg>`)."""

    def read_phase(self) -> float:
        """The phase the unit holds, in degrees (`PHASE?`)."""

    # ---- output attenuator -------------------------------------------------
    def set_attenuation(self, dB: float) -> None:
        """Set the output step attenuator in dB (`ATT <dB>`)."""

    def read_attenuation(self) -> float:
        """Read back the attenuator in dB (`ATT?`)."""

    # ---- RF output on/off ------------------------------------------------
    def set_output(self, on: bool) -> None:
        """Enable/disable the RF output (`OUTP:STAT ON|OFF`)."""

    def read_output(self) -> bool:
        """True if the RF output is on (`OUTP:STAT?`)."""

    # ---- carrier frequency -------------------------------------------------
    def set_frequency(self, mhz: float) -> None:
        """Tell the unit the carrier frequency, IF it has a command for it
        (the PS6000L V3 command list has none; see Device.freq_command)."""

    # ---- identity --------------------------------------------------------
    def idn(self) -> str:
        """Identification string (`*IDN?`). '' if unknown."""
