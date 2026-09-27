"""The client: talk to a Cs260Service, present a Monochromator-compatible facade.

A GUI, a script, or the coordinator can hold a Cs260Client exactly where it
would hold a Monochromator: same method names (set_wavelength, set_grating,
set_shutter, ...), same status() attributes, same get_config()/apply_config(),
same `_on_event` hook. So the caller does not care whether the monochromator is
in-process or across the lab -- only the address changes.

A background thread owns the SUB socket and keeps the latest status; commands go
out on a REQ socket guarded by a lock (REQ is strict request/reply, one at a time).
A command that the service refuses raises RuntimeError here, the same way the
in-process brain raises -- so the GUI handles both alike.
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
    """Same attributes a caller reads off the brain's Status. Every key of the
    status dict becomes an attribute, so a new status field needs no edit here;
    the defaults below only cover a frame that has not arrived yet."""

    _DEFAULTS = {
        "connected": False, "simulated": True, "idn": "", "hw_error": "",
        "wavelength_nm": float("nan"), "target_nm": float("nan"), "moving": False,
        "busy": "", "wl_min_nm": 0.0, "wl_max_nm": 0.0, "grating": 0,
        "grating_target": 0, "grating_lines": 0, "grating_label": "", "n_gratings": 1,
        "bandpass_nm": float("nan"), "shutter_open": False, "filter": 0,
        "filter_target": 0, "filter_label": "", "filter_fitted": False, "port": 1,
        "port_target": 1, "port_fitted": False, "step_position": 0, "error_code": -1,
        "error_text": "", "moves": 0, "readings": 0, "poll_ms": float("nan"),
        # None from a service that predates `describe`.
        "describe_rev": None,
    }

    def __init__(self, d: dict):
        for k, v in self._DEFAULTS.items():
            setattr(self, k, d.get(k, v))
        for k, v in d.items():
            if k not in self._DEFAULTS:
                setattr(self, k, v)


class Cs260Client:
    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000):
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

    # ---- Monochromator-compatible surface ------------------------------------

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
        """The service's parameter manifest: controls, indicators and actions.

        Limits inside it are LIVE, not constants, so compare
        `status().describe_rev` against the manifest's `revision` rather than
        caching this forever. See `net/describe.py`.
        """
        r = self._cmd({"cmd": "describe"})
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> RemoteStatus:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            r = self._cmd({"cmd": "status"})
            d = r.get("status", {})
        return RemoteStatus(d)

    def set_wavelength(self, nm: float) -> float:
        return self._ok({"cmd": "set_wavelength", "wavelength_nm": float(nm)}).get("target_nm", nm)

    def set_grating(self, n: int) -> int:
        return self._ok({"cmd": "set_grating", "grating": int(n)}).get("grating", n)

    def set_shutter(self, open_: bool) -> None:
        self._ok({"cmd": "set_shutter", "open": bool(open_)})

    def set_filter(self, n: int) -> int:
        return self._ok({"cmd": "set_filter", "filter": int(n)}).get("filter", n)

    def set_port(self, n: int) -> int:
        return self._ok({"cmd": "set_port", "port": int(n)}).get("port", n)

    def step(self, steps: int) -> None:
        self._ok({"cmd": "step", "steps": int(steps)})

    def abort(self) -> None:
        self._ok({"cmd": "abort"})

    def calibrate(self, nm: float) -> None:
        self._ok({"cmd": "calibrate", "wavelength_nm": float(nm)})

    def limits_for(self, grating: int) -> tuple[float, float]:
        """The range of one grating, from the service's `info`."""
        for g in self.info().get("gratings", []):
            if g.get("n") == int(grating):
                return float(g["min_nm"]), float(g["max_nm"])
        return 0.0, 0.0

    def live_limits(self) -> tuple[float, float]:
        s = self.status()
        return float(s.wl_min_nm), float(s.wl_max_nm)

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self._stop.set()
        time.sleep(0.25)
        self._req.close(0)
        self._sub.close(0)

    # ---- internals -------------------------------------------------------

    def _ok(self, d: dict) -> dict:
        r = self._cmd(d)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "command failed"))
        return r

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

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
        self._req.setsockopt(zmq.RCVTIMEO, 3000)
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
