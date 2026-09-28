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
        """Connect (and attach the tracking generator if there is one) and
        READ what the analyser is: model, serial, TG present. Queries only --
        no configure, no initiate, no abort (start-up rule, 2026-09-27)."""

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

    # ---- the tracking generator as a CW source (for the shsg module) ----------
    # TG SWEEPS need no method of their own: they are a `configure` with
    # settings.tg_on True, then start_sweep / finish_sweep as usual. A TG
    # sweep returns dB relative to the TG's calibrated output, not dBm.
    # There is NO "TG off" (measured 2026-09-28): the brain PARKS the TG with
    # set_tg_cw at hardware.tg_park_hz / tg_park_dbm instead.

    def set_tg_cw(self, freq_hz: float, level_dbm: float) -> None:
        """Make the TG emit a CW tone. Does not reconfigure the analyser."""

    def idle(self) -> None:
        """Stop whatever is initiated (saAbort on the real one) -- a TG sweep
        must be stopped before saSetTg is allowed. Does NOT silence the TG.
        Leaves the analyser unconfigured: the brain reconfigures next sweep."""
