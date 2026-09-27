"""The hardware interface -- what the rest of the code is allowed to assume.

We use typing.Protocol, Python's "structural interface": any object that has
these methods counts as a DualSynth, whether it is the real SynthHD PRO or the
simulator. The brain (Synthesizer) depends ONLY on this interface, so swapping
real hardware for the simulator changes nothing above this line.

Channels are numbered the way the instrument numbers them: 0 = RFoutA,
1 = RFoutB. (The wire and the GUI say "a"/"b"; the brain translates.)

Clamping to the safety limits is the brain's job, not the backend's. The
backend also does no threading of its own: exactly ONE thread (the brain's
worker) ever calls it, which is what makes a single serial port safe to share
between two channels.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DualSynth(Protocol):
    """A two-channel programmable RF synthesizer (Windfreak SynthHD PRO v2)."""

    def open(self) -> None:
        """Connect and leave BOTH outputs off, whatever the instrument booted
        into (a SynthHD can be told to power up radiating, from its EEPROM)."""

    def close(self) -> None:
        """Both outputs off, then disconnect. Safe to call on shutdown/crash."""

    # ---- per channel ------------------------------------------------------
    def set_output(self, ch: int, on: bool) -> None:
        """Switch one channel's RF output on or off."""

    def set_frequency(self, ch: int, hz: float) -> None:
        """Set one channel's CW frequency in Hz."""

    def read_frequency(self, ch: int) -> float:
        """The frequency the instrument reports, in Hz (snapped to its grid)."""

    def set_power(self, ch: int, dBm: float) -> None:
        """Set one channel's output level in dBm (the instrument levels it)."""

    def set_phase(self, ch: int, deg: float) -> None:
        """Set one channel's phase, ABSOLUTE degrees 0..360 relative to the
        backend's own zero. A backend whose hardware only takes phase steps
        does the bookkeeping itself."""

    def read_locked(self, ch: int) -> bool:
        """True if that channel's PLL reports phase lock."""

    def read_leveled(self, ch: int) -> bool:
        """True if the instrument reached the requested power (its leveling /
        calibration succeeded for the current frequency and power)."""

    # ---- shared -----------------------------------------------------------
    def set_reference(self, source: str, ext_MHz: float) -> None:
        """Select "external" / "internal_27MHz" / "internal_10MHz"; ext_MHz is
        the external reference frequency (used for "external" only)."""

    def read_temperature(self) -> float:
        """Internal temperature sensor in degC."""

    def idn(self) -> str:
        """A one-line identification (model, firmware). '' if unknown."""
