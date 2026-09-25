"""The hardware interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object with these
methods counts as a PowerMeterBackend, whether it is the real PM16 (through
Thorlabs' TLPMX library) or the simulator. The PowerMeter brain depends ONLY on
this, so swapping hardware for the simulator changes nothing above this line.

Clamping to safe limits is the brain's job, not the backend's. Every call here
may block for a USB round trip; `measure_power` blocks for a whole averaged
reading.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: flags `measure_power` can return next to the value
FLAG_OK = ""
FLAG_OVERRANGE = "overrange"     # signal above the current range (manual range too low)
FLAG_UNDERRUN = "underrun"       # signal below what the range can resolve
FLAG_NAN = "nan"                 # the meter had no valid value


@runtime_checkable
class PowerMeterBackend(Protocol):
    """A Thorlabs optical power meter with one sensor channel."""

    def open(self) -> None:
        """Connect. Must not change any setting the brain has not asked for."""

    def close(self) -> None:
        """Disconnect. Safe to call more than once and on a crash."""

    def idn(self) -> str:
        """'Thorlabs PM16-120 S/N ... fw ...', or '' if unknown."""

    def sensor_name(self) -> str:
        """The sensor head's name as the meter reports it, or ''."""

    # ---- wavelength (sets the responsivity) ------------------------------
    def set_wavelength(self, nm: float) -> None: ...
    def get_wavelength(self) -> float: ...
    def wavelength_range(self) -> tuple[float, float]:
        """(min, max) nm the sensor is calibrated for."""

    # ---- range -----------------------------------------------------------
    def set_auto_range(self, on: bool) -> None: ...
    def get_auto_range(self) -> bool: ...
    def set_range(self, watts: float) -> None:
        """Manual range: the largest power you expect to measure, in W."""
    def get_range(self) -> float:
        """The range currently in use, in W (also while auto-ranging)."""
    def range_limits(self) -> tuple[float, float]:
        """(smallest, largest) range the meter offers, in W."""

    # ---- averaging -------------------------------------------------------
    def average_time_s(self) -> float:
        """How long one reading averages. Read-only: the PM16 refuses to change it."""

    # ---- the measurement -------------------------------------------------
    def measure_power(self) -> tuple[float, str]:
        """One averaged reading: (watts, flag). Blocks until it is done."""

    # ---- dark / zero adjustment ------------------------------------------
    def start_zero(self) -> None:
        """Start the dark-offset adjustment. Cover the sensor first. Runs in
        the meter; no readings are possible until it finishes."""
    def cancel_zero(self) -> None: ...
    def zero_running(self) -> bool: ...
    def dark_offset(self) -> float:
        """The stored dark offset, in the sensor's native unit (A for a photodiode)."""
