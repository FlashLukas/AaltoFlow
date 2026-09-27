"""The hardware interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object with these
methods counts as a SpectrumBackend. There are two: the simulator (`sim.py`)
and the real Signal Hound (`sa_api.py`); nothing above this file knows which
one it has, except through the `simulated` flag.

Why `configure` is separate from sweeping: a Signal Hound analyser is set up
once (centre, span, RBW, ... then "initiate") and then hands out sweeps. Only
AFTER that set-up does it say which frequency bins it will return -- the grid
is the analyser's choice, not ours. So the brain calls `configure` whenever a
setting changed and keeps the Grid it returns.

The sweep is split in two: `start_sweep` returns at once,
the brain waits `sweep_time_s` WITHOUT holding the hardware lock (so a setter
is never stuck behind a slow narrow-RBW sweep), then `finish_sweep` collects
the trace. `abort_sweep` throws a started sweep away.

Averaging is the brain's: the backend returns single sweeps and the brain
averages them in linear power, so "N averages" means the same on both.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from ..instruments import Grid, SweepSettings


@runtime_checkable
class SpectrumBackend(Protocol):
    """A swept spectrum analyser with an optional tracking generator."""

    simulated: bool

    def open(self) -> None:
        """Connect (and attach the tracking generator if there is one). Must
        leave the TG output off."""

    def close(self) -> None:
        """Stop sweeping, TG off, disconnect. Safe to call twice and on a crash."""

    def idn(self) -> str:
        """'Signal Hound SA44B S/N ...', or '' if unknown."""

    def device_model(self) -> str:
        """'SA44B' or 'SA124B' -- decides the frequency and RBW range."""

    def tg_attached(self) -> bool:
        """Is a tracking generator paired with this analyser?"""

    def configure(self, settings: SweepSettings) -> Grid:
        """Apply every setting, start the requested mode (spectrum or TG
        sweep) and return the bin grid the analyser will use."""

    def sweep_time_s(self, settings: SweepSettings, points: int) -> float:
        """How long one sweep takes. Must NOT talk to the instrument."""

    def start_sweep(self) -> None:
        """Start ONE sweep with the configured settings. Returns at once."""

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        """The trace (dBm per bin) of the sweep just started, and a dict of
        anything else known about it (e.g. {"overload": bool})."""

    def abort_sweep(self) -> None:
        """Forget a started sweep without reading it. Safe with none pending."""
