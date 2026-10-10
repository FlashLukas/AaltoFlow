"""The service: wrap a SignalSource and expose it over ZeroMQ.

One process owns the instrument (or the simulator) and the brain. Besides the
brain's own threads (the worker, and while a sweep runs the sweep's thread;
they share the GPIB bus under one lock) it runs two threads:
  * publisher  -- owns the PUB socket; sends a status frame at `status_hz` and
                  forwards brain events as they happen (one socket, because a
                  ZeroMQ socket must be used from a single thread).
  * commander  -- owns the REP socket; receives a JSON command, dispatches it to
                  the brain, and replies. Never allowed to die.

Bind to tcp://0.0.0.0:<port> and the same code serves a client on localhost or
across the lab network -- only the address the client dials changes.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from .. import spec
from .. import secure
from ..control import ControlLease
from ..source import SignalSource
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class Hp8648Service:
    def __init__(self, source: SignalSource,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 5.0):
        self.src = source
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
        #   SAFETY = verbs a VIEWER may always send. For a signal generator the
        #   one "make it safe" action is RF off, so it is `rf_off` (the GUI's
        #   "RF Off" button): a viewer who sees the output hit something it
        #   should not must be able to take it away. `set_rf` is NOT in the
        #   list even though on=false is the same thing -- the same verb also
        #   switches the RF ON (and re-arms the reverse-power protection).
        #   `ramp_stop` ends a sweep (frequency or level) where it is: a
        #   stop, so a viewer may send it too.
        #   READ: `stream_read` only reads the sweeps' record.
        self.control = ControlLease(
            safety={"rf_off", "ramp_stop"},
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
        # keys"): when the lab's policy secures hp8648, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "hp8648",
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
        # route brain events into the publisher queue (thread-safe)
        self.src._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.src.start()
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
        print(f"hp8648 service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\nstopping ...")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the threads, then switch the RF off and disconnect."""
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        for t in (getattr(self, "_pub_t", None), getattr(self, "_cmd_t", None)):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=2.0)
        secure.release_server(self._guard)   # authenticator + marker go too
        self._guard = None
        if self._keep_outputs:
            print("shutdown: outputs left as they are (restart)")
        self.src.shutdown(keep_outputs=self._keep_outputs)

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = status_to_dict(self.src.status())
        st["describe_rev"] = self.describe_rev()
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
        return st

    def describe_rev(self) -> int:
        """Current manifest revision. Cheap (a dozen dicts and a CRC), so it is
        recomputed for every frame: the power ceiling moves the moment the
        frequency crosses 2500 MHz, and a cached value would lag behind it."""
        return build_manifest(self.src)["revision"]

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
                except Exception:                       # never let the loop die
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
                    # the raw frame, not recv_json: under encryption the frame
                    # carries the key that sent it (secure.user_id)
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
        if not isinstance(msg, dict):
            return {"ok": False, "error": "a command must be a JSON object"}
        # Who may change what (control.py): the gate answers the control verbs
        # itself and refuses a change from a viewer; anything else goes on.
        gate = self.control.handle(msg)
        if gate is not None:
            return gate
        cmd = msg.get("cmd")
        try:
            if cmd == "set_rf":
                self.src.set_rf(_bool(msg["on"]))
            elif cmd == "rf_off":
                # the SAFETY verb: set_rf(False), but a verb of its own so a
                # viewer may send it (it can only make things safer)
                self.src.rf_off()
            elif cmd == "set_power":
                self.src.set_power(float(msg["power_dBm"]))
            elif cmd == "set_frequency":
                self.src.set_frequency(float(msg["frequency_Hz"]))
            # The SWEEPS (fly scans): the reply's ramp_id is what status
            # `<knob>_ramp_id` shows while and after this sweep runs. No
            # ramp_phase: the 8648D has no phase control.
            elif cmd == "ramp_frequency":
                return {"ok": True, "ramp_id": self.src.ramp_frequency(
                    float(msg["frequency_Hz"]), float(msg["rate_Hz_per_s"]))}
            elif cmd == "ramp_power":
                return {"ok": True, "ramp_id": self.src.ramp_power(
                    float(msg["power_dBm"]), float(msg["rate_dB_per_s"]))}
            elif cmd == "ramp_stop":
                # no `knob`: every sweep stops (the safest reading of "stop")
                return {"ok": True, "stopped": self.src.ramp_stop(msg.get("knob") or None)}
            elif cmd == "stream_start":
                return {"ok": True, "stream_id": self.src.stream_start()}
            elif cmd == "stream_read":
                return {"ok": True, "stream": self.src.stream_read()}
            elif cmd == "stream_stop":
                return {"ok": True, "stream": self.src.stream_stop()}
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(self.src)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(self.src.cfg)}
            elif cmd == "set_config":
                apply_config_dict(self.src.cfg, msg["config"])
                self.src.apply_config()
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill
                # us (gotcha #25): serve_forever's finally: stop() switches the
                # RF off and closes the instrument.
                # keep_outputs=true: a restart for a code update -- close and
                # release everything, but leave the RF output as it is.
                self._keep_outputs = _as_bool(msg.get("keep_outputs", False))
                self._stop.set()
                return {"ok": True, "stopping": True,
                        "kept_outputs": self._keep_outputs}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        lim = self.src.cfg.limits
        hw = self.src.cfg.hardware
        st = self.src.status()
        return {
            "idn": st.idn,
            "model": "HP 8648D",
            # the EFFECTIVE limits (envelope inside the instrument's range),
            # the same numbers describe publishes
            "freq_min_Hz": self.src.freq_limits()[0],
            "freq_max_Hz": self.src.freq_limits()[1],
            "power_min_dBm": self.src.power_floor(),
            "power_max_dBm": lim.power_max_dBm,
            "power_ceiling_dBm": self.src.power_ceiling(),
            "enforce_spec_ceiling": lim.enforce_spec_ceiling,
            "option_1ea": hw.option_1ea,
            "freq_resolution_Hz": spec.FREQ_RESOLUTION_HZ,
            "power_resolution_dB": spec.POWER_RESOLUTION_DB,
        }


def _bool(v) -> bool:
    """A bool from JSON, or from a hand-typed "off" (gotcha #3)."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _as_bool(v) -> bool:
    """A JSON bool, or text such as "false" from a hand-typed console command.
    bool("false") would be True -- the same trap as gotcha #3."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
