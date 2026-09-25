"""RemoteXYStage -- drive the sample XY through the piezo-control SERVICE.

This is how the camera brain moves the sample on the real rig: it is a CLIENT of
the already-built piezo-control module, speaking its ZeroMQ wire protocol
(command port 5561 by default).  We deliberately speak the RAW protocol here --
``pyzmq`` + ``json`` only, no ``piezo`` package import -- so this module stays
self-contained and the two projects never develop a hard code dependency (the
same philosophy as the standalone consoles).

It implements the :class:`camera.backends.base.XYStage` Protocol, so from the
brain's point of view it is interchangeable with :class:`SimXYStage`.
"""

from __future__ import annotations

import threading

import zmq


class RemoteXYStage:
    def __init__(self, host: str = "127.0.0.1", cmd_port: int = 5561,
                 timeout_ms: int = 1500):
        self.host = host
        self.cmd_port = cmd_port
        self.timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self._lock = threading.Lock()
        self._req = None

    # -- lifecycle --------------------------------------------------------- #
    def open(self) -> None:
        self._make_req()

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
                # Broken REQ state after a timeout -> rebuild (lazy pirate).
                self._req.close(0)
                self._make_req()
                raise TimeoutError(f"piezo service did not answer {req.get('cmd')!r}")

    # -- XYStage Protocol -------------------------------------------------- #
    def move_xy(self, x_um: float, y_um: float) -> None:
        self._rpc(cmd="move_xy", x=float(x_um), y=float(y_um))

    def read_xy(self) -> tuple:
        reply = self._rpc(cmd="status")
        st = reply.get("status", {})
        pos = st.get("position", [0.0, 0.0])
        return (float(pos[0]), float(pos[1]))

    def moving(self) -> bool:
        try:
            reply = self._rpc(cmd="status")
        except TimeoutError:
            return False
        st = reply.get("status", {})
        mv = st.get("moving", [False, False])
        return bool(mv[0] or mv[1])
