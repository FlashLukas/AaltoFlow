"""The service: wrap the vector-magnet Controller and expose it over ZeroMQ.

One process owns the DAQ (or the simulator) and the Controller. Two extra
threads, as in every module:
  * publisher  -- owns the PUB socket; a status frame at `status_hz`, and
                  controller events as they happen (a ZeroMQ socket must stay on
                  one thread).
  * commander  -- owns the REP socket; JSON command in, dispatch, JSON reply.
The Controller's own loop thread does all the hardware I/O.

Bind to tcp://0.0.0.0:<port> and the same code serves localhost and the lab LAN.
"""

from __future__ import annotations

import json
import json as _json_mod
import queue
import threading
import time

import zmq

from .. import secure
from ..control import ControlLease
from ..controller import Controller, Refused
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, calibration_to_dict,
                       calibration_from_dict)


class PortInUse(RuntimeError):
    """A command or status port is already taken (a second copy of this
    service, or an orphan still holding it -- gotcha #7). Raised by start()
    BEFORE the instrument is opened, so there is nothing to close; the
    run_service script turns it into one line on stderr and exit code 2."""


class Mag2dcalService:
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
        self._rev = 0
        self._rev_at = -1e9
        self._started = False
        # One controller, many viewers (control.py, docs/DEVELOPER_NOTES.md
        # section 4 "Control"): the gate every command passes.
        #   SAFETY = verbs a VIEWER may always send, because they can only make
        #   the magnet safer: `zero` (field setpoint 0 mT; it only lowers, the
        #   controller allows it even during a FAULT, and it aborts a running
        #   calibration sweep -- the panic button) and `output_off` (ramp the
        #   coils to 0 V and switch the enable line off). `set_output`
        #   is NOT in the list even though it can switch off -- with
        #   enabled=true it energizes the coils; that is why `output_off` is a
        #   verb of its own. `clear_fault` and `set_water_bypass` are not
        #   safety either: both can lead to the coils being driven again; nor
        #   are `calibrate` (drives both axes to full field), `set_calibration`
        #   and `set_stabilizer` (they change how the field is reached).
        #   READ = none: every read-only verb here is already status/info/
        #   describe/get_config.
        #   `ramp_stop` (end a sweep where it is; the loop holds the field
        #   there) is a stop, so a viewer may send it too; `stream_read` only
        #   reads the record of the field readings.
        self.control = ControlLease(
            safety={"zero", "output_off", "ramp_stop"},
            read={"stream_read"},
            on_event=lambda level, msg: self._events.put({"level": level, "msg": msg}))

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Bind the sockets, then start the magnet. If the magnet refuses to
        start (water interlock) both sockets are closed again and the exception
        propagates, so no half-started service is left listening."""
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
        # keys"): when the lab's policy secures mag2dcal, both sockets become
        # CurveZMQ servers -- only PCs in the keyring can connect, and every
        # request is checked against the key that sent it. Must happen before
        # bind. With security off (the default) nothing changes.
        try:
            self._guard = secure.secure_server(
                self._ctx, [self._rep_sock, self._pub_sock], "mag2dcal",
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
        self._started = True
        self._pub_t = threading.Thread(target=self._publisher, name="svc-pub", daemon=True)
        self._cmd_t = threading.Thread(target=self._commander, name="svc-cmd", daemon=True)
        self._pub_t.start()
        self._cmd_t.start()

    def serve_forever(self) -> None:
        self.start()
        print(f"mag2dcal service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\nstopping ... (ramping the output to 0 V)")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        if not self._started:
            return
        self._started = False
        # Let the publisher send the last events, then ramp down and close. The
        # sockets are already closing; the ramp does not need them.
        time.sleep(self.status_dt + 0.1)
        secure.release_server(self._guard)
        self._guard = None
        if self._keep_outputs:
            print("shutdown: outputs left as they are (restart)")
        self.ctrl.shutdown(keep_outputs=self._keep_outputs)
        print("mag2dcal service stopped, output left as it is" if self._keep_outputs
              else "mag2dcal service stopped, output at 0 V and disabled")

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for the PUB frame and the
        `status` reply -- a field in only one of them vanishes intermittently
        (the describe_rev bug found in clMag)."""
        st = status_to_dict(self.ctrl.status())
        st["describe_rev"] = self.describe_rev()
        # who holds control, who is watching (every control bar reads this)
        st["control"] = self.control.status()
        return st

    def describe_rev(self, max_age_s: float = 1.0) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`
        (limits move when set_config changes field_max or the tolerance)."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.ctrl)["revision"]
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
                except Exception as exc:                  # never let the loop die
                    self._events.put({"level": "error", "msg": f"status publish failed: {exc}"})
                last = now
            time.sleep(0.01)
        pub.close(linger=200)

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
        c = self.ctrl
        try:
            if cmd == "set_field":
                angle = msg.get("angle_deg")
                c.set_field(float(msg["field_mT"]), None if angle is None else float(angle))
            elif cmd == "set_angle":
                c.set_angle(float(msg["angle_deg"]))
            elif cmd == "set_vector":
                c.set_vector(float(msg["bx_mT"]), float(msg["by_mT"]))
            elif cmd == "set_bx":
                c.set_bx(float(msg["bx_mT"]))
            elif cmd == "set_by":
                c.set_by(float(msg["by_mT"]))
            elif cmd == "zero":
                c.zero()
            elif cmd == "ramp_field":
                # the SWEEPS (fly scans): the reply's ramp_id is what status
                # `ramp_id` shows while, and after, this sweep runs
                return {"ok": True, "ramp_id": c.ramp_field(
                    float(msg["field_mT"]), float(msg["rate_mT_per_s"]))}
            elif cmd == "ramp_angle":
                return {"ok": True, "ramp_id": c.ramp_angle(
                    float(msg["angle_deg"]), float(msg["rate_deg_per_s"]))}
            elif cmd == "ramp_stop":
                return {"ok": True, "stopped": c.ramp_stop()}
            elif cmd == "stream_start":
                return {"ok": True, "stream_id": c.stream_start()}
            elif cmd == "stream_read":
                return {"ok": True, "stream": c.stream_read()}
            elif cmd == "stream_stop":
                return {"ok": True, "stream": c.stream_stop()}
            elif cmd == "set_output":
                c.set_output(_as_bool(msg["enabled"]))
            elif cmd == "output_off":
                # the SAFETY verb: set_output(false) under a name of its own,
                # so a viewer may send it (it can never energize anything)
                c.output_off()
            elif cmd == "set_water_bypass":
                c.set_water_bypass(_as_bool(msg["enabled"]))
            elif cmd == "set_stabilizer":
                c.set_stabilizer(_as_bool(msg["enabled"]))
            elif cmd == "clear_fault":
                c.clear_fault()
            elif cmd == "calibrate":
                c.calibrate(msg.get("n_per_leg"), msg.get("dwell_s"), msg.get("v_max"))
                self._rev_at = -1e9            # the field limits will move
            elif cmd == "get_calibration":
                return {"ok": True, "calibration": calibration_to_dict(c.get_calibration())}
            elif cmd == "set_calibration":
                c.set_calibration(calibration_from_dict(msg.get("calibration")))
                self._rev_at = -1e9            # the field limits just moved
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(c)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(c.cfg)}
            elif cmd == "set_config":
                apply_config_dict(c.cfg, msg["config"])
                c.apply_config()
                self._rev_at = -1e9            # limits may have moved: recompute now
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us.
                # serve_forever's finally: stop() ramps the coils down and closes
                # the DAQ -- a killed process could do neither (gotcha #25).
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
        except Refused as exc:
            return {"ok": False, "error": str(exc)}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        cfg = self.ctrl.cfg
        return {
            "module": "mag2dcal",
            # The LIVE envelope (the calibration may narrow it), because this is
            # what scan-core reads to bound a sweep.
            "field_max_mT": self.ctrl.field_envelope_mT(),
            "field_max_config_mT": cfg.limits.field_max_mT,
            "calibrated": self.ctrl.is_calibrated,
            "angle_min_deg": cfg.limits.angle_min_deg,
            "angle_max_deg": cfg.limits.angle_max_deg,
            "ao_limit_V": cfg.limits.ao_limit_V,
            "tolerance_mT": cfg.control.tolerance_mT,
            "stable_time_s": cfg.control.stable_time_s,
            "slew_V_per_s": cfg.control.slew_V_per_s,
            "settle_timeout_s": cfg.control.settle_timeout_s,
            "freeze_enabled": cfg.control.freeze_enabled,
            "field_step_mT": cfg.control.field_step_mT,
            "simulated": type(self.ctrl.backend).__name__.startswith("Sim"),
        }


def _as_bool(v) -> bool:
    """JSON true/false, but also "false"/0 from a hand-typed console command --
    bool("false") is True (the INI gotcha again)."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
