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
    """


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
