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

START-UP CHANGES NOTHING (Lukas's rule, 2026-09-27): `open()` and
`read_state()` only query. The brain adopts what `read_state()` reports and
calls `mark_in_sync()`, so its first `configure` sends nothing. Anything that
must change for a FRESH trace (a frozen/max-hold trace, the instrument's own
averaging) is changed by `ensure_live()`, called only when an acquisition
begins. SAFETY on SHUTDOWN: `close()` switches the tracking generator OFF.
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
        """Connect and identify. Queries only: nothing on the instrument changes."""

    def read_state(self) -> dict:
        """The instrument's current settings, from queries: any of start_Hz,
        stop_Hz, points, rbw_Hz, rbw_auto, vbw_Hz, vbw_auto, ref_level_dBm,
        atten_dB, atten_auto, sweep_time_s, sweep_time_auto, detector, preamp,
        tg_on, tg_level_dBm (a key it could not read is left out), plus
        "notes": sentences for the event log."""

    def mark_in_sync(self, s: SweepSettings) -> None:
        """Treat every setting of `s` not reported by read_state as already
        applied, so starting writes nothing."""

    def ensure_live(self) -> list[str]:
        """Before an acquisition: make the next read a fresh sweep, writing
        only what is wrong. Returns one sentence per change (for warn events)."""

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
