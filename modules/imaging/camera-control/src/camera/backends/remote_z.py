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

from ..control import make_identity


class RemoteZFocus:
    def __init__(self, host: str = "127.0.0.1", cmd_port: int = 5565,
                 timeout_ms: int = 1500):
        self.host = host
        self.cmd_port = cmd_port
        self.timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self._lock = threading.Lock()
        self._req = None
        # The camera is a MACHINE client of zpiezo (control.py), exactly as it is
        # of kim (remote_kim.py): autofocus and continuous focus keep setting the focus
        # while a person's GUI (or the suite's Control tab) on another PC holds
        # control of zpiezo -- opening a window must not break a running
        # autofocus (Lukas's choice, 2026-09-29). Sent with every command.
        self.identity = make_identity("machine", "camera")
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
            req.setdefault("client", self.identity)
            try:
                self._req.send_json(req)
                reply = self._req.recv_json()
            except zmq.Again:
                self._req.close(0)
                self._make_req()
                raise TimeoutError(f"zpiezo service did not answer {req.get('cmd')!r}")
        # A refusal ({"ok": false}) must RAISE, like a timeout. It used to be
        # returned as if it were a reply: a refused move looked done (autofocus
        # scored the same Z at every "level"), and a failed status read became
        # position 0 (deep cleaning 2026-09-28).
        if not reply.get("ok", False):
            raise RuntimeError(f"zpiezo {req.get('cmd')}: {reply.get('error', 'failed')}")
        return reply

    # -- ZFocus Protocol --------------------------------------------------- #
    def set_z(self, volts: float) -> None:
        self._rpc(cmd="set_voltage", volts=float(volts))

    def read_z(self) -> float:
        reply = self._rpc(cmd="status")
        st = reply.get("status", {})
        # zpiezo answers status even when ITS read of the KCube failed; it then
        # keeps the last good value (NaN if there never was one) and says so in
        # hw_error. Neither is a reading, so autofocus must not score it.
        if st.get("hw_error"):
            raise RuntimeError(f"zpiezo: {st['hw_error']}")
        return float(st.get("voltage", 0.0))

    def z_range(self) -> tuple:
        return self._range
