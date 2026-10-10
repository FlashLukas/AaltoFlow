"""The service: wrap the Cryostat brain and expose it over ZeroMQ.

One process owns MultiVu (or the simulator) and the Cryostat. It runs
two extra threads, like every service in the suite:
  * publisher  -- owns the PUB socket; sends a status frame at `status_hz` and
                  forwards brain events as they happen (one socket, because a
                  ZeroMQ socket must be used from a single thread).
  * commander  -- owns the REP socket; receives a JSON command, dispatches it to
                  the brain, and replies.

Bind to tcp://0.0.0.0:<port> and the same code serves a client on localhost or
across the lab network -- only the address the client dials changes.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from .. import secure
from ..control import ControlLease
from ..cryostat import Cryostat
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class PpmsService:
    def __init__(self, cryostat: Cryostat,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 5.0):
        self.cryo = cryostat
        self._rev = 0
        self._rev_at = 0.0
        self.cmd_addr = f"tcp://{host}:{cmd_port}"
        self.pub_addr = f"tcp://{host}:{pub_port}"
        self.status_dt = 1.0 / status_hz
        self._stop = threading.Event()
        # set by shutdown{keep_outputs: true}: a RESTART (code update) that must
        # not change what the instrument outputs; the next start adopts it
        self._keep_outputs = False
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()
        self._guard = None                   # secure.Guard while secured
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = none. Every verb here is a setpoint or a rate/approach
        #   change, and each of them can send the magnet or the temperature
        #   ANYWHERE -- including "Go to zero", which is only set_field(0): on a
        #   DynaCool a field sweep to zero is a deliberate move (it destroys
        #   whatever state the sample was in), not a way of making things
        #   safe. The DynaCool protects itself (its magnet supply and
        #   temperature controller run inside MultiVu), and this module has no
        #   verb that ONLY stops a ramp. So a viewer can only watch; a person
        #   who must stop a sweep takes control first (a deliberate, visible
        #   take-over).
        #   READ = none: every read-only verb here is already status/info/
        #   describe/get_config.
        #   Since 2026-10-09 there IS a verb that only stops: `ramp_stop`
        #   ends a field SWEEP (fly scans) where the field is, so a viewer
        #   may send it; `stream_read` only reads the poll thread's record.
        self.control = ControlLease(
            safety={"ramp_stop", "ramp_temperature_stop"},
            read={"stream_read"},
            on_event=lambda level, msg: self._events.put({"level": level, "msg": msg}))

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        # Bind BOTH sockets here, in the caller's thread, and BEFORE the
        # instrument is opened (gotcha #39). They used to be bound inside the
        # two daemon threads: a port already in use then killed only that
        # thread, with a traceback nobody reads, while the process lived on --
        # deaf, but holding the instrument and its hwlock claim. Now a taken
        # port raises PortInUse out of start(), before anything was opened or
        # claimed. (Handing a socket to the thread that will use it is allowed
        # in ZeroMQ; Thread.start() is the memory barrier it asks for.)
        self._pub_sock = self._ctx.socket(zmq.PUB)
        self._rep_sock = self._ctx.socket(zmq.REP)
        # Encryption and who-is-who (secure.py, README "Encryption and
        # keys"): when the lab's policy secures ppms, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "ppms",
                on_event=lambda level, msg: self._events.put({"level": level, "msg": msg}))
        except secure.SecurityError:
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            raise
        try:
            self._pub_sock.bind(self.pub_addr)
            self._rep_sock.bind(self.cmd_addr)
        except zmq.ZMQError as exc:
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            secure.release_server(self._guard)
            raise PortInUse(
                f"cannot listen on {self.cmd_addr} / {self.pub_addr} ({exc}); "
                f"is another service already using these ports?") from exc
        # route brain events into the publisher queue
        self.cryo._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.cryo.start()
        except BaseException:
            # the instrument did not start (busy, unplugged, ...):
            # give the ports back before the exception leaves
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            secure.release_server(self._guard)
            raise
        self._pub_t = threading.Thread(target=self._publisher, name="svc-pub", daemon=True)
        self._cmd_t = threading.Thread(target=self._commander, name="svc-cmd", daemon=True)
        self._pub_t.start()
        self._cmd_t.start()

    def serve_forever(self) -> None:
        self.start()
        print(f"ppms service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\nstopping ...")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        time.sleep(self.status_dt + 0.1)
        secure.release_server(self._guard)
        self._guard = None
        if self._keep_outputs:
            print("shutdown: outputs left as they are (restart)")
        self.cryo.shutdown(keep_outputs=self._keep_outputs)

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = status_to_dict(self.cryo.status())
        st["describe_rev"] = self.describe_rev()
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
        return st

    def describe_rev(self, max_age_s: float = 1.0) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.

        Every status frame carries it so a client can tell, for the cost of one
        integer compare, whether its cached manifest went stale.
        """
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.cryo)["revision"]
            self._rev_at = now
        return self._rev

    def _publisher(self) -> None:
        pub = self._pub_sock                 # bound in start()
        last = 0.0
        while not self._stop.is_set():
            # forward any pending events immediately
            try:
                while True:
                    ev = self._events.get_nowait()
                    pub.send_multipart([TOPIC_EVENT, _json(ev)])
            except queue.Empty:
                pass
            now = time.monotonic()
            if now - last >= self.status_dt:
                pub.send_multipart([TOPIC_STATUS, _json(self.status_payload())])
                last = now
            time.sleep(0.01)
        pub.close(0)

    def _commander(self) -> None:
        rep = self._rep_sock                 # bound in start()
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(200):
                try:
                    # the raw frame, not recv_json(): its metadata carries
                    # the key that sent it (secure.user_id)
                    frame = rep.recv(copy=False)
                    msg = json.loads(frame.bytes.decode("utf-8"))
                    # security first: does the identity match the key that
                    # sent it? (None = yes, or security is off)
                    refused = None
                    if self._guard is not None and isinstance(msg, dict):
                        refused = self._guard.check(msg, secure.user_id(frame))
                    rep.send_json(refused or self._dispatch(msg))
                except Exception as exc:                       # never let the loop die
                    try:
                        rep.send_json({"ok": False, "error": str(exc)})
                    except zmq.ZMQError:
                        pass
        # linger, not 0: after `shutdown` the reply may still be queued, and
        # close(0) would drop it -- the launcher would then kill us anyway.
        rep.close(linger=500)

    # -------------------------------------------------------------- dispatch

    def _dispatch(self, msg: dict) -> dict:
        # Who may change what (control.py): the gate answers the control verbs
        # itself and refuses a change from a viewer; anything else goes on.
        gate = self.control.handle(msg)
        if gate is not None:
            return gate
        cmd = msg.get("cmd")
        try:
            if cmd == "set_field":
                self.cryo.set_field(float(msg["field_mT"]))
            elif cmd == "ramp_field":
                # the field SWEEP (fly scans): MultiVu sweeps at this rate
                return {"ok": True, "ramp_id": self.cryo.ramp_field(
                    float(msg["field_mT"]), float(msg["rate_mT_per_s"]))}
            elif cmd == "ramp_stop":
                return {"ok": True, "stopped": self.cryo.ramp_stop()}
            elif cmd == "ramp_temperature":
                # the temperature SWEEP (fly scans): MultiVu sweeps at this
                # rate (K/s on the wire, converted to K/min by the brain)
                return {"ok": True, "ramp_id": self.cryo.ramp_temperature(
                    float(msg["temperature_K"]), float(msg["rate_K_per_s"]))}
            elif cmd == "ramp_temperature_stop":
                return {"ok": True, "stopped": self.cryo.ramp_temperature_stop()}
            elif cmd == "stream_start":
                return {"ok": True, "stream_id": self.cryo.stream_start()}
            elif cmd == "stream_read":
                return {"ok": True, "stream": self.cryo.stream_read()}
            elif cmd == "stream_stop":
                return {"ok": True, "stream": self.cryo.stream_stop()}
            elif cmd == "set_field_rate":
                self.cryo.set_field_rate(float(msg["rate_mT_per_s"]))
            elif cmd == "set_field_approach":
                self.cryo.set_field_approach(str(msg["approach"]))
            elif cmd == "set_temperature":
                self.cryo.set_temperature(float(msg["temperature_K"]))
            elif cmd == "set_temperature_rate":
                self.cryo.set_temperature_rate(float(msg["rate_K_per_min"]))
            elif cmd == "set_temperature_approach":
                self.cryo.set_temperature_approach(str(msg["approach"]))
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(self.cryo)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(self.cryo.cfg)}
            elif cmd == "set_config":
                apply_config_dict(self.cryo.cfg, msg["config"])
                self.cryo.apply_config()
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us: a
                # hard kill gives the brain no chance to close its hardware (it wedged
                # the PM16 until replugged, docs/DEVELOPER_NOTES.md gotcha #25). Setting _stop
                # ends serve_forever, whose finally: stop() shuts the brain down.
                # keep_outputs=true: a restart for a code update -- close and
                # release everything, but leave the outputs as they are.
                self._keep_outputs = _as_bool(msg.get("keep_outputs", False))
                self._stop.set()
                # This shutdown never changes the cryostat (field, temperature), so the
                # outputs are kept either way: kept_outputs is always true.
                return {"ok": True, "stopping": True, "kept_outputs": True}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}
        except RuntimeError as exc:           # e.g. MultiVu not connected
            return {"ok": False, "error": str(exc)}

    def _info(self) -> dict:
        lim = self.cryo.cfg.limits
        st = self.cryo.status()
        return {
            "idn": st.idn,
            "simulated": st.simulated,
            "field_max_mT": lim.field_max_mT,
            "temperature_min_K": lim.temperature_min_K,
            "temperature_max_K": lim.temperature_max_K,
            "field_rate_max_mT_per_s": lim.field_rate_max_mT_per_s,
            "temperature_rate_max_K_per_min": lim.temperature_rate_max_K_per_min,
        }


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")


def _as_bool(v) -> bool:
    """JSON true/false, but also "false"/0 from a hand-typed console command --
    bool("false") is True (the INI gotcha again, docs/DEVELOPER_NOTES.md #3)."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)
