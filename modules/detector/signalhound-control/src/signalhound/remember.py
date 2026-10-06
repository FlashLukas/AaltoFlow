"""The service REMEMBERS the operator's sweep window across a restart.

WHY (lab PC, 2026-10-06): a restart of the service reset the analyser window
to the config defaults (0.9 - 1.1 GHz, RBW 100 kHz) and the operator's window
(480 MHz - 12.4 GHz, RBW 6 MHz, VBW 100 kHz) had to be typed in again. Other
modules ADOPT such state from the instrument at start (docs/DEVELOPER_NOTES.md,
the adopt-on-start rule), but an SA44B / SA124B keeps NO settings of its own:
the API holds them in the host process and forgets them on close, and there is
no call to read them back. So the service itself must remember.

WHAT is remembered: the spectrum-mode sweep settings -- centre, span,
reference level, RBW, VBW, image rejection, detector, averages (the `[sweep]`
group of the config). NOT `continuous` (sweeping at start would CONFIGURE the
analyser, and start-up writes nothing -- `acquisition.sweep_on_start` is the
deliberate switch for that) and NOTHING of the tracking generator (that state
belongs to the client modules shsg / shsna).

WHERE: a small INI file of its own, `signalhound_sweep.ini` (a simulator uses
`signalhound_sweep_sim.ini`, so a simulated session on the lab PC cannot
overwrite the real analyser's window), next to signalhound.ini in the project
folder. Its own file, not signalhound.ini, because it is rewritten while the
service runs and configparser would strip the comments of a hand-edited
signalhound.ini every time -- and a broken memory file must never take the
PC's model / serial / DLL setup down with it. It IS an .ini on purpose: the
settings export, the installer's "keep lab files" rule and the lab backup all
carry `<module>/*.ini`, so the remembered window travels with them.

WHEN it is written: after every change (`note`), but at most once per
`min_interval_s` (a GUI spin box can send a value per keystroke), with the last
value always written within that interval, and once more on shutdown
(`flush`). Atomic: a temporary file beside it, then a rename (retried while
Windows briefly locks the file -- suite_common.fileio.replace_retry, copied
here because this module does not depend on suite-common).

Loading it only FILLS cfg.sweep: nothing is sent to the analyser. The values
become what the first deliberate sweep (a setter, continuous on, an acquire)
uses. A missing or unreadable file leaves the config defaults, with one log line.
"""

from __future__ import annotations

import configparser
import math
import os
import tempfile
import threading
import time
from pathlib import Path

from .config import Sweep, _cast

#: The remembered fields -- listed, not "every field of Sweep", so a setting
#: added to [sweep] later is remembered only when someone decides it should be.
REMEMBERED = ("center_Hz", "span_Hz", "ref_level_dBm", "rbw_Hz", "vbw_Hz",
              "reject", "detector", "averages")

SECTION = "sweep"
REAL_FILE = "signalhound_sweep.ini"
SIM_FILE = "signalhound_sweep_sim.ini"

_TYPES = {"center_Hz": "float", "span_Hz": "float", "ref_level_dBm": "float",
          "rbw_Hz": "float", "vbw_Hz": "float", "reject": "bool", "detector": "str",
          "averages": "int"}


def memory_path(folder, simulated: bool) -> Path:
    """The memory file for a service run from `folder` (the project folder)."""
    return Path(folder) / (SIM_FILE if simulated else REAL_FILE)


def _replace_retry(src, dst, retry_s: float = 2.0) -> None:
    """os.replace, retried while Windows reports the target as locked (the
    virus scanner, the indexer). MASTER: suite_common.fileio.replace_retry."""
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


class SweepMemory:
    """Remembers cfg.sweep in one file. Thread-safe: `note` runs in the
    command thread, the delayed write in a timer thread, `flush` at shutdown."""

    def __init__(self, path, min_interval_s: float = 1.0, clock=time.monotonic):
        self.path = Path(path)
        self.min_interval_s = float(min_interval_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict | None = None   # values not written yet
        self._written: dict | None = None   # what the file holds (as far as we know)
        self._last_write = float("-inf")
        self._timer: threading.Timer | None = None
        self.writes = 0                     # files written (tests count them)
        # where a failed write is reported; the brain sets its _emit here
        self.on_error = lambda msg: None

    # ---- reading (at start) ------------------------------------------------------

    def load_into(self, sweep: Sweep) -> tuple[str, str]:
        """Fill `sweep` from the file. Returns (level, message) for ONE log line.

        All or nothing: a file with one unreadable value is ignored as a whole
        -- half an operator's window mixed with defaults is worse than either.
        The values are not clamped here; the brain sanitises cfg right after
        (the connected MODEL decides the envelope)."""
        if not self.path.is_file():
            return "info", (f"no remembered sweep settings ({self.path.name}); "
                            "using the config defaults")
        try:
            cp = configparser.ConfigParser()
            cp.read(self.path, encoding="utf-8")
            if SECTION not in cp:
                raise ValueError(f"no [{SECTION}] section")
            sec = cp[SECTION]
            values = {}
            for name in REMEMBERED:
                if name not in sec:
                    continue                 # an older file: keep that default
                v = _cast(sec[name], _TYPES[name])
                if isinstance(v, float) and not math.isfinite(v):
                    raise ValueError(f"{name} = {sec[name]!r} is not a finite number")
                values[name] = v
            if not values:
                raise ValueError("no remembered value in it")
        except Exception as exc:
            return "warn", (f"remembered sweep settings in {self.path.name} unreadable "
                            f"({type(exc).__name__}: {exc}); using the config defaults")
        for name, v in values.items():
            setattr(sweep, name, v)
        with self._lock:
            self._written = self._snapshot(sweep)
        return "info", (f"remembered sweep settings loaded from {self.path.name} "
                        "(nothing sent to the analyser)")

    # ---- writing (after a change, throttled) ---------------------------------------

    @staticmethod
    def _snapshot(sweep: Sweep) -> dict:
        return {name: getattr(sweep, name) for name in REMEMBERED}

    def note(self, sweep: Sweep) -> None:
        """The settings changed: write them now, or within min_interval_s."""
        snap = self._snapshot(sweep)
        with self._lock:
            if snap == self._written and self._pending is None:
                return                       # nothing new (e.g. "settings applied")
            self._pending = snap
            wait = self._last_write + self.min_interval_s - self._clock()
            if wait > 0:
                # too soon after the last write: one timer writes the LATEST
                # values when the interval is up (later notes just update them)
                if self._timer is None:
                    self._timer = threading.Timer(wait, self._timer_fired)
                    self._timer.daemon = True
                    self._timer.start()
                return
        self._write_pending()

    def flush(self) -> None:
        """Write what is still pending at once (shutdown). Safe to call twice."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._write_pending()

    def _timer_fired(self) -> None:
        with self._lock:
            self._timer = None
        self._write_pending()

    def _write_pending(self) -> None:
        with self._lock:
            snap = self._pending
            if snap is None:
                return
            self._pending = None
            try:
                self._write(snap)
            except Exception as exc:
                # keep running: a lost memory is an inconvenience, not a fault.
                # The values stay pending so the next change or shutdown retries.
                self._pending = snap
                msg = f"could not remember the sweep settings in {self.path}: {exc}"
            else:
                self._written = snap
                self._last_write = self._clock()
                self.writes += 1
                return
        self.on_error(msg)

    def _write(self, snap: dict) -> None:
        cp = configparser.ConfigParser()
        cp[SECTION] = {k: repr(v) if isinstance(v, float) else str(v) for k, v in snap.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp",
                                   dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("# signalhound-control: the last sweep window, written by the "
                         "service.\n# Delete this file to start from the config defaults.\n")
                cp.write(fh)
            _replace_retry(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
