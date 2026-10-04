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

from .. import secure
from ..config import Config
from ..control import ControlClient
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
        self.output_on = d.get("output_on", False)
        self.aux = d.get("aux") or {}
        self.describe_rev = d.get("describe_rev")   # None from an older service
        self.cmd_done = d.get("cmd_done")           # None from an older service
        self.hw_error = d.get("hw_error", "")
        self.loop_error = d.get("loop_error", "")
        self.stabilizer = d.get("stabilizer")        # None from an older service
        self.stabilizer_trim_A = d.get("stabilizer_trim_A", 0.0)


def _taken_up(st, seq) -> bool:
    """Has the service taken up command number `seq`? True when there is no
    number to check (no command sent, or an older service without cmd_done)."""
    if seq is None or st.cmd_done is None:
        return True
    return st.cmd_done >= seq


class RemoteCalibration:
    """Enough of a FieldCalibration for the GUI to size its controls."""
    def __init__(self, lo: float, hi: float, n: int):
        self._lo, self._hi = lo, hi
        self.currents_A = [0.0] * n     # only len() is used by the GUI

    @property
    def range_mT(self):
        return (self._lo, self._hi)


class ClMagClient(ControlClient):
    """``kind`` / ``name``: who this client is to the service (control.py) --
    "gui" for a window, "script" (default) for a script or console, "machine"
    only for a program that must not be locked out (scan-core). While a GUI on
    another PC holds control, a script must ``take_control()`` before it may
    change anything; a refused command raises ``ControlRefused``."""

    def __init__(self, host: str = "localhost",
                 cmd_port: int = DEFAULT_CMD_PORT,
                 pub_port: int = DEFAULT_PUB_PORT,
                 timeout_ms: int = 3000,
                 kind: str = "script",
                 name: str = "clMag client"):
        self._control_setup(kind, name)
        self._ctx = zmq.Context.instance()
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.timeout_ms = timeout_ms
        self._make_req()
        # the SUB socket is created (and closed) by the listener thread that
        # uses it: a ZeroMQ socket belongs to one thread

        self._latest: dict = {}
        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._stop = threading.Event()
        self._stab = True
        # sequence number of the last command this client queued (the service
        # answers with `seq`); None until one has been sent / from old services
        self._last_seq = None
        self.calibration = None
        self.cfg = Config()          # kept in sync with the service via get/set_config
        self._on_event = lambda level, msg: None

        self._sub_t = threading.Thread(target=self._listen, name="cli-sub", daemon=True)
        self._sub_t.start()

    # ---- Controller-compatible surface -----------------------------------

    def start(self):
        """Fetch static info + the current config, and build the calibration facade."""
        info = self.info()
        self.start_heartbeat()   # "still here": counted as a viewer / keeps control
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
        return self._queued({"cmd": "set_field", "field_mT": field_mT, "use_pid": use_pid})

    def set_current(self, amps: float):
        return self._queued({"cmd": "set_current", "current_A": amps})

    def ramp_to_zero(self):
        """Ramp to 0 A -- the safety verb, allowed also while viewing."""
        return self._queued({"cmd": "ramp_to_zero"})

    def demag(self, amplitude_A: float):
        return self._queued({"cmd": "demag", "amplitude_A": amplitude_A})

    def calibrate(self, n_per_leg: int = 50, dwell_s: float = 0.5):
        return self._queued({"cmd": "calibrate", "n_per_leg": n_per_leg, "dwell_s": dwell_s})

    def set_lock(self, locked: bool):
        return self._queued({"cmd": "set_lock", "locked": locked})

    def _queued(self, d: dict):
        """Send a command the service QUEUES; remember its sequence number so
        the wait helpers can tell a frame from before it from one after it.
        Returns the number, or None (refused, or an older service)."""
        r = self._cmd(d)
        seq = r.get("seq") if r.get("ok") else None
        if seq is not None:
            self._last_seq = seq
        return seq

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
                    poll_s: float = 0.05, seq=None):
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

        `seq` (the number set_field returned) closes the last gap: a frame
        from BEFORE the command can carry the same setpoint -- setting the
        field it already had -- so with `seq` we also require the service to
        have taken the command up (`cmd_done >= seq`).

        Raises TimeoutError rather than returning a flag, because a scan that
        silently records unsettled points produces data that looks fine and is
        wrong.
        """
        def settled(st):
            if not _taken_up(st, seq):
                return False
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
        seq = self.set_field(field_mT, use_pid=use_pid)
        return self.wait_stable(field_mT, timeout_s=timeout_s, poll_s=poll_s, seq=seq)

    def wait_idle(self, timeout_s: float = 60.0, poll_s: float = 0.05):
        """Block until the service is back in IDLE; return the final status.

        For the operations with no setpoint to adopt -- `demag`, `calibrate` --
        where "finished" means the state machine came home.

        It waits for the LAST command this client queued to have been taken
        up first. Without that, a demag sent to an idle service returned at
        once: the cached frame said IDLE because the control thread had not
        dequeued the demag yet (deep cleaning 2026-09-28).
        """
        seq = self._last_seq
        return self._wait_for(lambda st: _taken_up(st, seq) and st.state == "IDLE",
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
        self.stop_heartbeat()
        self._stop.set()
        time.sleep(0.25)                 # the listener closes its SUB socket
        with self._req_lock:
            self._req.close(0)

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
        self._with_identity(d)           # say who we are (control.py)
        with self._req_lock:
            for attempt in (1, 2):
                try:
                    self._req.send_json(d)
                    reply = self._req.recv_json()
                    break
                except zmq.Again:
                    # timed out; the REQ socket is now in a bad state -> rebuild it.
                    self._req.close(0)
                    # The service may speak the other mode than the policy now
                    # says (it was started before the policy changed): the new
                    # socket tries that mode, once. Safe to resend: a request
                    # in the wrong mode never reaches the service.
                    flipped = secure.no_answer(self.host, "clMag")
                    self._make_req()
                    if not (flipped and attempt == 1):
                        return {"ok": False, "error": "service did not respond (timeout)"}
        # Refused because another PC holds control: RAISE (ControlRefused),
        # never a quiet {"ok": false} -- a script must not believe the magnet
        # went where it asked. Other failures keep their old error-dict shape.
        if not reply.get("ok", False):
            self._raise_refusal(reply)
        return reply

    def _rpc(self, **req) -> dict:
        """The name control.py's ControlClient calls (heartbeat, take_control)."""
        return self._cmd(req)

    def _make_req(self):
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        self._req.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures clMag (secure.py); plain otherwise
        secure.secure_client(self._req, self.host, "clMag")
        self._req.connect(f"tcp://{self.host}:{self.cmd_port}")

    def _make_sub(self):
        s = self._ctx.socket(zmq.SUB)
        secure.secure_client(s, self.host, "clMag")      # telemetry too
        s.connect(f"tcp://{self.host}:{self.pub_port}")
        s.setsockopt(zmq.SUBSCRIBE, b"")
        return s, secure.flip_generation()

    def _listen(self):
        sub, gen = self._make_sub()
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if gen != secure.flip_generation():
                    # a request found the service in the other mode
                    # (secure.no_answer): telemetry follows
                    poller.unregister(sub)
                    sub.close(0)
                    sub, gen = self._make_sub()
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
