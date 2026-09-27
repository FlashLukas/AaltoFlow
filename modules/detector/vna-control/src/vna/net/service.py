"""The service: wrap an Analyzer and expose it over ZeroMQ.

One process owns the analyser (simulated or real). Two extra threads, as in every module:
  * publisher  -- owns the PUB socket; a status frame at `status_hz`, events as
                  they happen (a ZeroMQ socket must stay on one thread).
  * commander  -- owns the REP socket; JSON command in, dispatch, JSON reply.
The Analyzer's own sweep thread does the measuring.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from ..analyzer import Analyzer
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, json_safe, trace_to_wire)


class VnaService:
    def __init__(self, vna: Analyzer,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 10.0):
        self.vna = vna
        self._rev = 0
        self._rev_at = 0.0
        self.cmd_addr = f"tcp://{host}:{cmd_port}"
        self.pub_addr = f"tcp://{host}:{pub_port}"
        self.status_dt = 1.0 / status_hz
        self._stop = threading.Event()
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._ctx = zmq.Context.instance()

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.vna._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        self.vna.start()
        self._pub_t = threading.Thread(target=self._publisher, name="svc-pub", daemon=True)
        self._cmd_t = threading.Thread(target=self._commander, name="svc-cmd", daemon=True)
        self._pub_t.start()
        self._cmd_t.start()

    def serve_forever(self) -> None:
        self.start()
        kind = "simulated" if self.vna.simulated else "REAL"
        print(f"vna service up ({kind})  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
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
        self.vna.shutdown()
        print("vna service stopped")

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for both the PUB frame and the
        `status` reply -- a field in only one of them vanishes intermittently."""
        st = status_to_dict(self.vna.status())
        st["describe_rev"] = self.describe_rev()
        return st

    def describe_rev(self, max_age_s: float = 0.5) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.
        It changes with start/stop (each bounds the other) and the point count."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.vna)["revision"]
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
                pub.send_multipart([TOPIC_STATUS, _json(self.status_payload())])
                last = now
            time.sleep(0.01)
        pub.close(0)

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
        # linger, not 0: after `shutdown` the reply may still be in the queue,
        # and close(0) would throw it away (suite gotcha #25).
        rep.close(linger=500)

    # -------------------------------------------------------------- dispatch

    def _dispatch(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        v = self.vna
        try:
            if cmd == "set_start":
                v.set_start(float(msg["start_Hz"]))
            elif cmd == "set_stop":
                v.set_stop(float(msg["stop_Hz"]))
            elif cmd == "set_points":
                v.set_points(int(round(float(msg["points"]))))
            elif cmd == "set_ifbw":
                v.set_ifbw(float(msg["ifbw_Hz"]))
            elif cmd == "set_power":
                v.set_power(float(msg["power_dBm"]))
            elif cmd == "set_averages":
                v.set_averages(int(round(float(msg["averages"]))))
            elif cmd == "set_sparam":
                v.set_sparam(str(msg["sparam"]))
            elif cmd == "set_continuous":
                v.set_continuous(bool(msg["on"]))
            elif cmd == "set_field_source":
                v.set_field_source(str(msg["source"]))
            elif cmd == "set_manual_field":
                angle = msg.get("angle_deg")
                v.set_manual_field(float(msg["field_mT"]),
                                   None if angle is None else float(angle))
            elif cmd == "set_manual_angle":
                v.set_manual_angle(float(msg["angle_deg"]))
            elif cmd == "set_sample":
                v.set_sample(str(msg["name"]), float(msg["value"]))
            elif cmd == "set_geometry":
                v.set_geometry(str(msg["geometry"]))
            elif cmd == "acquire":
                return {"ok": True, "acq_id": v.acquire()}
            elif cmd == "take_reference":
                return {"ok": True, "acq_id": v.take_reference()}
            elif cmd == "clear_reference":
                v.clear_reference()
            elif cmd == "abort":
                v.abort()
            elif cmd == "get_trace":
                try:
                    t = v.get_trace(str(msg.get("which", "sample")), str(msg.get("quantity", "s")))
                except ValueError as exc:
                    # "no reference", "aborted", "does not match": not a malformed
                    # request, so no "bad request" prefix -- the reason is the message
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, **trace_to_wire(t)}
            elif cmd == "get_frequencies":
                f = v.frequencies()
                return {"ok": True, "values": f.tolist(), "values_GHz": (f / 1e9).tolist()}
            elif cmd == "get_sample":
                return {"ok": True, "sample": json_safe(v.get_sample())}
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(v)}
            elif cmd == "info":
                return {"ok": True, "info": json_safe(self._info())}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(v.cfg)}
            elif cmd == "set_config":
                apply_config_dict(v.cfg, msg["config"])
                v.apply_config()
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us
                # (suite gotcha #25). The reply still goes out: the commander
                # sends it before it looks at _stop again.
                self._stop.set()
                return {"ok": True, "stopping": True}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        lim = self.vna.cfg.limits
        return {
            "idn": self.vna.status().idn, "simulated": self.vna.simulated,
            "sparams": ["S11", "S12", "S21", "S22"],
            "freq_min_Hz": lim.freq_min_Hz, "freq_max_Hz": lim.freq_max_Hz,
            "points_min": lim.points_min, "points_max": lim.points_max,
            "ifbw_min_Hz": lim.ifbw_min_Hz, "ifbw_max_Hz": lim.ifbw_max_Hz,
            "power_min_dBm": lim.power_min_dBm, "power_max_dBm": lim.power_max_dBm,
        }


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
