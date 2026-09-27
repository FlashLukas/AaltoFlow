"""The service: wrap a PowerMeter and expose it over ZeroMQ.

One process owns the meter. Two extra threads, as in every module:
  * publisher  -- owns the PUB socket; a status frame at `status_hz`, events as
                  they happen (a ZeroMQ socket must stay on one thread).
  * commander  -- owns the REP socket; JSON command in, dispatch, JSON reply.
The PowerMeter's own polling thread does the actual reading.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from ..meter import PowerMeter
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, json_safe)


class Pm16Service:
    def __init__(self, meter: PowerMeter,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 10.0):
        self.meter = meter
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
        self.meter._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        self.meter.start()
        self._pub_t = threading.Thread(target=self._publisher, name="svc-pub", daemon=True)
        self._cmd_t = threading.Thread(target=self._commander, name="svc-cmd", daemon=True)
        self._pub_t.start()
        self._cmd_t.start()

    def serve_forever(self) -> None:
        self.start()
        print(f"pm16 service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
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
        self.meter.shutdown()
        print("pm16 service stopped, meter closed")

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for both the PUB frame and the
        `status` reply -- a field in only one of them vanishes intermittently."""
        st = status_to_dict(self.meter.status())
        st["describe_rev"] = self.describe_rev()
        return st

    def describe_rev(self, max_age_s: float = 0.5) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.
        It changes when auto-range is toggled (range becomes a control)."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.meter)["revision"]
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
        # and close(0) would throw it away -- the launcher would then wait for
        # nothing and fall back to killing us.
        rep.close(linger=500)

    # -------------------------------------------------------------- dispatch

    def _dispatch(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        m = self.meter
        try:
            if cmd == "set_wavelength":
                m.set_wavelength(float(msg["wavelength_nm"]))
            elif cmd == "set_auto_range":
                m.set_auto_range(bool(msg["on"]))
            elif cmd == "set_range":
                m.set_range(float(msg["range_W"]))
            elif cmd == "set_acquisition":
                m.set_acquisition(int(msg["readings"]))
            elif cmd == "zero":
                m.zero()
            elif cmd == "cancel_zero":
                m.cancel_zero()
            elif cmd == "acquire":
                return {"ok": True, "acq_id": m.acquire()}
            elif cmd == "get_sample":
                return {"ok": True, "sample": json_safe(m.get_sample())}
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(m)}
            elif cmd == "info":
                return {"ok": True, "info": json_safe(self._info())}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(m.cfg)}
            elif cmd == "set_config":
                apply_config_dict(m.cfg, msg["config"])
                m.apply_config()
            elif cmd == "shutdown":
                # A CLEAN stop, asked for by the launcher before it would kill us.
                # A hard kill almost always lands inside a 58 ms USB read (the
                # poll thread reads continuously), and that leaves the PM16 stuck
                # with "I/O error" until it is unplugged -- measured 2026-09-15.
                # Setting _stop ends serve_forever, which closes the meter; the
                # reply still goes out, because the commander sends it before it
                # looks at _stop again.
                self._stop.set()
                return {"ok": True, "stopping": True}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        st = self.meter.status()
        return {
            "idn": st.idn, "sensor": st.sensor,
            "wavelength_min_nm": st.wavelength_min_nm,
            "wavelength_max_nm": st.wavelength_max_nm,
            "range_min_W": st.range_min_W, "range_max_W": st.range_max_W,
            "average_time_s": st.average_time_s,
        }


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
