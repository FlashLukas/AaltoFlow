"""The service: wrap a BipolarSupply and expose it over ZeroMQ.

One process owns the instrument (or the simulator) and the brain. It runs two
threads of its own (the brain runs a third, the worker):
  * publisher  -- owns the PUB socket; sends a status frame at `status_hz` and
                  forwards brain events as they happen (one socket, because a
                  ZeroMQ socket must be used from a single thread).
  * commander  -- owns the REP socket; receives a JSON command, dispatches it,
                  and replies. It never blocks on the instrument: setters only
                  change brain attributes, the worker does the GPIB traffic.

Bind to tcp://0.0.0.0:<port> and the same code serves a client on localhost or
across the lab network -- only the address the client dials changes.

SAFETY: every way out of serve_forever (Ctrl-C, the `shutdown` verb, an
exception) runs stop(), which makes the brain ramp to zero and switch the output
off before the process exits.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from .. import secure
from ..control import ControlLease
from ..supply import BipolarSupply
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class KepcoService:
    def __init__(self, supply: BipolarSupply,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 10.0):
        self.supply = supply
        self._rev = 0
        self._rev_at = 0.0
        self.cmd_addr = f"tcp://{host}:{cmd_port}"
        self.pub_addr = f"tcp://{host}:{pub_port}"
        self.status_dt = 1.0 / status_hz
        self._stop = threading.Event()
        self._stopped = False
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()
        self._guard = None                   # secure.Guard while secured
        # set by shutdown{keep_outputs: true}: a RESTART (code update) that must
        # not change what the instrument outputs; the next start adopts it
        self._keep_outputs = False
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = verbs a VIEWER may always send. For a power supply the
        #   "make it safe" action is taking the output away: `output_off`
        #   (ramp to zero, then off -- the gentle way, right for a coil) and
        #   `output_off_now` (the emergency switch-off without ramp). A viewer
        #   who sees the coil driven where it should not be must be able to
        #   stop it. `set_output` is NOT in the list even though on=false is
        #   the same thing -- the same verb also switches the output ON.
        #   READ: `ping` only says "a client is alive" (the lost-client
        #   watchdog); every client sends it, a viewer too.
        #   `ramp_stop` ends a current sweep where it is: a stop, so a viewer
        #   may send it. `stream_read` only reads the record of measurements.
        self.control = ControlLease(
            safety={"output_off", "output_off_now", "ramp_stop"},
            read={"ping", "stream_read"},
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
        # keys"): when the lab's policy secures kepco, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "kepco",
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
        self.supply._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.supply.start()
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
        print(f"kepco service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\nstopping ...")
        finally:
            self.stop()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        time.sleep(self.status_dt + 0.1)
        secure.release_server(self._guard)  # the keyring watcher and marker go
        self._guard = None
        if self._keep_outputs:
            print("shutdown: outputs left as they are (restart)")
        # ramp to zero, output off, disconnect -- unless this is a restart
        self.supply.shutdown(keep_outputs=self._keep_outputs)

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for the publisher and the
        `status` reply, so the two can never drift apart."""
        st = status_to_dict(self.supply.status())
        st["describe_rev"] = self.describe_rev()
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
        return st

    def describe_rev(self, max_age_s: float = 0.2) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.
        Short, because a mode change reshapes the manifest."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.supply)["revision"]
            self._rev_at = now
        return self._rev

    def _publisher(self) -> None:
        pub = self._pub_sock                 # bound in start()
        last = 0.0
        while not self._stop.is_set():
            try:
                while True:
                    ev = self._events.get_nowait()
                    pub.send_multipart([TOPIC_EVENT, _json(ev)])
            except queue.Empty:
                pass
            now = time.monotonic()
            if now - last >= self.status_dt:
                try:
                    pub.send_multipart([TOPIC_STATUS, _json(self.status_payload())])
                except Exception:          # never let the publisher die
                    pass
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
                    frame = rep.recv(copy=False)
                    msg = json.loads(frame.bytes.decode("utf-8"))
                    # security first: does the identity match the key
                    # that sent it? (None = yes, or security is off)
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
        s = self.supply
        s.touch()                        # any command proves a client is alive
        try:
            if cmd == "ping":
                return {"ok": True}
            elif cmd == "set_mode":
                s.set_mode(str(msg["mode"]))
            elif cmd == "set_output":
                s.set_output(bool(msg["on"]))
            elif cmd == "output_off":
                # the SAFETY verb: set_output(False), but a verb of its own so
                # a viewer may send it (it can only make things safer)
                s.output_off()
            elif cmd == "output_off_now":
                s.output_off_now()
            elif cmd == "set_current":
                s.set_current(float(msg["current_A"]))
            elif cmd == "ramp_current":
                # the current SWEEP (fly scans): the reply's ramp_id is what
                # status `ramp_id` shows while, and after, this sweep runs
                return {"ok": True, "ramp_id": s.ramp_current(
                    float(msg["current_A"]), float(msg["rate_A_per_s"]))}
            elif cmd == "ramp_stop":
                return {"ok": True, "stopped": s.ramp_stop()}
            elif cmd == "stream_start":
                return {"ok": True, "stream_id": s.stream_start()}
            elif cmd == "stream_read":
                return {"ok": True, "stream": s.stream_read()}
            elif cmd == "stream_stop":
                return {"ok": True, "stream": s.stream_stop()}
            elif cmd == "set_voltage":
                s.set_voltage(float(msg["voltage_V"]))
            elif cmd == "set_current_limit":
                s.set_current_limit(float(msg["current_A"]))
            elif cmd == "set_voltage_limit":
                s.set_voltage_limit(float(msg["voltage_V"]))
            elif cmd == "set_ramp":
                s.set_ramp(rate_A_per_s=_opt_float(msg.get("rate_A_per_s")),
                           rate_V_per_s=_opt_float(msg.get("rate_V_per_s")),
                           enabled=None if msg.get("enabled") is None
                           else bool(msg["enabled"]))
            elif cmd == "set_acquisition":
                s.set_acquisition(int(msg["readings"]))
            elif cmd == "acquire":
                return {"ok": True, "acq_id": s.acquire()}
            elif cmd == "get_sample":
                return {"ok": True, "sample": s.get_sample()}
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(s)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(s.cfg)}
            elif cmd == "set_config":
                apply_config_dict(s.cfg, msg["config"])
                s.apply_config()
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us
                # (docs gotcha #25). Setting _stop ends serve_forever, whose
                # finally: stop() ramps the output down and closes the BOP.
                # keep_outputs=true: a restart for a code update -- close and
                # release everything, but leave the output (no ramp) as it is.
                # ...EXCEPT here (Lukas, 2026-10-11): even a restart is a
                # safe stop for this module -- the supply (usually driving a magnet) is ramped to zero and switched off.
                # Nobody should come back to an instrument left running by
                # a code update. The reply says the request was not honoured.
                asked = _as_bool(msg.get("keep_outputs", False))
                self._keep_outputs = False
                self._stop.set()
                return {"ok": True, "stopping": True,
                        "kept_outputs": False,
                        **({"note": "keep_outputs is not honoured by this "
                                    "module: a restart is a safe stop"}
                           if asked else {})}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            return {"ok": True}
        except ValueError as exc:
            # the brain refuses with a sentence meant for a person
            return {"ok": False, "error": str(exc)}
        except (KeyError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        s = self.supply
        st = s.status()
        ilo, ihi = s.current_range()
        vlo, vhi = s.voltage_range()
        return {
            "idn": st.idn,
            "mode": s.mode,
            "current_min_A": ilo, "current_max_A": ihi,
            "voltage_min_V": vlo, "voltage_max_V": vhi,
            "current_limit_max_A": s.current_limit_max(),
            "voltage_limit_max_V": s.voltage_limit_max(),
        }


def _opt_float(v):
    return None if v is None else float(v)


def _as_bool(v) -> bool:
    """A JSON bool, or text such as "false" from a hand-typed console command.
    bool("false") would be True -- the same trap as gotcha #3."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
