"""The client: talk to a Ccs200Service, present a Spectrometer-compatible facade.

A GUI or script can hold a Ccs200Client exactly where it would hold a
Spectrometer: same method names, same status() attributes, same
get_config()/apply_config() and `_on_event` hook. Only the address changes.

It adds `acquire_blocking()` and `take_dark_blocking()` for scripts: trigger,
wait for THIS acquisition, return its spectrum.
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import zmq

from ..config import Config
from ..control import ControlClient
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict, trace_from_wire)

_NAN = float("nan")


class RemoteStatus:
    """Same attributes a caller reads off the Spectrometer's Status. JSON null
    (no value) comes back as NaN, so arithmetic and formatting just work."""

    _DEFAULTS = {"connected": False, "idn": "", "hw_error": "", "pixels": 0,
                 "averages": 1, "continuous": True, "dark_subtract": False,
                 "scanning": False, "scan_progress": 0.0, "scans": 0, "trace_id": 0,
                 "saturated": False, "live_dark_applied": False, "light_on": False,
                 "acq_id": 0, "acquiring": False, "acq_progress": 0.0,
                 "simulated": True, "acq_is_dark": False}

    def __init__(self, d: dict):
        for k, default in self._DEFAULTS.items():
            v = d.get(k, default)
            setattr(self, k, default if v is None else v)
        for k, v in d.items():
            if k not in self._DEFAULTS and k not in ("sample", "dark"):
                setattr(self, k, _NAN if v is None else v)
        self.sample = {k: (_NAN if v is None else v) for k, v in (d.get("sample") or {}).items()}
        self.dark = {k: (_NAN if v is None else v)
                     for k, v in (d.get("dark") or {"present": False}).items()}
        self.describe_rev = d.get("describe_rev")

    def __getattr__(self, name):
        # A float field the service has not sent yet (first frame) reads as NaN
        # rather than crashing the GUI's first refresh.
        if name.endswith(("_nm", "_s", "_K", "_per_s")) or name in (
                "peak_intensity", "integrated", "exposure", "offset", "read_noise"):
            return _NAN
        raise AttributeError(name)


class Ccs200Client(ControlClient):
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
                 name: str = "ccs200 client"):
        self._control_setup(kind, name)
        self._ctx = zmq.Context.instance()
        self._timeout_ms = timeout_ms
        self._endpoint = f"tcp://{host}:{cmd_port}"
        self._req = self._new_req()
        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.connect(f"tcp://{host}:{pub_port}")
        self._sub.setsockopt(zmq.SUBSCRIBE, b"")

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self.cfg = Config()          # kept in sync with the service via get/set_config
        self._on_event = lambda level, msg: None
        self._wl = None              # wavelength grid, fetched once

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- Spectrometer-compatible surface ------------------------------------------

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

    def set_integration_time(self, s):  return self._cmd({"cmd": "set_integration_time",
                                                          "integration_time_s": float(s)})
    def set_averages(self, n):        return self._cmd({"cmd": "set_averages", "averages": int(n)})
    def set_dark_subtract(self, on):  return self._cmd({"cmd": "set_dark_subtract", "on": bool(on)})
    def set_continuous(self, on):     return self._cmd({"cmd": "set_continuous", "on": bool(on)})
    def set_window_min(self, nm):     return self._cmd({"cmd": "set_window_min", "nm": float(nm)})
    def set_window_max(self, nm):     return self._cmd({"cmd": "set_window_max", "nm": float(nm)})
    def set_window(self, lo, hi):     return self._cmd({"cmd": "set_window", "min_nm": float(lo),
                                                        "max_nm": float(hi)})
    def set_light(self, on):          return self._checked({"cmd": "set_light", "on": bool(on)})
    def clear_dark(self):             return self._cmd({"cmd": "clear_dark"})

    def set_sim(self, name: str, value: float):
        return self._checked({"cmd": "set_sim", "name": str(name), "value": float(value)})

    def acquire(self) -> int:
        """Start an acquisition; returns its id (or raises ValueError if refused,
        e.g. dark subtraction on with no matching dark)."""
        return int(self._checked({"cmd": "acquire"})["acq_id"])

    def take_dark(self) -> int:
        """Start a dark acquisition; returns its id. Block the light first."""
        return int(self._checked({"cmd": "take_dark"})["acq_id"])

    def abort(self):
        return self._cmd({"cmd": "abort"})

    def get_sample(self) -> dict:
        return self._cmd({"cmd": "get_sample"}).get("sample", {})

    def wavelengths(self) -> np.ndarray:
        """nm of every pixel. Fetched once and cached: the calibration of the
        instrument does not change while the service runs."""
        if self._wl is None:
            self._wl = np.asarray(self._checked({"cmd": "get_wavelengths"})["values"],
                                  dtype=float)
        return self._wl.copy()

    def get_trace(self, which: str = "sample") -> dict:
        """The spectrum as numpy (`spectrum`, `wavelengths_nm`) and its
        conditions. Raises ValueError when the service has none (or it was
        aborted -- the message says which)."""
        d = self._checked({"cmd": "get_trace", "which": which})
        return trace_from_wire(d, self.wavelengths())

    def acquire_blocking(self, timeout_s: float | None = None, poll_s: float = 0.02) -> dict:
        """Trigger, wait for THIS acquisition to finish, return its spectrum.

        The wait checks the id before the flag: right after the trigger the
        cached status can still be the frame from BEFORE it, saying "not
        acquiring", and trusting that would return the previous spectrum.
        """
        return self._wait_for(self.acquire(), timeout_s, poll_s)

    def take_dark_blocking(self, timeout_s: float | None = None, poll_s: float = 0.02) -> dict:
        """Take a dark and wait for it; returns the dark spectrum."""
        self._wait_for(self.take_dark(), timeout_s, poll_s)
        return self.get_trace("dark")

    def _wait_for(self, n: int, timeout_s, poll_s) -> dict:
        limit = timeout_s if timeout_s is not None else self.cfg.acquisition.timeout_s
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            st = self._status_dict()
            if st.get("acq_id") == n and not st.get("acquiring", True):
                trace = self.get_trace("sample")
                if trace.get("acq_id") == n:
                    return trace
            time.sleep(poll_s)
        raise TimeoutError(f"acquisition {n} did not finish within {limit:g} s")

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self.stop_heartbeat()
        self._stop.set()
        time.sleep(0.25)
        self._req.close(0)
        self._sub.close(0)

    # ---- internals ------------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _checked(self, d: dict) -> dict:
        r = self._cmd(d)
        if not r.get("ok"):
            raise ValueError(r.get("error", f"{d.get('cmd')} refused"))
        return r

    def _status_dict(self) -> dict:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            d = self._cmd({"cmd": "status"}).get("status", {})
        return d

    def _new_req(self):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        s.setsockopt(zmq.LINGER, 0)
        s.connect(self._endpoint)
        return s

    def _cmd(self, d: dict) -> dict:
        self._with_identity(d)           # say who we are (control.py)
        with self._req_lock:
            self._req.send_json(d)
            try:
                reply = self._req.recv_json()
            except zmq.Again:
                # timed out; a REQ socket is now stuck mid-exchange -> rebuild it
                self._req.close(0)
                self._req = self._new_req()
                return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused),
        # never a quiet {"ok": false} -- a script must not believe the spectrometer
        # took a setting it refused. Other failures keep their error-dict shape.
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
            if poller.poll(200):
                topic, payload = self._sub.recv_multipart()
                d = json.loads(payload)
                if topic == TOPIC_STATUS:
                    with self._lock:
                        self._latest = d
                    self._control_from_status(d)
                elif topic == TOPIC_EVENT:
                    self._on_event(d.get("level", "info"), d.get("msg", ""))
