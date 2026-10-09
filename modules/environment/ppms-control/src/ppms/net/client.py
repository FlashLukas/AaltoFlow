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
        # the field SWEEP (an older service has none: never sweeping)
        self.ramping = bool(d.get("ramping", False))
        self.ramp_id = int(d.get("ramp_id") or 0)
        self.ramp_target_mT = _f(d, "ramp_target_mT")
        self.ramp_rate_mT_per_s = _f(d, "ramp_rate_mT_per_s")
        # None from a service that predates `describe`.
        self.describe_rev = d.get("describe_rev")


class PpmsClient(ControlClient):
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
                 name: str = "ppms client"):
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

    # ---- Cryostat-compatible surface -----------------------------------------

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

    def set_field(self, field_mT: float):
        return self._checked({"cmd": "set_field", "field_mT": float(field_mT)})

    def ramp_field(self, field_mT: float, rate_mT_per_s: float):
        """Sweep the field (fly scans; the GUI's Sweep button)."""
        return self._checked({"cmd": "ramp_field", "field_mT": float(field_mT),
                              "rate_mT_per_s": float(rate_mT_per_s)}).get("ramp_id")

    def ramp_stop(self):
        """End a sweep where it is (a safety verb: allowed also while viewing)."""
        return self._checked({"cmd": "ramp_stop"}).get("stopped")

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
                    flipped = secure.no_answer(self._host, "ppms")
                    self._req = self._new_req()
                    if not (flipped and attempt == 1):
                        return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused),
        # never a quiet {"ok": false} or a log line only -- a script must not
        # believe the field went where it asked. Other failures keep their old
        # shape (`_checked` turns them into an error event).
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
        # secures ppms (secure.py); plain otherwise
        secure.secure_client(s, self._host, "ppms")
        s.connect(self._endpoint)
        return s

    def _new_sub(self):
        """The status/event subscriber, in the mode the policy says now; also
        returns the flip generation it was made in (see _listen)."""
        s = self._ctx.socket(zmq.SUB)
        secure.secure_client(s, self._host, "ppms")      # telemetry too
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
