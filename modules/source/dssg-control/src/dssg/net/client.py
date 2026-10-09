"""The client: talk to a DssgService, present a Synthesizer-compatible facade.

A GUI, a script, or the coordinator can hold a DssgClient exactly where it would
hold a Synthesizer: same method names (set_rf, set_power, set_frequency,
set_phase, set_vernier, set_reference, limits, has_phase, has_vernier), same status() shape, same get_config()/apply_config(), same
`_on_event` hook. So the caller does not care whether the brain is in-process
or across the lab -- only the address changes.

All replies are checked: a refused command (e.g. phase on a unit without it)
raises RuntimeError, so a script cannot mistake 'refused' for 'done'.

A background thread owns the SUB socket and keeps the latest status; commands go
out on a REQ socket guarded by a lock (REQ is strict request/reply, one at a time).
"""

from __future__ import annotations

import json
import threading

import zmq

from .. import secure
from ..config import Config
from ..control import ControlClient
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict)


class RemoteStatus:
    """Same attributes a caller reads off the Synthesizer's Status."""
    def __init__(self, d: dict):
        self.rf_on = d.get("rf_on", False)
        self.frequency_Hz = d.get("frequency_Hz", 0.0)
        self.power_dBm = d.get("power_dBm", 0.0)
        self.phase_deg = d.get("phase_deg", 0.0)
        self.vernier = d.get("vernier", 0)
        self.reference = d.get("reference", "auto")
        self.ext_ref_detected = d.get("ext_ref_detected", False)
        self.usb_volts = d.get("usb_volts", 0.0)
        self.connected = d.get("connected", False)
        self.has_phase = d.get("has_phase", False)
        self.has_vernier = d.get("has_vernier", False)
        self.fine_power = d.get("fine_power", False)
        self.attenuator_dBm = d.get("attenuator_dBm", self.power_dBm)
        self.power_calibrated = d.get("power_calibrated", False)
        self.power_calibration = d.get("power_calibration", "")
        self.idn = d.get("idn", "")
        self.hw_error = d.get("hw_error", "")
        self.freq_min_Hz = d.get("freq_min_Hz", 0.0)
        self.freq_max_Hz = d.get("freq_max_Hz", 0.0)
        self.power_min_dBm = d.get("power_min_dBm", 0.0)
        self.power_max_dBm = d.get("power_max_dBm", 0.0)
        self.polls = d.get("polls", 0)
        # the frequency SWEEP (an older service has none: never sweeping)
        self.ramping = bool(d.get("ramping", False))
        self.ramp_id = d.get("ramp_id", 0)
        self.ramp_target_Hz = d.get("ramp_target_Hz", 0.0)
        self.ramp_rate_Hz_per_s = d.get("ramp_rate_Hz_per_s", 0.0)
        # None from a service that predates `describe`.
        self.describe_rev = d.get("describe_rev")


class DssgClient(ControlClient):
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
                 name: str = "dssg client"):
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

    # ---- Synthesizer-compatible surface ------------------------------------

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

    def set_rf(self, on: bool):
        self._checked({"cmd": "set_rf", "on": bool(on)})

    def rf_off(self):
        """RF off -- the safety verb, allowed also while viewing."""
        self._checked({"cmd": "rf_off"})

    def set_power(self, dBm: float):
        self._checked({"cmd": "set_power", "power_dBm": float(dBm)})

    def set_frequency(self, hz: float):
        self._checked({"cmd": "set_frequency", "frequency_Hz": float(hz)})

    def ramp_frequency(self, hz: float, rate_Hz_per_s: float):
        """Sweep the frequency (fly scans, the GUI's Sweep button)."""
        return self._checked({"cmd": "ramp_frequency", "frequency_Hz": float(hz),
                              "rate_Hz_per_s": float(rate_Hz_per_s)}).get("ramp_id")

    def ramp_stop(self):
        """End a sweep where it is (a safety verb: allowed also while viewing)."""
        return self._checked({"cmd": "ramp_stop"}).get("stopped")

    def set_phase(self, deg: float):
        self._checked({"cmd": "set_phase", "phase_deg": float(deg)})

    def set_vernier(self, n: int):
        """Fine power trim in raw integer counts (no unit)."""
        self._checked({"cmd": "set_vernier", "vernier": int(round(float(n)))})

    def set_reference(self, mode: str):
        self._checked({"cmd": "set_reference", "mode": str(mode)})

    def limits(self) -> dict:
        """The effective envelope, from the latest status frame."""
        s = self.status()
        lim = self.cfg.limits
        return {"freq_min_Hz": s.freq_min_Hz, "freq_max_Hz": s.freq_max_Hz,
                "power_min_dBm": s.power_min_dBm, "power_max_dBm": s.power_max_dBm,
                "phase_min_deg": lim.phase_min_deg, "phase_max_deg": lim.phase_max_deg,
                "vernier_min": int(lim.vernier_min), "vernier_max": int(lim.vernier_max)}

    def has_phase(self) -> bool:
        return bool(self.status().has_phase)

    def has_vernier(self) -> bool:
        return bool(self.status().has_vernier)

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self.stop_heartbeat()
        self._stop.set()
        # the listener thread owns the SUB socket and closes it on its way out
        self._sub_t.join(timeout=1.0)
        with self._req_lock:
            self._req.close(0)

    # ---- internals -------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _checked(self, d: dict) -> dict:
        r = self._cmd(d)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "command refused"))
        return r

    def _make_req(self) -> None:
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        self._req.setsockopt(zmq.SNDTIMEO, self._timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures dssg (secure.py); plain otherwise
        secure.secure_client(self._req, self.host, "dssg")
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
                    # timed out; the REQ socket is now stuck mid-exchange -> rebuild it
                    self._req.close(0)
                    # The service may speak the other mode than the policy now
                    # says (it was started before the policy changed): the new
                    # socket tries that mode, once. Safe to resend: a request
                    # in the wrong mode never reaches the service.
                    flipped = secure.no_answer(self.host, "dssg")
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
            secure.secure_client(s, self.host, "dssg")      # telemetry too
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
