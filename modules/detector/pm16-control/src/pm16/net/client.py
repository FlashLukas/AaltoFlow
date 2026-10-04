"""The client: talk to a Pm16Service, present a PowerMeter-compatible facade.

A GUI or script can hold a Pm16Client exactly where it would hold a PowerMeter:
same method names, same status() attributes, same get_config()/apply_config()
and `_on_event` hook. Only the address changes.

It adds `acquire_blocking()` for scripts: trigger, wait for THIS acquisition,
return its sample.
"""

from __future__ import annotations

import json
import threading
import time

import zmq

from ..config import Config, acquire_timeout_s
from ..control import ControlClient
from .. import secure
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict)

_NAN = float("nan")


def _num(v):
    """JSON null (no reading yet) back to NaN, so arithmetic and formatting work."""
    return _NAN if v is None else v


class RemoteStatus:
    """Same attributes a caller reads off the PowerMeter's Status."""

    _FLOATS = ("power_W", "read_ms", "wavelength_set_nm", "wavelength_nm",
               "wavelength_min_nm", "wavelength_max_nm", "range_set_W", "range_W",
               "range_min_W", "range_max_W", "average_time_s", "dark_offset")

    def __init__(self, d: dict):
        for k in self._FLOATS:
            setattr(self, k, _num(d.get(k)))
        self.connected = d.get("connected", False)
        self.idn = d.get("idn", "")
        self.sensor = d.get("sensor", "")
        self.hw_error = d.get("hw_error", "")
        self.flag = d.get("flag", "")
        self.readings = d.get("readings", 0)
        self.auto_range = d.get("auto_range", True)
        self.zeroing = d.get("zeroing", False)
        self.acq_readings = d.get("acq_readings", 1)
        self.acq_id = d.get("acq_id", 0)
        self.acquiring = d.get("acquiring", False)
        self.acq_progress = d.get("acq_progress", 0.0)
        self.sample = {k: _num(v) if k in ("power_W", "std_W") else v
                       for k, v in (d.get("sample") or {}).items()}
        self.describe_rev = d.get("describe_rev")


class Pm16Client(ControlClient):
    """``kind`` / ``name``: who this client is to the service (control.py) --
    "gui" for a window, "script" (default) for a script or console, "machine"
    only for a program that must not be locked out (scan-core, another
    module). While a GUI on another PC holds control, a script must
    ``take_control()`` before it may change anything; a refused command
    raises ``ControlRefused``."""

    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000,
                 kind: str = "script",
                 name: str = "pm16 client"):
        self._control_setup(kind, name)
        self._ctx = zmq.Context.instance()
        self._timeout_ms = timeout_ms
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

    # ---- PowerMeter-compatible surface -----------------------------------

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
        self._cmd({"cmd": "set_config", "config": config_to_dict(self.cfg)})

    def describe(self) -> dict:
        r = self._cmd({"cmd": "describe"})
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> RemoteStatus:
        return RemoteStatus(self._status_dict())

    def set_wavelength(self, nm: float):
        return self._cmd({"cmd": "set_wavelength", "wavelength_nm": float(nm)})

    def set_auto_range(self, on: bool):
        return self._cmd({"cmd": "set_auto_range", "on": bool(on)})

    def set_range(self, watts: float):
        return self._cmd({"cmd": "set_range", "range_W": float(watts)})

    def set_acquisition(self, readings: int):
        return self._cmd({"cmd": "set_acquisition", "readings": int(readings)})

    def zero(self):
        r = self._cmd({"cmd": "zero"})
        if not r.get("ok"):
            raise ValueError(r.get("error", "zero refused"))
        return r

    def cancel_zero(self):
        return self._cmd({"cmd": "cancel_zero"})

    def acquire(self) -> int:
        """Start an acquisition; returns its id (or raises if refused)."""
        r = self._cmd({"cmd": "acquire"})
        if not r.get("ok"):
            raise ValueError(r.get("error", "acquire refused"))
        return int(r["acq_id"])

    def get_sample(self) -> dict:
        return self._cmd({"cmd": "get_sample"}).get("sample", {})

    def acquire_blocking(self, timeout_s: float | None = None, poll_s: float = 0.02) -> dict:
        """Trigger, wait for THIS acquisition to finish, return its sample.

        The wait checks the id before the flag: right after the trigger the
        cached status can still be the frame from BEFORE it, saying "not
        acquiring", and trusting that would return the previous sample.
        """
        n = self.acquire()
        if timeout_s is None:
            # The SERVICE's readings-per-acquisition (self.cfg may be stale):
            # 1000 readings take ~60 s, longer than the configured 30 s floor.
            st = self._status_dict()
            limit = acquire_timeout_s(
                st.get("acq_readings") or self.cfg.acquisition.readings,
                self.cfg.acquisition.timeout_s, st.get("average_time_s"))
        else:
            limit = timeout_s
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            st = self._status_dict()
            if st.get("acq_id") == n and not st.get("acquiring", True):
                sample = st.get("sample") or {}
                if sample.get("acq_id") == n:
                    return sample
            time.sleep(poll_s)
        raise TimeoutError(f"acquisition {n} did not finish within {limit:g} s")

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self.stop_heartbeat()
        self._stop.set()
        time.sleep(0.25)
        self._req.close(0)
        self._sub.close(0)

    # ---- internals -------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _status_dict(self) -> dict:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            d = self._cmd({"cmd": "status"}).get("status", {})
        return d

    def _new_req(self):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        s.setsockopt(zmq.SNDTIMEO, self._timeout_ms)
        s.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures pm16 (secure.py); plain otherwise
        secure.secure_client(s, self._host, "pm16")
        s.connect(self._endpoint)
        return s

    def _new_sub(self):
        """The status/event subscriber, in the same mode as the REQ socket.
        Returns it with the secure.flip_generation() it was made under, so
        the listener can tell when it has to be rebuilt."""
        s = self._ctx.socket(zmq.SUB)
        secure.secure_client(s, self._host, "pm16")      # telemetry too
        s.connect(self._pub_endpoint)
        s.setsockopt(zmq.SUBSCRIBE, b"")
        return s, secure.flip_generation()

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
                    flipped = secure.no_answer(self._host, "pm16")
                    self._req = self._new_req()
                    if not (flipped and attempt == 1):
                        return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused), so
        # a script never believes the instrument took a setting it refused.
        if not reply.get("ok", False):
            self._raise_refusal(reply)
        return reply

    def _rpc(self, **req) -> dict:
        """The name control.py's ControlClient calls (heartbeat, take_control)."""
        return self._cmd(req)

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
