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

from ..control import make_identity
from .. import secure


class RemoteXYStage:
    def __init__(self, host: str = "127.0.0.1", cmd_port: int = 5561,
                 timeout_ms: int = 1500):
        self.host = host
        self.cmd_port = cmd_port
        self.timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self._lock = threading.Lock()
        self._req = None
        # The camera is a MACHINE client of piezo (control.py), exactly as it is
        # of kim (remote_kim.py): the stabiliser and click-to-go keep moving the sample
        # while a person's GUI (or the suite's Control tab) on another PC holds
        # control of piezo -- opening a window must not break a running
        # autofocus (Lukas's choice, 2026-09-29). Sent with every command.
        self.identity = make_identity("machine", "camera")

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
        # a send that cannot be delivered (no connection: a CurveZMQ handshake
        # refused in the wrong mode) must time out too, not wait forever
        self._req.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        # CurveZMQ once the lab's policy secures piezo (secure.py); plain
        # until then
        secure.secure_client(self._req, self.host, "piezo")
        self._req.connect(f"tcp://{self.host}:{self.cmd_port}")

    def _rpc(self, **req) -> dict:
        with self._lock:
            if self._req is None:
                self._make_req()
            req.setdefault("client", self.identity)
            for attempt in (1, 2):
                try:
                    self._req.send_json(req)
                    reply = self._req.recv_json()
                    break
                except zmq.Again:
                    # Broken REQ state after a timeout -> rebuild (lazy pirate).
                    self._req.close(0)
                    # piezo may speak the other mode than the policy now says
                    # (started before it changed): try that mode once. Safe
                    # to resend: a wrong-mode request never reaches piezo.
                    flipped = secure.no_answer(self.host, "piezo")
                    self._make_req()
                    if not (flipped and attempt == 1):
                        raise TimeoutError(
                            f"piezo service did not answer {req.get('cmd')!r}") from None
        # A refusal ({"ok": false}) must RAISE, like a timeout. It used to be
        # returned as if it were a reply: a refused move looked done (autofocus
        # scored the same Z at every "level"), and a failed status read became
        # position 0 (deep cleaning 2026-09-28).
        if not reply.get("ok", False):
            raise RuntimeError(f"piezo {req.get('cmd')}: {reply.get('error', 'failed')}")
        return reply

    # -- XYStage Protocol -------------------------------------------------- #
    def move_xy(self, x_um: float, y_um: float) -> None:
        self._rpc(cmd="move_xy", x=float(x_um), y=float(y_um))

    def read_xy(self) -> tuple:
        reply = self._rpc(cmd="status")
        st = reply.get("status", {})
        # piezo keeps the LAST GOOD position when its sensor read fails and
        # flags it in hw_error -- a stale position would steer the stabiliser.
        if st.get("hw_error"):
            raise RuntimeError(f"piezo: {st['hw_error']}")
        pos = st.get("position", [0.0, 0.0])
        return (float(pos[0]), float(pos[1]))

    def moving(self) -> bool:
        try:
            reply = self._rpc(cmd="status")
        except (TimeoutError, RuntimeError):   # unreachable or refused: as before
            return False
        st = reply.get("status", {})
        mv = st.get("moving", [False, False])
        return bool(mv[0] or mv[1])
