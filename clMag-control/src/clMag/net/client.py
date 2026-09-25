"""The client: talk to a ClMagService, present a Controller-compatible facade.

The GUI (and your scripts) can hold a ClMagClient exactly where they would hold a
Controller: same method names (set_field, set_current, demag, calibrate,
set_lock), same status() shape, same `stabilizer_enabled` and `calibration`
attributes, same `_on_event` hook. So the window does not care whether the
magnet is in-process or across the lab -- only the address changes.

A background thread owns the SUB socket and keeps the latest status; commands go
out on a REQ socket guarded by a lock (REQ is strict request/reply, so one call
at a time).
"""

from __future__ import annotations

import json
import threading
import time

import zmq

from ..config import Config
from .protocol import (DEFAULT_CMD_PORT, DEFAULT_PUB_PORT, TOPIC_STATUS, TOPIC_EVENT,
                       config_to_dict, apply_config_dict, calibration_from_dict,
                       calibration_to_dict)


#: How close the service's echoed setpoint must be to ours to count as
#: "adopted". The value round-trips through JSON as an exact float64, so this is
#: pure defence against a service that rounds or clamps by a hair -- without it,
#: such a mismatch would hang a scan on its first point.
_SETPOINT_EPS_MT = 1e-6


class RemoteStatus:
    """Same attributes the GUI reads off the Controller's Status."""
    def __init__(self, d: dict):
        self.state = d.get("state", "?")
        self.setpoint_field_mT = d.get("setpoint_field_mT")
        self.measured_field_mT = d.get("measured_field_mT", 0.0)
        self.current_A = d.get("current_A", 0.0)
        self.field_stable = d.get("field_stable", False)
        self.locked = d.get("locked", False)
        self.aux = d.get("aux") or {}
        self.describe_rev = d.get("describe_rev")   # None from an older service


class RemoteCalibration:
    """Enough of a FieldCalibration for the GUI to size its controls."""
    def __init__(self, lo: float, hi: float, n: int):
        self._lo, self._hi = lo, hi
        self.currents_A = [0.0] * n     # only len() is used by the GUI

    @property
    def range_mT(self):
        return (self._lo, self._hi)


class ClMagClient:
    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000):
        self._ctx = zmq.Context.instance()
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        self._req.connect(f"tcp://{host}:{cmd_port}")
        self._sub = self._ctx.socket(zmq.SUB)
        self._sub.connect(f"tcp://{host}:{pub_port}")
        self._sub.setsockopt(zmq.SUBSCRIBE, b"")

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self._stab = True
        self.calibration = None
        self.cfg = Config()          # kept in sync with the service via get/set_config
        self._on_event = lambda level, msg: None

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- Controller-compatible surface -----------------------------------

    def start(self):
        """Fetch static info + the current config, and build the calibration facade."""
        info = self.info()
        self.calibration = RemoteCalibration(
            info.get("field_lo", 0.0), info.get("field_hi", 0.0), info.get("n_points", 0))
        self.get_config()      # pull the service's settings into self.cfg
        return info

    # ---- settings (Settings dialog uses these, same names as Controller) --

    def get_config(self) -> Config:
        """Pull the service's live config into self.cfg (in place) and return it."""
        r = self._cmd({"cmd": "get_config"})
        if r.get("ok") and "config" in r:
            apply_config_dict(self.cfg, r["config"])
        return self.cfg

    def apply_config(self) -> None:
        """Push self.cfg to the service (it applies + re-tunes its controller)."""
        self._cmd({"cmd": "set_config", "config": config_to_dict(self.cfg)})

    def get_calibration(self):
        """Fetch the full calibration curve from the service (or None)."""
        r = self._cmd({"cmd": "get_calibration"})
        return calibration_from_dict(r.get("calibration")) if r.get("ok") else None

    def set_calibration(self, cal) -> None:
        self._cmd({"cmd": "set_calibration", "calibration": calibration_to_dict(cal)})

    def status(self) -> RemoteStatus:
        with self._lock:
            d = dict(self._latest)
        if not d:                       # no PUB frame yet -> ask directly
            r = self._cmd({"cmd": "status"})
            d = r.get("status", {})
        return RemoteStatus(d)

    def set_field(self, field_mT: float, use_pid: bool = True):
        self._cmd({"cmd": "set_field", "field_mT": field_mT, "use_pid": use_pid})

    def set_current(self, amps: float):
        self._cmd({"cmd": "set_current", "current_A": amps})

    def demag(self, amplitude_A: float):
        self._cmd({"cmd": "demag", "amplitude_A": amplitude_A})

    def calibrate(self, n_per_leg: int = 50, dwell_s: float = 0.5):
        self._cmd({"cmd": "calibrate", "n_per_leg": n_per_leg, "dwell_s": dwell_s})

    def set_lock(self, locked: bool):
        self._cmd({"cmd": "set_lock", "locked": locked})

    # ---- blocking helpers (what a coordinator needs) ---------------------
    #
    # Everything above is fire-and-forget: the reply {"ok": true} means the
    # service ACCEPTED the command, not that the magnet got there. That is the
    # right primitive for a GUI, which stays responsive and watches status.
    #
    # A scan engine wants the opposite: one call that returns when the point is
    # actually reached, so the detector is read at the right field. These three
    # helpers are that. scan-core's Settable.set wraps set_field_blocking.

    def wait_stable(self, target_mT=None, timeout_s: float = 30.0,
                    poll_s: float = 0.05):
        """Block until the field has settled; return the final status.

        Two conditions must hold, not one. Waiting on `field_stable` alone is a
        trap: `set_field` only QUEUES the command, so for the first few polls
        the service is still describing the PREVIOUS point -- and if that point
        had settled, `field_stable` is still True. You would read your detector
        at the old field and never notice.

        So when `target_mT` is given we first require the service to have
        ADOPTED it (`setpoint_field_mT == target`), and only then believe the
        stable flag. Passing `target_mT=None` skips that guard; only do that
        when you know no command is in flight.

        Raises TimeoutError rather than returning a flag, because a scan that
        silently records unsettled points produces data that looks fine and is
        wrong.
        """
        def settled(st):
            if target_mT is not None:
                sp = st.setpoint_field_mT
                if sp is None or abs(sp - target_mT) > _SETPOINT_EPS_MT:
                    return False          # not adopted yet -> stale status
            return bool(st.field_stable)

        where = "stable" if target_mT is None else f"stable at {target_mT:g} mT"
        return self._wait_for(settled, timeout_s, poll_s, where)

    def set_field_blocking(self, field_mT: float, use_pid: bool = True,
                           timeout_s: float = 30.0, poll_s: float = 0.05):
        """Set the field and return only once it has settled there.

        This is the settle primitive proven against the real service (and in the
        QCoDeS spike). A TimeoutError here usually means one of: no calibration
        loaded (the service refuses the setpoint, so it is never adopted), the
        target is outside what the magnet can reach, or the PI needs retuning on
        the real plant -- the service's event stream says which.
        """
        self.set_field(field_mT, use_pid=use_pid)
        return self.wait_stable(field_mT, timeout_s=timeout_s, poll_s=poll_s)

    def wait_idle(self, timeout_s: float = 60.0, poll_s: float = 0.05):
        """Block until the service is back in IDLE; return the final status.

        For the operations with no setpoint to adopt -- `demag`, `calibrate` --
        where "finished" means the state machine came home.
        """
        return self._wait_for(lambda st: st.state == "IDLE",
                              timeout_s, poll_s, "IDLE")

    def _wait_for(self, predicate, timeout_s: float, poll_s: float, what: str):
        """Poll cached status until `predicate` holds. Shared by the helpers above.

        status() reads the SUB thread's cached frame, so this costs no network
        round trip per poll -- it just reads whatever the 10 Hz status stream
        last delivered.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            st = self.status()
            if predicate(st):
                return st
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"clMag: waited {timeout_s:g} s for {what}; "
                    f"state={st.state} setpoint={st.setpoint_field_mT} "
                    f"measured={st.measured_field_mT:.3f} mT "
                    f"stable={st.field_stable}")
            time.sleep(poll_s)

    # ---- AUX I/O (same method names as Controller) -----------------------

    def aux_set_ao(self, channel: str, volts: float):
        self._cmd({"cmd": "aux_set_ao", "channel": channel, "volts": volts})

    def aux_set_do(self, line: str, state: bool):
        self._cmd({"cmd": "aux_set_do", "line": line, "state": bool(state)})

    def aux_read_ai(self, channel: str) -> float:
        r = self._cmd({"cmd": "aux_read_ai", "channel": channel})
        return r.get("volts", 0.0) if r.get("ok") else 0.0

    @property
    def stabilizer_enabled(self):
        return self._stab

    @stabilizer_enabled.setter
    def stabilizer_enabled(self, value: bool):
        self._stab = bool(value)
        self._cmd({"cmd": "set_stabilizer", "enabled": bool(value)})

    def shutdown(self):
        """Close the client. Does NOT stop the remote service."""
        self._stop.set()
        time.sleep(0.25)
        self._req.close(0)
        self._sub.close(0)

    # ---- internals -------------------------------------------------------

    def info(self) -> dict:
        return self._cmd({"cmd": "info"}).get("info", {})

    def describe(self) -> dict:
        """The service's parameter manifest: controls, indicators and actions.

        See `clMag/net/describe.py`. Limits inside it are live, not constants --
        the field range IS the loaded calibration's range -- so check
        `status().describe_rev` against the manifest's `revision` rather than
        caching this forever.
        """
        return self._cmd({"cmd": "describe"}).get("describe", {})

    def _cmd(self, d: dict) -> dict:
        with self._req_lock:
            self._req.send_json(d)
            try:
                return self._req.recv_json()
            except zmq.Again:
                # timed out; the REQ socket is now in a bad state -> rebuild it
                self._reset_req()
                return {"ok": False, "error": "service did not respond (timeout)"}

    def _reset_req(self):
        endpoint = self._req.LAST_ENDPOINT
        self._req.close(0)
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, 3000)
        self._req.setsockopt(zmq.LINGER, 0)
        if endpoint:
            self._req.connect(endpoint.decode() if isinstance(endpoint, bytes) else endpoint)

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
                elif topic == TOPIC_EVENT:
                    self._on_event(d.get("level", "info"), d.get("msg", ""))
