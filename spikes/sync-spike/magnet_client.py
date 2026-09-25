"""
magnet_client.py
================

The brain-compatible CLIENT FACADE, same idea as the client.py in every one of
your modules: a thin Python object that talks REQ/REP to the instrument server
and hides the JSON-over-ZeroMQ plumbing.

For the synchronization problem, the one method that matters is `wait_settled()`:
it turns your fire-and-forget + poll-status pattern into a single BLOCKING call
that only returns once the field has actually arrived. That blocking call is the
hook every measurement framework (QCoDeS, Bluesky) needs.

In your real project you already have this class -- you'd reuse it as-is and just
add `wait_settled()` if it isn't there yet.
"""

import threading
import time

import zmq


class MagnetClient:
    def __init__(self, port=5555, host="127.0.0.1", rcvtimeo_ms=1000):
        self._addr = f"tcp://{host}:{port}"
        self._rcvtimeo = rcvtimeo_ms
        self._ctx = zmq.Context.instance()
        self._lock = threading.Lock()   # REQ sockets are not thread-safe
        self._connect()

    def _connect(self):
        self._sock = self._ctx.socket(zmq.REQ)
        self._sock.setsockopt(zmq.RCVTIMEO, self._rcvtimeo)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.connect(self._addr)

    def _req(self, payload):
        """One request/reply round-trip, rebuilding the socket on timeout.

        A REQ socket that times out is stuck in a bad state (it expected a
        reply that never came), so we throw it away and reconnect -- the same
        rebuild-on-timeout trick your real client uses.
        """
        with self._lock:
            try:
                self._sock.send_json(payload)
                return self._sock.recv_json()
            except zmq.Again:
                self._sock.close(0)
                self._connect()
                raise TimeoutError(f"no reply from {self._addr} for {payload}")

    # --- high-level API the rest of the program uses -----------------------
    def set_field(self, value_mT):
        """Fire-and-forget: queue a new setpoint, return immediately."""
        return self._req({"cmd": "set_field", "value": value_mT})

    def status(self):
        return self._req({"cmd": "status"})

    def read_signal(self):
        return self._req({"cmd": "read_signal"})["signal"]

    def wait_settled(self, timeout_s=10.0, poll_s=0.02):
        """BLOCK until the field reports settled (or raise on timeout).

        This is the bridge between 'fire-and-forget + poll' and 'a blocking
        set()'. Everything above the client can now pretend setting the field
        is a single synchronous action.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.status().get("settled"):
                return
            time.sleep(poll_s)
        raise TimeoutError("field did not settle in time")

    def set_field_blocking(self, value_mT, timeout_s=10.0):
        """Convenience: set + wait, as one synchronous call."""
        self.set_field(value_mT)
        self.wait_settled(timeout_s=timeout_s)
