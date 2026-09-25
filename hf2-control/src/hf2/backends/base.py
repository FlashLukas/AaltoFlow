"""The hardware interface -- what the brain is allowed to assume about a lock-in.

A `typing.Protocol`: any object with these methods counts, whether it is the
real HF2LI or the simulator. The brain depends only on this, so swapping the
simulator for the instrument changes nothing above this file.

Everything is addressed by the instrument's own node indices (demodulator 0..5,
oscillator 0..1, input 0..1), the same numbers you see in LabOne. Mapping the
user's "channel 1 / channel 2" onto those indices is the brain's job, from the
config.

Two design rules the brain relies on:
  * setters apply immediately; clamping to limits happens in the brain.
  * `read_demods` is the ONLY call made at the polling rate. Everything else is
    called on a user action. The brain serialises ALL calls under one lock,
    because the vendor connection is not guaranteed to be thread-safe (kim's
    serial link desynchronised exactly this way when two threads shared it).
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from ..config import Channel


@runtime_checkable
class LockInBackend(Protocol):
    """A multi-demodulator lock-in amplifier (the Zurich Instruments HF2LI)."""

    def open(self) -> None:
        """Connect. Must not enable any signal OUTPUT."""

    def close(self) -> None:
        """Disconnect. Safe to call on shutdown or after a crash."""

    def idn(self) -> str:
        """Identification string ('' if unknown)."""

    # ---- per-channel set-up (routing, input, harmonic, phase, rate) ------
    def setup_channel(self, ch: Channel, rate_Sa_s: float) -> None:
        """Route demodulator ch.demod to ch.signal_input and ch.oscillator,
        apply input range/coupling/impedance, harmonic and phase, and enable
        the demodulator's data stream at `rate_Sa_s`."""

    # ---- reference -------------------------------------------------------
    def set_reference(self, oscillator: int, external: bool, ref_input: int) -> None:
        """External: lock `oscillator` to the signal on `ref_input` (a PLL).
        Internal: free-running oscillator whose frequency we set."""

    def pll_locked(self, oscillator: int) -> bool:
        """True if the external-reference PLL on this oscillator is locked."""

    def set_oscillator_frequency(self, oscillator: int, hz: float) -> None:
        """Internal mode: set the oscillator frequency."""

    # ---- demodulator filter ----------------------------------------------
    def set_time_constant(self, demod: int, tc_s: float) -> None:
        """Set the low-pass time constant. The hardware may round it."""

    def get_time_constant(self, demod: int) -> float:
        """The time constant the hardware actually applied."""

    def set_order(self, demod: int, order: int) -> None:
        """Set the filter order (1..8)."""

    def get_order(self, demod: int) -> int:
        """Read back the filter order."""

    # ---- data --------------------------------------------------------------
    def read_demods(self, demods: Sequence[int]) -> list[dict]:
        """Latest output of each demodulator: {"x": V, "y": V, "freq_Hz": Hz}.
        `freq_Hz` is the DEMODULATION frequency (oscillator x harmonic)."""

    def read_aux(self) -> list[float]:
        """Latest AUX IN 1 and AUX IN 2 voltages, [V, V]."""
