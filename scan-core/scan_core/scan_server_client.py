"""scan_server_client.py -- talking to a scan server (scan_server.py) from a
measurement suite, a script or a test. No Qt.

    from scan_core.scan_server_client import ScanServerClient
    c = ScanServerClient("lab-pc")            # default ports 5551/5552
    c.start()
    print(c.status()["where"])                # "point 25 / 125   field 40 mT ..."
    ds = c.get_live()                         # the dataset so far, or None if unchanged
    c.abort()                                 # always allowed (a safety verb)
    c.close()

The usual shape of a suite's client (docs/DEVELOPER_NOTES.md section 4): REQ
under a lock with a timeout and a fresh socket after a timeout (a REQ socket
that missed its reply is stuck for good), a SUB thread that caches the last
status frame and collects events, the control identity in every request and a
heartbeat (suite_common.control.ControlClient), and CurveZMQ when the lab's
policy secures "scanserver" (suite_common.secure).
"""

from __future__ import annotations

import json
import threading
import time

import zmq

from suite_common import secure
from suite_common.control import ControlClient, ControlRefused

from .scan_server import (DEFAULT_CMD_PORT, SERVER_KEY, TOPIC_EVENT, TOPIC_LIVE,
                          TOPIC_STATUS, dataset_from_text)


class ScanServerError(RuntimeError):
    """The server answered ok:false. `refused` says why when it is a rule
    ("control", "phase2", "busy", "invalid", "unsaved", "security")."""

    def __init__(self, message: str, refused: str = ""):
        super().__init__(message)
        self.refused = refused


class ScanServerClient(ControlClient):
    #: a status frame older than this is not trusted; status() asks directly
    STALE_S = 3.0

    def __init__(self, host: str = "localhost", cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int | None = None, timeout_ms: int = 5000,
                 kind: str = "gui", name: str = "measurement suite"):
        self.host = host
        self.cmd_port = int(cmd_port)
        self.pub_port = int(pub_port) if pub_port else self.cmd_port + 1
        self.timeout_ms = int(timeout_ms)
        self._ctx = zmq.Context.instance()
        self._req = None
        self._req_lock = threading.Lock()
        self._sub_stop = threading.Event()
        self._sub_thread: threading.Thread | None = None
        self._status: dict | None = None
        self._status_t = 0.0
        self._events: list[dict] = []
        self._events_lock = threading.Lock()
        self.live_notice = 0                 # the last live_rev the PUB announced
        self.live_rev = -1                   # the revision of the last get_live
        self._control_setup(kind, name)

    # ------------------------------------------------------------------ #
    def _new_req(self):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.LINGER, 0)
        s.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        s.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        secure.secure_client(s, self.host, SERVER_KEY)   # no-op unless secured
        s.connect(f"tcp://{self.host}:{self.cmd_port}")
        return s

    def start(self) -> dict:
        """Connect, start listening, and return the first status (raises
        ScanServerError / TimeoutError when nobody answers)."""
        with self._req_lock:
            if self._req is None:
                self._req = self._new_req()
        self._sub_stop.clear()
        if self._sub_thread is None or not self._sub_thread.is_alive():
            self._sub_thread = threading.Thread(target=self._listen, daemon=True,
                                                name="scanserver-sub")
            self._sub_thread.start()
        st = self.command("status")["status"]
        self._remember(st)
        self.start_heartbeat()
        return st

    def close(self) -> None:
        self.stop_heartbeat()
        self._sub_stop.set()
        if self._sub_thread is not None:
            self._sub_thread.join(timeout=1.5)
        with self._req_lock:
            if self._req is not None:
                self._req.close(0)
                self._req = None

    # ------------------------------------------------------------------ #
    def _rpc(self, **req) -> dict:
        """One request -> the reply dict (ControlClient's hook). TimeoutError
        when the server does not answer within timeout_ms."""
        self._with_identity(req)
        timeout_ms = req.pop("_timeout_ms", None)
        with self._req_lock:
            if self._req is None:
                self._req = self._new_req()
            if timeout_ms:
                self._req.setsockopt(zmq.RCVTIMEO, int(timeout_ms))
            try:
                self._req.send_json(req)
                return self._req.recv_json()
            except zmq.Again:
                # a REQ socket that missed its reply is stuck: a fresh one
                self._req.close(0)
                self._req = self._new_req()
                raise TimeoutError(f"scan server {self.host}:{self.cmd_port} did not "
                                   f"answer {req.get('cmd')!r} within "
                                   f"{(timeout_ms or self.timeout_ms) / 1000:.0f} s")
            finally:
                if timeout_ms and self._req is not None:
                    self._req.setsockopt(zmq.RCVTIMEO, self.timeout_ms)

    def command(self, verb: str, _timeout_ms: int | None = None, **kwargs) -> dict:
        """Send `verb`; the reply, or ScanServerError on ok:false (a control
        refusal raises ControlRefused, as for every module)."""
        r = self._rpc(cmd=verb, _timeout_ms=_timeout_ms, **kwargs)
        if not r.get("ok"):
            self._raise_refusal(r)
            raise ScanServerError(r.get("error", f"{verb} failed"), r.get("refused", ""))
        self._control_from_status(r)
        return r

    # ------------------------------------------------------------------ #
    def _remember(self, st: dict) -> None:
        self._status, self._status_t = st, time.monotonic()
        self._control_from_status(st)
        try:
            self.live_notice = max(self.live_notice, int(st.get("live_rev") or 0))
        except (TypeError, ValueError):
            pass

    def _listen(self):
        sub = self._ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.setsockopt(zmq.RCVTIMEO, 300)
        try:
            secure.secure_client(sub, self.host, SERVER_KEY)
        except secure.SecurityError:
            sub.close(0)
            return
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        for topic in (TOPIC_STATUS, TOPIC_EVENT, TOPIC_LIVE):
            sub.setsockopt(zmq.SUBSCRIBE, topic)
        try:
            while not self._sub_stop.is_set():
                try:
                    topic, raw = sub.recv_multipart()
                except zmq.Again:
                    continue
                except Exception:
                    continue
                try:
                    data = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if topic == TOPIC_STATUS:
                    self._remember(data)
                elif topic == TOPIC_EVENT:
                    with self._events_lock:
                        self._events.append(data)
                        del self._events[:-500]
                elif topic == TOPIC_LIVE:
                    try:
                        self.live_notice = max(self.live_notice, int(data.get("live_rev", 0)))
                    except (TypeError, ValueError):
                        pass
        finally:
            sub.close(0)

    def latest(self) -> dict | None:
        """The last status frame (never blocks), or None."""
        return self._status

    def status_age(self) -> float | None:
        return None if self._status is None else time.monotonic() - self._status_t

    def status(self) -> dict:
        """The status: the cached frame while fresh, else asked directly."""
        age = self.status_age()
        if age is not None and age < self.STALE_S:
            return self._status
        st = self.command("status")["status"]
        self._remember(st)
        return st

    def events(self) -> list[dict]:
        """Events (log lines) received since the last call."""
        with self._events_lock:
            out, self._events = self._events, []
        return out

    # ---- the verbs -------------------------------------------------------
    def submit(self, recipe, name: str | None = None, attrs: dict | None = None,
               allow_unsaved: bool = False) -> dict:
        """Start ONE scan on the server (phase 1: only from the server's PC).
        `recipe` = a scan_core Recipe or its dict."""
        return self.command("submit", recipe=_recipe_dict(recipe), name=name,
                            attrs=dict(attrs or {}), allow_unsaved=allow_unsaved,
                            _timeout_ms=max(self.timeout_ms, 20000))

    def submit_queue(self, entries, attrs: dict | None = None,
                     allow_unsaved: bool = False) -> dict:
        """Start a queue: entries = [(name, recipe)] or [{"name", "recipe"}]
        or scan_queue.QueueEntry objects."""
        out = []
        for e in entries:
            if isinstance(e, dict):
                out.append({"name": e.get("name"), "recipe": _recipe_dict(e["recipe"]),
                            "attrs": e.get("attrs")})
            elif isinstance(e, (tuple, list)):
                out.append({"name": e[0], "recipe": _recipe_dict(e[1])})
            else:                                   # a QueueEntry
                out.append({"name": e.name, "recipe": _recipe_dict(e.recipe)})
        return self.command("submit_queue", entries=out, attrs=dict(attrs or {}),
                            allow_unsaved=allow_unsaved,
                            _timeout_ms=max(self.timeout_ms, 20000))

    def abort(self) -> dict:
        return self.command("abort")

    def stop_queue(self) -> dict:
        return self.command("stop_queue")

    def answer_pause(self, answer) -> dict:
        """True = Continue, False = Abort scan, "all" = Abort all."""
        return self.command("answer_pause", answer=answer)

    def clear_fault(self, module: str) -> dict:
        return self.command("clear_fault", module=module)

    def get_log(self, since: int = 0) -> dict:
        return self.command("get_log", since=int(since))

    def get_scan(self) -> dict:
        """The submitted queue: every entry's definition (a recipe dict), run
        info and result, `current` (index running, -1 when idle)."""
        return self.command("get_scan")

    def get_view(self) -> dict:
        """{view, view_rev, by}: the plot choice of the suite on the server's PC."""
        return self.command("get_view")

    def set_design(self, path, kind: str = "gds", cell: str = "",
                   width_um: float = 0.0) -> dict:
        """Share the Navigator's design file (only from the server's own PC)."""
        import base64
        from pathlib import Path
        p = Path(path)
        return self.command("set_design", name=p.name, kind=kind, cell=cell,
                            width_um=float(width_um),
                            data=base64.b64encode(p.read_bytes()).decode("ascii"),
                            _timeout_ms=30000)

    def get_design(self) -> dict:
        """{design_rev, design: {name, kind, cell, width_um, data(base64)} | None}"""
        return self.command("get_design", _timeout_ms=30000)

    def get_layouts(self) -> dict:
        """The Control tab layouts saved on the server's PC: {name: entry}."""
        return self.command("get_layouts").get("layouts") or {}

    def set_view(self, view: dict) -> dict:
        """Publish this suite's plot choice (only from the server's own PC)."""
        return self.command("set_view", view=view)

    def get_live(self, force: bool = False):
        """The live dataset when it is newer than the last one fetched (None
        when unchanged or when the server has none yet)."""
        r = self.command("get_live", have_rev=-1 if force else self.live_rev,
                         _timeout_ms=max(self.timeout_ms, 30000))
        rev = int(r.get("live_rev") or 0)
        if r.get("unchanged") or not r.get("data"):
            self.live_rev = max(self.live_rev, rev) if r.get("unchanged") else self.live_rev
            return None
        ds = dataset_from_text(r["data"])
        self.live_rev = rev
        return ds


def _recipe_dict(recipe) -> dict:
    """A Recipe (or a dict) as plain JSON data (numbers, not numpy)."""
    if isinstance(recipe, dict):
        return json.loads(json.dumps(recipe, default=float))
    return json.loads(recipe.to_json())


__all__ = ["ScanServerClient", "ScanServerError", "ControlRefused"]
