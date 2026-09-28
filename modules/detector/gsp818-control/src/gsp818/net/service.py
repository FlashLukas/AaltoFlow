"""The service: wrap a SpectrumAnalyzer and expose it over ZeroMQ.

One process owns the analyser (simulated or real). Two extra threads, as in every module:
  * publisher  -- owns the PUB socket; a status frame at `status_hz`, events as
                  they happen (a ZeroMQ socket must stay on one thread).
  * commander  -- owns the REP socket; JSON command in, dispatch, JSON reply.
The analyser's own sweep thread does the measuring.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from ..analyzer import SpectrumAnalyzer
from ..model import DETECTORS
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, json_safe, trace_to_wire)


class Gsp818Service:
    def __init__(self, gsp818: SpectrumAnalyzer,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 10.0):
        self.gsp818 = gsp818
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
        self.gsp818._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        self.gsp818.start()
        self._pub_t = threading.Thread(target=self._publisher, name="svc-pub", daemon=True)
        self._cmd_t = threading.Thread(target=self._commander, name="svc-cmd", daemon=True)
        self._pub_t.start()
        self._cmd_t.start()

    def serve_forever(self) -> None:
        self.start()
        kind = "simulated" if self.gsp818.simulated else "REAL"
        print(f"gsp818 service up ({kind})  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
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
        self.gsp818.shutdown()
        print("gsp818 service stopped")

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for both the PUB frame and the
        `status` reply -- a field in only one of them vanishes intermittently."""
        st = status_to_dict(self.gsp818.status())
        st["describe_rev"] = self.describe_rev()
        return st

    def describe_rev(self, max_age_s: float = 0.5) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.
        It changes with the frequency range (start, stop, centre and span bound
        each other) and the point count."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.gsp818)["revision"]
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
        v = self.gsp818
        # verb -> (brain method, argument name, cast). One table instead of a
        # long if/elif: a new setter is one line, and the argument name the
        # wire expects sits next to the method it feeds.
        setters = {
            "set_start": (v.set_start, "start_Hz", float),
            "set_stop": (v.set_stop, "stop_Hz", float),
            "set_center": (v.set_center, "center_Hz", float),
            "set_span": (v.set_span, "span_Hz", float),
            "set_points": (v.set_points, "points", _int),
            "set_rbw": (v.set_rbw, "rbw_Hz", float),
            "set_rbw_auto": (v.set_rbw_auto, "on", _bool),
            "set_vbw": (v.set_vbw, "vbw_Hz", float),
            "set_vbw_auto": (v.set_vbw_auto, "on", _bool),
            "set_ref_level": (v.set_ref_level, "ref_level_dBm", float),
            "set_atten": (v.set_atten, "atten_dB", float),
            "set_atten_auto": (v.set_atten_auto, "on", _bool),
            "set_sweep_time": (v.set_sweep_time, "sweep_time_s", float),
            "set_sweep_time_auto": (v.set_sweep_time_auto, "on", _bool),
            "set_detector": (v.set_detector, "detector", str),
            "set_preamp": (v.set_preamp, "on", _bool),
            "set_averages": (v.set_averages, "averages", _int),
            "set_continuous": (v.set_continuous, "on", _bool),
            "set_tg": (v.set_tg, "on", _bool),
            "set_tg_level": (v.set_tg_level, "level_dBm", float),
            "set_dut": (v.set_dut, "dut", str),
            "set_carriers": (v.set_carriers, "carriers", str),
        }
        try:
            if cmd in setters:
                fn, arg, cast = setters[cmd]
                fn(cast(msg[arg]))
            elif cmd == "set_bench":
                v.set_bench(str(msg["name"]), float(msg["value"]))
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
                    t = v.get_trace(str(msg.get("which", "sample")),
                                    str(msg.get("quantity", "power")))
                except ValueError as exc:
                    # "no reference", "aborted", "does not match": not a malformed
                    # request, so no "bad request" prefix -- the reason is the message
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, **trace_to_wire(t)}
            elif cmd == "get_frequencies":
                f = v.frequencies()
                return {"ok": True, "values": f.tolist(), "values_MHz": (f / 1e6).tolist()}
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
                # (suite gotcha #25): the brain's shutdown switches the tracking
                # generator off. The reply still goes out: the commander sends it
                # before it looks at _stop again.
                self._stop.set()
                return {"ok": True, "stopping": True}
            else:
                return {"ok": False, "error": f"unknown command: {cmd!r}"}
            return {"ok": True}
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"bad {cmd} request: {exc}"}

    def _info(self) -> dict:
        lim = self.gsp818.cfg.limits
        return {
            "idn": self.gsp818.status().idn, "simulated": self.gsp818.simulated,
            "detectors": list(DETECTORS),
            "freq_min_Hz": lim.freq_min_Hz, "freq_max_Hz": lim.freq_max_Hz,
            "points_min": lim.points_min, "points_max": lim.points_max,
            "rbw_min_Hz": lim.rbw_min_Hz, "rbw_max_Hz": lim.rbw_max_Hz,
            "vbw_min_Hz": lim.vbw_min_Hz, "vbw_max_Hz": lim.vbw_max_Hz,
            "ref_level_min_dBm": lim.ref_level_min_dBm, "ref_level_max_dBm": lim.ref_level_max_dBm,
            "atten_max_dB": lim.atten_max_dB,
            "tg_level_min_dBm": lim.tg_level_min_dBm, "tg_level_max_dBm": lim.tg_level_max_dBm,
        }


def _int(x) -> int:
    return int(round(float(x)))


def _bool(x) -> bool:
    """JSON true/false, but also "off"/"0" typed by a human in a console:
    bool("off") would be True (the same trap as gotcha #3)."""
    if isinstance(x, str):
        return x.strip().lower() in ("1", "true", "yes", "on")
    return bool(x)


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
