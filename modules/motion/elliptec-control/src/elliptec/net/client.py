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


class ElliptecClient:
    def __init__(
        self,
        host: str = P.DEFAULT_HOST,
        cmd_port: int = P.DEFAULT_CMD_PORT,
        pub_port: int = P.DEFAULT_PUB_PORT,
        timeout_ms: int = 2000,
    ):
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
        try:
            info = self.info()
            self.addresses = list(info.get("addresses", []))
            self.names = list(info.get("names", []))
            self.n = len(self.addresses)
            self._status = _status_from_dict(self._rpc(cmd="status")["status"])
        except Exception:
            pass

    def close(self) -> None:
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
        self._req.setsockopt(zmq.LINGER, 0)
        self._req.connect(f"tcp://{self.host}:{self.cmd_port}")

    def _rpc(self, **req) -> dict:
        with self._lock:
            try:
                self._req.send_json(req)
                reply = self._req.recv_json()
            except zmq.Again:
                self._req.close(0)
                self._make_req()
                raise TimeoutError(f"no reply to {req.get('cmd')} within {self.timeout_ms} ms")
        if not reply.get("ok", False):
            raise RuntimeError(reply.get("error", "command failed"))
        return reply

    # ------------------------------------------------------------------ #
    # SUB caching thread
    # ------------------------------------------------------------------ #
    def _sub_loop(self) -> None:
        sub = self._ctx.socket(zmq.SUB)
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if dict(poller.poll(200)):
                    topic, raw = sub.recv_multipart()
                    payload = json.loads(raw.decode("utf-8"))
                    if topic == P.TOPIC_STATUS:
                        self._status = _status_from_dict(payload)
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
