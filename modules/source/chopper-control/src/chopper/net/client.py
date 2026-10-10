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

from .. import secure
from ..config import Config
from ..control import ControlClient
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict)


class RemoteStatus:
    """Same attributes a caller reads off the brain's Status. JSON null (no
    reading) comes back as NaN, like the local brain reports it."""

    _NUM = ("setpoint_frequency_Hz", "target_frequency_Hz", "frequency_Hz",
            "freq_error_Hz", "refout_frequency_Hz", "input_frequency_Hz",
            "freq_min_Hz", "freq_max_Hz", "poll_ms", "ramp_target_Hz",
            "ramp_rate_Hz_per_s")

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
        # the frequency SWEEP (an older service has none: never sweeping)
        self.ramping = bool(d.get("ramping", False))
        self.ramp_id = int(d.get("ramp_id", 0) or 0)
        # None from a service that predates `describe`.
        self.describe_rev = d.get("describe_rev")


class ChopperClient(ControlClient):
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
                 name: str = "chopper client"):
        self._control_setup(kind, name)
        self._ctx = zmq.Context.instance()
        # kept: the sockets are REBUILT after a timeout (and in the
        # other security mode after secure.no_answer)
        self._host = host
        self._cmd_endpoint = f"tcp://{host}:{cmd_port}"
        self._pub_endpoint = f"tcp://{host}:{pub_port}"
        self._timeout_ms = timeout_ms
        self._req = self._new_req(self._cmd_endpoint)
        self._sub, self._sub_gen = self._new_sub()

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

    def _checked(self, d: dict) -> dict:
        r = self._cmd(d)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "command failed"))
        return r

    def set_frequency(self, hz: float) -> float:
        return self._checked({"cmd": "set_frequency", "frequency_Hz": float(hz)}).get(
            "frequency_Hz", float(hz))

    def ramp_frequency(self, hz: float, rate_Hz_per_s: float) -> int:
        """Sweep the chopping frequency at a set pace (fly scans, the GUI's Sweep)."""
        return self._checked({"cmd": "ramp_frequency", "frequency_Hz": float(hz),
                              "rate_Hz_per_s": float(rate_Hz_per_s)}).get("ramp_id")

    def ramp_stop(self) -> bool:
        """End a sweep where it is (a safety verb: allowed also while viewing)."""
        return self._checked({"cmd": "ramp_stop"}).get("stopped")

    def set_phase(self, deg: float) -> float:
        return self._checked({"cmd": "set_phase", "phase_deg": float(deg)}).get(
            "phase_deg", float(deg))

    def set_enable(self, on: bool) -> int:
        return int(self._checked({"cmd": "set_enable", "on": bool(on)}).get("lock_gen", 0))

    def standby(self) -> int:
        """Stop the wheel -- the safety verb `stop`, allowed also while viewing."""
        return int(self._checked({"cmd": "stop"}).get("lock_gen", 0))

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
                    # timed out: the REQ socket is stuck mid-transaction ->
                    # rebuild it.
                    self._req.close(0)
                    # The service may speak the other mode than the policy now
                    # says (it was started before the policy changed): the new
                    # socket tries that mode, once. Safe to resend: a request
                    # in the wrong mode never reaches the service.
                    flipped = secure.no_answer(self._host, "chopper")
                    self._req = self._new_req(self._cmd_endpoint)
                    if not (flipped and attempt == 1):
                        return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused), so
        # a script never believes the chopper took a setting it refused.
        if not reply.get("ok", False):
            self._raise_refusal(reply)
        return reply

    def _rpc(self, **req) -> dict:
        """The name control.py's ControlClient calls (heartbeat, take_control)."""
        return self._cmd(req)

    def _new_req(self, endpoint: str):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        s.setsockopt(zmq.SNDTIMEO, self._timeout_ms)
        s.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures chopper (secure.py); plain otherwise
        secure.secure_client(s, self._host, "chopper")
        s.connect(endpoint)
        return s

    def _new_sub(self):
        """The telemetry socket, in the mode the policy (or the last flip,
        secure.no_answer) says; returns it with the flip generation it was
        built for, so _listen can notice when that changes."""
        s = self._ctx.socket(zmq.SUB)
        secure.secure_client(s, self._host, "chopper")      # telemetry too
        s.connect(self._pub_endpoint)
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
                self._sub, self._sub_gen = self._new_sub()
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
