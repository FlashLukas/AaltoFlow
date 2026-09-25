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

import queue
import threading
import time

import zmq

from ..controller import Controller
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, calibration_to_dict, calibration_from_dict)


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
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()
        # Cached describe revision. Every status frame carries it so clients can
        # tell, for the cost of a integer compare, whether their cached manifest
        # went stale -- clMag's field limits ARE the calibration range, so they
        # move the moment a calibration is loaded or measured. Recomputed at
        # most once a second; rebuilding the manifest at the 10 Hz status rate
        # would be pure waste.
        self._rev = 0
        self._rev_at = 0.0

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        # route controller events into the publisher queue
        self.ctrl._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        self.ctrl.start()
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
        self.ctrl.shutdown()

    # ---------------------------------------------------------------- threads

    def _publisher(self) -> None:
        pub = self._ctx.socket(zmq.PUB)
        pub.bind(self.pub_addr)
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
        return st

    def describe_rev(self, max_age_s: float = 1.0) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.ctrl)["revision"]
            self._rev_at = now
        return self._rev

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
        try:
            if cmd == "set_field":
                self.ctrl.set_field(float(msg["field_mT"]), bool(msg.get("use_pid", True)))
            elif cmd == "set_current":
                self.ctrl.set_current(float(msg["current_A"]))
            elif cmd == "demag":
                self.ctrl.demag(float(msg["amplitude_A"]))
            elif cmd == "calibrate":
                self.ctrl.calibrate(int(msg.get("n_per_leg", 50)), float(msg.get("dwell_s", 0.5)))
            elif cmd == "set_lock":
                self.ctrl.set_lock(bool(msg["locked"]))
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
                self._stop.set()
                return {"ok": True, "stopping": True}
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
