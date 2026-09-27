"""The client: talk to an Hp8648Service, present a SignalSource-compatible facade.

A GUI, a script, or the coordinator can hold an Hp8648Client exactly where it
would hold a SignalSource: same method names (set_rf, set_power,
set_frequency), same status() shape, same get_config()/apply_config(), same
`_on_event` hook. So the caller does not care whether the generator is
in-process or across the lab -- only the address changes.

A background thread owns the SUB socket and keeps the latest status; commands go
out on a REQ socket guarded by a lock (REQ is strict request/reply, one at a time).
"""

from __future__ import annotations

import json
import threading
import time

import zmq

from ..config import Config
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict)


class RemoteStatus:
    """Same attributes a caller reads off the SignalSource's Status."""
    def __init__(self, d: dict):
        self.rf_on = bool(d.get("rf_on", False))
        self.frequency_Hz = float(d.get("frequency_Hz", 0.0))
        self.power_dBm = float(d.get("power_dBm", 0.0))
        self.rf_set = bool(d.get("rf_set", False))
        self.frequency_set_Hz = float(d.get("frequency_set_Hz", self.frequency_Hz))
        self.power_set_dBm = float(d.get("power_set_dBm", self.power_dBm))
        self.power_ceiling_dBm = float(d.get("power_ceiling_dBm", 0.0))
        self.spec_max_dBm = float(d.get("spec_max_dBm", 0.0))
        self.rpp_tripped = bool(d.get("rpp_tripped", False))
        self.level_unspecified = bool(d.get("level_unspecified", False))
        self.modulation_off = bool(d.get("modulation_off", True))
        self.modulation = dict(d.get("modulation") or {})
        self.connected = bool(d.get("connected", False))
        self.idn = d.get("idn", "")
        self.hw_error = d.get("hw_error", "")
        self.describe_rev = d.get("describe_rev")


class Hp8648Client:
    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000):
        self._timeout_ms = int(timeout_ms)
        self._ctx = zmq.Context.instance()
        self._req = self._new_req(f"tcp://{host}:{cmd_port}")
        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.connect(f"tcp://{host}:{pub_port}")
        self._sub.setsockopt(zmq.SUBSCRIBE, b"")

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self.cfg = Config()          # kept in sync with the service via get/set_config
        self._on_event = lambda level, msg: None

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- SignalSource-compatible surface ---------------------------------

    def start(self) -> dict:
        """Fetch static info and pull the service's config into self.cfg."""
        info = self.info()
        self.get_config()
        return info

    def get_config(self) -> Config:
        """Pull the service's live config into self.cfg (in place) and return it."""
        r = self._cmd({"cmd": "get_config"})
        if r.get("ok") and "config" in r:
            apply_config_dict(self.cfg, r["config"])
        return self.cfg

    def apply_config(self) -> None:
        """Push self.cfg to the service (it applies + re-clamps)."""
        self._cmd({"cmd": "set_config", "config": config_to_dict(self.cfg)})

    def describe(self) -> dict:
        """The service's parameter manifest. Its power limit is LIVE (it
        follows the frequency), so compare `status().describe_rev` against the
        manifest's `revision` rather than caching it forever."""
        r = self._cmd({"cmd": "describe"})
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> RemoteStatus:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            r = self._cmd({"cmd": "status"})
            d = r.get("status", {})
        return RemoteStatus(d)

    def power_ceiling(self, freq_Hz: float | None = None) -> float:
        """The service's live ceiling at its CURRENT frequency (the argument
        exists for signature compatibility with SignalSource)."""
        return self.status().power_ceiling_dBm

    def set_rf(self, on: bool):
        return self._cmd({"cmd": "set_rf", "on": bool(on)})

    def set_power(self, dBm: float):
        return self._cmd({"cmd": "set_power", "power_dBm": float(dBm)})

    def set_frequency(self, hz: float):
        return self._cmd({"cmd": "set_frequency", "frequency_Hz": float(hz)})

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self._stop.set()
        self._sub_t.join(timeout=1.0)
        with self._req_lock:
            self._req.close(0)
        self._sub.close(0)

    # ---- internals -------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _new_req(self, endpoint: str):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        s.setsockopt(zmq.LINGER, 0)
        s.connect(endpoint)
        self._endpoint = endpoint
        return s

    def _cmd(self, d: dict) -> dict:
        with self._req_lock:
            try:
                self._req.send_json(d)
                return self._req.recv_json()
            except zmq.Again:
                # timed out; a REQ socket is now stuck mid-exchange -> rebuild it
                self._req.close(0)
                self._req = self._new_req(self._endpoint)
                return {"ok": False, "error": "service did not respond (timeout)"}

    def _listen(self):
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(200):
                try:
                    topic, payload = self._sub.recv_multipart()
                    d = json.loads(payload)
                except Exception:
                    continue
                if topic == TOPIC_STATUS:
                    with self._lock:
                        self._latest = d
                elif topic == TOPIC_EVENT:
                    try:
                        self._on_event(d.get("level", "info"), d.get("msg", ""))
                    except Exception:
                        pass
