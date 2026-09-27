"""The hardware interface -- what the rest of the code is allowed to assume.

We use typing.Protocol, Python's "structural interface": any object that has
these methods counts as a MicrowaveSource, whether it is the real SG12000L or
the simulator. The brain depends ONLY on this interface, so swapping real
hardware for the simulator changes nothing above this line.

Every setter commands the value directly: a synthesiser locks within a few
milliseconds (datasheet: ~3 ms typical), so there is no ramping here. Clamping
to the safety limits is the brain's job, not the backend's.

Threading: the brain calls these from its command thread AND its poll thread,
always under one lock, so a backend never has to be thread-safe itself.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MicrowaveSource(Protocol):
    """A programmable CW microwave source (the DS Instruments SG12000L)."""

    def open(self) -> None:
        """Connect and initialise. Must leave the RF OUTPUT OFF."""

    def close(self) -> None:
        """Turn RF off and disconnect. Safe to call on shutdown/crash/twice."""

    def idn(self) -> str:
        """Identification string (*IDN?). '' if unknown."""

    # ---- what this particular unit can do (read once at connect) ----------
    def freq_range(self) -> tuple[float, float]:
        """(min, max) CW frequency in Hz the unit supports."""

    def power_range(self) -> tuple[float, float]:
        """(min, max) calibrated output power in dBm."""

    def has_phase(self) -> bool:
        """True if the firmware accepts a phase setting."""

    # ---- RF output on/off -----------------------------------------------
    def set_output(self, on: bool) -> None: ...
    def read_output(self) -> bool: ...

    # ---- frequency (Hz) ---------------------------------------------------
    def set_frequency(self, hz: float) -> None: ...
    def read_frequency(self) -> float: ...

    # ---- power (dBm) ------------------------------------------------------
    def set_power(self, dBm: float) -> None: ...
    def read_power(self) -> float: ...

    # ---- phase (deg) ------------------------------------------------------
    def set_phase(self, deg: float) -> None: ...
    def read_phase(self) -> float: ...

    # ---- 10 MHz reference ------------------------------------------------
    def set_reference(self, mode: str) -> None:
        """'internal' | 'external' | 'auto'."""

    def read_reference(self) -> str:
        """The current reference setting, same vocabulary."""

    def external_ref_detected(self) -> bool:
        """True if a 10 MHz signal is present on the rear MCX jack."""

    # ---- health ------------------------------------------------------------
    def usb_volts(self) -> float:
        """USB supply voltage the unit sees (a weak port makes it misbehave)."""

    def errors(self) -> list[str]:
        """Drain the instrument's error queue. Empty list == clean."""
