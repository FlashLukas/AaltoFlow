"""ElliptecService -- owns the brain, serves commands, publishes status (section 6).

Two daemon threads, each owning exactly one socket (a ZeroMQ socket must not be
shared across threads):

  * publisher : PUB socket.  Sends a status frame every 1/status_hz seconds and
                drains the event queue as events arrive.
  * commander : REP socket.  poll(200ms) -> recv_json -> _dispatch -> send_json.
                Wrapped so a bad command can never kill the loop.

The brain's ``_on_event`` hook is redirected into a thread-safe queue so events
raised on the command thread reach the publisher thread cleanly.
"""

from __future__ import annotations

import queue
import threading
import time

import zmq

import re

from ..mount import RotationMount
from .describe import build_manifest
from . import protocol as P


class ElliptecService:
    def __init__(
        self,
        brain: RotationMount,
        host: str = "0.0.0.0",
        cmd_port: int = P.DEFAULT_CMD_PORT,
        pub_port: int = P.DEFAULT_PUB_PORT,
        status_hz: float = 8.0,
    ):
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

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        # Route brain events into our queue (they get PUBlished as b"event").
        self.brain._on_event = lambda level, msg: self._events.put(
            {"level": level, "msg": msg}
        )
        self.brain.start()

        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._publisher, name="elliptec-pub", daemon=True),
            threading.Thread(target=self._commander, name="elliptec-cmd", daemon=True),
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
        """Convenience: start and block until Ctrl-C."""
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    # ------------------------------------------------------------------ #
    # publisher thread
    # ------------------------------------------------------------------ #
    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = P.status_to_dict(self.brain.status())
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

    def _publisher(self) -> None:
        sock = self._ctx.socket(zmq.PUB)
        sock.bind(f"tcp://{self.host}:{self.pub_port}")
        period = 1.0 / self.status_hz
        next_status = time.monotonic()
        try:
            while not self._stop.is_set():
                # 1) forward any pending events immediately
                try:
                    while True:
                        evt = self._events.get_nowait()
                        sock.send_multipart([P.TOPIC_EVENT, _json(evt)])
                except queue.Empty:
                    pass

                # 2) publish status on schedule
                now = time.monotonic()
                if now >= next_status:
                    next_status = now + period
                    try:
                        payload = self.status_payload()
                        sock.send_multipart([P.TOPIC_STATUS, _json(payload)])
                    except Exception:
                        pass  # status must never take the publisher down

                time.sleep(0.005)
        finally:
            sock.close(0)

    # ------------------------------------------------------------------ #
    # commander thread
    # ------------------------------------------------------------------ #
    def _commander(self) -> None:
        sock = self._ctx.socket(zmq.REP)
        sock.bind(f"tcp://{self.host}:{self.cmd_port}")
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
                    except Exception as exc:  # never die on a bad command
                        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    try:
                        sock.send_json(reply)
                    except Exception:
                        pass
        finally:
            # linger, not 0: after `shutdown` the reply may still be queued, and
            # close(0) would drop it -- the launcher would then kill us anyway.
            sock.close(linger=500)

    # ------------------------------------------------------------------ #
    # command dispatch
    # ------------------------------------------------------------------ #
    def _dispatch(self, req: dict) -> dict:
        cmd = (req or {}).get("cmd")
        b = self.brain

        # -- universal commands (every module implements these) ---------- #
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
            info = b.info()
            info["limits"] = P.config_to_dict(b.cfg)["limits"]
            return {"ok": True, "info": info}
        if cmd == "get_config":
            return {"ok": True, "config": P.config_to_dict(b.cfg)}
        if cmd == "set_config":
            P.apply_config_dict(b.cfg, req.get("config", {}))
            b.apply_config()
            return {"ok": True}

        def axis(key="axis"):
            return P.parse_axis(req[key], b.addresses)

        # -- motion (replies once QUEUED; watch status for the arrival) --- #
        if cmd == "move_abs":
            return {"ok": True, **b.move_abs(axis(), req["angle_deg"])}
        if cmd == "move_rel":
            return {"ok": True, **b.move_rel(axis(), req["delta_deg"])}
        if cmd == "home":
            if req.get("axis") is None:
                runs = b.home_all(req.get("direction"))
                return {"ok": True, "move_ids": [r["move_id"] for r in runs]}
            return {"ok": True, **b.home(axis(), req.get("direction"))}
        if cmd == "stop":
            if req.get("axis") is None:
                b.stop_all()
            else:
                b.stop(axis())
            return {"ok": True}

        # -- parameters and frame ----------------------------------------- #
        if cmd == "set_velocity":
            return {"ok": True, "value": b.set_velocity(axis(), req["value"])}
        if cmd == "set_offset":
            return {"ok": True, "offset_deg": b.set_offset(axis(), req["value"])}
        if cmd == "set_zero":
            return {"ok": True, "offset_deg": b.set_zero(axis())}
        if cmd == "clear_zero":
            return {"ok": True, "offset_deg": b.clear_zero(axis())}

        # -- per-axis ACTIONS from describe: the id is the verb ------------ #
        # (home_0, set_zero_a): a control screen or a scan routine sends the
        # action's id with no arguments, so the address is in the name.
        m = _ACTION_RE.match(str(cmd or ""))
        if m and m.group(2).upper() in b.addresses:
            i = b.addresses.index(m.group(2).upper())
            if m.group(1) == "home":
                return {"ok": True, **b.home(i)}
            return {"ok": True, "offset_deg": b.set_zero(i)}

        return {"ok": False, "error": f"unknown command {cmd!r}"}


_ACTION_RE = re.compile(r"^(home|set_zero)_([0-9a-fA-F])$")


# zmq's send_json is fine, but events go out on a raw multipart frame, so we
# serialise those ourselves with the same encoder.
import json as _json_mod


def _json(obj) -> bytes:
    return _json_mod.dumps(obj).encode("utf-8")
