"""The hardware interface -- what the brain is allowed to assume about a 7230.

A `typing.Protocol`: any object with these methods counts, whether it is the
real instrument (`tcp7230.py`) or the simulator (`sim.py`). The brain depends
only on this, so swapping the simulator for the instrument changes nothing
above this file.

The methods speak the instrument's OWN numbers where it has them -- the index
of a time constant, of a sensitivity, of a reference source -- because that is
what the `TC n`, `SEN n`, `IE n` commands take. Turning "100 ms" into index 12
is the brain's job (with `tables.py`), so both backends stay thin.

Two design rules the brain relies on:
  * setters apply immediately; clamping to limits happens in the brain.
  * `read_outputs` is the ONLY call made at the polling rate. Everything else
    is called on a user action. The brain serialises ALL calls under one lock:
    the instrument handles one command at a time on one socket, and two
    threads interleaving bytes on it would desynchronise the replies.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LockInBackend(Protocol):
    """A single-reference DSP lock-in amplifier (the Signal Recovery 7230)."""

    def open(self) -> None:
        """Connect and put the instrument in single-reference mode. Must not
        raise the oscillator amplitude."""

    def close(self) -> None:
        """Disconnect. Safe to call on shutdown or after a crash."""

    def idn(self) -> str:
        """Identification ('' if unknown)."""

    # ---- reference channel + oscillator ----------------------------------
    def set_ref_source(self, index: int) -> None:
        """0 internal, 1 external TTL, 2 external analog (IE n)."""

    def set_osc_frequency(self, hz: float) -> None:
        """Internal oscillator frequency in Hz (OF.)."""

    def set_osc_amplitude(self, volts_rms: float) -> None:
        """Internal oscillator amplitude in V rms (OA.)."""

    def set_phase(self, deg: float) -> None:
        """Reference phase shift in degrees (REFP.)."""

    def get_phase(self) -> float:
        """Read the reference phase back -- auto-phase changes it."""

    def set_harmonic(self, n: int) -> None:
        """Demodulate at n x the reference frequency (REFN n)."""

    # ---- signal channel ----------------------------------------------------
    def set_input(self, imode: int, vmode: int) -> None:
        """IMODE (0 voltage, 1 high-BW current, 2 low-noise current) and
        VMODE (0 grounded, 1 A, 2 -B, 3 A-B)."""

    def set_coupling(self, dc: bool) -> None:
        """DC (True) or AC (False) input coupling."""

    def set_fet(self, fet: bool) -> None:
        """FET (True) or bipolar (False) input device."""

    def set_float(self, floating: bool) -> None:
        """Input connector shells floating (True) or grounded (False)."""

    def set_line_filter(self, mode: int, fifty_hz: bool) -> None:
        """Mains notch filter: mode 0 off, 1 at 1f, 2 at 2f, 3 both."""

    def set_auto_ac_gain(self, on: bool) -> None:
        """Let the AC gain follow the sensitivity (AUTOMATIC 1)."""

    def set_sensitivity_index(self, index: int) -> None:
        """Full-scale sensitivity by table index (SEN n)."""

    def get_sensitivity_index(self) -> int:
        """Read the index back -- auto-sensitivity changes it."""

    # ---- output filter -----------------------------------------------------
    def set_fast_mode(self, on: bool) -> None:
        """FASTMODE: short time constants, but only 6/12 dB/oct."""

    def set_tc_index(self, index: int) -> None:
        """Time constant by table index (TC n)."""

    def get_time_constant(self) -> float:
        """The time constant actually applied, in seconds (TC.)."""

    def set_slope_index(self, index: int) -> None:
        """0..3 = 6/12/18/24 dB/octave (SLOPE n)."""

    # ---- data ------------------------------------------------------------
    def read_outputs(self, read_adc: bool = True) -> dict:
        """One reading: {"x", "y" (V or A), "freq_Hz" (the reference frequency
        meter, 0 when an external reference is unlocked), "adc": [V, V] (NaN
        when not read), "status": status byte, "overload": overload byte}."""

    # ---- automatic operations (they take time on the instrument) -----------
    def auto_phase(self) -> None:
        """AQN: rotate the phase so the signal is all in X."""

    def auto_sensitivity(self) -> None:
        """AS: pick the sensitivity that puts R at 30-90 % of full scale."""

    def auto_measure(self) -> None:
        """ASM: auto-sensitivity, then auto-phase."""
