"""The hardware interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object with these
methods counts as an AmpBackend, whether it is the real amplifier or the
simulator. The brain depends ONLY on this, so swapping real hardware for the
simulator changes nothing above this line.

Rules for implementers:
  * open() only CONNECTS: it must not change the amplifier's state (no output
    off, no gain write, no reset). The brain reads the state afterwards and
    adopts it (Lukas's rule, 2026-09-27). close() switches the output off.
  * set_gain() receives a value the brain has already clamped to the safety
    envelope and quantised to the device's step. The backend just sends it.
  * Every method may be slow (a serial round trip) and may raise: the brain
    calls them only from its own lock, and the poll thread turns a failed read
    into `hw_error` in the status instead of a crash.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AmpBackend(Protocol):
    """A USB-controlled variable-gain RF amplifier (DS Instruments GB/PA family)."""

    def open(self) -> None:
        """Connect. Queries only -- must NOT change the output or the gain."""

    def close(self) -> None:
        """Switch the amplifier off and disconnect. Safe on shutdown/crash."""

    # ---- amplifier output on/off ---------------------------------------
    def set_output(self, on: bool) -> None:
        """Enable/disable the amplifier stage (OUTP:STAT ON|OFF)."""

    def read_output(self) -> bool:
        """True if the amplifier stage is on (OUTP:STAT?)."""

    # ---- gain -----------------------------------------------------------
    def set_gain(self, dB: float) -> None:
        """Set the gain SETTING in dB (GAIN <v>)."""

    def read_gain(self) -> float:
        """Read back the gain setting in dB (GAIN?)."""

    # ---- health ---------------------------------------------------------
    def read_temperature(self) -> float:
        """Internal temperature in C (*TEMP?)."""

    def read_supply(self) -> float:
        """Measured USB supply voltage in V (*SYSVOLTS?)."""

    # ---- identity -------------------------------------------------------
    def idn(self) -> str:
        """Identification string (*IDN?). '' if unknown."""
