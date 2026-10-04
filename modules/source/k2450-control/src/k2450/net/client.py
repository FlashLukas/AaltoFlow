"""The client: talk to a K2450Service, present a SourceMeter-compatible facade.

A GUI or script can hold a K2450Client exactly where it would hold a
SourceMeter: same method names, same status() attributes, same
get_config()/apply_config() and `_on_event` hook. Only the address changes.

For scripts it adds `set_voltage_blocking()` (set, wait until the service has
adopted it AND the source has settled) and `acquire_blocking()` (trigger, wait
for THIS acquisition, return its sample) -- together, one IV point.
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

_NAN = float("nan")


def _num(v):
    """JSON null (no reading) back to NaN, so arithmetic and formatting work."""
    return _NAN if v is None else v


class RemoteStatus:
    """Same attributes a caller reads off the SourceMeter's Status."""

    _FLOATS = ("source_voltage_set_V", "source_current_set_A", "source_current_set_uA",
               "current_limit_A", "voltage_limit_V", "level_max", "limit_min",
               "limit_max", "source_range", "measure_range", "nplc", "voltage_V",
               "current_A", "resistance_ohm", "read_ms")
    _SAMPLE_FLOATS = ("voltage_V", "voltage_std_V", "current_A", "current_std_A",
                      "resistance_ohm", "resistance_std_ohm")

    def __init__(self, d: dict):
        for k in self._FLOATS:
            setattr(self, k, _num(d.get(k)))
        self.connected = d.get("connected", False)
        self.idn = d.get("idn", "")
        self.hw_error = d.get("hw_error", "")
        self.output = d.get("output", False)
        self.settled = d.get("settled", True)
        self.source_function = d.get("source_function", "voltage")
        self.measure_function = d.get("measure_function", "current")
        self.source_auto_range = d.get("source_auto_range", True)
        self.measure_auto_range = d.get("measure_auto_range", True)
        self.four_wire = d.get("four_wire", False)
        self.tripped = d.get("tripped", False)
        self.flag = d.get("flag", "")
        self.readings = d.get("readings", 0)
        self.acq_readings = d.get("acq_readings", 1)
        self.acq_id = d.get("acq_id", 0)
        self.acquiring = d.get("acquiring", False)
        self.acq_progress = d.get("acq_progress", 0.0)
        self.sample = {k: _num(v) if k in self._SAMPLE_FLOATS else v
                       for k, v in (d.get("sample") or {}).items()}
        self.describe_rev = d.get("describe_rev")


class K2450Client(ControlClient):
    """``kind`` / ``name``: who this client is to the service (control.py) --
    "gui" for a window, "script" (default) for a script or console, "machine"
    only for a program that must not be locked out (scan-core, another
    module). While a GUI on another PC holds control, a script must
    ``take_control()`` before it may change anything; a refused command
    raises ``ControlRefused`` (also from the setters that otherwise return
    the reply unchecked)."""

    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000,
                 kind: str = "script",
                 name: str = "k2450 client"):
        self._control_setup(kind, name)
        self._ctx = zmq.Context.instance()
        self._timeout_ms = timeout_ms
        self._endpoint = f"tcp://{host}:{cmd_port}"
        self._req = self._new_req()
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

    # ---- SourceMeter-compatible surface -----------------------------------

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

    def set_output(self, on: bool):
        return self._checked({"cmd": "set_output", "on": bool(on)})

    def output_off(self):
        return self._cmd({"cmd": "output_off"})

    def set_source_function(self, fn: str):
        return self._checked({"cmd": "set_source_function", "function": str(fn)})

    def set_voltage(self, volts: float):
        return self._cmd({"cmd": "set_voltage", "voltage_V": float(volts)})

    def set_current(self, amps: float):
        return self._cmd({"cmd": "set_current", "current_A": float(amps)})

    def set_current_limit(self, amps: float):
        return self._cmd({"cmd": "set_current_limit", "current_limit_A": float(amps)})

    def set_voltage_limit(self, volts: float):
        return self._cmd({"cmd": "set_voltage_limit", "voltage_limit_V": float(volts)})

    def set_source_auto_range(self, on: bool):
        return self._cmd({"cmd": "set_source_auto_range", "on": bool(on)})

    def set_source_range(self, value: float):
        return self._cmd({"cmd": "set_source_range", "range": float(value)})

    def set_measure_auto_range(self, on: bool):
        return self._cmd({"cmd": "set_measure_auto_range", "on": bool(on)})

    def set_measure_range(self, value: float):
        return self._cmd({"cmd": "set_measure_range", "range": float(value)})

    def set_nplc(self, nplc: float):
        return self._cmd({"cmd": "set_nplc", "nplc": float(nplc)})

    def set_four_wire(self, on: bool):
        return self._cmd({"cmd": "set_four_wire", "on": bool(on)})

    def set_acquisition(self, readings: int):
        return self._cmd({"cmd": "set_acquisition", "readings": int(readings)})

    def acquire(self) -> int:
        """Start an acquisition; returns its id (or raises if refused)."""
        r = self._checked({"cmd": "acquire"})
        return int(r["acq_id"])

    def get_sample(self) -> dict:
        return self._cmd({"cmd": "get_sample"}).get("sample", {})

    # ---- blocking helpers for scripts ---------------------------------------

    def set_voltage_blocking(self, volts: float, timeout_s: float = 10.0,
                             poll_s: float = 0.02) -> None:
        """Set the voltage level and return once the service reports it ADOPTED
        and SETTLED. Adoption first (gotcha #2): right after the command the
        cached status can still describe the previous level, settled and all."""
        self.set_voltage(volts)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            st = self._status_dict()
            sp = st.get("source_voltage_set_V")
            if sp is not None and abs(sp - volts) <= max(1e-9, abs(volts) * 1e-9) \
                    and st.get("settled"):
                return
            time.sleep(poll_s)
        raise TimeoutError(f"voltage {volts:g} V not adopted/settled within {timeout_s:g} s "
                           "(clamped by a limit?)")

    def acquire_blocking(self, timeout_s: float | None = None, poll_s: float = 0.02) -> dict:
        """Trigger, wait for THIS acquisition to finish, return its sample.
        The id is checked before the flag (gotcha #17)."""
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
        """Close the client. Does NOT stop the remote service (or its output)."""
        self.stop_heartbeat()
        self._stop.set()
        time.sleep(0.25)
        self._req.close(0)
        self._sub.close(0)

    # ---- internals -------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _status_dict(self) -> dict:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            d = self._cmd({"cmd": "status"}).get("status", {})
        return d

    def _checked(self, d: dict) -> dict:
        """A command whose refusal the caller must hear about (output on,
        function change, acquire): raise instead of returning ok=False."""
        r = self._cmd(d)
        if not r.get("ok"):
            raise ValueError(r.get("error", f"{d.get('cmd')} refused"))
        return r

    def _new_req(self):
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        s.setsockopt(zmq.LINGER, 0)
        s.connect(self._endpoint)
        return s

    def _cmd(self, d: dict) -> dict:
        self._with_identity(d)           # say who we are (control.py)
        with self._req_lock:
            self._req.send_json(d)
            try:
                reply = self._req.recv_json()
            except zmq.Again:
                # timed out; a REQ socket is now stuck mid-exchange -> rebuild it
                self._req.close(0)
                self._req = self._new_req()
                return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused), so
        # a script never believes the SourceMeter took a setting it refused.
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
                topic, payload = self._sub.recv_multipart()
                d = json.loads(payload)
                if topic == TOPIC_STATUS:
                    with self._lock:
                        self._latest = d
                    self._control_from_status(d)
                elif topic == TOPIC_EVENT:
                    self._on_event(d.get("level", "info"), d.get("msg", ""))
