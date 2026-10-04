"""The client: talk to an Sr830Service, present a DspLockIn-compatible facade.

A GUI or a script can hold an Sr830Client exactly where it would hold a
DspLockIn: same setters, same status() attribute names, same
get_config()/apply_config(), same `_on_event` hook. Only the address changes.

It adds two things a local brain does not need: `acquire_blocking()` and
`wait_auto()`, the trigger-then-wait-for-MY-id loops, so a plain script gets a
settled sample (or a finished Auto Gain) in one line without re-implementing
the stale-status guard (gotcha #17).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import fields

import zmq

from .. import secure
from ..config import Config
from ..control import ControlClient
from ..lockin import Status
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict)


def _nan(v):
    """JSON null (a reading that does not exist yet) back to NaN."""
    if v is None:
        return float("nan")
    if isinstance(v, list):
        return [_nan(x) for x in v]
    if isinstance(v, dict):
        return {k: _nan(x) for k, x in v.items()}
    return v


class RemoteStatus:
    """Same attributes a caller reads off the brain's Status, from a wire dict.

    Built from the Status dataclass's own field list, so a field added there
    appears here without a second edit.
    """

    def __init__(self, d: dict):
        blank = Status(connected=False)
        for f in fields(Status):
            default = getattr(blank, f.name)
            value = d.get(f.name, default)
            if f.name in ("live", "sample", "overload"):
                value = _nan(value or {})
            elif value is None and isinstance(default, float):
                value = float("nan")
            elif isinstance(value, list):
                value = _nan(value)
            setattr(self, f.name, value)
        if not self.live:
            self.live = {"x": float("nan"), "y": float("nan"), "r": float("nan"),
                         "theta_deg": float("nan"), "freq_Hz": float("nan"),
                         "aux_in": [float("nan")] * 4}
        self.describe_rev = d.get("describe_rev")


class Sr830Client(ControlClient):
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
                 name: str = "sr830 client"):
        self._control_setup(kind, name)
        self._timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        # kept: the sockets are REBUILT after a timeout (and in the
        # other security mode after secure.no_answer)
        self._host = host
        self._cmd_endpoint = f"tcp://{host}:{cmd_port}"
        self._pub_endpoint = f"tcp://{host}:{pub_port}"
        self._req = self._new_req(self._cmd_endpoint)
        self._sub, self._sub_gen = self._new_sub()

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self.cfg = Config()          # kept in sync via get_config / apply_config
        self._on_event = lambda level, msg: None

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- brain-compatible surface --------------------------------------------

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
        r = self._cmd({"cmd": "set_config", "config": config_to_dict(self.cfg)})
        if not r.get("ok"):
            raise ValueError(r.get("error", "set_config refused"))

    def describe(self) -> dict:
        r = self._cmd({"cmd": "describe"})
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> RemoteStatus:
        return RemoteStatus(self._status_dict())

    # one-argument setters: same names and meaning as DspLockIn's
    def set_reference_source(self, source):
        return self._cmd({"cmd": "set_reference_source", "source": source})

    def set_frequency(self, hz):
        return self._cmd({"cmd": "set_frequency", "frequency_Hz": float(hz)})

    def set_harmonic(self, n):
        return self._cmd({"cmd": "set_harmonic", "harmonic": int(n)})

    def set_phase(self, deg):
        return self._cmd({"cmd": "set_phase", "phase_deg": float(deg)})

    def set_trigger(self, trigger):
        return self._cmd({"cmd": "set_trigger", "trigger": trigger})

    def set_sine_out(self, volts):
        return self._cmd({"cmd": "set_sine_out", "sine_out_V": float(volts)})

    def set_input_source(self, source):
        return self._cmd({"cmd": "set_input_source", "source": source})

    def set_input_ground(self, ground):
        return self._cmd({"cmd": "set_input_ground", "ground": ground})

    def set_input_coupling(self, coupling):
        return self._cmd({"cmd": "set_input_coupling", "coupling": coupling})

    def set_line_filter(self, line):
        return self._cmd({"cmd": "set_line_filter", "line_filter": line})

    def set_sensitivity(self, value):
        return self._cmd({"cmd": "set_sensitivity", "sensitivity": value})

    def set_reserve(self, reserve):
        return self._cmd({"cmd": "set_reserve", "reserve": reserve})

    def set_time_constant(self, value):
        return self._cmd({"cmd": "set_time_constant", "time_constant": value})

    def set_slope(self, slope):
        return self._cmd({"cmd": "set_slope", "slope": slope})

    def set_sync_filter(self, on):
        return self._cmd({"cmd": "set_sync_filter", "enabled": bool(on)})

    def set_aux_out(self, channel, volts):
        return self._cmd({"cmd": "set_aux_out", "channel": int(channel),
                          "volts": float(volts)})

    def output_off(self):
        """SINE OUT to minimum, every AUX OUT to 0 V -- the safety verb,
        allowed also while viewing."""
        return self._cmd({"cmd": "output_off"})

    def auto_gain(self) -> int:
        return self._run_id("auto_gain", "auto_id")

    def auto_reserve(self) -> int:
        return self._run_id("auto_reserve", "auto_id")

    def auto_phase(self) -> int:
        return self._run_id("auto_phase", "auto_id")

    def acquire(self) -> int:
        """Start an acquisition; returns its id (or raises if refused)."""
        return self._run_id("acquire", "acq_id")

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

    def wait_auto(self, n: int, timeout_s: float = 60.0, poll_s: float = 0.02) -> dict:
        """Wait until auto function run `n` has finished; return the status."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            st = self._status_dict()
            if st.get("auto_id") == n and not st.get("auto_busy", True):
                return st
            time.sleep(poll_s)
        raise TimeoutError(f"auto function #{n} did not finish within {timeout_s:g} s")

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

    def _run_id(self, verb: str, key: str) -> int:
        r = self._cmd({"cmd": verb})
        if not r.get("ok"):
            raise ValueError(r.get("error", f"{verb} refused"))
        return int(r[key])

    def _status_dict(self) -> dict:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            d = self._cmd({"cmd": "status"}).get("status", {})
        return d

    def _new_req(self, endpoint: str):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        s.setsockopt(zmq.SNDTIMEO, self._timeout_ms)
        s.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures sr830 (secure.py); plain otherwise
        secure.secure_client(s, self._host, "sr830")
        s.connect(endpoint)
        return s

    def _new_sub(self):
        """The telemetry socket, in the mode the policy (or the last flip,
        secure.no_answer) says; returns it with the flip generation it was
        built for, so _listen can notice when that changes."""
        s = self._ctx.socket(zmq.SUB)
        secure.secure_client(s, self._host, "sr830")      # telemetry too
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
                    # timed out: the REQ socket is stuck mid-transaction ->
                    # rebuild it.
                    self._req.close(0)
                    # The service may speak the other mode than the policy now
                    # says (it was started before the policy changed): the new
                    # socket tries that mode, once. Safe to resend: a request
                    # in the wrong mode never reaches the service.
                    flipped = secure.no_answer(self._host, "sr830")
                    self._req = self._new_req(self._cmd_endpoint)
                    if not (flipped and attempt == 1):
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
            if self._sub_gen != secure.flip_generation():
                # a request found the service in the other mode
                # (secure.no_answer): telemetry follows
                poller.unregister(self._sub)
                self._sub.close(0)
                self._sub, self._sub_gen = self._new_sub()
                poller.register(self._sub, zmq.POLLIN)
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
