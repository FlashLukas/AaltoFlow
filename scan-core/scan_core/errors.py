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


class ScanStopped(ScanAborted):
    """A ROUTINE STEP stopped the scan, with a reason (2026-10-04).

    `abort_if` whose condition came true, a `wait_until` that timed out with
    on_timeout: stop, or the operator choosing "Abort scan" at a `pause`. It is
    an Abort in every way that matters -- the after-scan routine runs, the
    points measured so far are kept and saved -- so it IS a ScanAborted, and
    every caller that already handles an Abort handles this one too. The
    difference is `reason`, which the engine writes into the data file
    (attribute `stopped_by`) so that a map that ends half way says WHY.
    """

    def __init__(self, reason: str, whole_queue: bool = False):
        super().__init__(reason)
        self.reason = reason
        #: "Abort all" (Lukas, 2026-10-05): stop this scan AND the queue it
        #: runs in -- a safety stop must not let the next scan start. False =
        #: "Abort scan": only this scan, a queue goes on with the next one.
        self.whole_queue = bool(whole_queue)


class SkipPoint(Exception):
    """Internal: a `skip_if` step asked the engine to leave this point out.

    Raised from inside a routine at before_point / after_point and caught by
    the engine's point loop, which then stores the point as NOT MEASURED (NaN,
    or the storage's fill value) and goes on to the next one. Never reaches a
    caller of engine.run.
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


# ------------------------------ faults ---------------------------------------
#
# Lukas's decisions of 2026-09-28: (A) a failed hardware read must be LOUD, and
# a dead service must not be served from its last status frame forever; (B)
# when the camera loses its pattern, the measurement must stop or pause for the
# operator. Both come down to one question the engine asks at every point:
# "is every instrument this scan uses in a state whose readings I can trust?"
# The answer is a list of Fault(name, message) -- empty when all is well.

from typing import NamedTuple  # noqa: E402  (kept next to what uses it)


class Fault(NamedTuple):
    """One reason not to trust a reading: which instrument, and why.

    A plain (name, message) pair, so `for name, msg in faults` works. `name`
    is the instrument's connection name (its parameter prefix), which is also
    what `Lab.clear_fault(name)` takes.
    """
    name: str
    message: str


class ScanFault(RuntimeError):
    """The scan met a fault and nobody is there to fix it (no pause handler).

    Raised by the engine in a headless run (a script, a demo) where there is
    no operator to PAUSE for. Carries the faults and, like ScanAborted, the
    points measured before it (`dataset`; unmeasured ones NaN), so a caller
    can still save them. The faulted point itself is NOT in there: its
    reading was taken while an instrument said it could not be trusted.
    """

    def __init__(self, message: str, faults=(), dataset=None):
        super().__init__(message)
        self.faults = list(faults)
        self.dataset = dataset


def format_faults(faults) -> str:
    """'camera: pattern lost; pm16: hardware read failed (...)' -- one line."""
    return "; ".join(f"{f[0]}: {f[1]}" for f in faults)
