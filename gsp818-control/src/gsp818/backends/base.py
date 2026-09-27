"""The hardware interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object with these
methods counts as a SpectrumBackend. There are two: the simulator (`sim.py`)
and the real GSP-818 (`gsp.py`); nothing above this file knows which one it
has, except through the `simulated` flag.

The sweep is split in three on purpose, because that is how a real analyser
is driven:
  configure(settings)   push the settings (only what changed) and read back
                        what the instrument really uses -- an RBW typed as
                        2 kHz may come back as 3 kHz, an auto sweep time is the
                        instrument's own number.
  start_sweep(settings) -> seconds to wait. Returns at once. The brain waits
                        WITHOUT holding the hardware lock, so a setter is never
                        stuck behind a 60-second narrow-RBW sweep.
  finish_sweep()        the trace in dBm, one value per point.
`abort_sweep` throws a started sweep away (a settings change, a new trigger,
shutdown).

Averaging is the brain's: it asks for single sweeps and averages them in
linear power itself, so "N averages" means the same thing on both backends.

SAFETY: `open()` and `close()` must leave the tracking generator OFF.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from ..model import SweepSettings


@runtime_checkable
class SpectrumBackend(Protocol):
    """A swept spectrum analyser with a tracking generator."""

    simulated: bool

    def open(self) -> None:
        """Connect, switch the tracking generator OFF, set dBm as the unit."""

    def close(self) -> None:
        """Tracking generator OFF, then disconnect. Safe to call more than
        once and on a crash."""

    def idn(self) -> str:
        """'Vendor,Model,S/N,fw', or '' if unknown."""

    def configure(self, s: SweepSettings) -> dict:
        """Apply `s` (only what changed) and return what is really in use:
        {"rbw_Hz", "vbw_Hz", "atten_dB", "sweep_time_s"}."""

    def start_sweep(self, s: SweepSettings) -> float:
        """Start ONE fresh sweep with the configured settings; return how many
        seconds to wait before `finish_sweep`. Returns at once."""

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        """The trace in dBm (len = points) and a dict of anything the backend
        knows about it (the simulator: whether the mixer was overloaded)."""

    def abort_sweep(self) -> None:
        """Forget a started sweep without reading it. Safe with none pending."""
