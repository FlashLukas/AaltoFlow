"""The service: wrap a SourceMeter and expose it over ZeroMQ.

One process owns the instrument. Two extra threads, as in every module:
  * publisher  -- owns the PUB socket; a status frame at `status_hz`, events as
                  they happen (a ZeroMQ socket must stay on one thread).
  * commander  -- owns the REP socket; JSON command in, dispatch, JSON reply.
The SourceMeter's own polling thread takes the readings.

A reply {"ok": true} means ACCEPTED, not done: after set_voltage, wait for
status `settled` (scan-core does this from `describe`).
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from .. import secure
from ..control import ControlLease
from ..smu import SourceMeter
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, json_safe)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class K2450Service:
    def __init__(self, smu: SourceMeter,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 10.0):
        self.smu = smu
        self._rev = 0
        self._rev_at = -1e9
        self.cmd_addr = f"tcp://{host}:{cmd_port}"
        self.pub_addr = f"tcp://{host}:{pub_port}"
        self.status_dt = 1.0 / status_hz
        self._stop = threading.Event()
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()
        self._guard = None                   # secure.Guard while secured
        # set by shutdown{keep_outputs: true}: a RESTART (code update) that must
        # not change what the instrument outputs; the next start adopts it
        self._keep_outputs = False
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = verbs a VIEWER may always send. For a SourceMeter the one
        #   "make it safe" action is `output_off` (the GUI's Output OFF): a
        #   viewer who sees a sample driven where it should not be must be able
        #   to take the source away. `set_output` is NOT in the list even
        #   though on=false is the same thing -- the same verb also switches
        #   the output ON. `acquire` is not safety either: a trigger replaces
        #   the sample other clients wait on.
        #   READ: none beyond get_/read_/list_ and the universal verbs.
        self.control = ControlLease(
            # `ramp_stop` ends a level sweep where it is: a stop, so a viewer
            # may send it; `stream_read` only reads the record of readings
            safety={"output_off", "ramp_stop"},
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
        # keys"): when the lab's policy secures k2450, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "k2450",
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
        self.smu._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.smu.start()
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
        print(f"k2450 service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
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
        # output OFF, then disconnect -- unless this is a restart
        self.smu.shutdown(keep_outputs=self._keep_outputs)
        print("k2450 service stopped, "
              + ("output left as it is" if self._keep_outputs else "output off")
              + ", instrument closed")

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for both the PUB frame and the
        `status` reply -- a field in only one of them vanishes intermittently."""
        st = status_to_dict(self.smu.status())
        st["describe_rev"] = self.describe_rev()
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
        return st

    def describe_rev(self, max_age_s: float = 0.2) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.
        It moves with the source function, the ranges and the output boxes."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.smu)["revision"]
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
        s = self.smu
        try:
            if cmd == "set_output":
                s.set_output(bool(msg["on"]))
            elif cmd == "output_off":
                s.output_off()
            elif cmd == "set_source_function":
                s.set_source_function(str(msg["function"]))
            elif cmd == "set_voltage":
                s.set_voltage(float(msg["voltage_V"]))
            elif cmd == "set_current":
                # Two spellings of one verb: amperes for people and scripts, uA
                # for scan-core (describe.py explains why).
                if "current_uA" in msg:
                    s.set_current(float(msg["current_uA"]) * 1e-6)
                else:
                    s.set_current(float(msg["current_A"]))
            elif cmd == "ramp_voltage":
                # the level SWEEPS (fly scans): the reply's ramp_id is what
                # status `ramp_id` shows while, and after, this sweep runs
                return {"ok": True, "ramp_id": s.ramp_voltage(
                    float(msg["voltage_V"]), float(msg["rate_V_per_s"]))}
            elif cmd == "ramp_current":
                # uA + uA/s (scan-core, like set_current's uA spelling) or
                # A + A/s (people and scripts)
                if "current_uA" in msg:
                    amps, rate = float(msg["current_uA"]) * 1e-6, float(msg["rate_uA_per_s"]) * 1e-6
                else:
                    amps, rate = float(msg["current_A"]), float(msg["rate_A_per_s"])
                return {"ok": True, "ramp_id": s.ramp_current(amps, rate)}
            elif cmd == "ramp_stop":
                return {"ok": True, "stopped": s.ramp_stop()}
            elif cmd == "stream_start":
                return {"ok": True, "stream_id": s.stream_start()}
            elif cmd == "stream_read":
                return {"ok": True, "stream": s.stream_read()}
            elif cmd == "stream_stop":
                return {"ok": True, "stream": s.stream_stop()}
            elif cmd == "set_current_limit":
                s.set_current_limit(float(msg["current_limit_A"]))
            elif cmd == "set_voltage_limit":
                s.set_voltage_limit(float(msg["voltage_limit_V"]))
            elif cmd == "set_source_auto_range":
                s.set_source_auto_range(bool(msg["on"]))
            elif cmd == "set_source_range":
                s.set_source_range(float(msg["range"]))
            elif cmd == "set_measure_auto_range":
                s.set_measure_auto_range(bool(msg["on"]))
            elif cmd == "set_measure_range":
                s.set_measure_range(float(msg["range"]))
            elif cmd == "set_nplc":
                s.set_nplc(float(msg["nplc"]))
            elif cmd == "set_four_wire":
                s.set_four_wire(bool(msg["on"]))
            elif cmd == "set_acquisition":
                s.set_acquisition(int(msg["readings"]))
            elif cmd == "acquire":
                return {"ok": True, "acq_id": s.acquire()}
            elif cmd == "get_sample":
                return {"ok": True, "sample": json_safe(s.get_sample())}
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(s)}
            elif cmd == "info":
                return {"ok": True, "info": json_safe(self._info())}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(s.cfg)}
            elif cmd == "set_config":
                # "Not settled" BEFORE the new values become visible in status:
                # a new level written into cfg must never appear as settled.
                s.mark_unsettled()
                apply_config_dict(s.cfg, msg["config"])
                s.apply_config()
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us
                # (gotcha #25): a killed service cannot switch the output off.
                # Setting _stop ends serve_forever, whose finally: stop() turns
                # the output off and closes the instrument; the reply still goes
                # out, because the commander sends it before it checks _stop.
                # keep_outputs=true: a restart for a code update -- close and
                # release everything, but leave the output as it is.
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
        lim = self.smu.cfg.limits
        st = self.smu.status()
        return {
            "idn": st.idn,
            "voltage_max_V": lim.voltage_max_V,
            "current_max_A": lim.current_max_A,
            "box_voltage_V": lim.box_voltage_V,
            "box_current_A": lim.box_current_A,
            "nplc_min": lim.nplc_min, "nplc_max": lim.nplc_max,
            "line_freq_Hz": self.smu.cfg.hardware.line_freq_Hz,
        }


def _as_bool(v) -> bool:
    """A JSON bool, or text such as "false" from a hand-typed console command.
    bool("false") would be True -- the same trap as gotcha #3."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
