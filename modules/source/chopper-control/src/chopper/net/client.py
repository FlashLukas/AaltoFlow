"""The client: talk to a ChopperService, present a Chopper-compatible facade.

A GUI, a script, or the coordinator can hold a ChopperClient exactly where it
would hold a Chopper brain: same method names (set_frequency, set_phase,
set_enable, set_blade, ...), same status() shape, same
get_config()/apply_config(), same `_on_event` hook. So the caller does not care
whether the brain is in-process or across the lab -- only the address changes.

A refused command (a blade change while running, a frequency on external
reference) raises RuntimeError with the service's reason, just as the local
brain raises ValueError -- the GUI shows either in its log.

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


class RemoteStatus:
    """Same attributes a caller reads off the brain's Status. JSON null (no
    reading) comes back as NaN, like the local brain reports it."""

    _NUM = ("setpoint_frequency_Hz", "target_frequency_Hz", "frequency_Hz",
            "freq_error_Hz", "refout_frequency_Hz", "input_frequency_Hz",
            "freq_min_Hz", "freq_max_Hz", "poll_ms")

    def __init__(self, d: dict):
        for k in self._NUM:
            v = d.get(k)
            setattr(self, k, float("nan") if v is None else float(v))
        self.connected = bool(d.get("connected", False))
        self.simulated = bool(d.get("simulated", False))
        self.idn = d.get("idn", "")
        self.hw_error = d.get("hw_error", "")
        self.blade = d.get("blade", "")
        self.ref_mode = d.get("ref_mode", "")
        self.output_mode = d.get("output_mode", "")
        self.external = bool(d.get("external", False))
        self.enabled = bool(d.get("enabled", False))
        self.locked = bool(d.get("locked", False))
        self.lock_source = d.get("lock_source", "")
        self.lock_gen = int(d.get("lock_gen", 0) or 0)
        self.phase_deg = float(d.get("phase_deg", 0.0) or 0.0)
        self.nharmonic = int(d.get("nharmonic", 1) or 1)
        self.dharmonic = int(d.get("dharmonic", 1) or 1)
        self.owned_blades = list(d.get("owned_blades", []) or [])
        self.readings = int(d.get("readings", 0) or 0)
        # None from a service that predates `describe`.
        self.describe_rev = d.get("describe_rev")


class ChopperClient:
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

    # ---- Chopper-compatible surface ------------------------------------

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

    def _checked(self, d: dict) -> dict:
        r = self._cmd(d)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "command failed"))
        return r

    def set_frequency(self, hz: float) -> float:
        return self._checked({"cmd": "set_frequency", "frequency_Hz": float(hz)}).get(
            "frequency_Hz", float(hz))

    def set_phase(self, deg: float) -> float:
        return self._checked({"cmd": "set_phase", "phase_deg": float(deg)}).get(
            "phase_deg", float(deg))

    def set_enable(self, on: bool) -> int:
        return int(self._checked({"cmd": "set_enable", "on": bool(on)}).get("lock_gen", 0))

    def set_blade(self, name: str) -> None:
        self._checked({"cmd": "set_blade", "blade": str(name)})

    def set_ref_mode(self, mode: str) -> None:
        self._checked({"cmd": "set_ref_mode", "mode": str(mode)})

    def set_output_mode(self, mode: str) -> None:
        self._checked({"cmd": "set_output_mode", "mode": str(mode)})

    def set_harmonics(self, n=None, d=None) -> None:
        msg = {"cmd": "set_harmonics"}
        if n is not None:
            msg["n"] = int(n)
        if d is not None:
            msg["d"] = int(d)
        self._checked(msg)

    def freq_limits(self) -> tuple[float, float]:
        s = self.status()
        return s.freq_min_Hz, s.freq_max_Hz

    def mode_options(self) -> tuple[tuple, tuple]:
        """Reference in / out names of the mounted blade (from the blade table,
        the same one the service uses)."""
        from ..blades import blade_by_name
        try:
            b = blade_by_name(self.status().blade)
        except ValueError:
            return (), ()
        return b.ref_modes, b.output_modes

    def blade_options(self) -> list[str]:
        s = self.status()
        owned = list(s.owned_blades)
        return owned + ([s.blade] if s.blade and s.blade not in owned else [])

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self._stop.set()
        time.sleep(0.25)
        self._req.close(0)
        self._sub.close(0)

    # ---- internals -------------------------------------------------------

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
