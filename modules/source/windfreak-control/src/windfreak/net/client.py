"""The client: talk to a WindfreakService, present a Synthesizer-compatible facade.

A GUI, a script, or the coordinator can hold a WindfreakClient exactly where it
would hold a Synthesizer: same method names (set_rf, set_frequency, set_power,
set_phase, set_reference, all_rf_off), same status() dict, same
get_config()/apply_config(), same `_on_event` hook. So the caller does not care
whether the synthesizer is in-process or across the lab -- only the address
changes.

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


class WindfreakClient(ControlClient):
    """``kind`` / ``name``: who this client is to the service (control.py) --
    "gui" for a window, "script" (default) for a script or console, "machine"
    only for a program that must not be locked out (scan-core, another
    module). While a GUI on another PC holds control, a script must
    ``take_control()`` before it may change anything; a refused command
    raises ``ControlRefused``. ``all_rf_off()`` is the safety verb: it works
    also while viewing."""

    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000,
                 kind: str = "script",
                 name: str = "windfreak client"):
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

    # ---- Synthesizer-compatible surface ----------------------------------

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
        """Push self.cfg to the service (it applies + re-clamps)."""
        self._cmd({"cmd": "set_config", "config": config_to_dict(self.cfg)})

    def describe(self) -> dict:
        """The service's parameter manifest: controls, indicators and actions.

        Limits inside it are LIVE, not constants, so compare
        `status()["describe_rev"]` against the manifest's `revision` rather than
        caching this forever. See `net/describe.py`.
        """
        r = self._cmd({"cmd": "describe"})
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> dict:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            r = self._cmd({"cmd": "status"})
            d = r.get("status", {})
        return d

    def fresh_status(self) -> dict:
        """Ask the service directly (not the cached PUB frame)."""
        return self._cmd({"cmd": "status"}).get("status", {})

    def set_rf(self, channel, on: bool):
        return self._cmd({"cmd": "set_rf", "channel": channel, "on": bool(on)})

    def set_frequency(self, channel, hz: float):
        return self._cmd({"cmd": "set_frequency", "channel": channel,
                          "frequency_Hz": float(hz)})

    def set_power(self, channel, dBm: float):
        return self._cmd({"cmd": "set_power", "channel": channel, "power_dBm": float(dBm)})

    def set_phase(self, channel, deg: float):
        return self._cmd({"cmd": "set_phase", "channel": channel, "phase_deg": float(deg)})

    def set_reference(self, source: str, ext_MHz: float | None = None):
        msg = {"cmd": "set_reference", "source": source}
        if ext_MHz is not None:
            msg["ext_MHz"] = float(ext_MHz)
        return self._cmd(msg)

    def set_ext_ref(self, ext_MHz: float):
        return self._cmd({"cmd": "set_ext_ref", "ext_MHz": float(ext_MHz)})

    def all_rf_off(self):
        return self._cmd({"cmd": "all_rf_off"})

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
        # Refused because another PC holds control: RAISE (ControlRefused), so
        # a script never believes the synthesizer took a setting it refused.
        # Any other failed reply is returned as before.
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
