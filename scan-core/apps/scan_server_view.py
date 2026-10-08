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
    scan_info = QtCore.Signal(object)       # get_scan: the submitted definitions
    view = QtCore.Signal(object)            # get_view: the server PC's plot choice
    design = QtCore.Signal(object)          # the server PC's Navigator design, as a local copy

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
        self._scan_rev = -1                  # what get_scan / get_view were last
        self._view_rev = -1                  # fetched at (-1: fetch on first contact)
        self._design_rev = 0                 # 0 = none shared yet
        self.design_meta: dict | None = None  # the last design fetched (+ "path" here)
        self.scan: dict = {}                 # the last get_scan reply
        self.last_view: dict = {}            # the last get_view reply
        self._stop = threading.Event()
        self._poll = ScanServerClient(host, self.cmd_port, self.pub_port, timeout_ms=4000,
                                      kind="gui", name="measurement suite (watching)")
        self.client = ScanServerClient(host, self.cmd_port, self.pub_port, timeout_ms=4000,
                                       kind="gui", name="measurement suite")
        self._started = False
        # the plot choice to publish (set_view), sent from the poll thread so
        # dragging a slider never waits on the network; newest one wins
        self._view_out: dict | None = None
        self._design_out: tuple | None = None   # (path, kind, cell, width_um) to upload
        self._view_lock = threading.Lock()
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
                self._send_view()
                self._fetch_log(st)
                self._fetch_scan_and_view(st)
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

    def _fetch_scan_and_view(self, st: dict):
        """Definitions and view are fetched only when their revision moved:
        a definition is a few kB, not something to send twice a second."""
        rev = st.get("scan_rev")
        if rev is not None and int(rev) != self._scan_rev:
            r = self._poll.get_scan()
            self._scan_rev = int(r.get("scan_rev", rev))
            self.scan = r
            self.scan_info.emit(r)
        rev = st.get("design_rev")
        if rev and int(rev) != self._design_rev:
            self._fetch_design(int(rev))
        rev = st.get("view_rev")
        if rev is not None and int(rev) != self._view_rev:
            r = self._poll.get_view()
            self._view_rev = int(r.get("view_rev", rev))
            self.last_view = r
            self.view.emit(r)

    def _fetch_design(self, rev: int):
        """The server PC's design file, written to a local cache folder (the
        Navigator opens files), then announced with its kind / cell / width."""
        import base64
        import hashlib
        import tempfile
        from pathlib import Path
        r = self._poll.get_design()
        self._design_rev = int(r.get("design_rev") or rev)
        d = r.get("design")
        if not d or not d.get("data"):
            return
        raw = base64.b64decode(d["data"])
        folder = Path(tempfile.gettempdir()) / "aaltoflow-watched-designs"
        folder.mkdir(parents=True, exist_ok=True)
        name = Path(str(d.get("name") or "design")).name           # a name, never a path
        path = folder / f"{hashlib.sha1(raw).hexdigest()[:12]}_{name}"
        if not path.exists():
            path.write_bytes(raw)
        meta = {k: d.get(k) for k in ("name", "kind", "cell", "width_um")}
        meta["path"] = str(path)
        self.design_meta = meta
        self.design.emit(meta)

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

    def pause(self):
        return self._do(self.client.pause)

    def resume(self):
        return self._do(self.client.resume)

    def answer(self, value):
        return self._do(self.client.answer_pause, value)

    def clear_fault(self, module: str):
        return self._do(self.client.clear_fault, module)

    def submit(self, recipe, attrs=None):
        return self._do(self.client.submit, recipe, attrs=attrs)

    def submit_queue(self, entries, attrs=None):
        return self._do(self.client.submit_queue, entries, attrs=attrs)

    def set_view(self, view: dict) -> None:
        """Publish this suite's plot choice (sent at the next poll; only the
        suite on the server's own PC is allowed to)."""
        with self._view_lock:
            self._view_out = dict(view)

    def set_design(self, path, kind: str = "gds", cell: str = "",
                   width_um: float = 0.0) -> None:
        """Share the Navigator's design file (uploaded at the next poll; only
        the suite on the server's own PC is allowed to)."""
        with self._view_lock:
            self._design_out = (str(path), kind, cell, float(width_um))

    def _send_view(self):
        with self._view_lock:
            up, self._design_out = self._design_out, None
        if up is not None:
            try:
                self._poll.set_design(*up)
            except Exception:
                pass                      # sharing a drawing is never an error
        with self._view_lock:
            v, self._view_out = self._view_out, None
        if v is not None:
            try:
                self._poll.set_view(v)            # the poll connection: never delays an Abort
            except Exception:
                pass                      # a view is a convenience, never an error

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


# ───────────────── a submitted definition, as lines to read ──────────────────

#: run info attributes shown under a watched scan, in this order
RUN_INFO_KEYS = ("sample", "structure", "operator", "project", "series", "tags", "comment")


def _num(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def _axis_text(ax: dict) -> str:
    """One axis of a recipe dict, the way the Scan tab would say it."""
    t = ax.get("type", "linear")
    if t == "raster":
        x, y = ax.get("x") or {}, ax.get("y") or {}
        return (f"raster  {x.get('param', '?')} {_num(x.get('start'))} -> "
                f"{_num(x.get('stop'))} ({x.get('num', '?')})  x  {y.get('param', '?')} "
                f"{_num(y.get('start'))} -> {_num(y.get('stop'))} ({y.get('num', '?')})")
    if t == "zip":
        parts = [f"{m.get('param', '?')} {_num(m.get('start'))} -> {_num(m.get('stop'))}"
                 for m in ax.get("params") or ax.get("members") or []]
        return f"together ({ax.get('num', '?')} pts):  " + ",  ".join(parts)
    if t == "repeat":
        how = "averaged" if ax.get("mode") == "average" else "every run kept"
        every = f", every {_num(ax['interval_s'])} s" if ax.get("interval_s") else ""
        return f"repeat x{ax.get('num', '?')} ({how}{every})"
    if t == "array":
        return f"{ax.get('param', '?')}: {len(ax.get('values') or [])} listed values"
    if t == "file":
        return f"{ax.get('param', '?')}: values from {ax.get('path') or ax.get('file', '?')}"
    text = (f"{ax.get('param', '?')}  {_num(ax.get('start'))} -> {_num(ax.get('stop'))}, "
            f"{ax.get('num', '?')} pts")
    if t == "fly":
        text += f"  (fly at {_num(ax.get('speed'))}/s" + (
            f", moving {ax['move']}" if ax.get("move") else "") + ")"
    return text


def _routine_text(hook: dict) -> str:
    """One hook: when, and what it does."""
    when = hook.get("when", "?")
    if when == "each_sweep":
        when = f"{hook.get('edge', 'start')} of each {hook.get('axis', '?')} sweep"
        if int(hook.get("every") or 1) > 1:
            when += f" (every {hook['every']})"
    elif when == "every_n_points":
        when = f"every {hook.get('n', '?')} points"
    else:
        when = when.replace("_", " ")
    action = hook.get("action", "?")
    if action != "call":
        return f"{when}: {action}"
    from scan_core.hooks import routine_steps
    try:
        steps = routine_steps(hook.get("args") or {})
    except Exception:
        steps = []
    parts = []
    for s in steps:                      # ("set", id, v) / ("action", id) / (kind, spec)
        if s[0] == "set":
            parts.append(f"{s[1]} = {_num(s[2])}")
        elif s[0] == "action":
            parts.append(f"run {s[1]}")
        else:
            parts.append(str(s[0]).replace("_", " "))
    return f"{when}: " + ("; ".join(parts) if parts else "(routine)")


def definition_lines(recipe: dict, attrs: dict | None = None) -> list[tuple[str, str]]:
    """(section, text) lines describing a submitted scan: run info, axes
    (outer first), conditions, routines, detectors. Plain data -> testable."""
    out: list[tuple[str, str]] = []
    attrs = attrs or {}
    for k in RUN_INFO_KEYS:
        if attrs.get(k):
            out.append(("run info", f"{k}: {attrs[k]}"))
    for i, ax in enumerate(recipe.get("axes") or []):
        out.append(("axes", f"{i + 1}. " + _axis_text(ax)))
    for k, v in (recipe.get("fixed") or {}).items():
        out.append(("conditions", f"{k} = {_num(v)}"))
    for h in recipe.get("hooks") or []:
        out.append(("routines", _routine_text(h)))
    dets = recipe.get("detectors") or []
    if dets:
        out.append(("detectors", ", ".join(dets)))
    if recipe.get("zigzag"):
        out.append(("axes", "zig-zag"))
    if recipe.get("diagonal"):
        out.append(("axes", "diagonal row change"))
    if recipe.get("mask"):
        m = recipe["mask"]
        out.append(("axes", f"XY mask from {m['from']}" if m.get("from")
                    else f"XY mask: {m.get('detector')} every {m.get('step', 3)}. point"))
    return out
