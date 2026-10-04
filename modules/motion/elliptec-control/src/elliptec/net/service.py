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

import json as _json_mod
import queue
import threading
import time

import zmq

import re

from ..mount import RotationMount
from ..control import ControlLease
from .. import secure
from .describe import build_manifest
from . import protocol as P


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


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
        self._guard = None                   # secure.Guard while secured
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = verbs a VIEWER may always send. For rotation mounts that
        #   is `stop` (one mount, or all): it drops the queued moves and halts
        #   the motion, so a viewer who sees a mount turn where it should not
        #   must be able to stop it. `home` is not safety: it turns the mount.
        #   READ = nothing extra: every read-only verb here is already
        #   status / info / describe / get_config.
        self.control = ControlLease(
            safety={"stop"},
            read=set(),
            on_event=lambda level, msg: self._events.put({"level": level, "msg": msg}))

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        # Bind BOTH sockets here, in the caller's thread, and BEFORE the
        # instrument is opened (gotcha #39). They used to be bound inside the
        # two daemon threads: a port already in use then killed only that
        # thread, with a traceback nobody reads, while the process lived on --
        # deaf, but holding the instrument and its hwlock claim. Now a taken
        # port raises PortInUse out of start(), before anything was opened or
        # claimed. (Handing a socket to the thread that will use it is allowed
        # in ZeroMQ; Thread.start() is the memory barrier it asks for.)
        cmd_addr = f"tcp://{self.host}:{self.cmd_port}"
        pub_addr = f"tcp://{self.host}:{self.pub_port}"
        self._pub_sock = self._ctx.socket(zmq.PUB)
        self._rep_sock = self._ctx.socket(zmq.REP)
        # Encryption and who-is-who (secure.py, README "Encryption and
        # keys"): when the lab's policy secures elliptec, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "elliptec",
                on_event=lambda level, msg: self._events.put({"level": level, "msg": msg}))
        except secure.SecurityError:
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            raise
        try:
            self._pub_sock.bind(pub_addr)
            self._rep_sock.bind(cmd_addr)
        except zmq.ZMQError as exc:
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            secure.release_server(self._guard)
            raise PortInUse(
                f"cannot listen on {cmd_addr} / {pub_addr} ({exc}); "
                f"is another service already using these ports?") from exc
        # Route brain events into our queue (they get PUBlished as b"event").
        self.brain._on_event = lambda level, msg: self._events.put(
            {"level": level, "msg": msg}
        )
        try:
            self.brain.start()
        except BaseException:
            # the instrument did not start (busy, unplugged, ...):
            # give the ports back before the exception leaves
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            secure.release_server(self._guard)
            raise

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
        secure.release_server(self._guard)
        self._guard = None
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
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
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
        sock = self._pub_sock                # bound in start()
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
        sock = self._rep_sock                # bound in start()
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if dict(poller.poll(200)):
                    try:
                        frame = sock.recv(copy=False)
                        raw = frame.bytes
                    except Exception:
                        continue
                    # A REP socket that has received MUST send before it can
                    # receive again. A request that was not JSON used to be
                    # skipped with `continue` and no reply: the socket then
                    # refused every later recv, and this loop never answered
                    # anyone again -- one bad message took the whole command
                    # port down (gotcha #39). Now it gets an error reply like
                    # any other failed command.
                    try:
                        req = _json_mod.loads(raw.decode("utf-8"))
                        # security first: does the identity match the key
                        # that sent it? (None = yes, or security is off)
                        refused = None
                        if self._guard is not None and isinstance(req, dict):
                            refused = self._guard.check(req, secure.user_id(frame))
                        reply = refused or (self._dispatch(req) if isinstance(req, dict) else
                                            {"ok": False, "error": "request must be a JSON object"})
                    except Exception as exc:  # never die on a bad command
                        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    # Serialise BEFORE sending, for the same reason: a reply
                    # json cannot encode (a numpy number, say) must still be
                    # answered, or the socket is stuck again.
                    try:
                        data = _json(reply)
                    except Exception as exc:
                        data = _json({"ok": False, "error": f"reply not JSON-encodable: {exc}"})
                    try:
                        sock.send(data)
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
        # Who may change what (control.py): the gate answers the control verbs
        # itself and refuses a change from a viewer; anything else goes on.
        gate = self.control.handle(req)
        if gate is not None:
            return gate
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
