"""ZPiezoService -- owns the brain, serves commands, publishes status (§6).

Two daemon threads (publisher + commander), one socket each -- identical pattern
to every other module in the suite.
"""

from __future__ import annotations

import json as _json_mod
import queue
import threading
import time

import zmq

from .describe import build_manifest
from ..zpiezo import ZPiezo, status_to_dict
from . import protocol as P


class ZPiezoService:
    def __init__(self, brain: ZPiezo, host: str = "0.0.0.0",
                 cmd_port: int = P.DEFAULT_CMD_PORT,
                 pub_port: int = P.DEFAULT_PUB_PORT, status_hz: float = 8.0):
        self.brain = brain
        self._rev = 0
        self._rev_at = 0.0
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.status_hz = status_hz
        self._ctx = zmq.Context.instance()
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        # Bind BOTH ports first, here in the calling thread, and only then open
        # the KCube (deep cleaning 2026-09-28).  The binds used to happen inside
        # the two threads: a port already in use killed the thread with an
        # exception nobody saw, and the service ran on -- deaf, but holding the
        # KCube (and its hwlock).  Now a clash is a RuntimeError before any
        # hardware is touched; run_service.py turns it into one line + exit 2.
        # (Creating a ZeroMQ socket in one thread and handing it to another is
        # allowed; starting the thread is the memory barrier ZeroMQ asks for.)
        pub = self._ctx.socket(zmq.PUB)
        rep = self._ctx.socket(zmq.REP)
        try:
            pub.bind(f"tcp://{self.host}:{self.pub_port}")
            rep.bind(f"tcp://{self.host}:{self.cmd_port}")
        except zmq.ZMQError as exc:
            pub.close(0)
            rep.close(0)
            raise RuntimeError(
                f"cannot listen on ports {self.cmd_port}/{self.pub_port} ({exc}); "
                f"is another service already using them?") from exc
        self.brain._on_event = lambda level, msg: self._events.put({"level": level, "msg": msg})
        try:
            self.brain.start()
        except BaseException:
            pub.close(0)
            rep.close(0)
            raise
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._publisher, args=(pub,), name="z-pub", daemon=True),
            threading.Thread(target=self._commander, args=(rep,), name="z-cmd", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        try:
            self.brain.shutdown()
        except Exception:
            pass

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = status_to_dict(self.brain.status())
        st["describe_rev"] = self.describe_rev()
        return st

    def describe_rev(self, max_age_s: float = 1.0) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.

        Every status frame carries it so a client can tell, for the cost of one
        integer compare, whether its cached manifest went stale. Rebuilding the
        manifest at the status rate would be pure waste.
        """
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.brain)["revision"]
            self._rev_at = now
        return self._rev

    def _publisher(self, sock) -> None:
        period = 1.0 / self.status_hz
        nxt = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    while True:
                        sock.send_multipart([P.TOPIC_EVENT, _json(self._events.get_nowait())])
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now >= nxt:
                    nxt = now + period
                    try:
                        sock.send_multipart([P.TOPIC_STATUS, _json(self.status_payload())])
                    except Exception:
                        pass
                time.sleep(0.005)
        finally:
            sock.close(0)

    def _commander(self, sock) -> None:
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if dict(poller.poll(200)):
                    try:
                        req = sock.recv_json()
                    except Exception:
                        continue
                    try:
                        reply = self._dispatch(req)
                    except Exception as exc:
                        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    try:
                        sock.send_json(reply)
                    except Exception:
                        pass
        finally:
            # linger, not 0: after `shutdown` the reply may still be queued, and
            # close(0) would drop it -- the launcher would then kill us anyway.
            sock.close(linger=500)

    def _dispatch(self, req: dict) -> dict:
        cmd = (req or {}).get("cmd")
        b = self.brain
        if cmd == "status":
            return {"ok": True, "status": self.status_payload()}
        if cmd == "describe":
            return {"ok": True, "describe": build_manifest(self.brain)}
        if cmd == "shutdown":
            # A CLEAN stop, asked for by the launcher before it would kill us: a
            # hard kill gives the brain no chance to close its hardware (it wedged
            # the PM16 until replugged, docs/DEVELOPER_NOTES.md gotcha #25). Setting _stop
            # ends serve_forever, whose finally: stop() shuts the brain down.
            self._stop.set()
            return {"ok": True, "stopping": True}
        if cmd == "info":
            # The LIVE envelope (cfg.limits), not backend.range(): that is a copy
            # taken at build time and went stale after a set_config.  The camera
            # sizes its autofocus sweep from these two numbers.
            lim = b.cfg.limits
            return {"ok": True, "info": {"idn": b.backend.idn(),
                                         "limits": {"v_min": float(lim.v_min),
                                                    "v_max": float(lim.v_max)}}}
        if cmd == "get_config":
            return {"ok": True, "config": P.config_to_dict(b.cfg)}
        if cmd == "set_config":
            # brain.set_config validates first (all or nothing) and raises on a
            # bad envelope -> the commander turns that into {"ok": false}.
            b.set_config(req.get("config", {}))
            return {"ok": True}
        if cmd == "set_voltage":
            return {"ok": True, "voltage": b.set_voltage(req["volts"])}
        if cmd == "read_voltage":
            return {"ok": True, "voltage": b.read_voltage()}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


def _json(obj) -> bytes:
    return _json_mod.dumps(obj).encode("utf-8")
