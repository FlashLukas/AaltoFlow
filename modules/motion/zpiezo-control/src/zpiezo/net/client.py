"""ZPiezoClient -- brain-compatible facade over the socket (blueprint §6)."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

import zmq

from .. import secure
from ..control import ControlClient
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
    #: "" while healthy; the service's message while its voltage reads fail.
    hw_error: str = ""


def _status_from_dict(d: dict) -> RemoteStatus:
    return RemoteStatus(
        describe_rev=d.get("describe_rev"),
        connected=d.get("connected", False), voltage=d.get("voltage", 0.0),
        target=d.get("target", 0.0), v_min=d.get("v_min", 0.0), v_max=d.get("v_max", 75.0),
        hw_error=d.get("hw_error", "") or "")


class ZPiezoClient(ControlClient):
    """``kind`` / ``name``: who this client is to the service (control.py) --
    "gui" for a window, "script" (default) for a script or console, "machine"
    only for a program that must not be locked out (scan-core, the camera).
    A script must ``take_control()`` before it may change anything while a GUI
    on another PC holds control; a refused command raises ``ControlRefused``."""

    def __init__(self, host=P.DEFAULT_HOST, cmd_port=P.DEFAULT_CMD_PORT,
                 pub_port=P.DEFAULT_PUB_PORT, timeout_ms=2000,
                 kind: str = "script", name: str = "zpiezo client"):
        self._control_setup(kind, name)
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
        self.start_heartbeat()           # "still here": counted as a viewer / keeps control
        try:
            self.info()
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

    def _make_req(self) -> None:
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        self._req.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        # encrypted, and the service's key checked, when the lab's policy
        # secures zpiezo (secure.py); plain otherwise
        secure.secure_client(self._req, self.host, "zpiezo")
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
                    flipped = secure.no_answer(self.host, "zpiezo")
                    self._make_req()
                    if not (flipped and attempt == 1):
                        raise TimeoutError(
                            f"no reply to {req.get('cmd')} within {self.timeout_ms} ms") from None
        if not reply.get("ok", False):
            self._raise_refusal(reply)       # ControlRefused: another client has control
            raise RuntimeError(reply.get("error", "command failed"))
        return reply

    def _sub_loop(self) -> None:
        def make_sub():
            s = self._ctx.socket(zmq.SUB)
            secure.secure_client(s, self.host, "zpiezo")      # telemetry too
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
