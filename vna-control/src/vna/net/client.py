"""The client: talk to a VnaService, present an Analyzer-compatible facade.

A GUI or script can hold a VnaClient exactly where it would hold an Analyzer:
same method names, same status() attributes, same get_config()/apply_config()
and `_on_event` hook. Only the address changes.

It adds `acquire_blocking()` and `take_reference_blocking()` for scripts:
trigger, wait for THIS acquisition, return its trace.
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import zmq

from ..config import Config
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict, trace_from_wire)

_NAN = float("nan")


class RemoteStatus:
    """Same attributes a caller reads off the Analyzer's Status. JSON null (no
    value) comes back as NaN, so arithmetic and formatting just work."""

    _DEFAULTS = {"connected": False, "idn": "", "hw_error": "", "points": 0,
                 "averages": 1, "continuous": True, "sweeping": False,
                 "sweep_progress": 0.0, "sweeps": 0, "trace_id": 0,
                 "field_source_set": "", "field_source": "", "field_ok": False,
                 "geometry": "in_plane", "acq_id": 0, "acquiring": False,
                 "acq_progress": 0.0, "simulated": True, "sparam": "S21",
                 "acq_is_reference": False}

    def __init__(self, d: dict):
        for k, default in self._DEFAULTS.items():
            setattr(self, k, d.get(k, default))
        for k, v in d.items():
            if k not in self._DEFAULTS and k not in ("sample", "reference"):
                setattr(self, k, _NAN if v is None else v)
        self.sample = {k: (_NAN if v is None else v) for k, v in (d.get("sample") or {}).items()}
        self.reference = {k: (_NAN if v is None else v)
                          for k, v in (d.get("reference") or {"present": False}).items()}
        self.describe_rev = d.get("describe_rev")

    def __getattr__(self, name):
        # A float field the service has not sent yet (first frame) reads as NaN
        # rather than crashing the GUI's first refresh.
        if name.endswith(("_Hz", "_mT", "_dB", "_dBm", "_s", "_deg")) or name in ("alpha",):
            return _NAN
        raise AttributeError(name)


class VnaClient:
    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000):
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

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- Analyzer-compatible surface ------------------------------------------

    def start(self) -> dict:
        """Fetch static info and pull the service's config into self.cfg."""
        info = self.info()
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

    def set_start(self, hz):          return self._cmd({"cmd": "set_start", "start_Hz": float(hz)})
    def set_stop(self, hz):           return self._cmd({"cmd": "set_stop", "stop_Hz": float(hz)})
    def set_points(self, n):          return self._cmd({"cmd": "set_points", "points": int(n)})
    def set_ifbw(self, hz):           return self._cmd({"cmd": "set_ifbw", "ifbw_Hz": float(hz)})
    def set_power(self, dbm):         return self._cmd({"cmd": "set_power", "power_dBm": float(dbm)})
    def set_averages(self, n):        return self._cmd({"cmd": "set_averages", "averages": int(n)})
    def set_sparam(self, sp):         return self._checked({"cmd": "set_sparam", "sparam": str(sp)})
    def set_manual_angle(self, deg):  return self._cmd({"cmd": "set_manual_angle", "angle_deg": float(deg)})
    def clear_reference(self):        return self._cmd({"cmd": "clear_reference"})
    def set_continuous(self, on):     return self._cmd({"cmd": "set_continuous", "on": bool(on)})
    def set_field_source(self, src):  return self._checked({"cmd": "set_field_source", "source": str(src)})
    def set_manual_field(self, mT, angle_deg=None):
        msg = {"cmd": "set_manual_field", "field_mT": float(mT)}
        if angle_deg is not None:
            msg["angle_deg"] = float(angle_deg)
        return self._cmd(msg)

    def set_geometry(self, g):        return self._checked({"cmd": "set_geometry", "geometry": str(g)})

    def set_sample(self, name: str, value: float):
        return self._checked({"cmd": "set_sample", "name": str(name), "value": float(value)})

    def acquire(self) -> int:
        """Start an acquisition; returns its id (or raises if refused)."""
        return int(self._checked({"cmd": "acquire"})["acq_id"])

    def take_reference(self) -> int:
        """Start a reference acquisition; returns its id (or raises if refused)."""
        return int(self._checked({"cmd": "take_reference"})["acq_id"])

    def abort(self):
        return self._cmd({"cmd": "abort"})

    def get_sample(self) -> dict:
        return self._cmd({"cmd": "get_sample"}).get("sample", {})

    def get_trace(self, which: str = "sample", quantity: str = "s") -> dict:
        """The trace as numpy: `s`, `u` or `ln` (complex), freqs_Hz, and its
        conditions. Raises ValueError when the service has none (or it was
        aborted, or u / ln has no matching reference -- the message says which)."""
        return trace_from_wire(self._checked({"cmd": "get_trace", "which": which,
                                              "quantity": quantity}))

    def frequencies(self) -> np.ndarray:
        return np.asarray(self._checked({"cmd": "get_frequencies"})["values"], dtype=float)

    def acquire_blocking(self, timeout_s: float | None = None, poll_s: float = 0.02) -> dict:
        """Trigger, wait for THIS acquisition to finish, return its trace.

        The wait checks the id before the flag: right after the trigger the
        cached status can still be the frame from BEFORE it, saying "not
        acquiring", and trusting that would return the previous trace.
        """
        return self._wait_for(self.acquire(), timeout_s, poll_s)

    def take_reference_blocking(self, timeout_s: float | None = None, poll_s: float = 0.02) -> dict:
        """Take a reference and wait for it; returns the reference trace."""
        self._wait_for(self.take_reference(), timeout_s, poll_s)
        return self.get_trace("reference")

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
        with self._req_lock:
            self._req.send_json(d)
            try:
                return self._req.recv_json()
            except zmq.Again:
                # timed out; a REQ socket is now stuck mid-exchange -> rebuild it
                self._req.close(0)
                self._req = self._new_req()
                return {"ok": False, "error": "service did not respond (timeout)"}

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
