"""An array detector that lives in ANOTHER PROCESS -- the VNA case over ZeroMQ.

The in-process sim VNA (`build_sim_registry`) hands the engine numpy arrays
directly. A real module cannot: its trace is too big for the status stream, and
JSON has no complex numbers. So a manifest declares

    "read": {"verb": "get_trace", "key": "s21", "args": {"which": "sample"}}

and the module answers with {"re": [...], "im": [...]}. These tests drive that
path through a fake service that speaks nothing but the wire contract.
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

zmq = pytest.importorskip("zmq")

from scan_core.engine import run                                   # noqa: E402
from scan_core.instrument import Instrument                        # noqa: E402
from scan_core.manifest import decode_wire_value, register_manifest  # noqa: E402
from scan_core.recipe import Recipe                                # noqa: E402
from scan_core.registry import Registry, Settable                  # noqa: E402

N = 5


def test_decode_wire_value():
    z = decode_wire_value({"re": [1.0, None], "im": [2.0, 3.0]}, complex_=True)
    assert z[0] == 1 + 2j and np.isnan(z[1].real) and z[1].imag == 3.0
    f = decode_wire_value([1.0, None, 2.5])
    assert f.dtype == float and np.isnan(f[1])
    assert decode_wire_value([1.0, 2.0], complex_=True).dtype == complex
    assert decode_wire_value(4.2) == 4.2


class FakeVna:
    """Acquisition n latches the trace (n + k) + i*(-n) for k = 0..N-1, so every
    point of a scan can be checked for being the RIGHT acquisition."""

    def __init__(self, cmd_port):
        self.cmd_port, self.pub_port = cmd_port, cmd_port + 1
        self._lock = threading.Lock()
        self._id, self._acquiring, self._trace = 0, False, None
        self._stop = threading.Event()
        self._ctx = zmq.Context.instance()
        acquire = {"group": "sweep", "trigger_verb": "acquire", "target_key": "acq_id",
                   "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                             "flag_key": "acquiring", "invert": True}}
        self.manifest = {"schema": 1, "module": "vna", "revision": 1, "parameters": [
            {"id": "s21", "label": "S21", "kind": "indicator", "type": "array",
             "unit": "", "dtype": "complex", "shape": ["freq"], "acquire": acquire,
             "dims": [{"name": "freq", "label": "Frequency", "unit": "GHz",
                       "length": N, "coord_verb": "get_frequencies",
                       "coord_key": "values_GHz"}],
             "read": {"verb": "get_trace", "key": "s21", "args": {"which": "sample"}}}]}

    def start(self):
        for fn in (self._serve, self._publish):
            threading.Thread(target=fn, daemon=True).start()
        time.sleep(0.15)
        return self

    def stop(self):
        self._stop.set()
        time.sleep(0.2)

    def _sweep(self, n):
        time.sleep(0.1)
        k = np.arange(N, dtype=float)
        with self._lock:
            self._trace = {"re": (n + k).tolist(), "im": [-float(n)] * N}
            self._acquiring = False

    def _handle(self, msg):
        cmd = msg.get("cmd")
        if cmd == "acquire":
            with self._lock:
                self._id += 1
                self._acquiring = True
                n = self._id
            threading.Thread(target=self._sweep, args=(n,), daemon=True).start()
            return {"ok": True, "acq_id": n}
        if cmd == "get_trace":
            assert msg.get("which") == "sample"        # the declared args arrive
            with self._lock:
                return {"ok": True, "acq_id": self._id, "s21": self._trace}
        if cmd == "get_frequencies":
            f = np.linspace(1e9, 2e9, N)
            return {"ok": True, "values": f.tolist(), "values_GHz": (f / 1e9).tolist()}
        if cmd == "status":
            return {"ok": True, "status": self._status()}
        if cmd == "describe":
            return {"ok": True, "describe": self.manifest}
        return {"ok": False, "error": f"unknown {cmd}"}

    def _status(self):
        with self._lock:
            return {"acq_id": self._id, "acquiring": self._acquiring}

    def _serve(self):
        rep = self._ctx.socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep.bind(f"tcp://127.0.0.1:{self.cmd_port}")
        poller = zmq.Poller(); poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(100):
                try:
                    reply = self._handle(rep.recv_json())
                except Exception as exc:
                    reply = {"ok": False, "error": str(exc)}
                rep.send_json(reply)
        rep.close(0)

    def _publish(self):
        pub = self._ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(f"tcp://127.0.0.1:{self.pub_port}")
        while not self._stop.is_set():
            pub.send_multipart([b"status", json.dumps(self._status()).encode()])
            time.sleep(0.03)
        pub.close(0)


def test_a_complex_trace_from_a_service_becomes_a_cube():
    svc = FakeVna(15970).start()
    inst = Instrument("vna", host="127.0.0.1", cmd_port=15970)
    try:
        time.sleep(0.2)
        state = {"x": 0.0}
        reg = Registry()
        reg.add(Settable("x", "X", "mm", (-10, 10),
                         set_fn=lambda v: state.__setitem__("x", v),
                         get_fn=lambda: state["x"]))
        register_manifest(reg, inst, inst.command("describe")["describe"], prefix=True)
        r = Recipe(name="t", axes=[{"type": "linear", "param": "x",
                                    "start": 0, "stop": 2, "num": 3}],
                   detectors=["vna.s21"])
        ds = run(r, reg, created_iso="t")

        assert dict(ds.sizes) == {"x": 3, "vna.freq": N}
        assert ds.coords["vna.freq"].attrs["units"] == "GHz"
        assert np.allclose(ds.coords["vna.freq"].values, np.linspace(1, 2, N))
        z = ds["vna.s21_real"].values + 1j * ds["vna.s21_imag"].values
        k = np.arange(N)
        for i in range(3):
            n = i + 1                                  # point i is acquisition i+1, not i
            assert np.allclose(z[i], (n + k) - 1j * n), f"point {i} read the wrong sweep"
    finally:
        inst.close()
        svc.stop()
