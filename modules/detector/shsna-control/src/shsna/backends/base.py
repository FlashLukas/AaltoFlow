"""The measurement interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object with these
methods counts as an SnaBackend. There are two: the simulator (`sim.py`) and
the client of the signalhound service (`remote_sa.py`, `--real`); nothing
above this file knows which one it has, except through the `simulated` flag.

ONE TG ACQUISITION is split in three, because that is how the owner service
runs it: `start_sweep` asks for it and returns at once, `poll` says whether it
has finished (and RAISES SweepFailed if it failed or was aborted), `fetch`
collects the result. The brain waits in between WITHOUT holding its hardware
lock, so a setter or a new trigger is never stuck behind a slow sweep.

Averaging is the backend's: the whole acquisition -- all `averages` sweeps --
is ONE exclusive block on the analyser (the owner pauses its spectrum display
and any signal-generator CW once, not once per sweep), and both backends
average in linear power.

THE GRID is the ANALYSER's: `fetch` reports start + bin * i as measured, never
the start/stop/points that were asked for.

THE UNIT is dB RELATIVE TO THE TG OUTPUT, not dBm: that is what the TG44A
reports for a TG sweep (measured 2026-09-28), and the TG's output level is not
settable in sweep mode, so there is no level anywhere in this interface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class SweepFailed(RuntimeError):
    """The TG acquisition did not produce a trace (owner refused, aborted it,
    lost the TG, timed out). The message is the reason, shown to the user and
    latched into the failed sample."""


@runtime_checkable
class SnaBackend(Protocol):
    simulated: bool

    def open(self) -> None:
        """Connect and LOOK, nothing else (Lukas's rule, 2026-09-27: a module
        reads the instrument's state at start and changes nothing). Must not
        raise just because the owner is not running yet: `health` says so."""

    def close(self) -> None:
        """Disconnect. Safe to call more than once and on a crash."""

    def idn(self) -> str:
        """What is measuring, in words."""

    def health(self) -> str:
        """"" when a TG sweep can be trusted now, else why not (owner silent,
        no TG attached, the owner reports a hardware error). From cached state
        only -- no request -- because the brain asks every 0.1 s."""

    def owner_status(self) -> dict:
        """What the brain shows about the owner: address, reachable,
        tg_attached, tg_mode, hw_error. Cached state only."""

    def estimate_time_s(self, points: int, averages: int) -> float:
        """A rough duration of one acquisition, for the progress bar. No I/O."""

    def predicted_grid(self, start_Hz: float, stop_Hz: float, points: int):
        """(start_Hz, bin_Hz, points) the analyser WILL use for this band, or
        None when that is not known before a sweep has been made."""

    def start_sweep(self, start_Hz: float, stop_Hz: float, points: int,
                    rbw_Hz: float, averages: int) -> None:
        """Ask for one TG acquisition; returns at once. Raises SweepFailed
        (refused) or ConnectionError (owner unreachable)."""

    def poll(self) -> bool:
        """True once the acquisition has finished; raises SweepFailed if it
        failed or was aborted."""

    def fetch(self) -> dict:
        """The finished acquisition: start_Hz, bin_Hz, points, rbw_Hz,
        averages, db (numpy array, dB relative to the TG output), overload."""

    def abort(self) -> None:
        """Abandon the acquisition this backend started, if it is still
        running. Never aborts somebody else's. Safe with none pending."""
