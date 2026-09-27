"""SmaractClient -- a brain-compatible facade over the socket (section 6).

The client presents the SAME method names as the :class:`Positioner` brain, so the
GUI can drive a local brain or a remote service through identical calls -- only
the object it is handed changes.  A background SUB thread caches the newest
status so ``status()`` is instant and never blocks on the network.

Commands go out on a REQ socket under a lock.  If a reply times out
(``zmq.Again``) the REQ socket is in a broken state, so we close and rebuild it
(the standard lazy-pirate recovery).
"""

from __future__ import annotations

import threading
from dataclasses import fields

import zmq

from ..smaract import SmaractStatus
from . import protocol as P


def _nan(v):
    """null on the wire (no reading yet) -> NaN, which the GUI shows as '--'."""
    return float("nan") if v is None else v


def _status_from_dict(d: dict) -> SmaractStatus:
    """Wire dict -> the SAME dataclass the local brain returns, so the GUI
    cannot tell a remote service from a local brain. Unknown keys are ignored
    (a newer service), missing ones keep their defaults (an older one)."""
    known = {f.name for f in fields(SmaractStatus)}
    st = SmaractStatus(**{k: v for k, v in d.items() if k in known})
    for f in ("position_mm", "target_mm", "relative_mm"):
        setattr(st, f, _nan(getattr(st, f)))
    return st


#: Kept as a name for code that imported it; it IS the brain's status class.
RemoteStatus = SmaractStatus


class SmaractClient:
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
        self._status = SmaractStatus()
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
            target=self._sub_loop, name="smaract-client-sub", daemon=True
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

    def status(self) -> SmaractStatus:
        return self._status

    def info(self) -> dict:
        return self._rpc(cmd="info")["info"]

    def get_config(self) -> dict:
        return self._rpc(cmd="get_config")["config"]

    def set_config(self, config: dict) -> None:
        self._rpc(cmd="set_config", config=config)

    # motion (fire-and-forget: each returns once the service ACCEPTED it)
    def move_to(self, position) -> float:
        return self._rpc(cmd="move_to", position=position)["target"]

    def move_by(self, delta) -> float:
        return self._rpc(cmd="move_by", delta=delta)["target"]

    def move_from_zero(self, value) -> float:
        return self._rpc(cmd="move_from_zero", value=value)["target"]

    def find_reference(self) -> int:
        return self._rpc(cmd="find_reference")["ref_id"]

    def stop(self) -> None:
        self._rpc(cmd="stop")

    # parameters
    def set_velocity(self, value) -> float:
        return self._rpc(cmd="set_velocity", value=value)["value"]

    def set_hold_time(self, value) -> int:
        return self._rpc(cmd="set_hold_time", value=value)["value"]

    # relative frame ("zero here")
    def set_zero(self) -> float:
        return self._rpc(cmd="set_zero")["rel_origin_mm"]

    def clear_zero(self) -> None:
        self._rpc(cmd="clear_zero")

    # stored positions
    def store_position(self, slot, name="") -> dict:
        return self._rpc(cmd="store_position", slot=slot, name=name)["position"]

    def clear_position(self, slot) -> None:
        self._rpc(cmd="clear_position", slot=slot)

    def goto_position(self, slot) -> float:
        return self._rpc(cmd="goto_position", slot=slot)["target"]

    def get_positions(self) -> list:
        return self._rpc(cmd="get_positions")["positions"]

    def save_positions(self, path) -> None:
        self._rpc(cmd="save_positions", path=path)

    def load_positions(self, path) -> list:
        return self._rpc(cmd="load_positions", path=path)["positions"]

    # fly-scan stream
    def stream_start(self) -> int:
        return self._rpc(cmd="stream_start")["stream_id"]

    def stream_read(self) -> dict:
        return self._rpc(cmd="stream_read")["stream"]

    def stream_stop(self) -> dict:
        return self._rpc(cmd="stream_stop")["stream"]
