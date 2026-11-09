"""The polarity relay on a digital line of the usb6001-control SERVICE.

The relay coil is driven by a DAQ output line (on the vacuum setup: Dev1/
port0/line0 of an NI USB-6001). That card belongs to usb6001-control -- one
card, one service, and its analog inputs (the Hall probe, the vacuum gauge,
the photodiodes) are read there. So this module does not open the card: it is
a CLIENT of that service, exactly as the camera is a client of the kim stage.

Raw pyzmq, no import of the usb6001 package (the projects stay decoupled):
    {"cmd": "set_do", "line": "p0.0", "state": true}  -> drive the line
    {"cmd": "status"}                                 -> status["dio"][index]

We identify as a MACHINE client (control.py): the relay must still switch when
a person has the usb6001 window open and holds control of the DAQ -- a sign
change in the middle of a field sweep must not fail because of that.

Nothing is written on open() or close(): the relay stays where it is (Lukas,
2026-11-09: adopt at start, leave it on stop).
"""

from __future__ import annotations

import threading

import zmq

from .. import secure
from ..config import dio_index
from ..control import make_identity


class Usb6001Relay:
    """Implements PolarityRelay over usb6001-control's set_do / status."""

    def __init__(self, host: str = "127.0.0.1", cmd_port: int = 5629,
                 line: str = "p0.0", inverted_level: str = "high",
                 timeout_ms: int = 1500):
        self.host = host
        self.cmd_port = int(cmd_port)
        self.line = str(line)
        self.index = dio_index(line)              # raises for a bad line name
        level = str(inverted_level).strip().lower()
        if level not in ("high", "low"):
            raise ValueError(f"polarity.inverted_level must be 'high' or 'low', not {inverted_level!r}")
        # inverted <-> which level: _HIGH_IS_INV True = high level means inverted
        self._high_is_inverted = level == "high"
        self.timeout_ms = int(timeout_ms)
        self._ctx = zmq.Context.instance()
        self._lock = threading.Lock()
        self._req = None
        self.identity = make_identity("machine", "alr3203 polarity relay")

    # -- lifecycle --------------------------------------------------------- #
    def open(self) -> None:
        with self._lock:
            if self._req is None:
                self._make_req()

    def close(self) -> None:
        with self._lock:
            if self._req is not None:
                self._req.close(0)
                self._req = None

    def _make_req(self) -> None:
        self._req = self._ctx.socket(zmq.REQ)
        self._req.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self._req.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self._req.setsockopt(zmq.LINGER, 0)
        # usb6001's key from the lab keyring, when the policy secures it
        secure.secure_client(self._req, self.host, "usb6001")
        self._req.connect(f"tcp://{self.host}:{self.cmd_port}")

    # -- the relay ---------------------------------------------------------- #
    def read(self):
        """True = inverted, False = normal, None = unknown: the service is not
        reachable, the line is not an output there, or its level is unknown
        (never written, card cannot read it back)."""
        try:
            st = self._rpc(cmd="status")["status"]
        except Exception:
            return None
        if st.get("hw_error"):
            return None
        dirs = st.get("dio_dir") or []
        if self.index >= len(dirs) or dirs[self.index] != "out":
            return None
        level = (st.get("dio") or [None] * 13)[self.index]
        if level is None:
            return None
        return bool(level) == self._high_is_inverted

    def set(self, inverted: bool) -> None:
        """Drive the line; raises if usb6001-control refuses or does not answer."""
        level = bool(inverted) == self._high_is_inverted
        self._rpc(cmd="set_do", line=self.line, state=level)

    def describe(self) -> str:
        return (f"usb6001 line {self.line} at {self.host}:{self.cmd_port} "
                f"({'high' if self._high_is_inverted else 'low'} = inverted)")

    # -- one request -------------------------------------------------------- #
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
                    # a REQ socket that timed out is stuck mid-exchange: rebuild
                    # it; the service may speak the other mode (plain <->
                    # CurveZMQ) than the policy now says: try that once
                    self._req.close(0)
                    flipped = secure.no_answer(self.host, "usb6001")
                    self._make_req()
                    if not (flipped and attempt == 1):
                        raise TimeoutError(
                            f"usb6001-control at {self.host}:{self.cmd_port} did not "
                            f"answer {req.get('cmd')!r} -- is it running?") from None
        if not reply.get("ok", False):
            raise RuntimeError(f"usb6001 {req.get('cmd')}: {reply.get('error', 'failed')}")
        return reply
