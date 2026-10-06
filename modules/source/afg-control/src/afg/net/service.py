"""The service: wrap a Generator and expose it over ZeroMQ.

One process owns the instrument (or the simulator) and the Generator. It runs
two extra threads, like every service in the suite:
  * publisher  -- owns the PUB socket; sends a status frame at `status_hz` and
                  forwards generator events as they happen (one socket,
                  because a ZeroMQ socket must be used from a single thread).
  * commander  -- owns the REP socket; receives a JSON command, dispatches it to
                  the generator, and replies.

(The generator has a third thread of its own, the worker that owns the USB
connection -- see generator.py.)

Commands (a reply means ACCEPTED, not done -- poll status for the effect):
    set_output        {channel: "ch1"|"ch2", on: bool}
    set_waveform      {channel, waveform: "sine"|"square"|"pulse"|"ramp"|"noise"|"dc"}
    set_frequency     {channel, frequency_Hz}
    set_amplitude     {channel, amplitude_Vpp}
    set_offset        {channel, offset_V}
    set_phase         {channel, phase_deg}
    set_duty          {channel, duty_pct}          (pulse)
    set_symmetry      {channel, symmetry_pct}      (ramp)
    set_load          {channel, load: "50"|"high-Z"|<ohm>}
    set_follow        {on: bool, phase_offset_deg?}   CH2 follows CH1
    set_phase_offset  {deg}
    align_phase       {}  -> {"op_id": n}
    outputs_off       {}  -> {"op_id": n}           the safety verb
  + the universal verbs status, info, get_config, set_config, describe, shutdown.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from .. import secure
from ..control import ControlLease
from ..generator import Generator
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class AfgService:
    def __init__(self, gen: Generator,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 5.0):
        self.gen = gen
        self._rev = 0
        self._rev_at = -1e9
        self.cmd_addr = f"tcp://{host}:{cmd_port}"
        self.pub_addr = f"tcp://{host}:{pub_port}"
        self.status_dt = 1.0 / status_hz
        self._stop = threading.Event()
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()
        self._guard = None                   # secure.Guard while secured
        self._stopped = False
        # set by shutdown{keep_outputs: true}: a RESTART (code update) that must
        # not switch off what the instrument outputs; the next start adopts it
        self._keep_outputs = False
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = verbs a VIEWER may always send. For a function generator
        #   (CH1 may drive a magnet amplifier) the one "make it safe" action
        #   is outputs off, and `outputs_off` can ONLY do that: a viewer who
        #   sees the drive do something it should not must be able to stop
        #   it. `set_output` is NOT in the list even though on=false is the
        #   same thing for one channel -- the same verb also switches an
        #   output ON.
        #   READ: none beyond get_/read_/list_ and the universal verbs.
        self.control = ControlLease(
            safety={"outputs_off"},
            read=set(),
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
        # keys"): when the lab's policy secures afg, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "afg",
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
        # route generator events into the publisher queue
        self.gen._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.gen.start()
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
        print(f"afg service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
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
        for t in (getattr(self, "_pub_t", None), getattr(self, "_cmd_t", None)):
            if t is not None:
                t.join(timeout=2.0)
        secure.release_server(self._guard)   # authenticator + marker go too
        self._guard = None
        if self._keep_outputs:
            print("shutdown: outputs left as they are (restart)")
        # every output OFF, then disconnect -- unless this is a restart
        self.gen.shutdown(keep_outputs=self._keep_outputs)

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = _nan_to_none(status_to_dict(self.gen.status()))
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
            self._rev = build_manifest(self.gen)["revision"]
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
        # Who may change what (control.py): the gate answers the control verbs
        # itself and refuses a change from a viewer; anything else goes on.
        gate = self.control.handle(msg)
        if gate is not None:
            return gate
        cmd = msg.get("cmd")
        g = self.gen
        try:
            ch = msg.get("channel")
            if cmd == "set_output":
                g.set_output(ch, _as_bool(msg["on"]))
            elif cmd == "set_waveform":
                g.set_waveform(ch, str(msg["waveform"]))
            elif cmd == "set_frequency":
                g.set_frequency(ch, float(msg["frequency_Hz"]))
            elif cmd == "set_amplitude":
                g.set_amplitude(ch, float(msg["amplitude_Vpp"]))
            elif cmd == "set_offset":
                g.set_offset(ch, float(msg["offset_V"]))
            elif cmd == "set_phase":
                g.set_phase(ch, float(msg["phase_deg"]))
            elif cmd == "set_duty":
                g.set_duty(ch, float(msg["duty_pct"]))
            elif cmd == "set_symmetry":
                g.set_symmetry(ch, float(msg["symmetry_pct"]))
            elif cmd == "set_load":
                g.set_load(ch, msg["load"])
            elif cmd == "set_follow":
                off = msg.get("phase_offset_deg")
                g.set_follow(_as_bool(msg["on"]), None if off is None else float(off))
            elif cmd == "set_phase_offset":
                g.set_phase_offset(float(msg["deg"]))
            elif cmd == "align_phase":
                return {"ok": True, "op_id": g.align_phase()}
            elif cmd == "outputs_off":
                return {"ok": True, "op_id": g.outputs_off()}
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(g)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(g.cfg)}
            elif cmd == "set_config":
                apply_config_dict(g.cfg, msg["config"])
                g.apply_config()
                self._rev_at = -1e9       # limits may have moved: recompute now
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us: a
                # hard kill gives the brain no chance to switch the outputs off
                # (docs/DEVELOPER_NOTES.md gotcha #25). Setting _stop ends
                # serve_forever, whose finally: stop() shuts the brain down.
                # keep_outputs=true: a restart for a code update -- close and
                # release everything, but leave the outputs as they are.
                self._keep_outputs = _as_bool(msg.get("keep_outputs", False))
                self._stop.set()
                return {"ok": True, "stopping": True,
                        "kept_outputs": self._keep_outputs}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            if cmd in ("set_waveform", "set_load", "set_follow"):
                self._rev_at = -1e9       # the manifest's SHAPE / ranges follow these
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        g = self.gen
        return {
            "idn": g.status().get("idn", ""),
            "model": g.caps.get("model", ""),
            "channels": list(g.channels),
            "waveforms": list(g.caps.get("waveforms", ())),
            "limits": {ch: dict(vars(g.cfg.limits(ch))) for ch in g.channels},
            "envelope": {ch: g.envelope(ch) for ch in g.channels},
        }


def _as_bool(v) -> bool:
    """A JSON bool, or text such as "false" from a hand-typed console command.
    bool("false") would be True -- the same trap as gotcha #3."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _json(d: dict) -> bytes:
    # NaN (no read-back yet) is not valid JSON for strict readers: send null
    return json.dumps(_nan_to_none(d)).encode("utf-8")


def _nan_to_none(d: dict) -> dict:
    return {k: (None if isinstance(v, float) and v != v else v) for k, v in d.items()}
