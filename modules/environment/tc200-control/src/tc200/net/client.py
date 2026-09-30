"""The client: talk to a Tc200Service, present a Heater-compatible facade.

A GUI, a script, or a coordinator can hold a Tc200Client exactly where it would
hold a Heater: same method names (set_temperature, set_enabled, the gain / PMAX /
TMAX / sensor setters), same status() shape, same get_config()/apply_config(),
same `_on_event` hook. So the caller does not care whether the heater brain is
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
from ..control import ControlClient
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict)

_NAN = float("nan")


def _f(d: dict, key: str) -> float:
    """A float from the wire; null ("no reading yet") comes back as NaN."""
    v = d.get(key)
    return _NAN if v is None else float(v)


class RemoteStatus:
    """Same attributes a caller reads off the Heater's Status."""

    def __init__(self, d: dict):
        self.connected = bool(d.get("connected", False))
        self.simulated = bool(d.get("simulated", True))
        self.idn = d.get("idn", "")
        self.hw_error = d.get("hw_error", "")
        self.setpoint_C = _f(d, "setpoint_C")
        self.temperature_C = _f(d, "temperature_C")
        self.temperature_error_C = _f(d, "temperature_error_C")
        self.temperature_stable = bool(d.get("temperature_stable", False))
        self.temperature_min_C = _f(d, "temperature_min_C")
        self.temperature_max_C = _f(d, "temperature_max_C")
        self.enabled = bool(d.get("enabled", False))
        self.mode = d.get("mode", "")
        self.sensor_alarm = bool(d.get("sensor_alarm", False))
        self.tmax_alarm = bool(d.get("tmax_alarm", False))
        self.sensor = d.get("sensor", "")
        self.sensor_ok = bool(d.get("sensor_ok", False))
        self.p_gain = int(d.get("p_gain") or 0)
        self.i_gain = int(d.get("i_gain") or 0)
        self.d_gain = int(d.get("d_gain") or 0)
        self.pmax_W = _f(d, "pmax_W")
        self.tmax_C = _f(d, "tmax_C")
        self.readings = int(d.get("readings") or 0)
        self.poll_ms = _f(d, "poll_ms")
        # None from a service that predates `describe`.
        self.describe_rev = d.get("describe_rev")


class Tc200Client(ControlClient):
    """``kind`` / ``name``: who this client is to the service (control.py) --
    "gui" for a window, "script" (default) for a script or console, "machine"
    only for a program that must not be locked out (scan-core). While a GUI on
    another PC holds control, a script must ``take_control()`` before it may
    change anything; a refused command raises ``ControlRefused``."""

    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000,
                 kind: str = "script",
                 name: str = "tc200 client"):
        self._control_setup(kind, name)
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

    # ---- Heater-compatible surface -----------------------------------------

    def start(self) -> dict:
        """Fetch static info and pull the service's config into self.cfg."""
        info = self.info()
        self.start_heartbeat()   # "still here": counted as a viewer / keeps control
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

    def temperature_max(self) -> float:
        """The service's LIVE setpoint ceiling (it follows the box's TMAX)."""
        return self.status().temperature_max_C

    def set_temperature(self, temperature_C: float):
        return self._checked({"cmd": "set_temperature", "temperature_C": float(temperature_C)})

    def set_enabled(self, enabled: bool):
        return self._checked({"cmd": "set_enabled", "enabled": bool(enabled)})

    def heater_off(self):
        """Switch the heater output off -- the safety verb, allowed also while viewing."""
        return self._checked({"cmd": "heater_off"})

    def set_p_gain(self, p: int):
        return self._checked({"cmd": "set_p_gain", "p": int(p)})

    def set_i_gain(self, i: int):
        return self._checked({"cmd": "set_i_gain", "i": int(i)})

    def set_d_gain(self, d: int):
        return self._checked({"cmd": "set_d_gain", "d": int(d)})

    def set_pid(self, p: int, i: int, d: int):
        return self._checked({"cmd": "set_pid", "p": int(p), "i": int(i), "d": int(d)})

    def set_pmax(self, watts: float):
        return self._checked({"cmd": "set_pmax", "pmax_W": float(watts)})

    def set_tmax(self, tmax_C: float):
        return self._checked({"cmd": "set_tmax", "tmax_C": float(tmax_C)})

    def set_sensor(self, sensor: str):
        return self._checked({"cmd": "set_sensor", "sensor": str(sensor)})

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self.stop_heartbeat()
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
        self._with_identity(d)           # say who we are (control.py)
        with self._req_lock:
            self._req.send_json(d)
            try:
                reply = self._req.recv_json()
            except zmq.Again:
                # timed out; the REQ socket is now in a bad state -> rebuild it
                self._reset_req()
                return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused),
        # never a quiet {"ok": false} -- a script must not believe the heater
        # did what it asked. Other failures keep their old error-dict shape.
        if not reply.get("ok", False):
            self._raise_refusal(reply)
        return reply

    def _rpc(self, **req) -> dict:
        """The name control.py's ControlClient calls (heartbeat, take_control)."""
        return self._cmd(req)

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
                    self._control_from_status(d)
                elif topic == TOPIC_EVENT:
                    self._on_event(d.get("level", "info"), d.get("msg", ""))
