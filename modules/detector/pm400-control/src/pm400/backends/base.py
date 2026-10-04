"""The hardware interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object with these
methods counts as a ConsoleBackend, whether it is the real PM400 (through
Thorlabs' TLPMX library) or the simulator. The Pm400Meter brain depends ONLY on
this, so swapping hardware for the simulator changes nothing above this line.

Clamping to safe limits is the brain's job, not the backend's. Every call here
may block for a USB round trip; `measure_power` / `measure_energy` block for a
whole reading (one averaging time, or until the next pulse).

The PM400 is a console with ONE sensor connector. Which head is plugged in
decides everything else, so `sensor_info()` comes first: it says the head KIND
(below) and whether the head measures power (W) or energy per pulse (J).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: flags a measurement can return next to the value
FLAG_OK = ""
FLAG_OVERRANGE = "overrange"     # signal above the current range (manual range too low)
FLAG_UNDERRUN = "underrun"       # signal below what the range can resolve
FLAG_NAN = "nan"                 # the console had no valid value
FLAG_NO_SENSOR = "no_sensor"     # set by the brain: no usable head plugged in
#: every flag status can report (status `flag`; describe's enum options)
READING_FLAGS = (FLAG_OK, FLAG_OVERRANGE, FLAG_UNDERRUN, FLAG_NAN, FLAG_NO_SENSOR)

#: head kinds, the vocabulary of sensor_info()["kind"]
HEAD_NONE = "none"               # nothing plugged in
HEAD_PHOTODIODE = "photodiode"   # power, fast, responsivity depends strongly on wavelength
HEAD_THERMAL = "thermal"         # power, flat spectrum, ~1 s response
HEAD_PYRO = "pyro"               # energy per pulse
HEAD_OTHER = "other"             # e.g. a 4-quadrant head: not supported by this module

HEAD_KINDS = (HEAD_PHOTODIODE, HEAD_THERMAL, HEAD_PYRO, HEAD_NONE, HEAD_OTHER)


def empty_sensor_info() -> dict:
    """What sensor_info() returns when nothing is plugged in."""
    return {"kind": HEAD_NONE, "name": "", "serial": "", "energy": False,
            "wavelength_settable": False, "zero_supported": False}


@runtime_checkable
class ConsoleBackend(Protocol):
    """A Thorlabs power/energy meter console with one sensor connector."""

    def open(self) -> None:
        """Connect. Must not change any setting the brain has not asked for."""

    def close(self) -> None:
        """Disconnect. Safe to call more than once and on a crash."""

    def idn(self) -> str:
        """'Thorlabs PM400 S/N ... fw ...', or '' if unknown."""

    def sensor_info(self) -> dict:
        """The head now plugged in: {kind, name, serial, energy (bool),
        wavelength_settable (bool), zero_supported (bool)}. See HEAD_*."""

    # ---- wavelength (the correction the console applies) -----------------
    def set_wavelength(self, nm: float) -> None: ...
    def get_wavelength(self) -> float: ...
    def wavelength_range(self) -> tuple[float, float]:
        """(min, max) nm the head is calibrated for."""

    # ---- power range (photodiode / thermal heads) -------------------------
    def set_auto_range(self, on: bool) -> None: ...
    def get_auto_range(self) -> bool: ...
    def set_range(self, watts: float) -> None:
        """Manual range: the largest power you expect to measure, in W."""
    def get_range(self) -> float:
        """The power range currently in use, in W (also while auto-ranging)."""
    def range_limits(self) -> tuple[float, float]:
        """(smallest, largest) power range the head offers, in W."""

    # ---- energy range (pyroelectric heads) --------------------------------
    def set_energy_range(self, joules: float) -> None: ...
    def get_energy_range(self) -> float: ...
    def energy_range_limits(self) -> tuple[float, float]: ...

    # ---- averaging (power heads) -----------------------------------------
    def set_avg_time(self, seconds: float) -> None:
        """How long the console averages its samples into one reading."""
    def get_avg_time(self) -> float: ...
    def avg_time_limits(self) -> tuple[float, float]: ...

    # ---- the measurement -------------------------------------------------
    def measure_power(self) -> tuple[float, str]:
        """One averaged power reading: (watts, flag). Blocks until done."""
    def measure_energy(self) -> tuple[float, str]:
        """The energy of one pulse: (joules, flag). Blocks until done."""
    def measure_frequency(self) -> float:
        """The pulse repetition rate the head sees, Hz (pyro heads)."""

    # ---- dark / zero adjustment (not on energy heads) ---------------------
    def start_zero(self) -> None:
        """Start the zero adjustment. Cover the head first. Runs in the
        console; no readings are possible until it finishes."""
    def cancel_zero(self) -> None: ...
    def zero_running(self) -> bool: ...
    def dark_offset(self) -> float:
        """The stored zero offset, in the head's native unit (A for a
        photodiode, V for a thermopile)."""
