"""A fake instrument service, so scan-core can be tested without the lab.

scan-core talks to instruments over the wire and imports no instrument package.
That decoupling pays off here: to test it, we only need something that speaks
the suite's contract -- REQ/REP JSON commands plus a PUB status stream. Fifty
lines of pyzmq, no clMag, no hardware, no vendor drivers.

The fake deliberately reproduces the timing that makes fire-and-forget subtle:
after `set_field` returns ok, there is a window in which the service is still
reporting the PREVIOUS point, stable flag and all. That window is what
`adopt_then_flag` exists to survive, so the fake has to have it or the tests
would prove nothing.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

zmq = pytest.importorskip("zmq")


class FakeService:
    """A clMag-shaped service: closed-loop field, AUX inputs, status at 20 Hz."""

    def __init__(self, cmd_port: int, adopt_delay: float = 0.25,
                 settle_delay: float = 0.25, manifest=None):
        #: What `describe` returns. None = this service predates the verb and
        #: answers ok:false, which is how a coordinator finds out it has to fall
        #: back to a hand-written declaration.
        self.manifest = manifest
        self.cmd_port = cmd_port
        self.pub_port = cmd_port + 1
        self.adopt_delay = adopt_delay      # accepted -> setpoint visible
        self.settle_delay = settle_delay    # setpoint visible -> stable

        # Start already settled at 0 mT. That matters: it means the very first
        # `set_field` in a test faces a live stale `field_stable=True`.
        self._lock = threading.Lock()
        self._setpoint = 0.0
        self._measured = 0.0
        self._stable = True
        self._rf_power = -10.0

        self.commands: list[str] = []       # what the client actually sent
        self._stop = threading.Event()
        self._ctx = zmq.Context.instance()
        self._threads: list[threading.Thread] = []

    # ---- lifecycle -------------------------------------------------------

    def start(self):
        for target, name in ((self._serve, "fake-rep"), (self._publish, "fake-pub")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        time.sleep(0.15)                    # let the sockets bind
        return self

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.5)

    # ---- the contract ----------------------------------------------------

    def _status(self) -> dict:
        with self._lock:
            return {"state": "STABLE" if self._stable else "SEEK",
                    "setpoint_field_mT": self._setpoint,
                    "measured_field_mT": self._measured,
                    "current_A": self._measured / 40.0,
                    "field_stable": self._stable,
                    "power_dBm": self._rf_power,
                    "describe_rev": (self.manifest or {}).get("revision")}

    def _handle(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        self.commands.append(cmd)
        if cmd == "info":
            return {"ok": True, "info": {"field_lo": -95.0, "field_hi": 95.0,
                                         "n_points": 50, "tolerance": 0.1,
                                         "power_min_dBm": -145.0,
                                         "power_max_dBm": 18.0}}
        if cmd == "status":
            return {"ok": True, "status": self._status()}
        if cmd == "describe":
            if self.manifest is None:
                return {"ok": False, "error": "unknown command: 'describe'"}
            return {"ok": True, "describe": self.manifest}
        if cmd == "set_field":
            target = float(msg["field_mT"])
            # Fire-and-forget: reply at once, change nothing yet. The status
            # keeps describing the old point until _adopt catches up.
            threading.Thread(target=self._adopt, args=(target,), daemon=True).start()
            return {"ok": True}
        if cmd == "set_power":
            with self._lock:
                self._rf_power = float(msg["power_dBm"])
            return {"ok": True}
        if cmd == "aux_read_ai":
            ch = msg.get("channel", "")
            return {"ok": True, "volts": 0.1 * len(ch)}   # deterministic
        return {"ok": False, "error": f"unknown command: {cmd!r}"}

    def _adopt(self, target: float):
        time.sleep(self.adopt_delay)
        with self._lock:
            self._setpoint = target         # adopted; no longer stale
            self._stable = False
        time.sleep(self.settle_delay)
        with self._lock:
            self._measured = target
            self._stable = True

    # ---- sockets ---------------------------------------------------------

    def _serve(self):
        rep = self._ctx.socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep.bind(f"tcp://127.0.0.1:{self.cmd_port}")
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                if poller.poll(100):
                    try:
                        reply = self._handle(rep.recv_json())
                    except Exception as exc:                # never die
                        reply = {"ok": False, "error": str(exc)}
                    rep.send_json(reply)
        finally:
            rep.close(0)

    def _publish(self):
        pub = self._ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(f"tcp://127.0.0.1:{self.pub_port}")
        try:
            while not self._stop.is_set():
                pub.send_multipart([b"status",
                                    json.dumps(self._status()).encode()])
                time.sleep(0.05)
        finally:
            pub.close(0)


@pytest.fixture
def fake_service():
    """Yield a factory so each test picks its own port and timing."""
    made: list[FakeService] = []

    def make(cmd_port: int, **kwargs):
        svc = FakeService(cmd_port, **kwargs).start()
        made.append(svc)
        return svc

    yield make
    for svc in made:
        svc.stop()


#: A manifest shaped like clMag's, small enough to reason about in a test.
#: Deliberately includes the awkward cases: a bool control, an enum control
#: (which scan-core cannot sweep), a control with a readback that should also be
#: recordable, and an action (which is not a scan axis).
DEMO_MANIFEST = {
    "schema": 1,
    "module": "fake",
    "label": "Fake instrument",
    "revision": 12345,
    "parameters": [
        {"id": "field", "label": "Magnetic field", "kind": "control",
         "type": "float", "unit": "mT", "min": -95.0, "max": 95.0,
         "writable": True, "plottable": True,
         "read_path": ["measured_field_mT"],
         "set": {"verb": "set_field", "arg": "field_mT"},
         "settle": {"policy": "adopt_then_flag",
                    "setpoint_key": "setpoint_field_mT",
                    "flag_key": "field_stable"}},
        {"id": "rf_power", "label": "RF power", "kind": "control",
         "type": "float", "unit": "dBm", "min": -30.0, "max": 18.0,
         "writable": True, "read_path": ["power_dBm"],
         "set": {"verb": "set_power", "arg": "power_dBm"},
         "settle": {"policy": "echoes", "key": "power_dBm", "tol": 1e-3}},
        {"id": "enabled", "label": "Enabled", "kind": "control", "type": "bool",
         "unit": "", "writable": True, "read_path": ["field_stable"],
         "set": {"verb": "set_enabled", "arg": "on"},
         "settle": {"policy": "immediate"}},
        {"id": "mode", "label": "Ramp mode", "kind": "control", "type": "enum",
         "unit": "", "options": ["hardware", "software", "off"],
         "writable": True, "read_path": ["state"],
         "set": {"verb": "set_mode", "arg": "mode"},
         "settle": {"policy": "immediate"}},
        {"id": "measured_field", "label": "Measured field", "kind": "indicator",
         "type": "float", "unit": "mT", "plottable": True,
         "read_path": ["measured_field_mT"]},
        {"id": "state", "label": "State", "kind": "indicator", "type": "string",
         "unit": "", "read_path": ["state"]},
        {"id": "demag", "label": "Demagnetise", "kind": "action",
         "type": "action", "danger": True,
         "args": [{"name": "amplitude_A", "type": "float", "default": 1.5}]},
    ],
}
