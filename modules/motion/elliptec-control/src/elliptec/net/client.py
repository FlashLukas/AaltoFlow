"""ElliptecClient -- a brain-compatible facade over the socket (section 6 of the guide).

The client presents the SAME method names as the :class:`RotationMount` brain,
so the GUI can drive a local brain or a remote service through identical calls
-- only the object it is handed changes.  A background SUB thread caches the
newest status so ``status()`` is instant and never blocks on the network.

Commands go out on a REQ socket under a lock.  If a reply times out
(``zmq.Again``) the REQ socket is stuck mid-transaction, so it is closed and
rebuilt (the standard "lazy pirate" recovery).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, fields

import zmq

from ..control import ControlClient
from .. import secure
from . import protocol as P


@dataclass
class RemoteStatus:
    """Mirror of :class:`elliptec.mount.MountStatus`, built from the wire dict.

    Same attribute names as the brain's status, so GUI code does not care
    whether it talks to a local brain or a remote service.
    """

    addresses: list = field(default_factory=list)
    names: list = field(default_factory=list)
    angle_deg: list = field(default_factory=list)
    device_deg: list = field(default_factory=list)
    target_deg: list = field(default_factory=list)
    moving: list = field(default_factory=list)
    homed: list = field(default_factory=list)
    velocity_pct: list = field(default_factory=list)
    offset_deg: list = field(default_factory=list)
    error_code: list = field(default_factory=list)
    error: list = field(default_factory=list)
    move_id: list = field(default_factory=list)
    connected: bool = False
    n_axes: int = 0
    #: Manifest revision from the service; None if it predates `describe`.
    describe_rev: int | None = None


_FIELDS = {f.name for f in fields(RemoteStatus)}


def _status_from_dict(d: dict) -> RemoteStatus:
    return RemoteStatus(**{k: v for k, v in d.items() if k in _FIELDS})


class ElliptecClient(ControlClient):
    """``kind`` / ``name``: who this client is to the service (control.py) --
    "gui" for a window, "script" (default) for a script or console, "machine"
    only for a program that must not be locked out (scan-core). While a GUI on
    another PC holds control, a script must ``take_control()`` before it may
    change anything; a refused command raises ``ControlRefused``."""

    def __init__(
        self,
        host: str = P.DEFAULT_HOST,
        cmd_port: int = P.DEFAULT_CMD_PORT,
        pub_port: int = P.DEFAULT_PUB_PORT,
        timeout_ms: int = 2000,
        kind: str = "script",
        name: str = "elliptec client",
    ):
        self._control_setup(kind, name)
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.timeout_ms = timeout_ms

        self._ctx = zmq.Context.instance()
        self._lock = threading.Lock()
        self._req = None
        self._make_req()

        self._status = RemoteStatus()
        self.addresses: list = []
        self.names: list = []
        self.n = 0
        self._on_event = lambda level, msg: None
        self._stop = threading.Event()
        self._sub_thread = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Begin caching status and fetch the axis list, so the GUI can build
        one row per mount right after start()."""
        self._stop.clear()
        self._sub_thread = threading.Thread(
            target=self._sub_loop, name="elliptec-client-sub", daemon=True
        )
        self._sub_thread.start()
        self.start_heartbeat()           # "still here": counted as a viewer / keeps control
        try:
            info = self.info()
            self.addresses = list(info.get("addresses", []))
            self.names = list(info.get("names", []))
            self.n = len(self.addresses)
            st = self._rpc(cmd="status")["status"]
            self._status = _status_from_dict(st)
            self._control_from_status(st)
        except Exception:
            pass

    def close(self) -> None:
        self.stop_heartbeat()
        self._stop.set()
        if self._sub_thread is not None:
            self._sub_thread.join(timeout=1.0)
        with self._lock:
            if self._req is not None:
                self._req.close(0)
                self._req = None

    # ------------------------------------------------------------------ #
    # REQ plumbing
    # ------------------------------------------------------------------ #
    def _make_req(self) -> None:
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        self._req.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures elliptec (secure.py); plain otherwise
        secure.secure_client(self._req, self.host, "elliptec")
        self._req.connect(f"tcp://{self.host}:{self.cmd_port}")

    def _rpc(self, **req) -> dict:
        self._with_identity(req)         # say who we are (control.py)
        with self._lock:
            for attempt in (1, 2):
                try:
                    self._req.send_json(req)
                    reply = self._req.recv_json()
                    break
                except zmq.Again:
                    # Timed out: REQ socket is stuck mid-transaction -> rebuild it.
                    self._req.close(0)
                    # The service may speak the other mode than the policy now
                    # says (it was started before the policy changed): the new
                    # socket tries that mode, once. Safe to resend: a request
                    # in the wrong mode never reaches the service.
                    flipped = secure.no_answer(self.host, "elliptec")
                    self._make_req()
                    if not (flipped and attempt == 1):
                        raise TimeoutError(
                            f"no reply to {req.get('cmd')} within {self.timeout_ms} ms") from None
        if not reply.get("ok", False):
            self._raise_refusal(reply)       # ControlRefused: another client has control
            raise RuntimeError(reply.get("error", "command failed"))
        return reply

    # ------------------------------------------------------------------ #
    # SUB caching thread
    # ------------------------------------------------------------------ #
    def _sub_loop(self) -> None:
        def make_sub():
            s = self._ctx.socket(zmq.SUB)
            secure.secure_client(s, self.host, "elliptec")      # telemetry too
            s.connect(f"tcp://{self.host}:{self.pub_port}")
            s.setsockopt(zmq.SUBSCRIBE, b"")  # all topics
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
                if dict(poller.poll(200)):
                    topic, raw = sub.recv_multipart()
                    payload = json.loads(raw.decode("utf-8"))
                    if topic == P.TOPIC_STATUS:
                        self._status = _status_from_dict(payload)
                        self._control_from_status(payload)
                    elif topic == P.TOPIC_EVENT:
                        try:
                            self._on_event(payload.get("level", "info"), payload.get("msg", ""))
                        except Exception:
                            pass
        finally:
            sub.close(0)

    # ------------------------------------------------------------------ #
    # brain-compatible surface
    # ------------------------------------------------------------------ #
    def describe(self) -> dict:
        """The service's parameter manifest (limits inside are LIVE: compare
        ``status().describe_rev`` with its ``revision`` before trusting a cache)."""
        return self._rpc(cmd="describe").get("describe", {})

    def status(self) -> RemoteStatus:
        return self._status

    def info(self) -> dict:
        return self._rpc(cmd="info")["info"]

    def get_config(self) -> dict:
        return self._rpc(cmd="get_config")["config"]

    def set_config(self, config: dict) -> None:
        self._rpc(cmd="set_config", config=config)

    # motion
    def move_abs(self, axis, angle_deg) -> dict:
        r = self._rpc(cmd="move_abs", axis=axis, angle_deg=angle_deg)
        return {"target": r["target"], "move_id": r["move_id"]}

    def move_rel(self, axis, delta_deg) -> dict:
        r = self._rpc(cmd="move_rel", axis=axis, delta_deg=delta_deg)
        return {"target": r["target"], "move_id": r["move_id"]}

    def home(self, axis, direction=None) -> dict:
        r = self._rpc(cmd="home", axis=axis, direction=direction)
        return {"target": r["target"], "move_id": r["move_id"]}

    def home_all(self, direction=None) -> list:
        return self._rpc(cmd="home", axis=None, direction=direction)["move_ids"]

    def stop(self, axis) -> None:
        self._rpc(cmd="stop", axis=axis)

    def stop_all(self) -> None:
        self._rpc(cmd="stop", axis=None)

    # parameters and frame
    def set_velocity(self, axis, value) -> int:
        return self._rpc(cmd="set_velocity", axis=axis, value=value)["value"]

    def set_offset(self, axis, value) -> float:
        return self._rpc(cmd="set_offset", axis=axis, value=value)["offset_deg"]

    def set_zero(self, axis) -> float:
        return self._rpc(cmd="set_zero", axis=axis)["offset_deg"]

    def clear_zero(self, axis) -> float:
        return self._rpc(cmd="clear_zero", axis=axis)["offset_deg"]
