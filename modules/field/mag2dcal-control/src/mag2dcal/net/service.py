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
import queue
import threading
import time

import zmq

from ..controller import Controller, Refused
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, calibration_to_dict,
                       calibration_from_dict)


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
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()
        self._rev = 0
        self._rev_at = -1e9
        self._started = False

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Start the magnet, then the sockets. If the magnet refuses to start
        (water interlock) the exception propagates and NO socket is opened."""
        self.ctrl._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        self.ctrl.start()
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
        self.ctrl.shutdown()
        print("mag2dcal service stopped, output at 0 V and disabled")

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for the PUB frame and the
        `status` reply -- a field in only one of them vanishes intermittently
        (the describe_rev bug found in clMag)."""
        st = status_to_dict(self.ctrl.status())
        st["describe_rev"] = self.describe_rev()
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
        pub = self._ctx.socket(zmq.PUB)
        pub.bind(self.pub_addr)
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
        rep = self._ctx.socket(zmq.REP)
        rep.bind(self.cmd_addr)
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(200):
                try:
                    msg = rep.recv_json()
                    rep.send_json(self._dispatch(msg))
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
            elif cmd == "set_output":
                c.set_output(_as_bool(msg["enabled"]))
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
                self._stop.set()
                return {"ok": True, "stopping": True}
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
