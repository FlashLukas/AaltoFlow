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
from .. import secure
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
        # the temperature SWEEP (an older service has none: never sweeping)
        self.ramping = bool(d.get("ramping", False))
        self.ramp_id = int(d.get("ramp_id") or 0)
        self.ramp_target_C = _f(d, "ramp_target_C")
        self.ramp_rate_C_per_s = _f(d, "ramp_rate_C_per_s")
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
        self._ctx = zmq.Context.instance()
        self._timeout_ms = timeout_ms
        # the host is kept: the encryption keys are looked up per host
        # (secure.py), and a socket rebuilt after a timeout needs it again
        self._host = host
        self._endpoint = f"tcp://{host}:{cmd_port}"
        self._pub_endpoint = f"tcp://{host}:{pub_port}"
        self._req = self._new_req()
        self._sub, self._sub_gen = self._new_sub()

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

    def ramp_temperature(self, temperature_C: float, rate_C_per_s: float):
        """Sweep the setpoint (fly scans; the GUI's Sweep button). Rate in C/s."""
        return self._checked({"cmd": "ramp_temperature",
                              "temperature_C": float(temperature_C),
                              "rate_C_per_s": float(rate_C_per_s)}).get("ramp_id")

    def ramp_stop(self):
        """End a sweep where it is (a safety verb: allowed also while viewing)."""
        return self._checked({"cmd": "ramp_stop"}).get("stopped")

    def ramp_rate_limits(self) -> tuple[float, float]:
        """(min, max) sweep pace in C/s, from the service's config (as last
        fetched by start() / get_config(): no round trip here)."""
        lim = self.cfg.limits
        lo = max(1e-6, float(lim.ramp_rate_min_C_per_s))
        return lo, max(lo, float(lim.ramp_rate_max_C_per_s))

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
            for attempt in (1, 2):
                try:
                    self._req.send_json(d)
                    reply = self._req.recv_json()
                    break
                except zmq.Again:
                    # timed out; a REQ socket is now stuck mid-exchange -> rebuild it
                    self._req.close(0)
                    # The service may speak the other mode than the policy now
                    # says (it was started before the policy changed): the new
                    # socket tries that mode, once. Safe to resend: a request
                    # in the wrong mode never reaches the service.
                    flipped = secure.no_answer(self._host, "tc200")
                    self._req = self._new_req()
                    if not (flipped and attempt == 1):
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

    def _new_req(self):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        s.setsockopt(zmq.SNDTIMEO, self._timeout_ms)
        s.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures tc200 (secure.py); plain otherwise
        secure.secure_client(s, self._host, "tc200")
        s.connect(self._endpoint)
        return s

    def _new_sub(self):
        """The status/event subscriber, in the mode the policy says now; also
        returns the flip generation it was made in (see _listen)."""
        s = self._ctx.socket(zmq.SUB)
        secure.secure_client(s, self._host, "tc200")      # telemetry too
        s.connect(self._pub_endpoint)
        s.setsockopt(zmq.SUBSCRIBE, b"")
        return s, secure.flip_generation()

    def _listen(self):
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        while not self._stop.is_set():
            if self._sub_gen != secure.flip_generation():
                # a request found the service in the other mode
                # (secure.no_answer): telemetry follows
                poller.unregister(self._sub)
                self._sub.close(0)
                self._sub, self._sub_gen = self._new_sub()
                poller.register(self._sub, zmq.POLLIN)
            if poller.poll(200):
                topic, payload = self._sub.recv_multipart()
                d = json.loads(payload)
                if topic == TOPIC_STATUS:
                    with self._lock:
                        self._latest = d
                    self._control_from_status(d)
                elif topic == TOPIC_EVENT:
                    self._on_event(d.get("level", "info"), d.get("msg", ""))
