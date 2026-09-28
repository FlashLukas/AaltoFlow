"""errors.py -- the exceptions a scan can end with, in a file that imports nothing.

Why a separate file: `ScanAborted` used to live in `instrument.py`, which imports
pyzmq. The engine now has to CATCH it (so the after-scan routine still runs when
the operator presses Abort), and the engine must stay importable -- and testable
-- without a network library. `instrument.py` re-exports it, so
`from scan_core.instrument import ScanAborted` keeps working.
"""

from __future__ import annotations


class ScanAborted(RuntimeError):
    """The operator pressed Abort while a settle wait was in progress.

    Its own class, not a TimeoutError: nothing went wrong with the instrument,
    so the run should end quietly rather than be reported as a failure.

    `dataset` (set by the engine, deep cleaning 2026-09-28): the points
    measured BEFORE the Abort, unmeasured ones NaN -- or None when the scan
    was aborted before its first point. A real scan spends most of its time in
    settle waits, so this is where an Abort usually lands; without it every
    point measured so far was thrown away, while an Abort that happened to land
    between two points kept them.
    """

    dataset = None


class RoutineError(RuntimeError):
    """A routine (a `call` hook) failed AFTER the points were measured.

    Carries the finished `dataset`, because the after-scan routine runs last:
    if "field -> 0" times out at the end of a two-hour map, the map itself is
    fine and must not be thrown away with the exception. The caller saves
    `exc.dataset` and reports the error.
    """

    def __init__(self, message: str, dataset=None):
        super().__init__(message)
        self.dataset = dataset
