"""ZPiezoClient -- brain-compatible facade over the socket (blueprint §6)."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

import zmq

from . import protocol as P


@dataclass
class RemoteStatus:
    connected: bool = False
    voltage: float = 0.0
    target: float = 0.0
    v_min: float = 0.0
    v_max: float = 75.0
    #: Manifest revision from the service; None if it predates `describe`.
    describe_rev: int | None = None


def _status_from_dict(d: dict) -> RemoteStatus:
    return RemoteStatus(
        describe_rev=d.get("describe_rev"),
        connected=d.get("connected", False), voltage=d.get("voltage", 0.0),
        target=d.get("target", 0.0), v_min=d.get("v_min", 0.0), v_max=d.get("v_max", 75.0))


class ZPiezoClient:
    def __init__(self, host=P.DEFAULT_HOST, cmd_port=P.DEFAULT_CMD_PORT,
                 pub_port=P.DEFAULT_PUB_PORT, timeout_ms=2000):
        self.host, self.cmd_port, self.pub_port = host, cmd_port, pub_port
        self.timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self._lock = threading.Lock()
        self._req = None
        self._make_req()
        self._status = RemoteStatus()
        self._on_event = lambda level, msg: None
        self._stop = threading.Event()
        self._sub_thread = None

    def start(self) -> None:
        self._stop.clear()
        self._sub_thread = threading.Thread(target=self._sub_loop, name="z-client-sub", daemon=True)
        self._sub_thread.start()
        try:
            self.info()
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

    # brain-compatible surface
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

    def set_voltage(self, volts: float) -> float:
        return self._rpc(cmd="set_voltage", volts=volts)["voltage"]

    def read_voltage(self) -> float:
        return self._rpc(cmd="read_voltage")["voltage"]
