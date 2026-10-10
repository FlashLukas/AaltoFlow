"""The client: talk to a KepcoService, present a BipolarSupply-compatible facade.

A GUI, a script, or a coordinator can hold a KepcoClient exactly where it would
hold a BipolarSupply: same method names, same status() shape, same
get_config()/apply_config(), same `_on_event` hook. So the caller does not care
whether the supply is in-process or across the lab -- only the address changes.

A background thread owns the SUB socket and keeps the latest status; commands go
out on a REQ socket guarded by a lock (REQ is strict request/reply, one at a
time). The same thread sends a `ping` every second, which feeds the service's
lost-client watchdog (`safety.watchdog_s`, off by default).

Refusals: the brain raises ValueError for a request it will not do (a mode
change with the output on, set_current in voltage mode). Over the wire that
comes back as {"ok": false, "error": ...}; the client re-raises it as
ValueError, so a GUI shows the same message locally and remotely. A command
refused because another PC holds control raises ControlRefused instead
(control.py), so a script never believes the supply took a setting it refused.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import fields

import zmq

from .. import secure
from ..config import Config
from ..control import ControlClient
from ..supply import Status
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict)

_NAN = float("nan")


class RemoteStatus:
    """The same attributes a caller reads off the brain's Status."""

    def __init__(self, d: dict):
        blank = Status()
        for f in fields(Status):
            v = d.get(f.name)
            if v is None:
                # missing or null (the wire sends NaN as null): the blank
                # Status default, with NaN for a float ("not measured yet")
                default = getattr(blank, f.name)
                v = _NAN if isinstance(default, float) else default
            setattr(self, f.name, v)
        # None from a service that predates `describe`.
        self.describe_rev = d.get("describe_rev")


class KepcoClient(ControlClient):
    """``kind`` / ``name``: who this client is to the service (control.py) --
    "gui" for a window, "script" (default) for a script or console, "machine"
    only for a program that must not be locked out (scan-core, another
    module). While a GUI on another PC holds control, a script must
    ``take_control()`` before it may change anything; a refused command
    raises ``ControlRefused``."""

    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000, ping_s: float = 1.0,
                 kind: str = "script",
                 name: str = "kepco client"):
        self._control_setup(kind, name)
        self._ctx = zmq.Context.instance()
        self._host = host
        self._cmd_port = int(cmd_port)
        self._pub_port = int(pub_port)
        self._timeout_ms = int(timeout_ms)
        self._make_req()
        self._sub, self._sub_gen = self._make_sub()

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self._ping_s = float(ping_s)
        self.cfg = Config()          # kept in sync with the service via get/set_config
        self._on_event = lambda level, msg: None

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- BipolarSupply-compatible surface ---------------------------------

    def start(self) -> dict:
        """Fetch static info and pull the service's config into self.cfg."""
        info = self.info()
        self.start_heartbeat()   # "still here": counted as a viewer / keeps control
        self.get_config()
        return info

    def get_config(self) -> Config:
        r = self._cmd({"cmd": "get_config"})
        if r.get("ok") and "config" in r:
            apply_config_dict(self.cfg, r["config"])
        return self.cfg

    def apply_config(self) -> None:
        self._checked({"cmd": "set_config", "config": config_to_dict(self.cfg)})

    def describe(self) -> dict:
        """The service's parameter manifest. Its shape follows the MODE, so
        compare `status().describe_rev` with the manifest's `revision`."""
        r = self._cmd({"cmd": "describe"})
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> RemoteStatus:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            r = self._cmd({"cmd": "status"})
            d = r.get("status", {})
        return RemoteStatus(d)

    @property
    def mode(self) -> str:
        return self.status().mode

    def current_range(self):
        lim = self.cfg.limits
        return lim.current_min_A, lim.current_max_A

    def voltage_range(self):
        lim = self.cfg.limits
        return lim.voltage_min_V, lim.voltage_max_V

    def current_limit_max(self) -> float:
        return max(abs(x) for x in self.current_range())

    def voltage_limit_max(self) -> float:
        return max(abs(x) for x in self.voltage_range())

    def set_mode(self, mode: str):
        self._checked({"cmd": "set_mode", "mode": str(mode)})

    def set_output(self, on: bool):
        self._checked({"cmd": "set_output", "on": bool(on)})

    def output_off(self):
        """Ramp to zero, then output off -- the safety verb, allowed also
        while viewing."""
        self._checked({"cmd": "output_off"})

    def output_off_now(self):
        self._checked({"cmd": "output_off_now"})

    def set_current(self, amps: float):
        self._checked({"cmd": "set_current", "current_A": float(amps)})

    def set_voltage(self, volts: float):
        self._checked({"cmd": "set_voltage", "voltage_V": float(volts)})

    def ramp_current(self, amps: float, rate_A_per_s: float) -> int:
        """Sweep the current at a set pace (fly scans, the GUI's Sweep button)."""
        return self._checked({"cmd": "ramp_current", "current_A": float(amps),
                              "rate_A_per_s": float(rate_A_per_s)}).get("ramp_id")

    def ramp_stop(self) -> bool:
        """End a sweep where it is (a safety verb: allowed also while viewing)."""
        return self._checked({"cmd": "ramp_stop"}).get("stopped")

    def set_current_limit(self, amps: float):
        self._checked({"cmd": "set_current_limit", "current_A": float(amps)})

    def set_voltage_limit(self, volts: float):
        self._checked({"cmd": "set_voltage_limit", "voltage_V": float(volts)})

    def set_ramp(self, rate_A_per_s=None, rate_V_per_s=None, enabled=None):
        msg = {"cmd": "set_ramp"}
        if rate_A_per_s is not None:
            msg["rate_A_per_s"] = float(rate_A_per_s)
        if rate_V_per_s is not None:
            msg["rate_V_per_s"] = float(rate_V_per_s)
        if enabled is not None:
            msg["enabled"] = bool(enabled)
        self._checked(msg)

    def set_acquisition(self, readings: int):
        self._checked({"cmd": "set_acquisition", "readings": int(readings)})

    def acquire(self) -> int:
        return int(self._checked({"cmd": "acquire"}).get("acq_id", 0))

    def get_sample(self) -> dict:
        return self._checked({"cmd": "get_sample"}).get("sample", {})

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self.stop_heartbeat()
        self._stop.set()
        time.sleep(0.25)
        with self._req_lock:
            self._req.close(0)
        self._sub.close(0)

    # ---- internals -------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _checked(self, d: dict) -> dict:
        r = self._cmd(d)
        if not r.get("ok"):
            raise ValueError(r.get("error", "request refused"))
        return r

    def _cmd(self, d: dict) -> dict:
        self._with_identity(d)           # say who we are (control.py)
        with self._req_lock:
            if self._stop.is_set():
                return {"ok": False, "error": "client closed"}
            for attempt in (1, 2):
                try:
                    self._req.send_json(d)
                    reply = self._req.recv_json()
                    break
                except zmq.Again:
                    # timed out; the REQ socket is now in a bad state -> rebuild it
                    self._req.close(0)
                    # The service may speak the other mode than the policy now
                    # says (it was started before the policy changed): the new
                    # socket tries that mode, once. Safe to resend: a request
                    # in the wrong mode never reaches the service.
                    flipped = secure.no_answer(self._host, "kepco")
                    self._make_req()
                    if not (flipped and attempt == 1):
                        return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused), not
        # the ValueError of an ordinary refusal -- taking control is the cure.
        if not reply.get("ok", False):
            self._raise_refusal(reply)
        return reply

    def _rpc(self, **req) -> dict:
        """The name control.py's ControlClient calls (heartbeat, take_control)."""
        return self._cmd(req)

    def _make_req(self):
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        self._req.setsockopt(zmq.SNDTIMEO, self._timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures kepco (secure.py); plain otherwise
        secure.secure_client(self._req, self._host, "kepco")
        self._req.connect(f"tcp://{self._host}:{self._cmd_port}")

    def _make_sub(self):
        s = self._ctx.socket(zmq.SUB)
        secure.secure_client(s, self._host, "kepco")      # telemetry too
        s.connect(f"tcp://{self._host}:{self._pub_port}")
        s.setsockopt(zmq.SUBSCRIBE, b"")
        return s, secure.flip_generation()

    def _listen(self):
        poller = zmq.Poller()
        poller.register(self._sub, zmq.POLLIN)
        last_ping = time.monotonic()
        while not self._stop.is_set():
            if self._sub_gen != secure.flip_generation():
                # a request found the service in the other mode
                # (secure.no_answer): telemetry follows
                poller.unregister(self._sub)
                self._sub.close(0)
                self._sub, self._sub_gen = self._make_sub()
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
            now = time.monotonic()
            if self._ping_s > 0 and now - last_ping >= self._ping_s:
                last_ping = now
                try:
                    self._cmd({"cmd": "ping"})
                except zmq.ZMQError:
                    pass
