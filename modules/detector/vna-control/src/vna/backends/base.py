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

Two OPTIONAL attributes, read with getattr (2026-10-09, the fly-scan stream):
  uses_field_end     True = finish_sweep accepts `field_end` (the field when
                     the sweep ended); the simulator uses it to compute a sweep
                     taken under a moving field. A real analyser leaves it out.
  trigger_latency_s  seconds between start_sweep returning and the first
                     point being measured; the stream's time stamps add it.
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
        """Connect and LOOK, nothing else (Lukas's rule, 2026-09-27: every module
        reads the instrument's state at start and changes nothing). Queries,
        plus *CLS (it only empties the error queue). Everything this module
        needs to drive single sweeps -- trigger mode, its own measurement,
        data format, averaging off -- is set up by the FIRST `start_sweep`,
        i.e. only once somebody asks this module to measure."""

    def read_state(self) -> dict:
        """What the analyser is doing right now, read with queries only, for
        the brain to ADOPT at start. Keys (any may be missing when a query
        failed -- the brain then keeps its config value):
            start_Hz, stop_Hz, points, ifbw_Hz, power_dBm, sparam (S11..S22 or
            None if the active measurement is not one), continuous (True only
            if the brain may show live sweeps WITHOUT changing the instrument:
            the simulator yes, a real analyser no -- driving it means taking
            over its trigger), plus anything informative (sweep_mode,
            averaging_on, correction_on, ...) that goes to status as it is."""

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
