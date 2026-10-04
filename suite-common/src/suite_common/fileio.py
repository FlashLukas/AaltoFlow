"""A rename that survives Windows' brief file locks.

Why: every save in the suite is ATOMIC -- write a temporary file beside the
target, then swap it in with os.replace -- so a reader never sees half a
file. On Windows that final rename can fail with "Access is denied"
(WinError 5) or a sharing violation (WinError 32) for a fraction of a second
when another program touches a file that was just written: the virus
scanner, the search indexer, a cloud-sync client. Seen on 2026-10-04 twice
under load: a finished scan "could NOT be saved", and the launcher's
settings file. Waiting a moment and trying again is the standard cure; a
real problem (no permission, disk full) still fails after the retries, with
the original error.
"""

from __future__ import annotations

import os
import time

#: how long to keep retrying a rename that Windows refuses, in seconds
RETRY_S = 2.0


def replace_retry(src, dst, retry_s: float = RETRY_S) -> None:
    """os.replace(src, dst), retried for up to `retry_s` seconds while
    Windows reports the file as locked (PermissionError)."""
    deadline = time.monotonic() + retry_s
    delay = 0.02
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)
