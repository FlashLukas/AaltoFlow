"""The service: wrap a Synthesizer and expose it over ZeroMQ.

One process owns the instrument (or the simulator) and the Synthesizer. It runs
two extra threads, like every service in the suite:
  * publisher  -- owns the PUB socket; sends a status frame at `status_hz` and
                  forwards synthesizer events as they happen (one socket,
                  because a ZeroMQ socket must be used from a single thread).
  * commander  -- owns the REP socket; receives a JSON command, dispatches it to
                  the synthesizer, and replies.

(The synthesizer has a third thread of its own, the worker that owns the serial
port -- see synthesizer.py.)

Bind to tcp://0.0.0.0:<port> and the same code serves a client on localhost or
across the lab network -- only the address the client dials changes.

Commands (a reply means ACCEPTED, not done -- poll status for the effect):
    set_rf        {channel: "a"|"b", on: bool}
    set_frequency {channel, frequency_Hz}
    set_power     {channel, power_dBm}
    set_phase     {channel, phase_deg}
    set_reference {source: "internal_10MHz"|"internal_27MHz"|"external", ext_MHz?}
    set_ext_ref   {ext_MHz}
    all_rf_off    {}
  The SWEEPS (fly scans; the reply carries `ramp_id`):
    ramp_frequency {channel, frequency_Hz, rate_Hz_per_s}
    ramp_power     {channel, power_dBm, rate_dB_per_s}
    ramp_phase     {channel, phase_deg, rate_deg_per_s}
    ramp_stop      {knob?}   e.g. "a_frequency"; none = every sweep
    stream_start / stream_read / stream_stop   the record of every value sent
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
from ..synthesizer import Synthesizer, CHANNELS
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class WindfreakService:
    def __init__(self, synth: Synthesizer,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 5.0):
        self.synth = synth
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
        self._stopped = False
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = verbs a VIEWER may always send. For an RF synthesizer the
        #   one "make it safe" action is RF off; this module already has a
        #   verb that can ONLY do that, `all_rf_off` (both outputs off -- the
        #   GUI's "All RF off" button, already an action in describe): a
        #   viewer who sees an output hit something it should not must be able
        #   to take it away. `set_rf` is NOT in the list even though on=false
        #   is the same thing for one channel -- the same verb also switches
        #   an output ON. `ramp_stop` ends a sweep (frequency, power or phase
        #   of either channel) where it is: a stop, so a viewer may send it too.
        #   READ: `stream_read` only reads the sweeps' record.
        self.control = ControlLease(
            safety={"all_rf_off", "ramp_stop"},
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
        # keys"): when the lab's policy secures windfreak, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "windfreak",
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
        # route synthesizer events into the publisher queue
        self.synth._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        try:
            self.synth.start()
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
        print(f"windfreak service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
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
        # both outputs OFF, then disconnect -- unless this is a restart
        self.synth.shutdown(keep_outputs=self._keep_outputs)

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = _nan_to_none(status_to_dict(self.synth.status()))
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
            self._rev = build_manifest(self.synth)["revision"]
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
        s = self.synth
        try:
            if cmd == "set_rf":
                s.set_rf(msg["channel"], _as_bool(msg["on"]))
            elif cmd == "set_frequency":
                s.set_frequency(msg["channel"], float(msg["frequency_Hz"]))
            elif cmd == "set_power":
                s.set_power(msg["channel"], float(msg["power_dBm"]))
            elif cmd == "set_phase":
                s.set_phase(msg["channel"], float(msg["phase_deg"]))
            elif cmd == "set_reference":
                ext = msg.get("ext_MHz")
                s.set_reference(str(msg["source"]), None if ext is None else float(ext))
            elif cmd == "set_ext_ref":
                s.set_ext_ref(float(msg["ext_MHz"]))
            elif cmd == "all_rf_off":
                s.all_rf_off()
            # The SWEEPS (fly scans): the reply's ramp_id is what status
            # `<channel>_<knob>_ramp_id` shows while and after this sweep runs.
            elif cmd == "ramp_frequency":
                return {"ok": True, "ramp_id": s.ramp_frequency(
                    msg["channel"], float(msg["frequency_Hz"]), float(msg["rate_Hz_per_s"]))}
            elif cmd == "ramp_power":
                return {"ok": True, "ramp_id": s.ramp_power(
                    msg["channel"], float(msg["power_dBm"]), float(msg["rate_dB_per_s"]))}
            elif cmd == "ramp_phase":
                return {"ok": True, "ramp_id": s.ramp_phase(
                    msg["channel"], float(msg["phase_deg"]), float(msg["rate_deg_per_s"]))}
            elif cmd == "ramp_stop":
                # no `knob`: every sweep of both channels stops (the safest
                # reading of "stop")
                return {"ok": True, "stopped": s.ramp_stop(msg.get("knob") or None)}
            elif cmd == "stream_start":
                return {"ok": True, "stream_id": s.stream_start()}
            elif cmd == "stream_read":
                return {"ok": True, "stream": s.stream_read()}
            elif cmd == "stream_stop":
                return {"ok": True, "stream": s.stream_stop()}
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
                self._rev_at = -1e9       # limits may have moved: recompute now
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us: a
                # hard kill gives the brain no chance to switch the RF off
                # (docs/DEVELOPER_NOTES.md gotcha #25). Setting _stop ends
                # serve_forever, whose finally: stop() shuts the brain down.
                # keep_outputs=true: a restart for a code update -- close and
                # release everything, but leave both RF outputs as it is.
                self._keep_outputs = _as_bool(msg.get("keep_outputs", False))
                self._stop.set()
                return {"ok": True, "stopping": True,
                        "kept_outputs": self._keep_outputs}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            if cmd in ("set_reference", "set_ext_ref"):
                self._rev_at = -1e9       # the manifest's SHAPE follows the reference
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        lim = self.synth.cfg.limits
        st = self.synth.status()
        return {
            "idn": st.get("idn", ""),
            "channels": list(CHANNELS),
            "freq_min_Hz": lim.freq_min_Hz,
            "freq_max_Hz": lim.freq_max_Hz,
            "power_min_dBm": lim.power_min_dBm,
            "power_max_dBm": lim.power_max_dBm,
            "phase_min_deg": lim.phase_min_deg,
            "phase_max_deg": lim.phase_max_deg,
            "ext_ref_min_MHz": lim.ext_ref_min_MHz,
            "ext_ref_max_MHz": lim.ext_ref_max_MHz,
        }


def _as_bool(v) -> bool:
    """A JSON bool, or text such as "false" from a hand-typed console command.
    bool("false") would be True -- the same trap as gotcha #3."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _json(d: dict) -> bytes:
    # NaN (no temperature yet) is not valid JSON for strict readers: send null
    return json.dumps(_nan_to_none(d)).encode("utf-8")


def _nan_to_none(d: dict) -> dict:
    return {k: (None if isinstance(v, float) and v != v else v) for k, v in d.items()}
