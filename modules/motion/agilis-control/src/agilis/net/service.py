"""AgilisService -- owns the brain, serves commands, publishes status (section 6).

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

from ..agilis import AgilisStage
from .. import secure
from ..control import ControlLease
from .describe import build_manifest
from . import protocol as P


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class AgilisService:
    def __init__(
        self,
        brain: AgilisStage,
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
        #   SAFETY = verbs a VIEWER may always send. For a stage that is
        #   `stop`: it halts every axis AND aborts a running routine (MA, PA,
        #   measure step size), so a viewer who sees the stage run away must
        #   be able to stop it. `jog` with speed 0 also ends a jog, but it can
        #   start one too, so it is not in the list.
        #   READ = read-only verbs whose names do not start with get_/read_/
        #   list_: `stream_read` only drains the recorded fly-scan positions.
        self.control = ControlLease(
            safety={"stop"},
            read={"stream_read"},
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
        # keys"): when the lab's policy secures agilis, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "agilis",
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
            threading.Thread(target=self._publisher, name="agilis-pub", daemon=True),
            threading.Thread(target=self._commander, name="agilis-cmd", daemon=True),
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
            return {
                "ok": True,
                "info": {
                    "idn": b.backend.idn(),
                    "axes": ["X", "Y"],
                    "limits": P.config_to_dict(b.cfg)["limits"],
                    "calibration": P.config_to_dict(b.cfg)["calibration"],
                    "n_slots": len(b.positions.slots),
                    # what start() had to WRITE to read the controller (MR,
                    # plus a safety ST if a leftover jog was found)
                    "startup_writes": list(b.startup_writes),
                },
            }
        if cmd == "get_config":
            return {"ok": True, "config": P.config_to_dict(b.cfg)}
        if cmd == "set_config":
            P.apply_config_dict(b.cfg, req.get("config", {}))
            b.apply_config()
            return {"ok": True}

        # -- motion: STEP language --------------------------------------- #
        if cmd == "move_to_step":
            return {"ok": True, "target": b.move_to_step(P.parse_axis(req["axis"]), req["position"])}
        if cmd == "move_steps":
            return {"ok": True, "target": b.move_steps(P.parse_axis(req["axis"]), req["delta"])}

        # -- motion: MICROMETRE language --------------------------------- #
        if cmd == "move_to_um":
            return {"ok": True, "target": b.move_to_um(P.parse_axis(req["axis"]), req["position"])}
        if cmd == "move_relative_um":
            return {"ok": True, "target": b.move_relative_um(P.parse_axis(req["axis"]), req["delta"])}

        # -- continuous jog (dead-man: repeat within motion.jog_timeout_s) -- #
        if cmd == "jog":
            return {"ok": True, "mode": b.jog(P.parse_axis(req["axis"]), int(req["speed"]))}

        if cmd == "stop":
            if req.get("axis") is None:
                b.stop_all()
            else:
                b.stop(P.parse_axis(req["axis"]))
            return {"ok": True}

        # -- datum + display origin -------------------------------------- #
        if cmd == "zero_counter":
            if req.get("axis") is None:
                b.zero_counter_all()
            else:
                b.zero_counter(P.parse_axis(req["axis"]))
            return {"ok": True}
        if cmd in ("datum_x", "datum_y"):        # the describe actions (fired by id)
            b.zero_counter(0 if cmd == "datum_x" else 1)
            return {"ok": True}
        if cmd == "set_zero":
            if req.get("axis") is None:
                origins = b.set_zero_all()
            else:
                origins = [b.set_zero(P.parse_axis(req["axis"]))]
            return {"ok": True, "rel_origin": origins}
        if cmd == "clear_zero":
            if req.get("axis") is None:
                b.clear_zero_all()
            else:
                b.clear_zero(P.parse_axis(req["axis"]))
            return {"ok": True}

        # -- amplitude + calibration --------------------------------------- #
        if cmd == "set_amplitude":
            v = b.set_amplitude(P.parse_axis(req["axis"]), req["value"],
                                P.parse_direction(req.get("direction", 0)))
            return {"ok": True, "value": v}
        if cmd == "set_step_size":
            return {"ok": True, "step": b.set_step_size(bool(req["large"]))}
        if cmd == "set_calibration":
            # direction: 0/absent = both ways, +1 forward only, -1 backward only
            v = b.set_calibration(P.parse_axis(req["axis"]), req["value"],
                                  P.parse_direction(req.get("direction", 0)))
            return {"ok": True, "value": v}
        # -- limit-switch stage (AG-LS25): MV, MA, PA, step-size routine ---- #
        if cmd == "move_to_limit":
            return {"ok": True, "mode": b.move_to_limit(
                P.parse_axis(req["axis"]), P.parse_direction(req["direction"]) or 1,
                int(req.get("speed", 3)))}
        if cmd == "measure_position":
            return {"ok": True, "routine_id": b.measure_position(P.parse_axis(req["axis"]))}
        if cmd in ("measure_position_x", "measure_position_y"):
            return {"ok": True, "routine_id": b.measure_position(0 if cmd.endswith("x") else 1)}
        if cmd == "move_absolute":
            return {"ok": True, "routine_id": b.move_absolute(
                P.parse_axis(req["axis"]), float(req["position"]))}
        if cmd == "measure_step_size":
            return {"ok": True, "routine_id": b.measure_step_size(P.parse_axis(req["axis"]))}
        if cmd in ("measure_step_size_x", "measure_step_size_y"):
            return {"ok": True, "routine_id": b.measure_step_size(0 if cmd.endswith("x") else 1)}

        if cmd == "set_leash":
            state = b.set_leash(enabled=req.get("enabled"), leash_steps=req.get("leash_steps"))
            return {"ok": True, "leash": state}

        # -- fly-scan stream: the position recorded continuously ----------- #
        if cmd == "stream_start":
            return {"ok": True, "stream_id": b.stream_start(req.get("rate_hz"))}
        if cmd == "stream_read":
            return {"ok": True, "stream": b.stream.read()}
        if cmd == "stream_stop":
            return {"ok": True, "stream": b.stream_stop()}

        # -- position list ----------------------------------------------- #
        if cmd == "store_position":
            return {"ok": True, "position": b.store_position(int(req["slot"]), req.get("name", ""))}
        if cmd == "clear_position":
            b.clear_position(int(req["slot"]))
            return {"ok": True}
        if cmd == "goto_position":
            return {"ok": True, "targets": b.goto_position(int(req["slot"]))}
        if cmd == "get_positions":
            return {"ok": True, "positions": b.get_positions()}
        if cmd == "save_positions":
            b.save_positions(req["path"])
            return {"ok": True}
        if cmd == "load_positions":
            b.load_positions(req["path"])
            return {"ok": True, "positions": b.get_positions()}

        return {"ok": False, "error": f"unknown command {cmd!r}"}


# zmq's send_json is fine, but events go out on a raw multipart frame, so we
# serialise those ourselves with the same encoder.
import json as _json_mod


def _json(obj) -> bytes:
    return _json_mod.dumps(obj).encode("utf-8")
