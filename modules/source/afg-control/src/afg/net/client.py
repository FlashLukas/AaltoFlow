"""The client: talk to an AfgService, present a Generator-compatible facade.

A GUI, a script, or the coordinator can hold an AfgClient exactly where it
would hold a Generator: same method names (set_output, set_waveform,
set_frequency, set_amplitude, set_offset, set_phase, set_duty, set_symmetry,
set_load, set_follow, set_phase_offset, align_phase, outputs_off), same
status() dict, same get_config()/apply_config(), same `_on_event` hook. So the
caller does not care whether the generator is in-process or across the lab --
only the address changes.

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


class AfgClient(ControlClient):
    """``kind`` / ``name``: who this client is to the service (control.py) --
    "gui" for a window, "script" (default) for a script or console, "machine"
    only for a program that must not be locked out (scan-core, another
    module). While a GUI on another PC holds control, a script must
    ``take_control()`` before it may change anything; a refused command
    raises ``ControlRefused``. ``outputs_off()`` is the safety verb: it works
    also while viewing."""

    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000,
                 kind: str = "script",
                 name: str = "afg client"):
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

    # ---- Generator-compatible surface ------------------------------------

    def start(self) -> dict:
        """Fetch static info and pull the service's config into self.cfg."""
        info = self.info()
        if info.get("channels"):
            self.channels = tuple(info["channels"])
            self.caps = dict(self.caps, waveforms=info.get("waveforms",
                                                           self.caps["waveforms"]),
                             model=info.get("model", ""),
                             ramp_symmetry=bool(info.get("ramp_symmetry", True)))
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

    # For a local Generator these exist as attributes; a GUI built for
    # either reads them. Filled from `info` by start().
    channels = ("ch1", "ch2")
    caps: dict = {"waveforms": ["sine", "square", "pulse", "ramp", "noise", "dc"],
                  "load_settable": True, "phase_align": True}

    def envelope(self, ch: str) -> dict:
        """The live envelope of channel `ch`, as the service computes it."""
        return self.info().get("envelope", {}).get(ch, {})

    def _ch(self, verb: str, channel, **kw):
        return self._cmd({"cmd": verb, "channel": channel, **kw})

    def set_output(self, channel, on: bool):
        return self._ch("set_output", channel, on=bool(on))

    def set_waveform(self, channel, waveform: str):
        return self._ch("set_waveform", channel, waveform=str(waveform))

    def set_frequency(self, channel, hz: float):
        return self._ch("set_frequency", channel, frequency_Hz=float(hz))

    def set_amplitude(self, channel, vpp: float):
        return self._ch("set_amplitude", channel, amplitude_Vpp=float(vpp))

    def set_offset(self, channel, volts: float):
        return self._ch("set_offset", channel, offset_V=float(volts))

    def set_phase(self, channel, deg: float):
        return self._ch("set_phase", channel, phase_deg=float(deg))

    def set_duty(self, channel, pct: float):
        return self._ch("set_duty", channel, duty_pct=float(pct))

    def set_symmetry(self, channel, pct: float):
        return self._ch("set_symmetry", channel, symmetry_pct=float(pct))

    def set_load(self, channel, load):
        """50, "50", None / "high-Z" for high-Z, or a value in ohm."""
        return self._ch("set_load", channel, load="high-Z" if load is None else str(load))

    def set_phase_follow(self, on: bool):
        return self._cmd({"cmd": "set_phase_follow", "on": bool(on)})

    def set_follow(self, on: bool, phase_offset_deg: float | None = None,
                   phase: bool | None = None):
        msg = {"cmd": "set_follow", "on": bool(on)}
        if phase is not None:
            msg["phase"] = bool(phase)
        if phase_offset_deg is not None:
            msg["phase_offset_deg"] = float(phase_offset_deg)
        return self._cmd(msg)

    def set_phase_offset(self, deg: float):
        return self._cmd({"cmd": "set_phase_offset", "deg": float(deg)})

    def align_phase(self):
        return self._cmd({"cmd": "align_phase"})

    def outputs_off(self):
        return self._cmd({"cmd": "outputs_off"})

    def ramp_start(self, ch, knob: str, to: float, rate: float):
        """Sweep `knob` of `ch` to `to` at `rate` per second; reply has ramp_id."""
        return self._cmd({"cmd": "ramp_start", "channel": ch, "knob": knob,
                          "to": float(to), "rate": float(rate)})

    def ramp_stop(self):
        return self._cmd({"cmd": "ramp_stop"})

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

    def _make_req(self) -> None:
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        self._req.setsockopt(zmq.SNDTIMEO, self._timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures afg (secure.py); plain otherwise
        secure.secure_client(self._req, self.host, "afg")
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
                    flipped = secure.no_answer(self.host, "afg")
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
            secure.secure_client(s, self.host, "afg")      # telemetry too
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
