"""The service: wrap a Controller and expose it over ZeroMQ.

One process owns the hardware (or the simulator) and the Controller. It runs two
extra threads:
  * publisher  -- owns the PUB socket; sends a status frame at `status_hz` and
                  forwards controller events as they happen (all through one
                  socket, because a ZeroMQ socket must be used from one thread).
  * commander  -- owns the REP socket; receives a JSON command, dispatches it to
                  the controller, and replies.

Bind to tcp://0.0.0.0:<port> and the same code serves a client on localhost or
across the lab network -- the only difference is the address the client dials.
"""

from __future__ import annotations

import json as _json_mod
import queue
import threading
import time

import zmq

from .. import secure
from ..control import ControlLease
from ..controller import Controller
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, calibration_to_dict, calibration_from_dict)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class ClMagService:
    def __init__(self, controller: Controller,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 10.0):
        self.ctrl = controller
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
        # Cached describe revision. Every status frame carries it so clients can
        # tell, for the cost of a integer compare, whether their cached manifest
        # went stale -- clMag's field limits ARE the calibration range, so they
        # move the moment a calibration is loaded or measured. Recomputed at
        # most once a second; rebuilding the manifest at the 10 Hz status rate
        # would be pure waste.
        self._rev = 0
        self._rev_at = 0.0
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = verbs a VIEWER may always send. For a magnet the only
        #   "make it safe" action is taking the current away, so it is
        #   `ramp_to_zero` (the GUI's "Ramp to Zero & Stop"): a viewer who sees
        #   the coil run hot must be able to switch it off. `set_current` is NOT
        #   in the list even though it can also go to 0 A -- it can go anywhere
        #   else too. `demag` is not safety either: it swings the current
        #   through large alternating values before it ends at zero.
        #   READ = read-only verbs whose names do not start with get_/read_/
        #   list_: `aux_read_ai` only reads a DAQ input.
        #   `ramp_stop` (end a field sweep where it is, current held) is a
        #   stop: a viewer may send it. `stream_read` only reads the record.
        self.control = ControlLease(
            safety={"ramp_to_zero", "ramp_stop"},
            read={"aux_read_ai", "stream_read"},
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
        # keys"): when the lab's policy secures clMag, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "clMag",
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
            self._guard = None
            raise PortInUse(
                f"cannot listen on {self.cmd_addr} / {self.pub_addr} ({exc}); "
                f"is another service already using these ports?") from exc
        # route controller events into the publisher queue
        self.ctrl._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.ctrl.start()
        except BaseException:
            # the instrument did not start (busy, unplugged, ...):
            # give the ports back before the exception leaves
            self._pub_sock.close(0)
            self._rep_sock.close(0)
            secure.release_server(self._guard)
            self._guard = None
            raise
        self._pub_t = threading.Thread(target=self._publisher, name="svc-pub", daemon=True)
        self._cmd_t = threading.Thread(target=self._commander, name="svc-cmd", daemon=True)
        self._pub_t.start()
        self._cmd_t.start()

    def serve_forever(self) -> None:
        self.start()
        print(f"clMag service up  ·  commands {self.cmd_addr}  ·  status {self.pub_addr}")
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
        self.ctrl.shutdown(keep_outputs=self._keep_outputs)

    # ---------------------------------------------------------------- threads

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

    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently. That is exactly how `describe_rev` was
        broken when it was first added to the publisher alone.
        """
        st = status_to_dict(self.ctrl.status())
        st["describe_rev"] = self.describe_rev()
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
        return st

    def describe_rev(self, max_age_s: float = 1.0) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.ctrl)["revision"]
            self._rev_at = now
        return self._rev

    def _commander(self) -> None:
        rep = self._rep_sock                 # bound in start()
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(200):
                try:
                    # the raw frame, not recv_json: under encryption the frame
                    # carries the key that sent it (secure.user_id)
                    frame = rep.recv(copy=False)
                    msg = _json_mod.loads(frame.bytes.decode("utf-8"))
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
        # Queued commands answer with their sequence number `seq`; a status
        # frame whose `cmd_done` >= seq already reflects the command (the one
        # guard against a stale frame that works for every verb, gotcha #2).
        try:
            if cmd == "set_field":
                return {"ok": True, "seq": self.ctrl.set_field(
                    float(msg["field_mT"]), bool(msg.get("use_pid", True)))}
            elif cmd == "set_current":
                return {"ok": True, "seq": self.ctrl.set_current(float(msg["current_A"]))}
            elif cmd == "ramp_to_zero":
                # the SAFETY verb: the same as set_current 0, but a verb of its
                # own so a viewer may send it (it can only make things safer)
                return {"ok": True, "seq": self.ctrl.set_current(0.0)}
            elif cmd == "demag":
                return {"ok": True, "seq": self.ctrl.demag(float(msg["amplitude_A"]))}
            elif cmd == "calibrate":
                return {"ok": True, "seq": self.ctrl.calibrate(
                    int(msg.get("n_per_leg", 50)), float(msg.get("dwell_s", 0.5)))}
            elif cmd == "ramp_field":
                # the field SWEEP (fly scans): the reply's ramp_id is what
                # status `ramp_id` reaches when the sweep is taken up
                return {"ok": True, "ramp_id": self.ctrl.ramp_field(
                    float(msg["field_mT"]), float(msg["rate_mT_per_s"]))}
            elif cmd == "ramp_stop":
                return {"ok": True, "seq": self.ctrl.ramp_stop()}
            elif cmd == "stream_start":
                return {"ok": True, "stream_id": self.ctrl.stream_start()}
            elif cmd == "stream_read":
                return {"ok": True, "stream": self.ctrl.stream_read()}
            elif cmd == "stream_stop":
                return {"ok": True, "stream": self.ctrl.stream_stop()}
            elif cmd == "set_lock":
                return {"ok": True, "seq": self.ctrl.set_lock(bool(msg["locked"]))}
            elif cmd == "set_stabilizer":
                self.ctrl.stabilizer_enabled = bool(msg["enabled"])
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(self.ctrl)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(self.ctrl.cfg)}
            elif cmd == "set_config":
                apply_config_dict(self.ctrl.cfg, msg["config"])
                self.ctrl.apply_config()
            elif cmd == "get_calibration":
                return {"ok": True, "calibration": calibration_to_dict(self.ctrl.calibration)}
            elif cmd == "set_calibration":
                self.ctrl.set_calibration(calibration_from_dict(msg["calibration"]))
            elif cmd == "aux_set_ao":
                self.ctrl.aux_set_ao(msg["channel"], float(msg["volts"]))
            elif cmd == "aux_set_do":
                self.ctrl.aux_set_do(msg["line"], bool(msg["state"]))
            elif cmd == "aux_read_ai":
                return {"ok": True, "volts": self.ctrl.aux_read_ai(msg["channel"])}
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us: a
                # hard kill gives the brain no chance to close its hardware (it wedged
                # the PM16 until replugged, docs/DEVELOPER_NOTES.md gotcha #25). Setting _stop
                # ends serve_forever, whose finally: stop() shuts the brain down.
                # keep_outputs=true: a restart for a code update -- close and
                # release everything, but leave the outputs as they are.
                # ...EXCEPT here (Lukas, 2026-10-11): even a restart is a
                # safe stop for this module -- the magnet is ramped to zero and switched off.
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
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        cal = self.ctrl.calibration
        lo, hi = cal.range_mT if (cal and cal.currents_A) else (0.0, 0.0)
        return {
            "field_lo": lo,
            "field_hi": hi,
            "n_points": len(cal.currents_A) if cal else 0,
            "current_max": self.ctrl.cfg.limits.current_max_A,
            "tolerance": self.ctrl.cfg.limits.field_tolerance_mT,
        }


def _json(d: dict) -> bytes:
    import json
    return json.dumps(d).encode("utf-8")


def _as_bool(v) -> bool:
    """JSON true/false, but also "false"/0 from a hand-typed console command --
    bool("false") is True (the INI gotcha again, docs/DEVELOPER_NOTES.md #3)."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)
