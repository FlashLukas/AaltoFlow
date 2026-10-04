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
from .. import secure
from ..control import ControlClient
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


class Usb6001Client(ControlClient):
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
                 name: str = "usb6001 client"):
        self._control_setup(kind, name)
        self._ctx = zmq.Context.instance()
        self._timeout_ms = timeout_ms
        self._host = host
        self._endpoint = f"tcp://{host}:{cmd_port}"
        self._pub_endpoint = f"tcp://{host}:{pub_port}"
        self._req = self._new_req()

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
        self.stop_heartbeat()
        self._stop.set()
        time.sleep(0.25)
        self._sub_t.join(timeout=1.0)    # the listener closes its own SUB socket
        self._req.close(0)

    # ---- internals -------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _cmd(self, d: dict) -> dict:
        self._with_identity(d)           # say who we are (control.py)
        with self._req_lock:
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
                    flipped = secure.no_answer(self._host, "usb6001")
                    self._req = self._new_req()
                    if not (flipped and attempt == 1):
                        return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused),
        # never a quiet {"ok": false} -- a script must not believe the output
        # was set. Other failures keep their old shape (`_checked` turns them
        # into ValueError).
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
        # secures usb6001 (secure.py); plain otherwise
        secure.secure_client(s, self._host, "usb6001")
        s.connect(self._endpoint)
        return s

    def _listen(self):
        # The SUB socket lives in this thread only (a ZeroMQ socket must not
        # be shared across threads), so it is created and closed here.
        def make_sub():
            s = self._ctx.socket(zmq.SUB)
            s.setsockopt(zmq.LINGER, 0)
            secure.secure_client(s, self._host, "usb6001")      # telemetry too
            s.connect(self._pub_endpoint)
            s.setsockopt(zmq.SUBSCRIBE, b"")
            return s, secure.flip_generation()

        sub, gen = make_sub()
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if gen != secure.flip_generation():
                    # a request found the service in the other mode
                    # (secure.no_answer): telemetry follows
                    poller.unregister(sub)
                    sub.close(0)
                    sub, gen = make_sub()
                    poller.register(sub, zmq.POLLIN)
                if poller.poll(200):
                    topic, payload = sub.recv_multipart()
                    d = json.loads(payload)
                    if topic == TOPIC_STATUS:
                        with self._lock:
                            self._latest = d
                        self._control_from_status(d)
                    elif topic == TOPIC_EVENT:
                        self._on_event(d.get("level", "info"), d.get("msg", ""))
        finally:
            sub.close(0)
