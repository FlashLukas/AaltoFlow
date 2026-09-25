"""The client: talk to a PpmsService, present a Cryostat-compatible facade.

A GUI, a script, or a coordinator can hold a PpmsClient exactly where it would
hold a Cryostat: same method names (set_field, set_temperature, the rate and
approach setters), same status() shape, same get_config()/apply_config(), same
`_on_event` hook. So the caller does not care whether the cryostat brain is
in-process or in the service -- only the address changes.

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

_NAN = float("nan")


def _f(d: dict, key: str) -> float:
    """A float from the wire; null ("no reading yet") comes back as NaN."""
    v = d.get(key)
    return _NAN if v is None else float(v)


class RemoteStatus:
    """Same attributes a caller reads off the Cryostat's Status."""

    def __init__(self, d: dict):
        self.connected = bool(d.get("connected", False))
        self.simulated = bool(d.get("simulated", True))
        self.idn = d.get("idn", "")
        self.hw_error = d.get("hw_error", "")
        self.setpoint_field_mT = _f(d, "setpoint_field_mT")
        self.measured_field_mT = _f(d, "measured_field_mT")
        self.field_error_mT = _f(d, "field_error_mT")
        self.field_status = d.get("field_status", "")
        self.field_stable = bool(d.get("field_stable", False))
        self.field_rate_mT_per_s = _f(d, "field_rate_mT_per_s")
        self.field_approach = d.get("field_approach", "")
        self.setpoint_temperature_K = _f(d, "setpoint_temperature_K")
        self.temperature_K = _f(d, "temperature_K")
        self.temperature_error_K = _f(d, "temperature_error_K")
        self.temperature_status = d.get("temperature_status", "")
        self.temperature_stable = bool(d.get("temperature_stable", False))
        self.temperature_rate_K_per_min = _f(d, "temperature_rate_K_per_min")
        self.temperature_approach = d.get("temperature_approach", "")
        self.chamber = d.get("chamber", "")
        self.readings = int(d.get("readings") or 0)
        self.poll_ms = _f(d, "poll_ms")
        # None from a service that predates `describe`.
        self.describe_rev = d.get("describe_rev")


class PpmsClient:
    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000):
        self._timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        self._req.connect(f"tcp://{host}:{cmd_port}")
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

    # ---- Cryostat-compatible surface -----------------------------------------

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
        """Push self.cfg to the service (it re-checks it; nothing is commanded)."""
        self._cmd({"cmd": "set_config", "config": config_to_dict(self.cfg)})

    def describe(self) -> dict:
        """The service's parameter manifest (see `net/describe.py`). Compare
        `status().describe_rev` against its `revision` rather than caching it."""
        r = self._cmd({"cmd": "describe"})
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> RemoteStatus:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            r = self._cmd({"cmd": "status"})
            d = r.get("status", {})
        return RemoteStatus(d)

    def set_field(self, field_mT: float):
        return self._checked({"cmd": "set_field", "field_mT": float(field_mT)})

    def set_field_rate(self, rate_mT_per_s: float):
        return self._checked({"cmd": "set_field_rate", "rate_mT_per_s": float(rate_mT_per_s)})

    def set_field_approach(self, approach: str):
        return self._checked({"cmd": "set_field_approach", "approach": str(approach)})

    def set_temperature(self, temperature_K: float):
        return self._checked({"cmd": "set_temperature", "temperature_K": float(temperature_K)})

    def set_temperature_rate(self, rate_K_per_min: float):
        return self._checked({"cmd": "set_temperature_rate",
                              "rate_K_per_min": float(rate_K_per_min)})

    def set_temperature_approach(self, approach: str):
        return self._checked({"cmd": "set_temperature_approach", "approach": str(approach)})

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self._stop.set()
        time.sleep(0.25)
        self._req.close(0)
        self._sub.close(0)

    # ---- internals -------------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _checked(self, d: dict) -> dict:
        """Send a command; a refusal becomes an event, so a GUI shows it."""
        r = self._cmd(d)
        if not r.get("ok"):
            self._on_event("error", r.get("error", "command failed"))
        return r

    def _cmd(self, d: dict) -> dict:
        with self._req_lock:
            self._req.send_json(d)
            try:
                return self._req.recv_json()
            except zmq.Again:
                # timed out; the REQ socket is now in a bad state -> rebuild it
                self._reset_req()
                return {"ok": False, "error": "service did not respond (timeout)"}

    def _reset_req(self):
        endpoint = self._req.LAST_ENDPOINT
        self._req.close(0)
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        if endpoint:
            self._req.connect(endpoint.decode() if isinstance(endpoint, bytes) else endpoint)

    def _listen(self):
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(200):
                topic, payload = self._sub.recv_multipart()
                d = json.loads(payload)
                if topic == TOPIC_STATUS:
                    with self._lock:
                        self._latest = d
                elif topic == TOPIC_EVENT:
                    self._on_event(d.get("level", "info"), d.get("msg", ""))
