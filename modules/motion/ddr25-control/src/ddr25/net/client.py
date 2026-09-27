"""Ddr25Client -- a brain-compatible facade over the socket (section 6).

The client presents the SAME method names as the :class:`Rotator` brain, so
the GUI can drive a local brain or a remote service through identical calls --
only the object it is handed changes. A background SUB thread caches the
newest status so ``status()`` is instant and never blocks on the network.

Commands go out on a REQ socket under a lock. If a reply times out
(``zmq.Again``) the REQ socket is in a broken state, so we close and rebuild it
(the standard lazy-pirate recovery).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

import zmq

from . import protocol as P


def _f(v, default=float("nan")):
    """A float from the wire, where null stands for NaN."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass
class RemoteStatus:
    """Mirror of :class:`ddr25.rotator.RotatorStatus`, built from the wire dict.
    Same attribute names, so GUI code cannot tell local from remote."""

    angle_deg: float = float("nan")
    raw_deg: float = float("nan")
    target_deg: float | None = None
    moving: bool = False
    homed: bool = False
    homing: bool = False
    home_id: int = 0
    move_id: int = 0
    velocity: float = float("nan")
    acceleration: float = float("nan")
    zero_deg: float = 0.0
    wrap: str = "literal"
    streaming: bool = False
    connected: bool = False
    hw_error: str = ""
    #: Manifest revision from the service; None if it predates `describe`.
    describe_rev: int | None = None


def _status_from_dict(d: dict) -> RemoteStatus:
    t = d.get("target_deg")
    return RemoteStatus(
        angle_deg=_f(d.get("angle_deg")),
        raw_deg=_f(d.get("raw_deg")),
        target_deg=None if t is None else _f(t),
        moving=bool(d.get("moving", False)),
        homed=bool(d.get("homed", False)),
        homing=bool(d.get("homing", False)),
        home_id=int(d.get("home_id") or 0),
        move_id=int(d.get("move_id") or 0),
        velocity=_f(d.get("velocity")),
        acceleration=_f(d.get("acceleration")),
        zero_deg=_f(d.get("zero_deg"), 0.0),
        wrap=str(d.get("wrap", "literal")),
        streaming=bool(d.get("streaming", False)),
        connected=bool(d.get("connected", False)),
        hw_error=str(d.get("hw_error") or ""),
        describe_rev=d.get("describe_rev"),
    )


class Ddr25Client:
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
        self._status = RemoteStatus()
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
            target=self._sub_loop, name="ddr25-client-sub", daemon=True
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

    def shutdown_service(self) -> None:
        """Ask the SERVICE to stop and exit (the launcher's clean stop)."""
        self._rpc(cmd="shutdown")

    # motion
    def move_to(self, angle) -> float:
        return self._rpc(cmd="move_to", angle=angle)["target"]

    def move_by(self, delta) -> float:
        return self._rpc(cmd="move_by", delta=delta)["target"]

    def home(self) -> int:
        return self._rpc(cmd="home")["home_id"]

    def stop(self, immediate: bool = False) -> None:
        self._rpc(cmd="stop", immediate=bool(immediate))

    # profile
    def set_velocity(self, value) -> float:
        return self._rpc(cmd="set_velocity", value=value)["value"]

    def set_acceleration(self, value) -> float:
        return self._rpc(cmd="set_acceleration", value=value)["value"]

    def set_wrap(self, policy) -> str:
        return self._rpc(cmd="set_wrap", wrap=policy)["wrap"]

    # display zero
    def set_zero(self) -> float:
        return self._rpc(cmd="set_zero")["zero_deg"]

    def clear_zero(self) -> None:
        self._rpc(cmd="clear_zero")

    # stored angles
    def store_angle(self, slot, name="") -> dict:
        return self._rpc(cmd="store_angle", slot=slot, name=name)["slot"]

    def clear_angle(self, slot) -> None:
        self._rpc(cmd="clear_angle", slot=slot)

    def goto_angle(self, slot) -> float:
        return self._rpc(cmd="goto_angle", slot=slot)["target"]

    def get_angles(self) -> list:
        return self._rpc(cmd="get_angles")["angles"]

    def save_angles(self, path) -> None:
        self._rpc(cmd="save_angles", path=path)

    def load_angles(self, path) -> list:
        return self._rpc(cmd="load_angles", path=path)["angles"]

    # fly-scan stream
    def stream_start(self, rate_hz=None) -> int:
        return self._rpc(cmd="stream_start", rate_hz=rate_hz)["stream_id"]

    def stream_read(self) -> dict:
        return self._rpc(cmd="stream_read")["stream"]

    def stream_stop(self) -> dict:
        return self._rpc(cmd="stream_stop")["stream"]
