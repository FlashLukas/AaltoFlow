"""The client: talk to a ScopeService, present a Scope-compatible facade.

A GUI or script can hold a ScopeClient exactly where it would hold a Scope:
same method names, the same status() dict, get_config()/apply_config() and
the `_on_event` hook. Only the address changes.

`acquire_blocking()` is for scripts: trigger, wait for THIS acquisition,
return its traces and numbers.
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import zmq

from ..config import Config
from ..control import ControlClient
from .. import secure
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict, trace_from_wire)


class ScopeClient(ControlClient):
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
                 name: str = "scope client"):
        self._control_setup(kind, name)
        self._ctx = zmq.Context.instance()
        self._timeout_ms = timeout_ms
        self._host = host
        self._endpoint = f"tcp://{host}:{cmd_port}"
        self._pub_endpoint = f"tcp://{host}:{pub_port}"
        self._req = self._new_req()
        self._sub, self._sub_gen = self._new_sub()

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self.cfg = Config()          # kept in sync with the service via get/set_config
        self._on_event = lambda level, msg: None

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- Scope-compatible surface ------------------------------------------

    def start(self) -> dict:
        """Fetch static info and pull the service's config into self.cfg."""
        info = self.info()
        if info.get("channels"):
            self.channels = tuple(info["channels"])
            self.simulated = bool(info.get("simulated", True))
            self.caps = {"channels": list(info["channels"]), "model": info.get("model", ""),
                         "generator_channels": int(info.get("generator_channels", 0)),
                         "trigger_sources": info.get("trigger_sources"),
                         "trigger_options": info.get("trigger_options") or {},
                         "couplings": info.get("couplings"),
                         "supplies": info.get("supplies") or {}}
            # the generator (W1/W2), if the instrument has one: a stand-in
            # with the generator brain's methods, speaking the gen_* verbs
            self.gen = _RemoteGen(self) if self.caps["generator_channels"] else None
        self.start_heartbeat()   # "still here": counted as a viewer / keeps control
        self.get_config()
        return info

    def get_config(self) -> Config:
        r = self._cmd({"cmd": "get_config"})
        if r.get("ok") and "config" in r:
            apply_config_dict(self.cfg, r["config"])
        return self.cfg

    def apply_config(self) -> None:
        self._cmd({"cmd": "set_config", "config": config_to_dict(self.cfg)})

    def describe(self) -> dict:
        r = self._cmd({"cmd": "describe"})
        return r.get("describe", {}) if r.get("ok") else {}

    # Filled from `info` by start(); a local Scope has them as attributes.
    channels = ("ch1", "ch2")
    simulated = True
    caps: dict = {"channels": ["ch1", "ch2"], "generator_channels": 0}

    gen = None

    def status(self) -> dict:
        return self._status_dict()

    # -- what this instrument has (from `info`; a local Scope has the same methods)
    def trigger_sources(self) -> list:
        return list(self.caps.get("trigger_sources") or ("ch1", "ch2", "ext", "ext5", "line"))

    def trigger_options(self, source=None) -> dict:
        if source is None:
            source = self.status().get("trigger_source")
        return dict((self.caps.get("trigger_options") or {}).get(
            source, {"level": True, "slope": True}))

    def couplings(self) -> list:
        return list(self.caps.get("couplings") or ("dc", "ac", "gnd"))

    def has_supplies(self) -> bool:
        return bool(self.caps.get("supplies"))

    def supply_limits(self, which):
        return tuple((self.caps.get("supplies") or {}).get(which, (0.0, 0.0)))

    def set_supply(self, which, on=None, volts=None):
        d = {"cmd": "set_supply", "supply": which}
        if on is not None:
            d["on"] = bool(on)
        if volts is not None:
            d["volts"] = float(volts)
        return self._cmd(d)

    def supplies_off(self):
        return self._cmd({"cmd": "supplies_off"})

    # -- the scope's settings
    def _ch(self, verb, channel, **kw):
        return self._cmd({"cmd": verb, "channel": channel, **kw})

    def set_channel_enabled(self, ch, on):  return self._ch("set_channel_enabled", ch, on=bool(on))
    def set_vdiv(self, ch, v):              return self._ch("set_vdiv", ch, vdiv_V=float(v))
    def set_offset(self, ch, v):            return self._ch("set_offset", ch, offset_V=float(v))
    def set_coupling(self, ch, c):          return self._ch("set_coupling", ch, coupling=str(c))
    def set_probe(self, ch, f):             return self._ch("set_probe", ch, probe=float(f))
    def set_tdiv(self, s):                  return self._cmd({"cmd": "set_tdiv", "tdiv_s": float(s)})
    def set_delay(self, s):                 return self._cmd({"cmd": "set_delay", "delay_s": float(s)})
    def set_trigger_source(self, src):      return self._cmd({"cmd": "set_trigger_source", "source": src})
    def set_trigger_level(self, v):         return self._cmd({"cmd": "set_trigger_level", "level_V": float(v)})
    def set_trigger_slope(self, sl):        return self._cmd({"cmd": "set_trigger_slope", "slope": sl})
    def set_trigger_mode(self, m):          return self._cmd({"cmd": "set_trigger_mode", "mode": m})

    # -- the module's settings
    def set_points(self, n):                return self._cmd({"cmd": "set_points", "points": int(n)})
    def set_averages(self, n):              return self._cmd({"cmd": "set_averages", "averages": int(n)})
    def set_keep_raw(self, on):             return self._cmd({"cmd": "set_keep_raw", "on": bool(on)})
    def restart_average(self):              return self._cmd({"cmd": "restart_average"})

    def set_filter(self, lowpass_Hz=None, highpass_Hz=None, order=None):
        msg = {"cmd": "set_filter"}
        for k, v in (("lowpass_Hz", lowpass_Hz), ("highpass_Hz", highpass_Hz), ("order", order)):
            if v is not None:
                msg[k] = v
        return self._cmd(msg)

    def set_physical(self, ch, scale=None, offset=None, unit=None, label=None):
        msg = {"cmd": "set_physical", "channel": ch}
        for k, v in (("scale", scale), ("offset", offset), ("unit", unit), ("label", label)):
            if v is not None:
                msg[k] = v
        return self._cmd(msg)

    def set_sim(self, name: str, value):
        return self._checked({"cmd": "set_sim", "name": str(name), "value": value})

    # -- measuring
    def acquire(self) -> int:
        """Start an acquisition; returns its id (raises ValueError if refused,
        e.g. the scope is stopped)."""
        return int(self._checked({"cmd": "acquire"})["acq_id"])

    def abort(self):
        return self._cmd({"cmd": "abort"})

    def get_sample(self) -> dict:
        return self._cmd({"cmd": "get_sample"}).get("sample", {})

    def get_time(self) -> np.ndarray:
        return np.asarray(self._checked({"cmd": "get_time"})["values"], dtype=float)

    def get_trace(self, which: str = "live") -> dict:
        """Traces as numpy ("time_s", "ch1", "ch1_raw", ...) plus the numbers.
        Raises ValueError when there is none (the message says why)."""
        return trace_from_wire(self._checked({"cmd": "get_trace", "which": which}))

    def acquire_blocking(self, timeout_s: float | None = None, poll_s: float = 0.02) -> dict:
        """Trigger, wait for THIS acquisition, return get_trace("sample").

        The wait checks the id before the flag: right after the trigger the
        cached status can still be the frame from BEFORE it, saying "not
        acquiring", and trusting that would return the previous point."""
        n = self.acquire()
        from .describe import acquisition_timeout_s
        limit = (timeout_s if timeout_s is not None else
                 acquisition_timeout_s(self.cfg, float(self.status().get("record_s") or 0.0)))
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            st = self._status_dict()
            if st.get("acq_id") == n and not st.get("acquiring", True):
                trace = self.get_trace("sample")
                if trace.get("acq_id") == n:
                    return trace
            time.sleep(poll_s)
        raise TimeoutError(f"acquisition {n} did not finish within {limit:g} s")

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self.stop_heartbeat()
        self._stop.set()
        time.sleep(0.25)
        self._req.close(0)
        self._sub.close(0)

    # ---- internals ------------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _checked(self, d: dict) -> dict:
        r = self._cmd(d)
        if not r.get("ok"):
            raise ValueError(r.get("error", f"{d.get('cmd')} refused"))
        return r

    def _status_dict(self) -> dict:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            d = self._cmd({"cmd": "status"}).get("status", {})
        return d

    def _new_req(self):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        s.setsockopt(zmq.SNDTIMEO, self._timeout_ms)
        s.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures scope (secure.py); plain otherwise
        secure.secure_client(s, self._host, "scope")
        s.connect(self._endpoint)
        return s

    def _new_sub(self):
        """The status/event subscriber, in the same mode as the REQ socket.
        Returns it with the secure.flip_generation() it was made under, so
        the listener can tell when it has to be rebuilt."""
        s = self._ctx.socket(zmq.SUB)
        secure.secure_client(s, self._host, "scope")      # telemetry too
        s.connect(self._pub_endpoint)
        s.setsockopt(zmq.SUBSCRIBE, b"")
        return s, secure.flip_generation()

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
                    flipped = secure.no_answer(self._host, "scope")
                    self._req = self._new_req()
                    if not (flipped and attempt == 1):
                        return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused),
        # never a quiet {"ok": false} -- a script must not believe the scope
        # took a setting it refused. Other failures keep their error-dict shape.
        if not reply.get("ok", False):
            self._raise_refusal(reply)
        return reply

    def _rpc(self, **req) -> dict:
        """The name control.py's ControlClient calls (heartbeat, take_control)."""
        return self._cmd(req)

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


class _RemoteGen:
    """The scope service's generator (W1/W2) with the generator brain's
    methods, so the Generator tab's cards (afg-control's) work the same on a
    local scope and over the network. Commands go out as gen_<verb>; status()
    is the generator's own status taken out of the scope's (generator/wire.py)."""

    def __init__(self, client):
        self._c = client
        info = client._cmd({"cmd": "gen_info"}).get("info", {}) or {}
        self.channels = tuple(info.get("channels") or ("w1", "w2"))
        # the generator's config (the Sweep box offers its default rates)
        from ..generator.config import GenConfig
        from .protocol import apply_config_dict
        self.cfg = GenConfig()
        r = client._cmd({"cmd": "gen_get_config"})
        if r.get("ok") and isinstance(r.get("config"), dict):
            apply_config_dict(self.cfg, r["config"])
        self.caps = {"model": info.get("model", ""), "channels": len(self.channels),
                     "waveforms": list(info.get("waveforms") or ()),
                     "ramp_symmetry": bool(info.get("ramp_symmetry", True)),
                     "load_settable": bool(info.get("load_settable", False)),
                     "phase_align": bool(info.get("phase_align", False))}

    def _g(self, verb, **kw):
        return self._c._cmd({"cmd": "gen_" + verb, **kw})

    def status(self) -> dict:
        from ..generator import wire
        return wire.unstatus(self._c.status())

    def envelope(self, ch, waveform=None, load_ohm="current"):
        r = self._g("envelope", channel=ch)
        return r.get("envelope") or {}

    def set_output(self, ch, on):        return self._g("set_output", channel=ch, on=bool(on))
    def set_waveform(self, ch, wf):      return self._g("set_waveform", channel=ch, waveform=wf)
    def set_frequency(self, ch, hz):     return self._g("set_frequency", channel=ch, frequency_Hz=float(hz))
    def set_amplitude(self, ch, vpp):    return self._g("set_amplitude", channel=ch, amplitude_Vpp=float(vpp))
    def set_offset(self, ch, v):         return self._g("set_offset", channel=ch, offset_V=float(v))
    def set_phase(self, ch, deg):        return self._g("set_phase", channel=ch, phase_deg=float(deg))
    def set_duty(self, ch, pct):         return self._g("set_duty", channel=ch, duty_pct=float(pct))
    def set_symmetry(self, ch, pct):     return self._g("set_symmetry", channel=ch, symmetry_pct=float(pct))
    def set_load(self, ch, load):        return self._g("set_load", channel=ch, load=load)

    def set_follow(self, on, phase_offset_deg=None, phase=None):
        d = {"on": bool(on)}
        if phase_offset_deg is not None:
            d["phase_offset_deg"] = float(phase_offset_deg)
        if phase is not None:
            d["phase"] = bool(phase)
        return self._g("set_follow", **d)

    def set_phase_follow(self, on):      return self._g("set_phase_follow", on=bool(on))
    def set_phase_offset(self, deg):     return self._g("set_phase_offset", deg=float(deg))
    def align_phase(self):               return self._g("align_phase")
    def outputs_off(self):               return self._g("outputs_off")
    def ramp_stop(self):                 return self._g("ramp_stop")

    def ramp_start(self, ch, knob, to, rate):
        return self._g("ramp_start", channel=ch, knob=knob, to=float(to), rate=float(rate))
