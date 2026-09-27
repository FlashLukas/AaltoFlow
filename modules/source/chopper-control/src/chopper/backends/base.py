"""The hardware interface -- what the rest of the code is allowed to assume.

We use typing.Protocol, Python's "structural interface": any object that has
these methods counts as a ChopperBackend, whether it is the real MC2000B or the
simulator. The brain depends ONLY on this interface, so swapping real hardware
for the simulator changes nothing above this line.

The interface speaks the controller's own language on purpose: blade, reference
mode and output mode are the INDICES the MC2000B uses (`blade=n`, `ref=n`,
`output=n`), not names. Turning an index into a name depends on the blade
(`ref=2` means "ext-outer" on a 10/100 blade and does not exist on a 60-slot
one), and that knowledge lives in ONE place, `blades.py`, used by the brain.

Every call may block for a few milliseconds of serial traffic; the brain calls
them from its poll thread or from a command, always under one hardware lock.
Clamping is the brain's job, not the backend's.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ChopperBackend(Protocol):
    """A Thorlabs MC2000B optical chopper controller (or a simulation of one)."""

    def open(self) -> None:
        """Connect. Must NOT change the chopper's state (it is adopted)."""

    def close(self) -> None:
        """Disconnect. Leaves the wheel as it is; the brain decides whether to stop it."""

    def idn(self) -> str:
        """Model and firmware (id?). '' if unknown."""

    # ---- configuration (changeable in standby only, manual 5.2) -----------
    def get_blade(self) -> int: ...
    def set_blade(self, index: int) -> None: ...
    def get_ref(self) -> int: ...
    def set_ref(self, index: int) -> None: ...
    def get_output(self) -> int: ...
    def set_output(self, index: int) -> None: ...
    def get_nharmonic(self) -> int: ...
    def set_nharmonic(self, n: int) -> None: ...
    def get_dharmonic(self) -> int: ...
    def set_dharmonic(self, d: int) -> None: ...

    # ---- run-time controls (changeable while running) ---------------------
    def get_frequency(self) -> float:
        """Internal reference (synthesiser) frequency in Hz (freq?)."""
    def set_frequency(self, hz: float) -> None: ...
    def get_phase(self) -> float:
        """Phase adjust in degrees (phase?)."""
    def set_phase(self, deg: float) -> None: ...
    def get_enable(self) -> bool:
        """True while the motor runs (enable?)."""
    def set_enable(self, on: bool) -> None: ...

    # ---- measurements ------------------------------------------------------
    def read_refout_frequency(self) -> float:
        """Frequency of the REF OUT signal in Hz (refoutfreq?). With the output
        on a sensor this is the MEASURED wheel; on "target" it is the synthesiser."""
    def read_input_frequency(self) -> float:
        """Frequency on EXT REF IN in Hz (input?); 0 when nothing is connected."""
