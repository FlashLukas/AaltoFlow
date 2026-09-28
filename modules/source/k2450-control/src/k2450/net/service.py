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

from ..smu import SourceMeter
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, json_safe)


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

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.smu._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        self.smu.start()
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
        self.smu.shutdown()                  # output OFF, then disconnect
        print("k2450 service stopped, output off, instrument closed")

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for both the PUB frame and the
        `status` reply -- a field in only one of them vanishes intermittently."""
        st = status_to_dict(self.smu.status())
        st["describe_rev"] = self.describe_rev()
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
        # linger, not 0: after `shutdown` the reply may still be queued, and
        # close(0) would drop it -- the launcher would then kill us anyway.
        rep.close(linger=500)

    # -------------------------------------------------------------- dispatch

    def _dispatch(self, msg: dict) -> dict:
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
                self._stop.set()
                return {"ok": True, "stopping": True}
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


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
