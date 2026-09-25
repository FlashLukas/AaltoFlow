"""The service: wrap the Cryostat brain and expose it over ZeroMQ.

One process owns MultiVu (or the simulator) and the Cryostat. It runs
two extra threads, like every service in the suite:
  * publisher  -- owns the PUB socket; sends a status frame at `status_hz` and
                  forwards brain events as they happen (one socket, because a
                  ZeroMQ socket must be used from a single thread).
  * commander  -- owns the REP socket; receives a JSON command, dispatches it to
                  the brain, and replies.

Bind to tcp://0.0.0.0:<port> and the same code serves a client on localhost or
across the lab network -- only the address the client dials changes.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import zmq

from ..cryostat import Cryostat
from .describe import build_manifest
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, status_to_dict, config_to_dict,
                       apply_config_dict)


class PpmsService:
    def __init__(self, cryostat: Cryostat,
                 host: str = "0.0.0.0",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 status_hz: float = 5.0):
        self.cryo = cryostat
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
        # route brain events into the publisher queue
        self.cryo._on_event = lambda lvl, msg: self._events.put({"level": lvl, "msg": msg})
        self.cryo.start()
        self._pub_t = threading.Thread(target=self._publisher, name="svc-pub", daemon=True)
        self._cmd_t = threading.Thread(target=self._commander, name="svc-cmd", daemon=True)
        self._pub_t.start()
        self._cmd_t.start()

    def serve_forever(self) -> None:
        self.start()
        print(f"ppms service up  -  commands {self.cmd_addr}  -  status {self.pub_addr}")
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
        self.cryo.shutdown()

    # ---------------------------------------------------------------- threads

    def status_payload(self) -> dict:
        """The status dict, built in ONE place.

        The publisher and the `status` command reply must not drift: a client
        falls back to the REQ path whenever no PUB frame has arrived yet (ZeroMQ
        SUB is a slow joiner), so a field present in only one of them is a field
        that vanishes intermittently.
        """
        st = status_to_dict(self.cryo.status())
        st["describe_rev"] = self.describe_rev()
        return st

    def describe_rev(self, max_age_s: float = 1.0) -> int:
        """Current manifest revision, recomputed at most once per `max_age_s`.

        Every status frame carries it so a client can tell, for the cost of one
        integer compare, whether its cached manifest went stale.
        """
        now = time.monotonic()
        if now - self._rev_at >= max_age_s:
            self._rev = build_manifest(self.cryo)["revision"]
            self._rev_at = now
        return self._rev

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
                self.cryo.set_field(float(msg["field_mT"]))
            elif cmd == "set_field_rate":
                self.cryo.set_field_rate(float(msg["rate_mT_per_s"]))
            elif cmd == "set_field_approach":
                self.cryo.set_field_approach(str(msg["approach"]))
            elif cmd == "set_temperature":
                self.cryo.set_temperature(float(msg["temperature_K"]))
            elif cmd == "set_temperature_rate":
                self.cryo.set_temperature_rate(float(msg["rate_K_per_min"]))
            elif cmd == "set_temperature_approach":
                self.cryo.set_temperature_approach(str(msg["approach"]))
            elif cmd == "status":
                return {"ok": True, "status": self.status_payload()}
            elif cmd == "describe":
                return {"ok": True, "describe": build_manifest(self.cryo)}
            elif cmd == "info":
                return {"ok": True, "info": self._info()}
            elif cmd == "get_config":
                return {"ok": True, "config": config_to_dict(self.cryo.cfg)}
            elif cmd == "set_config":
                apply_config_dict(self.cryo.cfg, msg["config"])
                self.cryo.apply_config()
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
        except RuntimeError as exc:           # e.g. MultiVu not connected
            return {"ok": False, "error": str(exc)}

    def _info(self) -> dict:
        lim = self.cryo.cfg.limits
        st = self.cryo.status()
        return {
            "idn": st.idn,
            "simulated": st.simulated,
            "field_max_mT": lim.field_max_mT,
            "temperature_min_K": lim.temperature_min_K,
            "temperature_max_K": lim.temperature_max_K,
            "field_rate_max_mT_per_s": lim.field_rate_max_mT_per_s,
            "temperature_rate_max_K_per_min": lim.temperature_rate_max_K_per_min,
        }


def _json(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")
