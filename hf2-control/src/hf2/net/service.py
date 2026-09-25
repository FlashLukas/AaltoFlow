"""The service: wrap a LockIn and expose it over ZeroMQ.

One process owns the instrument (or the simulator). Two daemon threads, as in
every module: a publisher (owns PUB: status at `status_hz` + events) and a
commander (owns REP: one JSON command in, one reply out). The LockIn runs its
own third thread, the demodulator poller; neither of these two ever touches the
hardware directly except through the brain's locked setters.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from ..lockin import LockIn
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict, _json_safe)


class Hf2Service:
    def __init__(self, lockin: LockIn,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 10.0):
        self.lockin = lockin
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
        self.lockin._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        self.lockin.start()
        self._pub_t = threading.Thread(target=self._publisher, name="svc-pub", daemon=True)
        self._cmd_t = threading.Thread(target=self._commander, name="svc-cmd", daemon=True)
        self._pub_t.start()
        self._cmd_t.start()

    def serve_forever(self) -> None:
        self.start()
        # ASCII only: mission-control reads this through a pipe (gotcha #14)
        print(f"hf2 service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\nstopping ...")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        time.sleep(max(self.status_dt, 0.2) + 0.1)   # let both loops notice and close
        self.lockin.shutdown()

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place for the PUB frame and the
        `status` reply, so the two can never drift apart."""
        st = status_to_dict(self.lockin.status())
        st["describe_rev"] = self.describe_rev()
        return st

    def describe_rev(self, max_age_s: float = 0.5) -> int:
        """Manifest revision, recomputed at most every `max_age_s`."""
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.lockin)["revision"]
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
                except Exception as exc:                     # keep publishing
                    self._events.put({"level": "error", "msg": f"status failed: {exc}"})
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
                except Exception as exc:                     # never let the loop die
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
        li = self.lockin
        try:
            if cmd == "set_time_constant":
                li.set_time_constant(msg["channel"], msg["time_constant_s"])
            elif cmd == "set_order":
                li.set_order(msg["channel"], msg["order"])
            elif cmd == "set_frequency":
                li.set_frequency(msg["channel"], msg["frequency_Hz"])
            elif cmd == "set_reference":
                li.set_reference(msg["channel"], msg["mode"])
            elif cmd == "acquire":
                # Returns at once with the id. The caller waits for status to
                # show THIS id with acquiring == false.
                return {"ok": True, "acq_id": li.acquire()}
            elif cmd == "get_sample":
                return {"ok": True, "sample": _json_safe(li.get_sample())}
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(li)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(li.cfg)}
            elif cmd == "set_config":
                apply_config_dict(li.cfg, msg["config"])
                li.apply_config()
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
        cfg = self.lockin.cfg
        lim = cfg.limits
        return {
            "idn": self.lockin.status().idn,
            "device": cfg.hardware.device_id,
            "tc_min_s": lim.tc_min_s, "tc_max_s": lim.tc_max_s,
            "freq_min_Hz": lim.freq_min_Hz, "freq_max_Hz": lim.freq_max_Hz,
            "order_min": lim.order_min, "order_max": lim.order_max,
            "channels": [
                {"channel": i + 1, "demod": c.demod, "signal_input": c.signal_input,
                 "oscillator": c.oscillator, "ref_input": c.ref_input}
                for i, c in enumerate((cfg.ch1, cfg.ch2))
            ],
        }


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
