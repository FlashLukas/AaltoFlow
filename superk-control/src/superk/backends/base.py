"""The hardware interface -- what the rest of the code is allowed to assume.

We use typing.Protocol, Python's "structural interface": any object that has
these methods counts as a SupercontinuumBackend, whether it is the real SuperK
(through NKT's DLL) or the simulator. The brain depends ONLY on this interface,
so swapping real hardware for the simulator changes nothing above this line.

Two physical modules sit behind it, and the method names say which:
  * the SuperK EXTREME main module  -- emission, power level, interlock, status
  * the SuperK SELECT RF driver     -- RF on/off, which crystal, and 8 lines
                                       (wavelength + amplitude each)

Everything is in ENGINEERING units here (nm, %, C). Converting to the register
units (pm, per-mille) is the real backend's job, so the brain never sees them.
Line/channel indices are 0-based at this layer (0..7).

Clamping to the safety limits and refusing emission with an open interlock are
the BRAIN's job, not the backend's -- a backend does exactly what it is told.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SupercontinuumBackend(Protocol):
    """A SuperK EXTREME laser with a SuperK SELECT AOTF RF driver."""

    def open(self) -> None:
        """Connect. Must NOT switch emission or RF on."""

    def close(self) -> None:
        """Emission off, RF off, disconnect. Safe to call on shutdown/crash."""

    def identify(self) -> str:
        """Human-readable identification (module types / firmware). '' if unknown."""

    # ---- EXTREME main module -------------------------------------------
    def set_emission(self, on: bool) -> None: ...
    def read_emission(self) -> bool:
        """True while the laser reports emission (status bit 0)."""
    def read_interlock(self) -> int:
        """Interlock state code: 0 = circuit open, 1 = closed but waiting for a
        reset, 2 = OK (# VERIFY against the SDK register description)."""
    def reset_interlock(self) -> None: ...
    def read_status_bits(self) -> int: ...
    def set_power(self, pct: float) -> None: ...
    def read_power(self) -> float: ...
    def read_inlet_temp(self) -> float: ...
    def set_watchdog(self, seconds: int) -> None:
        """Emission switches off by itself if the laser hears nothing for this long."""

    # ---- SELECT RF driver ------------------------------------------------
    def set_rf(self, on: bool) -> None: ...
    def read_rf(self) -> bool: ...
    def select_crystal(self, crystal: int) -> None:
        """Route the RF to crystal `crystal`, in NKT's numbering: 1, 2 = the two
        crystal slots of the SELECT housing with the LOWEST bus address, 3, 4 =
        the next housing. RF power must be OFF (the manual forbids switching
        under RF). Raises if the crystal cannot be reached -- e.g. it sits in
        another housing and the RF cable has to be moved by hand."""
    def read_crystal(self) -> int:
        """The crystal the RF driver reports as connected (0 = none)."""
    def read_crystal_range(self) -> tuple[float, float] | None:
        """(min_nm, max_nm) of the active crystal as the driver reports it, or
        None when the driver does not know (then the config's range is used)."""
    def read_crystal_temp(self) -> float: ...
    def set_wavelength(self, ch: int, nm: float) -> None: ...
    def read_wavelength(self, ch: int) -> float: ...
    def set_amplitude(self, ch: int, pct: float) -> None: ...
    def read_amplitude(self, ch: int) -> float: ...
