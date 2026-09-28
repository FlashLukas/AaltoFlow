"""The client: talk to an Usb6001Service, present a Daq-compatible facade.

A GUI, a script, or the coordinator can hold an Usb6001Client exactly where it
would hold a Daq: same method names (set_ao, set_do, read_ai, read_di, acquire),
same status() shape, same get_config()/apply_config(), same `_on_event` hook. So the caller does not care whether the card is in-process
or across the lab -- only the address changes.

A background thread owns the SUB socket and keeps the latest status; commands go
out on a REQ socket guarded by a lock (REQ is strict request/reply, one at a time).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import MISSING, fields

import zmq

from ..config import Config
from ..daq import Status
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict)


class RemoteStatus:
    """Same attributes a caller reads off the Daq's Status, built from the
    wire dict. Missing keys get the Status defaults, so an older service does
    not break a newer GUI."""

    def __init__(self, d: dict):
        for f in fields(Status):
            default = Status.__dataclass_fields__[f.name]
            if f.name in d:
                val = d[f.name]
            elif default.default_factory is not MISSING:        # lists
                val = default.default_factory()
            else:
                val = default.default
            setattr(self, f.name, val)
        # JSON has no NaN guarantee across tools; accept null for "unknown"
        self.ao_V = [float("nan") if v is None else v for v in self.ao_V]
        # None from a service that predates `describe`.
        self.describe_rev = d.get("describe_rev")


class Usb6001Client:
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

    # ---- Daq-compatible surface ------------------------------------------

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

    # Errors come back as {"ok": false}; the GUI expects an exception, like
    # the local Daq raises, so it can show the message.
    def set_ao(self, channel, volts: float):
        return self._checked({"cmd": "set_ao", "channel": channel, "volts": float(volts)}).get("volts")

    def set_do(self, line, state: bool):
        return self._checked({"cmd": "set_do", "line": line, "state": bool(state)}).get("state")

    def read_ai(self, channel=None) -> dict:
        r = self._checked({"cmd": "read_ai", "channel": channel})
        r.pop("ok", None)
        return r

    def read_di(self, line=None) -> dict:
        r = self._checked({"cmd": "read_di", "line": line})
        r.pop("ok", None)
        return r

    def acquire(self) -> int:
        return int(self._checked({"cmd": "acquire"})["acq_id"])

    def get_sample(self) -> dict:
        return self._checked({"cmd": "get_sample"}).get("sample", {})

    def save_config(self, path=None) -> str:
        return self._checked({"cmd": "save_config", "path": path}).get("path", "")

    def _checked(self, d: dict) -> dict:
        r = self._cmd(d)
        if not r.get("ok"):
            raise ValueError(r.get("error", "command failed"))
        return r

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
