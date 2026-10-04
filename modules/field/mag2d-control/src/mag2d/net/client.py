"""The client: talk to a Mag2dService, present a Controller-compatible facade.

The GUI (and your scripts) can hold a Mag2dClient exactly where they would hold
a Controller: same method names (set_field, set_angle, set_vector, set_output,
clear_fault, ...), same status() attributes, same `_on_event` hook, and the same
`Refused` exception when the magnet says no. So the window does not care whether
the magnet is in this process or across the lab.

A background thread owns the SUB socket and keeps the latest status; commands go
out on a REQ socket under a lock (REQ is strictly one request, one reply).
"""

from __future__ import annotations

import json
import threading
import time

import zmq

from ..config import Config
from .. import secure
from ..control import ControlClient
from ..controller import Refused
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS, TOPIC_EVENT,
                       config_to_dict, apply_config_dict)

#: How close the echoed setpoint must be to ours to count as "adopted" -- the
#: same tolerance scan-core uses. The service stores the value exactly, so this
#: only guards against float noise in JSON.
_SETPOINT_EPS = 1e-6

_NAN = float("nan")


def _num(v):
    return _NAN if v is None else v


class RemoteStatus:
    """Same attributes the GUI reads off the Controller's Status."""

    def __init__(self, d: dict):
        self.raw = d
        self.state = d.get("state", "?")
        self.energized = bool(d.get("energized", False))
        self.setpoint_field_mT = _num(d.get("setpoint_field_mT"))
        self.setpoint_angle_deg = _num(d.get("setpoint_angle_deg"))
        self.setpoint_bx_mT = _num(d.get("setpoint_bx_mT"))
        self.setpoint_by_mT = _num(d.get("setpoint_by_mT"))
        self.measured_bx_mT = _num(d.get("measured_bx_mT"))
        self.measured_by_mT = _num(d.get("measured_by_mT"))
        self.measured_field_mT = _num(d.get("measured_field_mT"))
        self.measured_magnitude_mT = _num(d.get("measured_magnitude_mT"))
        self.measured_angle_deg = _num(d.get("measured_angle_deg"))
        self.error_mT = _num(d.get("error_mT"))
        self.field_stable = bool(d.get("field_stable", False))
        self.output_V = [_num(x) for x in (d.get("output_V") or [None, None])]
        self.hall_V = [_num(x) for x in (d.get("hall_V") or [None, None])]
        self.temp_C = [_num(x) for x in (d.get("temp_C") or [None, None])]
        self.water_ok = bool(d.get("water_ok", False))
        self.water_bypass = bool(d.get("water_bypass", False))
        self.temp_monitor = bool(d.get("temp_monitor", False))
        self.fault = d.get("fault", "") or ""
        self.hw_error = d.get("hw_error", "") or ""
        self.describe_rev = d.get("describe_rev")


class Mag2dClient(ControlClient):
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
                 name: str = "mag2d client"):
        self._control_setup(kind, name)
        self._timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self._make_req()
        # the SUB socket is created (and closed) by the listener thread that
        # uses it: a ZeroMQ socket belongs to one thread

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self.cfg = Config()          # kept in sync with the service via get/set_config
        self._on_event = lambda level, msg: None

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- Controller-compatible surface -----------------------------------

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
        self._do({"cmd": "set_config", "config": config_to_dict(self.cfg)})

    def status(self) -> RemoteStatus:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            d = self._cmd({"cmd": "status"}).get("status", {})
        return RemoteStatus(d)

    def set_field(self, field_mT: float, angle_deg: float | None = None):
        msg = {"cmd": "set_field", "field_mT": field_mT}
        if angle_deg is not None:
            msg["angle_deg"] = angle_deg
        self._do(msg)

    def set_angle(self, angle_deg: float):
        self._do({"cmd": "set_angle", "angle_deg": angle_deg})

    def set_vector(self, bx_mT: float, by_mT: float):
        self._do({"cmd": "set_vector", "bx_mT": bx_mT, "by_mT": by_mT})

    def set_bx(self, bx_mT: float):
        self._do({"cmd": "set_bx", "bx_mT": bx_mT})

    def set_by(self, by_mT: float):
        self._do({"cmd": "set_by", "by_mT": by_mT})

    def zero(self):
        self._do({"cmd": "zero"})

    def set_output(self, enabled: bool):
        self._do({"cmd": "set_output", "enabled": bool(enabled)})

    def output_off(self):
        """Ramp down to 0 V and switch off -- the safety verb, allowed also
        while viewing (set_output can also switch ON, so it is not)."""
        self._do({"cmd": "output_off"})

    def set_water_bypass(self, enabled: bool):
        self._do({"cmd": "set_water_bypass", "enabled": bool(enabled)})

    def clear_fault(self):
        self._do({"cmd": "clear_fault"})

    def shutdown(self):
        """Close the CLIENT. Does NOT stop the remote service (use stop_service)."""
        self.stop_heartbeat()
        self._stop.set()
        time.sleep(0.25)                 # the listener closes its SUB socket
        with self._req_lock:
            self._req.close(0)

    def stop_service(self) -> dict:
        """Ask the service to ramp down and exit (the universal `shutdown` verb)."""
        return self._cmd({"cmd": "shutdown"})

    # ---- blocking helpers (for scripts; scan-core uses its own policies) --

    def set_field_blocking(self, field_mT: float, angle_deg: float | None = None,
                           timeout_s: float | None = None, poll_s: float = 0.05):
        """Set the field and return only once it is STABLE there.

        Two conditions, in order: the service has ADOPTED our setpoint, and then
        field_stable. field_stable alone would still be True from the previous
        point for a moment after the command. Raises TimeoutError, because a
        script that silently carries on at an unsettled field records data that
        looks fine and is wrong.
        """
        self.set_field(field_mT, angle_deg)

        def settled(st):
            if abs(st.setpoint_field_mT - field_mT) > _SETPOINT_EPS:
                return False
            if angle_deg is not None and abs(st.setpoint_angle_deg - angle_deg) > _SETPOINT_EPS:
                return False
            return st.field_stable

        timeout = self.cfg.control.settle_timeout_s if timeout_s is None else timeout_s
        return self._wait_for(settled, timeout, poll_s,
                              f"stable at {field_mT:g} mT"
                              + ("" if angle_deg is None else f", {angle_deg:g} deg"))

    def _wait_for(self, predicate, timeout_s: float, poll_s: float, what: str):
        deadline = time.monotonic() + timeout_s
        while True:
            st = self.status()
            if predicate(st):
                return st
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"mag2d: waited {timeout_s:g} s for {what}; state={st.state} "
                    f"setpoint={st.setpoint_field_mT} mT @ {st.setpoint_angle_deg} deg, "
                    f"error={st.error_mT:.3f} mT, fault={st.fault!r}")
            time.sleep(poll_s)

    # ---- plumbing -----------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def describe(self) -> dict:
        """The parameter manifest (net/describe.py). Its bounds follow the
        service's config, so compare `status().describe_rev` with its revision."""
        return self._cmd({"cmd": "describe"}).get("describe", {})

    def _do(self, d: dict) -> dict:
        """Send a command that must succeed; a refusal raises `Refused` with the
        service's message -- the same exception a local Controller raises."""
        r = self._cmd(d)
        if not r.get("ok"):
            raise Refused(r.get("error", "refused"))
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
                    # timed out; the REQ socket is now in a bad state -> rebuild it.
                    self._req.close(0)
                    # The service may speak the other mode than the policy now
                    # says (it was started before the policy changed): the new
                    # socket tries that mode, once. Safe to resend: a request
                    # in the wrong mode never reaches the service.
                    flipped = secure.no_answer(self.host, "mag2d")
                    self._make_req()
                    if not (flipped and attempt == 1):
                        return {"ok": False, "error": "service did not respond (timeout)"}
                except zmq.ZMQError as exc:
                    self._req.close(0)
                    self._make_req()
                    return {"ok": False, "error": f"socket error: {exc}"}
        # Refused because another PC holds control: RAISE (ControlRefused),
        # never a quiet {"ok": false} -- a script must not believe the magnet
        # went where it asked. Other failures keep their old shape (`_do`
        # turns them into Refused).
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
        # secures mag2d (secure.py); plain otherwise
        secure.secure_client(self._req, self.host, "mag2d")
        self._req.connect(f"tcp://{self.host}:{self.cmd_port}")

    def _make_sub(self):
        s = self._ctx.socket(zmq.SUB)
        secure.secure_client(s, self.host, "mag2d")      # telemetry too
        s.connect(f"tcp://{self.host}:{self.pub_port}")
        s.setsockopt(zmq.SUBSCRIBE, b"")
        return s, secure.flip_generation()

    def _listen(self):
        sub, gen = self._make_sub()
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if gen != secure.flip_generation():
                    # a request found the service in the other mode
                    # (secure.no_answer): telemetry follows
                    poller.unregister(sub)
                    sub.close(0)
                    sub, gen = self._make_sub()
                    poller.register(sub, zmq.POLLIN)
                try:
                    if poller.poll(200):
                        topic, payload = sub.recv_multipart()
                        d = json.loads(payload)
                        if topic == TOPIC_STATUS:
                            with self._lock:
                                self._latest = d
                            self._control_from_status(d)
                        elif topic == TOPIC_EVENT:
                            self._on_event(d.get("level", "info"), d.get("msg", ""))
                except zmq.ZMQError:
                    return
        finally:
            sub.close(0)
