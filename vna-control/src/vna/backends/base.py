"""The hardware interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object with these
methods counts as a VnaBackend. There are two: the simulator (`sim.py`) and the
Keysight PNA-X (`pna.py`); nothing above this file knows which one it has,
except through the `simulated` flag (the GUI shows the Kittel model only then).

The sweep is split in two on purpose, because that is how a real VNA is driven:
`start_sweep` triggers ONE sweep and returns at once, the instrument sweeps for
`sweep_time_s`, and `finish_sweep` collects the trace. The brain waits in
between WITHOUT holding the hardware lock, so a setter or a new trigger is never
stuck behind a 20-second narrow-IFBW sweep. `abort_sweep` throws a started sweep
away (a settings change, a new trigger, shutdown).

The FIELD is not the backend's business: the brain reads it (from the magnet
service) and hands the reading to `start_sweep`. The simulator uses it for the
physics; the real analyser ignores it -- the brain files it with the trace.

Averaging is the brain's too: it asks for single sweeps and averages the
complex traces itself, so "N averages" means the same thing on both backends.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from ..field import FieldReading


@runtime_checkable
class VnaBackend(Protocol):
    """A two-port vector network analyser."""

    simulated: bool

    def open(self) -> None:
        """Connect. Must not change any setting the brain has not asked for
        (beyond what single-sweep operation needs)."""

    def close(self) -> None:
        """Disconnect. Safe to call more than once and on a crash."""

    def idn(self) -> str:
        """'Vendor Model S/N fw', or '' if unknown."""

    def sweep_time_s(self, points: int, ifbw_Hz: float) -> float:
        """How long one sweep with these settings takes. Must NOT talk to the
        instrument: status() calls it ten times a second."""

    def start_sweep(self, freqs_Hz: np.ndarray, ifbw_Hz: float, power_dBm: float,
                    sparam: str, field: FieldReading) -> None:
        """Apply the settings (if changed) and start ONE sweep. Returns at once."""

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        """The complex trace of the sweep just started, and a dict of anything
        the backend knows about it (the simulator: the model's resonance)."""

    def abort_sweep(self) -> None:
        """Forget a started sweep without reading it. Safe with none pending."""
