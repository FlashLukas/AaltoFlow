"""The client: talk to an Hp8648Service, present a SignalSource-compatible facade.

A GUI, a script, or the coordinator can hold an Hp8648Client exactly where it
would hold a SignalSource: same method names (set_rf, set_power,
set_frequency), same status() shape, same get_config()/apply_config(), same
`_on_event` hook. So the caller does not care whether the generator is
in-process or across the lab -- only the address changes.

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
    """Same attributes a caller reads off the SignalSource's Status."""
    def __init__(self, d: dict):
        self.rf_on = bool(d.get("rf_on", False))
        self.frequency_Hz = float(d.get("frequency_Hz", 0.0))
        self.power_dBm = float(d.get("power_dBm", 0.0))
        self.rf_set = bool(d.get("rf_set", False))
        self.frequency_set_Hz = float(d.get("frequency_set_Hz", self.frequency_Hz))
        self.power_set_dBm = float(d.get("power_set_dBm", self.power_dBm))
        self.power_ceiling_dBm = float(d.get("power_ceiling_dBm", 0.0))
        self.spec_max_dBm = float(d.get("spec_max_dBm", 0.0))
        self.rpp_tripped = bool(d.get("rpp_tripped", False))
        self.level_unspecified = bool(d.get("level_unspecified", False))
        self.modulation_off = bool(d.get("modulation_off", True))
        self.modulation = dict(d.get("modulation") or {})
        self.connected = bool(d.get("connected", False))
        self.idn = d.get("idn", "")
        self.hw_error = d.get("hw_error", "")
        # the SWEEPS (an older service has none: never sweeping); the flat
        # per-knob keys are kept as a dict, e.g. sweep["power_ramp_id"]
        self.ramping = bool(d.get("ramping", False))
        self.sweep = {k: v for k, v in d.items() if "ramp" in k}
        self.describe_rev = d.get("describe_rev")


class Hp8648Client(ControlClient):
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
                 name: str = "hp8648 client"):
        self._control_setup(kind, name)
        self.host = host
        self._cmd_port = cmd_port
        self._pub_port = pub_port
        self._timeout_ms = int(timeout_ms)
        self._ctx = zmq.Context.instance()
        self._make_req()

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self.cfg = Config()          # kept in sync with the service via get/set_config
        self._on_event = lambda level, msg: None

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- SignalSource-compatible surface ---------------------------------

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
        """The service's parameter manifest. Its power limit is LIVE (it
        follows the frequency), so compare `status().describe_rev` against the
        manifest's `revision` rather than caching it forever."""
        r = self._cmd({"cmd": "describe"})
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> RemoteStatus:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            r = self._cmd({"cmd": "status"})
            d = r.get("status", {})
        return RemoteStatus(d)

    def power_ceiling(self, freq_Hz: float | None = None) -> float:
        """The service's live ceiling at its CURRENT frequency (the argument
        exists for signature compatibility with SignalSource)."""
        return self.status().power_ceiling_dBm

    def set_rf(self, on: bool):
        return self._cmd({"cmd": "set_rf", "on": bool(on)})

    def rf_off(self):
        """RF off -- the safety verb, allowed also while viewing."""
        return self._cmd({"cmd": "rf_off"})

    def set_power(self, dBm: float):
        return self._cmd({"cmd": "set_power", "power_dBm": float(dBm)})

    def set_frequency(self, hz: float):
        return self._cmd({"cmd": "set_frequency", "frequency_Hz": float(hz)})

    # ---- the SWEEPS (fly scans, the GUI's Sweep card) --------------------

    def _ramp(self, d: dict):
        r = self._cmd(d)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "sweep refused"))
        return r.get("ramp_id")

    def ramp_frequency(self, hz: float, rate_Hz_per_s: float):
        return self._ramp({"cmd": "ramp_frequency", "frequency_Hz": float(hz),
                           "rate_Hz_per_s": float(rate_Hz_per_s)})

    def ramp_power(self, dBm: float, rate_dB_per_s: float):
        return self._ramp({"cmd": "ramp_power", "power_dBm": float(dBm),
                           "rate_dB_per_s": float(rate_dB_per_s)})

    def ramp_stop(self, knob: str | None = None):
        """End a sweep where it is (all of them without `knob`). A safety
        verb: allowed also while viewing."""
        d = {"cmd": "ramp_stop"}
        if knob:
            d["knob"] = knob
        return self._cmd(d).get("stopped")

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self.stop_heartbeat()
        self._stop.set()
        self._sub_t.join(timeout=1.0)
        with self._req_lock:
            self._req.close(0)
        # (the listener thread owns the SUB socket and closes it on its way out)

    # ---- internals -------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _make_req(self) -> None:
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        self._req.setsockopt(zmq.SNDTIMEO, self._timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures hp8648 (secure.py); plain otherwise
        secure.secure_client(self._req, self.host, "hp8648")
        self._req.connect(f"tcp://{self.host}:{self._cmd_port}")

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
                    flipped = secure.no_answer(self.host, "hp8648")
                    self._make_req()
                    if not (flipped and attempt == 1):
                        return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused), so
        # a script never believes the generator took a setting it refused.
        # Any other failed reply is returned as before.
        if not reply.get("ok", False):
            self._raise_refusal(reply)
        return reply

    def _rpc(self, **req) -> dict:
        """The name control.py's ControlClient calls (heartbeat, take_control)."""
        return self._cmd(req)

    def _listen(self):
        def make_sub():
            s = self._ctx.socket(zmq.SUB)
            secure.secure_client(s, self.host, "hp8648")      # telemetry too
            s.connect(f"tcp://{self.host}:{self._pub_port}")
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
                    try:
                        topic, payload = sub.recv_multipart()
                        d = json.loads(payload)
                    except Exception:
                        continue
                    if topic == TOPIC_STATUS:
                        with self._lock:
                            self._latest = d
                        self._control_from_status(d)
                    elif topic == TOPIC_EVENT:
                        try:
                            self._on_event(d.get("level", "info"), d.get("msg", ""))
                        except Exception:
                            pass
        finally:
            sub.close(0)
