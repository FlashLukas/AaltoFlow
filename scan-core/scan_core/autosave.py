"""autosave.py -- WHERE a scan's data file goes, and writing it safely.

One place for the rules, so the measurement suite (apps/scan_builder.py) and a
script (scan_core/api.py) save a scan in exactly the same way:

    <data dir>/<YYYY-MM-DD>/<HHMMSS>_<name>.nc

* dated folders, because a day's scans belong together;
* the start time in the name, because a scan is normally repeated with one
  thing changed -- overwriting the previous one is how an afternoon's work
  disappears;
* a counter (`_2`, `_3` ...) when two scans start within one second, never an
  overwrite.

No Qt in here: a script must be able to save without a screen. Moved out of
apps/scan_builder.py on 2026-10-04 (the scripting API); the builder now calls
these functions.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from suite_common.fileio import replace_retry


def safe_name(name: str | None) -> str:
    """A scan name made safe for a file name ("map 5 K" -> "map_5_K")."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", (name or "").strip() or "scan")


def default_data_dir(root=None) -> Path:
    """Where data goes when nobody said: the suite's setting, else scan-core/out.

    The same order the measurement suite uses: the folder chosen on this PC's
    Settings tab (suite_local.json `settings.data_dir`, written by the suite),
    otherwise the project's own out/ folder.
    """
    try:
        from suite_common import get_setting
        chosen = get_setting("data_dir", root=root)
    except Exception:              # no suite-common, or an unreadable settings file
        chosen = None
    if chosen:
        return Path(chosen)
    return Path(__file__).resolve().parent.parent / "out"


def autosave_path(data_dir, name: str | None, now: datetime | None = None) -> Path:
    """One file per run: <data dir>/<date>/<time>_<name>.nc (never an existing one)."""
    now = now or datetime.now()
    safe = safe_name(name)
    path = (Path(data_dir) / now.strftime("%Y-%m-%d")
            / f"{now.strftime('%H%M%S')}_{safe}.nc")
    # Two scans of a queue (or a script loop) can start within one second: a
    # counter, never an overwrite.
    k = 2
    while path.exists():
        path = path.with_name(f"{now.strftime('%H%M%S')}_{safe}_{k}.nc")
        k += 1
    return path


def probe_save_target(data_dir) -> tuple[bool, str]:
    """Can a scan be written under `data_dir` today? Returns (ok, message).

    It TRIES: writes a probe file in the dated folder if it exists, otherwise
    in the nearest parent that does -- being allowed to write there is what
    "we can create the dated folder" means. A folder can exist and be
    read-only, a network share can be gone; both only show up at save time,
    which on a long scan is an hour after you walked away. Deliberately no
    mkdir: it must not leave an empty dated folder behind on a day when
    nothing was measured. `message` names the folder probed (ok) or the
    problem (not ok).
    """
    folder = Path(data_dir) / datetime.now().strftime("%Y-%m-%d")
    target = folder
    while not target.exists() and target.parent != target:
        target = target.parent
    probe = target / f".write_test_{os.getpid()}"
    try:
        probe.write_bytes(b"aaltoflow")
        probe.unlink()
    except OSError as exc:
        return False, f"CANNOT SAVE in {target}: {exc.strerror or exc}"
    return True, str(folder)


def write_dataset(ds, path) -> Path:
    """Write `ds` to `path` ATOMICALLY: a temporary file beside it, then a rename.

    A netCDF written in place is unreadable while it is being written, and a
    crash mid-write would take the finished points with it. Writing beside it
    and renaming means the file on disk is always a complete scan (the last
    checkpoint, or the end).
    """
    path = Path(path)
    from .framestore import frames_of, write_with_frames
    if frames_of(ds):
        # A big camera map whose frames were written into the file as they
        # arrived (framestore.py): the small variables go into that SAME file
        # in place -- rewriting gigabytes of frames at every checkpoint is
        # exactly what writing them as they come avoids.
        return write_with_frames(ds, path)
    tmp = path.with_suffix(".writing.nc")
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(tmp)
    # retried: Windows can refuse the rename for a moment (fileio.py)
    replace_retry(tmp, path)
    return path
