"""The client: talk to an Sr7230Service, present a LockIn-compatible facade.

A GUI or a script can hold an Sr7230Client exactly where it would hold a LockIn:
same setters, same status() attribute names, same get_config()/apply_config(),
same `_on_event` hook. Only the address changes.

It adds one thing a local LockIn does not need: `acquire_blocking()`, the
trigger-then-wait-for-MY-id loop, so a plain script gets a settled sample in
one line without re-implementing the stale-status guard.
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


def _num(v):
    """JSON null (a reading that does not exist yet) back to NaN."""
    return float("nan") if v is None else v


class RemoteStatus:
    """Same attributes a caller reads off the LockIn's Status."""

    def __init__(self, d: dict):
        self.connected = d.get("connected", False)
        self.idn = d.get("idn", "")
        self.hw_error = d.get("hw_error", "")
        self.ref_source = d.get("ref_source", "internal")
        self.ref_locked = d.get("ref_locked")
        for key in ("freq_set_Hz", "ref_freq_Hz", "demod_freq_Hz", "freq_max_Hz",
                    "amplitude_V", "phase_deg", "full_scale", "tc_set_s", "tc_s",
                    "settle_s"):
            setattr(self, key, _num(d.get(key)))
        self.harmonic = d.get("harmonic", 1)
        self.input = d.get("input", "A")
        self.unit = d.get("unit", "V")
        self.ac_coupled = d.get("ac_coupled", True)
        self.coupling = d.get("coupling", "AC")
        self.sensitivity = d.get("sensitivity", "")
        self.sensitivity_index = d.get("sensitivity_index", 0)
        self.fast_mode = d.get("fast_mode", False)
        self.slope = d.get("slope", "")
        self.slope_db = d.get("slope_db", 12)
        live = d.get("live") or {}
        self.live = {k: _num(live.get(k)) for k in ("x", "y", "r", "theta_deg", "r_fs")}
        self.live["adc"] = [_num(v) for v in (live.get("adc") or [None, None])]
        self.overload = d.get("overload") or {"input": False, "output": False,
                                              "x": False, "y": False, "byte": 0}
        self.acq_id = d.get("acq_id", 0)
        self.acquiring = d.get("acquiring", False)
        self.acq_progress = d.get("acq_progress", 0.0)
        sample = d.get("sample") or {}
        self.sample = {k: ([_num(x) for x in v] if isinstance(v, list) else _num(v))
                       for k, v in sample.items()}
        self.auto_id = d.get("auto_id", 0)
        self.auto_busy = d.get("auto_busy", False)
        self.auto_op = d.get("auto_op", "")
        self.auto_error = d.get("auto_error", "")
        self.describe_rev = d.get("describe_rev")


class Sr7230Client(ControlClient):
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
                 name: str = "sr7230 client"):
        self._control_setup(kind, name)
        self._timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self._req = self._new_req(f"tcp://{host}:{cmd_port}")
        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.connect(f"tcp://{host}:{pub_port}")
        self._sub.setsockopt(zmq.SUBSCRIBE, b"")

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self.cfg = Config()          # kept in sync via get_config / apply_config
        self._on_event = lambda level, msg: None

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- LockIn-compatible surface --------------------------------------------

    def start(self) -> dict:
        """Fetch static info and pull the service's config into self.cfg."""
        info = self.info()
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

    def status(self) -> RemoteStatus:
        return RemoteStatus(self._status_dict())

    def set_reference(self, source: str):
        return self._cmd({"cmd": "set_reference", "source": str(source)})

    def set_frequency(self, hz: float):
        return self._cmd({"cmd": "set_frequency", "frequency_Hz": float(hz)})

    def set_amplitude(self, volts: float):
        return self._cmd({"cmd": "set_amplitude", "amplitude_V": float(volts)})

    def output_off(self):
        """OSC OUT amplitude to 0 V -- the safety verb, allowed also while
        viewing."""
        return self._cmd({"cmd": "output_off"})

    def set_phase(self, deg: float):
        return self._cmd({"cmd": "set_phase", "phase_deg": float(deg)})

    def set_harmonic(self, n: int):
        return self._cmd({"cmd": "set_harmonic", "harmonic": int(n)})

    def set_input(self, mode: str):
        return self._cmd({"cmd": "set_input", "mode": str(mode)})

    def set_coupling(self, coupling: str):
        return self._cmd({"cmd": "set_coupling", "coupling": str(coupling)})

    def set_sensitivity(self, value):
        """A label ("100 mV") or a table index (24)."""
        return self._cmd({"cmd": "set_sensitivity", "sensitivity": value})

    def set_full_scale(self, value: float):
        return self._cmd({"cmd": "set_full_scale", "full_scale": float(value)})

    def set_time_constant(self, tc_s: float):
        return self._cmd({"cmd": "set_time_constant", "time_constant_s": float(tc_s)})

    def set_slope(self, slope):
        return self._cmd({"cmd": "set_slope", "slope": slope})

    def set_fast_mode(self, enabled: bool):
        return self._cmd({"cmd": "set_fast_mode", "enabled": bool(enabled)})

    def auto(self, op: str) -> int:
        """auto_phase / auto_sensitivity / auto_measure; returns its id."""
        r = self._cmd({"cmd": str(op)})
        if not r.get("ok"):
            raise RuntimeError(r.get("error", f"{op} refused"))
        return int(r["auto_id"])

    def acquire(self) -> int:
        """Start an acquisition; returns its id (or raises if refused)."""
        r = self._cmd({"cmd": "acquire"})
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "acquire refused"))
        return int(r["acq_id"])

    def get_sample(self) -> dict:
        return self._cmd({"cmd": "get_sample"}).get("sample", {})

    def acquire_blocking(self, timeout_s: float | None = None, poll_s: float = 0.02) -> dict:
        """Trigger, wait for THIS acquisition to finish, return its sample.

        The wait checks the id before the flag. Right after the trigger, the
        cached status can still be the frame from BEFORE it, saying "not
        acquiring" -- trusting that flag alone would return the previous
        point's sample.
        """
        n = self.acquire()
        limit = timeout_s if timeout_s is not None else self.cfg.acquisition.timeout_s
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            st = self._status_dict()
            if st.get("acq_id") == n and not st.get("acquiring", True):
                sample = st.get("sample") or {}
                if sample.get("acq_id") == n:
                    return sample
            time.sleep(poll_s)
        raise TimeoutError(f"acquisition {n} did not finish within {limit:g} s")

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self.stop_heartbeat()
        self._stop.set()
        time.sleep(0.25)
        self._req.close(0)
        self._sub.close(0)

    # ---- internals -------------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _status_dict(self) -> dict:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            d = self._cmd({"cmd": "status"}).get("status", {})
        return d

    def _new_req(self, endpoint: str):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        s.setsockopt(zmq.LINGER, 0)
        s.connect(endpoint)
        return s

    def _cmd(self, d: dict) -> dict:
        self._with_identity(d)           # say who we are (control.py)
        with self._req_lock:
            self._req.send_json(d)
            try:
                reply = self._req.recv_json()
            except zmq.Again:
                # a timed-out REQ socket is stuck; rebuild it
                endpoint = self._req.LAST_ENDPOINT
                self._req.close(0)
                self._req = self._new_req(endpoint.decode() if isinstance(endpoint, bytes)
                                          else endpoint)
                return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused), so
        # a script never believes the instrument took a setting it refused.
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
            if poller.poll(200):
                try:
                    topic, payload = self._sub.recv_multipart()
                except zmq.ZMQError:
                    break
                d = json.loads(payload)
                if topic == TOPIC_STATUS:
                    with self._lock:
                        self._latest = d
                    self._control_from_status(d)
                elif topic == TOPIC_EVENT:
                    self._on_event(d.get("level", "info"), d.get("msg", ""))
