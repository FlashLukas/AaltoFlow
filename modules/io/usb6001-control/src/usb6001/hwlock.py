"""hwlock.py -- one physical instrument, one service at a time.

WHY: an instrument is identified by its PHYSICAL ADDRESS (GPIB0::6, COM5, a
USB serial number, an IP address), not by which module drives it. clMag and
kepco can both be pointed at the same Kepco BOP; mag2d and mag2dcal at the same
DAQ card. If both services run, two programs send commands to one instrument
and neither knows what the other did. So every REAL backend claims its address
here before it opens the instrument, and a second claim of the same address --
from any module -- is refused with a message naming the holder.

HOW: the address is normalised (so "GPIB::6", "gpib0::6::INSTR" and "GPIB0::6"
are the same instrument) and an operating-system file lock is taken on a file
named after it in %LOCALAPPDATA%\\AaltoFlow\\locks. The OS releases the lock
when the process ends, even on a crash or a hard kill, so a dead service can
never leave an instrument "busy". A simulator claims nothing.

The lock is per PC. That covers GPIB, USB and serial (they hang on one PC);
a network instrument shared between PCs is not protected across PCs.

MASTER COPY: suite-common/src/suite_common/hwlock.py. Every module carries an
identical copy (src/<pkg>/hwlock.py), because modules are installed on their
own and do not depend on suite-common -- the same convention as theme.py.
tools/check_modules.py checks the copies are identical. Edit the master, then
copy it. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

__all__ = ["HardwareBusy", "HardwareLock", "claim", "normalize", "lock_dir", "held"]


class HardwareBusy(RuntimeError):
    """The address is already claimed by another running service."""


def lock_dir() -> Path:
    base = os.environ.get("AALTOFLOW_LOCK_DIR")  # tests point this at a temp folder
    if base:
        return Path(base)
    root = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".local", "state")
    return Path(root) / "AaltoFlow" / "locks"


def normalize(address: str) -> str:
    """One canonical spelling per physical instrument.

    GPIB0::6::INSTR, GPIB::6, gpib0::6        -> GPIB0::6
    GPIB0::6::2::INSTR (secondary address)   -> GPIB0::6::2
    COM5, com5, ASRL5::INSTR, \\\\.\\COM5        -> COM5
    TCPIP0::10.0.0.5::inst0::INSTR           -> TCPIP::10.0.0.5
    TCPIP0::10.0.0.5::5025::SOCKET, 10.0.0.5:5025, 10.0.0.5 -> TCPIP::10.0.0.5
    USB0::0x1313::0x8078::P001::INSTR        -> USB::0X1313::0X8078::P001
    anything else (a Kinesis serial, a device name like Dev1) -> upper-cased text

    A network instrument is keyed by its HOST only: two ports on one box are
    still one box.
    """
    a = str(address).strip()
    if a.startswith("\\\\.\\"):
        a = a[4:]
    u = a.upper()
    m = re.fullmatch(r"GPIB(\d*)::(\d+)(?:::(\d+))?(?:::INSTR)?", u)
    if m:
        board = m.group(1) or "0"
        return f"GPIB{board}::{m.group(2)}" + (f"::{m.group(3)}" if m.group(3) else "")
    m = re.fullmatch(r"ASRL(\d+)(?:::INSTR)?", u) or re.fullmatch(r"COM(\d+)", u)
    if m:
        return f"COM{int(m.group(1))}"
    m = re.fullmatch(r"TCPIP\d*::([^:]+)(?:::.*)?", u)
    if m:
        return f"TCPIP::{m.group(1)}"
    m = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3}|[A-Z0-9][A-Z0-9.-]*\.[A-Z]{2,})(?::\d+)?", u)
    if m:
        return f"TCPIP::{m.group(1)}"
    m = re.fullmatch(r"USB\d*::([^:]+)::([^:]+)::([^:]+)(?:::.*)?", u)
    if m:
        return f"USB::{m.group(1)}::{m.group(2)}::{m.group(3)}"
    return u


def _filename(norm: str) -> str:
    return re.sub(r"[^A-Z0-9.]+", "_", norm).strip("_") + ".lock"


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fh) -> bool:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(fh) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def _read_info(path: Path) -> dict:
    # The holder's details sit AFTER byte 0 (the locked byte), so they can be
    # read while the lock is held -- Windows forbids reading a locked range.
    try:
        with open(path, "rb") as fh:
            fh.seek(1)
            text = fh.read().decode("utf-8", errors="replace")
        return json.loads(text) if text.strip() else {}
    except (OSError, ValueError):
        return {}


class HardwareLock:
    """A held claim. Release with .release() (or use as a context manager)."""

    def __init__(self, address: str, norm: str, path: Path, fh):
        self.address, self.normalized, self.path, self._fh = address, norm, path, fh

    @property
    def held(self) -> bool:
        return self._fh is not None

    def release(self) -> None:
        if self._fh is None:
            return
        fh, self._fh = self._fh, None
        _unlock(fh)
        fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()

    def __del__(self):
        self.release()


def claim(address: str, module: str, wait_s: float = 2.0) -> HardwareLock:
    """Claim `address` for `module`, or raise HardwareBusy naming the holder.

    Call it in the real backend's open() BEFORE touching the instrument, keep
    the returned lock for as long as the instrument is open, release it in
    close(). Re-claiming an address this process already holds is refused too
    (it means two backends in one process -- a bug worth hearing about).

    wait_s: Windows frees a killed process's lock a moment AFTER the process
    is gone, so a service restarted right after a kill retries this long
    before giving up.
    """
    norm = normalize(address)
    d = lock_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / _filename(norm)
    fh = open(path, "a+b")  # never truncates: the holder's info must survive our failed try
    deadline = time.monotonic() + wait_s
    while not (got := _try_lock(fh)) and time.monotonic() < deadline:
        time.sleep(0.1)
    if not got:
        fh.close()
        info = _read_info(path)
        who = info.get("module", "another service")
        pid = info.get("pid")
        raise HardwareBusy(
            f"{norm} is already in use by {who}" + (f" (pid {pid})" if pid else "")
            + " -- one instrument can be driven by one service at a time; stop that one first")
    info = {"module": module, "pid": os.getpid(), "address": address,
            "normalized": norm, "since": time.strftime("%Y-%m-%d %H:%M:%S")}
    fh.seek(1)
    fh.truncate(1)
    fh.seek(0, os.SEEK_END)
    if fh.tell() == 0:
        fh.write(b" ")  # byte 0 is the one we lock
    fh.write(json.dumps(info).encode("utf-8"))
    fh.flush()
    return HardwareLock(address, norm, path, fh)


def held() -> list[dict]:
    """Every address currently claimed on this PC (for the launcher to show).

    A lock file whose lock can be taken is stale (its holder has exited): it
    is skipped, not reported.
    """
    out = []
    d = lock_dir()
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.lock")):
        try:
            fh = open(path, "a+b")
        except OSError:
            continue
        try:
            if _try_lock(fh):
                _unlock(fh)
                continue
        finally:
            fh.close()
        info = _read_info(path)
        if info:
            out.append(info)
    return out
