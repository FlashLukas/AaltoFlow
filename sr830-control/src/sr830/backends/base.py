"""The hardware interface -- what the brain is allowed to assume about an SR830.

A `typing.Protocol`: any object with these methods counts, whether it is the
real SR830 on GPIB or the simulator. The brain depends only on this, so
swapping the simulator for the instrument changes nothing above this file.

The methods mirror the SR830's GPIB commands one to one, and the integer
arguments ARE the GPIB parameters (OFLT 7 = 30 ms, SENS 20 = 10 mV, ...). The
tables that translate them live in `tables.py`; turning "30 ms" into 7 is the
brain's job, sending 7 is the backend's.

Rules the brain relies on:
  * setters apply immediately and do NOT clamp -- clamping is the brain's job.
  * the three `read_*` calls are the only ones made at the polling rate.
  * `busy()` must never queue behind a running command. On GPIB it is a
    SERIAL POLL (read_stb), which the SR830 answers even mid-command; a normal
    query would wait until an Auto Gain has finished, and time out.
  * the brain serialises ALL calls under one lock (a GPIB session is not
    thread-safe), so a backend needs no locking of its own for correctness.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SR830Backend(Protocol):
    """A Stanford Research SR830 DSP lock-in amplifier."""

    def open(self) -> None:
        """Connect and route replies to this interface (OUTX 1 on GPIB)."""

    def close(self) -> None:
        """Disconnect. Safe to call on shutdown or after a crash."""

    def idn(self) -> str:
        """Identification string ('' if unknown)."""

    # ---- reference (manual 5-4) ------------------------------------------------
    def set_ref_source(self, internal: bool) -> None:
        """FMOD: 1 = internal oscillator, 0 = external reference input."""

    def set_frequency(self, hz: float) -> None:
        """FREQ: internal oscillator frequency (only allowed in internal mode)."""

    def set_harmonic(self, n: int) -> None:
        """HARM: detect at the n-th harmonic."""

    def set_phase(self, deg: float) -> None:
        """PHAS: reference phase shift in degrees."""

    def set_trigger(self, i: int) -> None:
        """RSLP: external trigger 0 = sine, 1 = TTL rising, 2 = TTL falling."""

    def set_sine_out(self, volts: float) -> None:
        """SLVL: SINE OUT amplitude in Vrms (0.004 .. 5)."""

    # ---- input (5-5) ------------------------------------------------------------
    def set_input(self, source: int, ground: int, coupling: int, line: int) -> None:
        """ISRC, IGND, ICPL, ILIN in one call."""

    # ---- gain and filter (5-6, 5-7) -----------------------------------------------
    def set_sensitivity(self, i: int) -> None:
        """SENS 0..26."""

    def set_reserve(self, i: int) -> None:
        """RMOD 0 = high, 1 = normal, 2 = low noise."""

    def set_time_constant(self, i: int) -> None:
        """OFLT 0..19. The instrument may apply a different one (read back!)."""

    def set_slope(self, i: int) -> None:
        """OFSL 0..3 = 6/12/18/24 dB/oct."""

    def set_sync(self, on: bool) -> None:
        """SYNC: synchronous filter below 200 Hz."""

    # ---- aux out (5-9) -------------------------------------------------------------
    def set_aux_out(self, k: int, volts: float) -> None:
        """AUXV k (1..4), volts (-10.5 .. 10.5)."""

    # ---- read-back ---------------------------------------------------------------
    def read_settings(self) -> dict:
        """What the instrument is ACTUALLY set to (it changes some on its own):
        {"sens": i, "reserve": i, "tc": i, "slope": i, "phase_deg": x,
         "harmonic": n, "sine_out_V": x}."""

    # ---- data (at the polling rate) ---------------------------------------------------
    def read_outputs(self) -> dict:
        """SNAP?1,2,9: {"x": .., "y": .., "freq_Hz": reference frequency}.
        X and Y taken at the same instant, in V (or A in current mode)."""

    def read_aux(self) -> list[float]:
        """AUX IN 1..4 in volts (SNAP?5,6,7,8)."""

    def read_lia_status(self) -> int:
        """LIAS?: the LIA status byte. Reading CLEARS its latched bits.
        bit 0 input/reserve overload, 1 filter overload, 2 output overload,
        3 reference unlock, 4 frequency range switch, 5 time constant changed."""

    # ---- auto functions (5-11) ---------------------------------------------------------
    def auto(self, name: str) -> None:
        """Start AGAN ('gain'), ARSV ('reserve') or APHS ('phase')."""

    def busy(self) -> bool:
        """True while a command (an auto function) is still executing."""
