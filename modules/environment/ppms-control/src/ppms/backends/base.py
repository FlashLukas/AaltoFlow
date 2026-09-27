"""The hardware interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object with these
methods counts as a CryostatBackend. There are two: the simulator (`sim.py`)
and MultiVu through Quantum Design's MultiPyVu (`multivu.py`). The Cryostat
brain depends ONLY on this interface.

Units are the suite's: mT, mT/s, K, K/min. The real backend converts to and
from MultiVu's oersted; nothing above it ever sees Oe.

Statuses are MultiVu's own words ("Holding (driven)", "Ramping", "Stable",
"Chasing", ...), passed through unchanged. The brain decides what counts as
"reached" (see `cryostat.FIELD_HOLDING` / `TEMPERATURE_STABLE`), so the two
backends only have to agree on the vocabulary, which is MultiVu's.

Everything here is fire-and-forget, like the instrument: `set_field` STARTS a
ramp and returns; the brain watches the readings to see it arrive.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CryostatBackend(Protocol):
    """A Quantum Design cryostat (DynaCool): magnet, temperature, chamber."""

    simulated: bool

    def open(self) -> None:
        """Connect. Must NOT change any setpoint: the brain adopts what the
        system is already doing (a cryostat at 2 K and 5 T stays there)."""

    def close(self) -> None:
        """Disconnect. Must NOT change any setpoint either. Safe to call twice."""

    def idn(self) -> str:
        """Which system this is, e.g. 'DYNACOOL (MultiPyVu 3.6.1)'. '' if unknown."""

    # ---- field ---------------------------------------------------------------
    def read_field(self) -> tuple[float, str]:
        """(measured field in mT, MultiVu's field status)."""

    def read_field_setpoint(self) -> tuple[float, float, str]:
        """(setpoint mT, rate mT/s, approach name) as MultiVu holds them now."""

    def set_field(self, field_mT: float, rate_mT_per_s: float, approach: str) -> None:
        """Start driving to `field_mT`. Returns at once."""

    # ---- temperature -----------------------------------------------------------
    def read_temperature(self) -> tuple[float, str]:
        """(measured temperature in K, MultiVu's temperature status)."""

    def read_temperature_setpoint(self) -> tuple[float, float, str]:
        """(setpoint K, rate K/min, approach name) as MultiVu holds them now."""

    def set_temperature(self, temperature_K: float, rate_K_per_min: float,
                        approach: str) -> None:
        """Start driving to `temperature_K`. Returns at once."""

    # ---- chamber ---------------------------------------------------------------
    def read_chamber(self) -> str:
        """MultiVu's chamber status, e.g. 'Purged and Sealed'."""
