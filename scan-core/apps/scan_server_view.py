"""scan_server_view.py -- the measurement suite WATCHING a scan server.

The scan server (scan_core/scan_server.py) runs scans in its own process on
the lab PC. This file is the GUI half of watching one: a QObject that keeps
a connection, polls the server off the GUI thread, and hands the GUI what it
needs as Qt signals -- the status (progress, ETA, where, faults, the operator
question), new log lines, and the live dataset whenever the server has a newer
one. The Measurement tab then shows the server's scan with the SAME widgets it
shows its own scans with (ScanBuilder.attach_server).

Two connections, on purpose: a POLL client (status, log, live data -- a big
live map can take a moment to transfer) and a COMMAND client (Abort, Stop
queue, answers, take control). An Abort must never wait behind a transfer.

Nothing here owns the scan: closing the window closes the connections, and the
scan goes on (that is the point of the server).
"""

from __future__ import annotations

import threading
import time

from PySide6 import QtCore

from scan_core.scan_server import DEFAULT_CMD_PORT
from scan_core.scan_server_client import ScanServerClient, ScanServerError
from suite_common.control import ControlRefused, describe_holder, same_pc

#: seconds between two polls of the server
POLL_S = 0.5
_LOCAL = {"localhost", "127.0.0.1", "::1", ""}


def parse_target(text: str) -> tuple[str, int, int | None]:
    """'lab-pc', 'lab-pc:5551' or 'lab-pc:5551:5552' -> (host, cmd, pub|None)."""
    parts = [p.strip() for p in str(text or "").strip().split(":")]
    host = parts[0] or "localhost"
    cmd = int(parts[1]) if len(parts) > 1 and parts[1] else DEFAULT_CMD_PORT
    pub = int(parts[2]) if len(parts) > 2 and parts[2] else None
    return host, cmd, pub


class ServerWatch(QtCore.QObject):
    """One watched scan server. Signals arrive on the GUI thread."""

    status = QtCore.Signal(object)          # the status dict
    log_lines = QtCore.Signal(object)       # [str, ...] new lines, in order
    live = QtCore.Signal(object)            # an xarray.Dataset
    connection = QtCore.Signal(bool, str)   # answering?, why not

    def __init__(self, host: str = "localhost", cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int | None = None, parent=None, poll_s: float = POLL_S):
        super().__init__(parent)
        self.host, self.cmd_port = host, int(cmd_port)
        self.pub_port = int(pub_port) if pub_port else self.cmd_port + 1
        self.poll_s = float(poll_s)
        self.last: dict = {}
        self.answering = False
        self.why = "connecting ..."
        self._log_next = None                # None = only the tail on the first poll
        self._stop = threading.Event()
        self._poll = ScanServerClient(host, self.cmd_port, self.pub_port, timeout_ms=4000,
                                      kind="gui", name="measurement suite (watching)")
        self.client = ScanServerClient(host, self.cmd_port, self.pub_port, timeout_ms=4000,
                                       kind="gui", name="measurement suite")
        self._started = False
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="scanserver-watch")
        self._thread.start()

    # ------------------------------------------------------------------ #
    @property
    def label(self) -> str:
        return f"{self.host}:{self.cmd_port}"

    def is_local(self) -> bool:
        """True when the server runs on THIS PC (so this suite may submit)."""
        if self.host.lower() in _LOCAL:
            return True
        pc = str(self.last.get("pc") or "")
        return bool(pc) and same_pc({"host": f"x@{pc}"}, self.client.identity)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        for c in (self._poll, self.client):
            try:
                c.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    def _loop(self):
        while not self._stop.is_set():
            try:
                if not self._started:
                    st = self._poll.start()
                    self.client.start()               # identity + heartbeat
                    self._started = True
                else:
                    st = self._poll.status()
                if not self.answering:
                    self.answering, self.why = True, ""
                    self.connection.emit(True, "")
                self.last = st
                self.status.emit(st)
                self._fetch_log(st)
                if int(st.get("live_rev") or 0) > self._poll.live_rev \
                        or self._poll.live_notice > self._poll.live_rev:
                    ds = self._poll.get_live()
                    if ds is not None:
                        self.live.emit(ds)
            except Exception as exc:          # not answering: say so, keep trying
                if self.answering or self.why != str(exc):
                    self.answering, self.why = False, str(exc)
                    self.connection.emit(False, str(exc))
                self._started = False
                for c in (self._poll, self.client):
                    try:
                        c.close()
                    except Exception:
                        pass
            self._stop.wait(self.poll_s)

    def _fetch_log(self, st: dict):
        n = int(st.get("log_n") or 0)
        if self._log_next is None:
            # first contact: the last lines, not a whole night's log
            self._log_next = max(0, n - 30)
        if n <= self._log_next:
            return
        r = self._poll.get_log(self._log_next)
        lines = r.get("lines") or []
        self._log_next = int(r.get("next", n))
        if lines:
            self.log_lines.emit(list(lines))

    # ---- commands (GUI thread; short, own connection) -----------------
    def _do(self, fn, *a, **k) -> tuple[bool, str]:
        try:
            fn(*a, **k)
            return True, ""
        except ControlRefused as exc:
            return False, str(exc)
        except (ScanServerError, TimeoutError) as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def abort(self):
        return self._do(self.client.abort)

    def stop_queue(self):
        return self._do(self.client.stop_queue)

    def answer(self, value):
        return self._do(self.client.answer_pause, value)

    def clear_fault(self, module: str):
        return self._do(self.client.clear_fault, module)

    def submit(self, recipe, attrs=None):
        return self._do(self.client.submit, recipe, attrs=attrs)

    def submit_queue(self, entries, attrs=None):
        return self._do(self.client.submit_queue, entries, attrs=attrs)

    def take_control(self, force: bool = False):
        try:
            return (self.client.take_control(force=force), "")
        except Exception as exc:
            return False, str(exc)

    def release_control(self):
        return self._do(self.client.release_control)

    # ---- control, from the last status ----------------------------------
    def control_text(self) -> tuple[str, str]:
        """(state, text): state 'you' / 'other' / 'free'."""
        c = (self.last or {}).get("control") or {}
        h = c.get("holder")
        if not h:
            return "free", "control: nobody (anyone may act)"
        if h.get("id") == self.client.identity["id"] or same_pc(h, self.client.identity):
            return "you", "control: this PC"
        since = time.strftime("%H:%M", time.localtime(h.get("since", 0) or 0))
        return "other", f"control: {describe_holder(h)} since {since} -- you are watching"


class ServerFaults:
    """What the PAUSED banner asks of a 'fault lab' (ScanBuilder._on_paused):
    can this module's fault be cleared, and clear it -- answered from the
    server's status and sent to the server."""

    def __init__(self, watch: ServerWatch):
        self.watch = watch

    def can_clear_fault(self, name: str) -> bool:
        for f in (self.watch.last or {}).get("faults") or []:
            if f.get("module") == name:
                return bool(f.get("can_clear"))
        return False

    def clear_fault(self, name: str):
        ok, why = self.watch.clear_fault(name)
        if not ok:
            raise RuntimeError(why)
        return {"ok": True}
