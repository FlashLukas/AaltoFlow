"""The client: talk to an ShsgService, present a Generator-compatible facade.

A GUI, a script, or the coordinator can hold an ShsgClient exactly where it would
hold a Generator: same method names (set_rf, set_power, set_frequency), same status() shape, same get_config()/apply_config(), same
`_on_event` hook. So the caller does not care whether the generator is in-process
or across the lab -- only the address changes.

A background thread owns the SUB socket and keeps the latest status; commands go
out on a REQ socket guarded by a lock (REQ is strict request/reply, one at a time).
"""

from __future__ import annotations

import json
import threading
import time

import zmq

from .. import secure
from ..config import Config
from ..control import ControlClient
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict)


class RemoteStatus:
    """Same attributes a caller reads off the Generator's Status."""
    def __init__(self, d: dict):
        self.rf_on = d.get("rf_on", False)
        self.power_dBm = d.get("power_dBm", 0.0)
        self.frequency_Hz = d.get("frequency_Hz", 0.0)
        self.connected = d.get("connected", False)
        self.idn = d.get("idn", "")
        self.parked = d.get("parked", False)
        self.park_Hz = d.get("park_Hz")
        self.park_dBm = d.get("park_dBm")
        self.tg_busy = d.get("tg_busy", False)
        self.tg_unknown = d.get("tg_unknown", False)
        self.tg_ready = d.get("tg_ready", False)
        self.hw_error = d.get("hw_error", "")
        # the SWEEPS (an older service has none: never sweeping); the flat
        # per-knob keys are kept as a dict, e.g. sweep["power_ramp_id"]
        self.ramping = bool(d.get("ramping", False))
        self.sweep = {k: v for k, v in d.items() if "ramp" in k}
        # None from a service that predates `describe`.
        self.describe_rev = d.get("describe_rev")


class ShsgClient(ControlClient):
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
                 name: str = "shsg client"):
        self._control_setup(kind, name)
        self._ctx = zmq.Context.instance()
        self._host = host
        self._cmd_port = int(cmd_port)
        self._pub_port = int(pub_port)
        self._timeout_ms = int(timeout_ms)
        self._make_req()
        self._sub, self._sub_gen = self._make_sub()

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self.cfg = Config()          # kept in sync with the service via get/set_config
        self._on_event = lambda level, msg: None

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- Generator-compatible surface ------------------------------------

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

    # The setters RAISE on a refusal ({"ok": false}), exactly like the local
    # Generator does, so the GUI handles both the same way.
    def set_rf(self, on: bool):
        self._checked({"cmd": "set_rf", "on": bool(on)})

    def rf_off(self):
        """RF off (= park) -- the safety verb, allowed also while viewing."""
        self._checked({"cmd": "rf_off"})

    def set_power(self, dBm: float):
        self._checked({"cmd": "set_power", "power_dBm": float(dBm)})

    def set_frequency(self, hz: float):
        self._checked({"cmd": "set_frequency", "frequency_Hz": float(hz)})

    # ---- the SWEEPS (fly scans, the GUI's Sweep card) --------------------

    def ramp_frequency(self, hz: float, rate_Hz_per_s: float):
        return self._checked({"cmd": "ramp_frequency", "frequency_Hz": float(hz),
                              "rate_Hz_per_s": float(rate_Hz_per_s)}).get("ramp_id")

    def ramp_power(self, dBm: float, rate_dB_per_s: float):
        return self._checked({"cmd": "ramp_power", "power_dBm": float(dBm),
                              "rate_dB_per_s": float(rate_dB_per_s)}).get("ramp_id")

    def ramp_stop(self, knob: str | None = None):
        """End a sweep where it is (all of them without `knob`). A safety
        verb: allowed also while viewing."""
        d = {"cmd": "ramp_stop"}
        if knob:
            d["knob"] = knob
        return self._checked(d).get("stopped")

    def _checked(self, d: dict) -> dict:
        r = self._cmd(d)
        if not r.get("ok", False):
            raise RuntimeError(r.get("error", "refused"))
        return r

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
                    flipped = secure.no_answer(self._host, "shsg")
                    self._make_req()
                    if not (flipped and attempt == 1):
                        return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused), so
        # a script never believes the generator took a setting it refused.
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
        # secures shsg (secure.py); plain otherwise
        secure.secure_client(self._req, self._host, "shsg")
        self._req.connect(f"tcp://{self._host}:{self._cmd_port}")

    def _make_sub(self):
        s = self._ctx.socket(zmq.SUB)
        secure.secure_client(s, self._host, "shsg")      # telemetry too
        s.connect(f"tcp://{self._host}:{self._pub_port}")
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
                self._sub, self._sub_gen = self._make_sub()
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
