"""The client: talk to a Ls455Service, present a Gaussmeter-compatible facade.

A GUI or script can hold a Ls455Client exactly where it would hold a Gaussmeter:
same method names, same status() attributes, same get_config()/apply_config()
and `_on_event` hook. Only the address changes.

It adds `acquire_blocking()` for scripts: trigger, wait for THIS acquisition,
return its sample.
"""

from __future__ import annotations

import json
import threading
import time

import zmq

from ..config import Config
from .. import secure
from ..control import ControlClient
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS,
                       TOPIC_EVENT, config_to_dict, apply_config_dict)

_NAN = float("nan")


def _num(v):
    """JSON null (no reading yet) back to NaN, so arithmetic and formatting work."""
    return _NAN if v is None else v


class RemoteStatus:
    """Same attributes a caller reads off the Gaussmeter's Status."""

    _FLOATS = ("field_mT", "measured_field_mT", "field_rel_mT", "read_ms",
               "range_set_mT", "range_mT", "range_min_mT", "range_max_mT",
               "rel_setpoint_mT", "settle_s", "probe_sensitivity_mV_per_kG")

    def __init__(self, d: dict):
        for k in self._FLOATS:
            setattr(self, k, _num(d.get(k)))
        self.connected = d.get("connected", False)
        self.idn = d.get("idn", "")
        self.probe = d.get("probe", "")
        self.probe_serial = d.get("probe_serial", "")
        self.probe_type_code = d.get("probe_type_code", -1)
        self.probe_geometry = d.get("probe_geometry", "axial")
        self.probe_desc = d.get("probe_desc", "")
        self.quantity = d.get("quantity", "")
        self.peak_mode = d.get("peak_mode", "periodic")
        self.peak_display = d.get("peak_display", "positive")
        self.hw_error = d.get("hw_error", "")
        self.flag = d.get("flag", "")
        self.readings = d.get("readings", 0)
        self.mode = d.get("mode", "dc")
        self.dc_digits = d.get("dc_digits", 4)
        self.rms_band = d.get("rms_band", "wide")
        self.auto_range = d.get("auto_range", True)
        self.ranges_mT = list(d.get("ranges_mT") or [])
        self.display_unit = d.get("display_unit", "G")
        self.relative = d.get("relative", False)
        self.zeroing = d.get("zeroing", False)
        self.acq_readings = d.get("acq_readings", 1)
        self.acq_id = d.get("acq_id", 0)
        self.acquiring = d.get("acquiring", False)
        self.acq_progress = d.get("acq_progress", 0.0)
        self.sample = {k: _num(v) if k in ("field_mT", "std_mT", "range_mT") else v
                       for k, v in (d.get("sample") or {}).items()}
        self.describe_rev = d.get("describe_rev")


class Ls455Client(ControlClient):
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
                 name: str = "ls455 client"):
        self._control_setup(kind, name)
        self._ctx = zmq.Context.instance()
        self._timeout_ms = timeout_ms
        # the host is kept: the encryption keys are looked up per host
        # (secure.py), and a socket rebuilt after a timeout needs it again
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

    # ---- Gaussmeter-compatible surface -----------------------------------

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

    def set_mode(self, mode: str):
        return self._cmd({"cmd": "set_mode", "mode": str(mode)})

    def set_dc_digits(self, digits: int):
        return self._cmd({"cmd": "set_dc_digits", "digits": int(digits)})

    def set_rms_band(self, band: str):
        return self._cmd({"cmd": "set_rms_band", "band": str(band)})

    def set_auto_range(self, on: bool):
        return self._cmd({"cmd": "set_auto_range", "on": bool(on)})

    def set_range(self, full_scale_mT: float):
        return self._cmd({"cmd": "set_range", "range_mT": float(full_scale_mT)})

    def set_display_unit(self, unit: str):
        return self._cmd({"cmd": "set_display_unit", "unit": str(unit)})

    def set_relative(self, on: bool, setpoint_mT: float | None = None):
        msg = {"cmd": "set_relative", "on": bool(on)}
        if setpoint_mT is not None:
            msg["setpoint_mT"] = float(setpoint_mT)
        return self._cmd(msg)

    def relative_here(self):
        r = self._cmd({"cmd": "relative_here"})
        if not r.get("ok"):
            raise ValueError(r.get("error", "relative_here refused"))
        return r

    def set_acquisition(self, readings: int):
        return self._cmd({"cmd": "set_acquisition", "readings": int(readings)})

    def zero(self):
        r = self._cmd({"cmd": "zero"})
        if not r.get("ok"):
            raise ValueError(r.get("error", "zero refused"))
        return r

    def clear_zero(self):
        return self._cmd({"cmd": "clear_zero"})

    def reread_probe(self):
        r = self._cmd({"cmd": "reread_probe"})
        if not r.get("ok"):
            raise ValueError(r.get("error", "reread_probe refused"))
        return r

    def acquire(self) -> int:
        """Start an acquisition; returns its id (or raises if refused)."""
        r = self._cmd({"cmd": "acquire"})
        if not r.get("ok"):
            raise ValueError(r.get("error", "acquire refused"))
        return int(r["acq_id"])

    def get_sample(self) -> dict:
        return self._cmd({"cmd": "get_sample"}).get("sample", {})

    def acquire_blocking(self, timeout_s: float | None = None, poll_s: float = 0.02) -> dict:
        """Trigger, wait for THIS acquisition to finish, return its sample.

        The wait checks the id before the flag: right after the trigger the
        cached status can still be the frame from BEFORE it, saying "not
        acquiring", and trusting that would return the previous sample.
        """
        n = self.acquire()
        if timeout_s is not None:
            limit = timeout_s
        else:
            # same budget as the service's describe: margin + settling + readings
            st = self._status_dict()
            limit = (float(self.cfg.acquisition.timeout_s)
                     + float(st.get("settle_s") or 0.0)
                     + 0.5 * int(st.get("acq_readings") or 1))
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

    # ---- internals -------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def _status_dict(self) -> dict:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            d = self._cmd({"cmd": "status"}).get("status", {})
        return d

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
                    flipped = secure.no_answer(self._host, "ls455")
                    self._req = self._new_req()
                    if not (flipped and attempt == 1):
                        return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused),
        # never a quiet {"ok": false} -- a script must not believe the gaussmeter
        # took a setting it refused. Other failures keep their error-dict shape.
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
        # secures ls455 (secure.py); plain otherwise
        secure.secure_client(s, self._host, "ls455")
        s.connect(self._endpoint)
        return s

    def _new_sub(self):
        """The status/event subscriber, in the mode the policy says now; also
        returns the flip generation it was made in (see _listen)."""
        s = self._ctx.socket(zmq.SUB)
        secure.secure_client(s, self._host, "ls455")      # telemetry too
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
