"""RemoteZFocus -- drive the focus (Z) piezo through the zpiezo-control SERVICE.

This is how the camera brain moves focus on the real rig when Z is EXTERNAL: it is
a CLIENT of the standalone zpiezo-control module, speaking its ZeroMQ wire protocol
(command port 5565 by default).  Like RemoteXYStage, it speaks the RAW protocol
(``pyzmq`` + ``json`` only, no ``zpiezo`` package import) so the two projects stay
decoupled.

It implements the :class:`camera.backends.base.ZFocus` Protocol, so from the
brain's point of view it is interchangeable with SimZFocus / KCubeZFocus -- the
camera's autofocus and continuous-focus loops work unchanged.
"""

from __future__ import annotations

import threading

import zmq


class RemoteZFocus:
    def __init__(self, host: str = "127.0.0.1", cmd_port: int = 5565,
                 timeout_ms: int = 1500):
        self.host = host
        self.cmd_port = cmd_port
        self.timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self._lock = threading.Lock()
        self._req = None
        self._range = (0.0, 75.0)   # cached from info() on open

    # -- lifecycle --------------------------------------------------------- #
    def open(self) -> None:
        self._make_req()
        try:
            info = self._rpc(cmd="info").get("info", {})
            lim = info.get("limits", {})
            self._range = (float(lim.get("v_min", 0.0)), float(lim.get("v_max", 75.0)))
        except Exception:
            pass   # keep the default range if the service isn't up yet

    def close(self) -> None:
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
            if self._req is None:
                self._make_req()
            try:
                self._req.send_json(req)
                return self._req.recv_json()
            except zmq.Again:
                self._req.close(0)
                self._make_req()
                raise TimeoutError(f"zpiezo service did not answer {req.get('cmd')!r}")

    # -- ZFocus Protocol --------------------------------------------------- #
    def set_z(self, volts: float) -> None:
        self._rpc(cmd="set_voltage", volts=float(volts))

    def read_z(self) -> float:
        reply = self._rpc(cmd="status")
        return float(reply.get("status", {}).get("voltage", 0.0))

    def z_range(self) -> tuple:
        return self._range
