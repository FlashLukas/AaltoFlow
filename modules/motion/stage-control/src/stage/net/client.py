"""StageClient -- a brain-compatible facade over the socket (§6 of the guide).

The client presents the SAME method names as the :class:`Stage` brain, so the
GUI can drive a local brain or a remote service through identical calls -- only
the object it is handed changes.  A background SUB thread caches the newest
status so ``status()`` is instant and never blocks on the network.

Commands go out on a REQ socket under a lock.  If a reply times out
(``zmq.Again``) the REQ socket is in a broken state, so we close and rebuild it
(the standard lazy-pirate recovery).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import zmq

from ..stage import StageStatus
from . import protocol as P


@dataclass
class RemoteStatus:
    """Mirror of :class:`stage.stage.StageStatus`, built from the wire dict.

    Same attribute names as the brain's Status so GUI code is agnostic about
    local vs remote.
    """

    position: list
    logical: list
    relative: list
    rel_origin: list
    moving: list
    homed: list
    velocity: list
    acceleration: list
    offsets: list
    matrix: list
    connected: bool
    #: Manifest revision from the service; None if it predates `describe`.
    describe_rev: int | None = None


def _status_from_dict(d: dict) -> RemoteStatus:
    return RemoteStatus(
        describe_rev=d.get("describe_rev"),
        position=d.get("position", [0, 0, 0]),
        logical=d.get("logical", [0, 0, 0]),
        relative=d.get("relative", [0, 0, 0]),
        rel_origin=d.get("rel_origin", [0, 0, 0]),
        moving=d.get("moving", [False, False, False]),
        homed=d.get("homed", [False, False, False]),
        velocity=d.get("velocity", [0, 0, 0]),
        acceleration=d.get("acceleration", [0, 0, 0]),
        offsets=d.get("offsets", [0, 0, 0]),
        matrix=d.get("matrix", [1, 0, 0, 1]),
        connected=d.get("connected", False),
    )


class StageClient:
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

        # Latest cached status + last event, updated by the SUB thread.
        self._status = RemoteStatus(
            [0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0], [False] * 3, [False] * 3,
            [0, 0, 0], [0, 0, 0], [0, 0, 0], [1, 0, 0, 1], False,
        )
        self._on_event = lambda level, msg: None
        self._stop = threading.Event()
        self._sub_thread = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Begin caching status and fetch initial info + config."""
        self._stop.clear()
        self._sub_thread = threading.Thread(
            target=self._sub_loop, name="stage-client-sub", daemon=True
        )
        self._sub_thread.start()
        # Prime info/config so callers can rely on them right after start().
        try:
            self.info()
            self.get_config()
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
                # Timed out: REQ socket is stuck mid-transaction -> rebuild it.
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
        sub.setsockopt(zmq.SUBSCRIBE, b"")  # all topics
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if dict(poller.poll(200)):
                    topic, raw = sub.recv_multipart()
                    import json
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
        """The service's parameter manifest: controls, indicators and actions.

        Limits inside it are LIVE, not constants, so compare
        `status().describe_rev` against the manifest's `revision` rather than
        caching this forever. See `net/describe.py`.
        """
        r = self._rpc(cmd="describe")
        return r.get("describe", {}) if r.get("ok") else {}

    def status(self) -> RemoteStatus:
        return self._status

    def info(self) -> dict:
        return self._rpc(cmd="info")["info"]

    def get_config(self) -> dict:
        return self._rpc(cmd="get_config")["config"]

    def set_config(self, config: dict) -> None:
        self._rpc(cmd="set_config", config=config)

    # motion
    def move_axis(self, axis, position) -> float:
        return self._rpc(cmd="move_axis", axis=axis, position=position)["target"]

    def move_logical(self, u, v, w) -> list:
        return self._rpc(cmd="move_logical", u=u, v=v, w=w)["targets"]

    def move_relative(self, axis, value) -> float:
        return self._rpc(cmd="move_relative", axis=axis, value=value)["target"]

    def home(self, axis=None) -> None:
        self._rpc(cmd="home", axis=axis)

    def home_all(self) -> None:
        self._rpc(cmd="home", axis=None)

    def stop(self, axis=None) -> None:
        self._rpc(cmd="stop", axis=axis)

    def stop_all(self) -> None:
        self._rpc(cmd="stop", axis=None)

    # parameters
    def set_velocity(self, axis, value) -> float:
        return self._rpc(cmd="set_velocity", axis=axis, value=value)["value"]

    def set_acceleration(self, axis, value) -> float:
        return self._rpc(cmd="set_acceleration", axis=axis, value=value)["value"]

    # relative frame ("zero here")
    def set_zero(self, axis=None) -> list:
        return self._rpc(cmd="set_zero", axis=axis)["rel_origin"]

    def set_zero_all(self) -> list:
        return self._rpc(cmd="set_zero", axis=None)["rel_origin"]

    def clear_zero(self, axis=None) -> None:
        self._rpc(cmd="clear_zero", axis=axis)

    # coordinate frame
    def set_offset(self, axis, value) -> None:
        self._rpc(cmd="set_offset", axis=axis, value=value)

    def get_matrix(self) -> list:
        return self._rpc(cmd="get_matrix")["matrix"]

    def set_matrix(self, m00, m01, m10, m11) -> list:
        return self._rpc(cmd="set_matrix", m00=m00, m01=m01, m10=m10, m11=m11)["matrix"]

    # position list
    def store_position(self, slot, name="") -> dict:
        return self._rpc(cmd="store_position", slot=slot, name=name)["position"]

    def clear_position(self, slot) -> None:
        self._rpc(cmd="clear_position", slot=slot)

    def goto_position(self, slot) -> list:
        return self._rpc(cmd="goto_position", slot=slot)["targets"]

    def get_positions(self) -> list:
        return self._rpc(cmd="get_positions")["positions"]

    def save_positions(self, path) -> None:
        self._rpc(cmd="save_positions", path=path)

    def load_positions(self, path) -> list:
        return self._rpc(cmd="load_positions", path=path)["positions"]
