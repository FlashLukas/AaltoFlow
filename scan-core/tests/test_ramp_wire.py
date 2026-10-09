"""A fly scan over a SWEPT knob, over the wire: the `ramp` block in describe.

A fake generator service walks its frequency with suite_common/softramp.py
(the same code the dssg module uses) and declares

    "ramp": {"start": {"verb": "ramp_frequency", "args": {...}}, ...,
             "readback": {"stream": {"group": "ramp", ...}, "measured": false}}

on a control scanned in MHz and commanded in Hz (scale 1e6 -- the ramp's `to`
and rate are scaled like a set). A detector service (test_fly_wire's
FakeStreamer, sampled on its own clock) sees a resonance at a known
frequency. Everything scan-core knows comes from the two manifests.
"""

from __future__ import annotations

import bisect
import json
import threading
import time

import numpy as np
import pytest

zmq = pytest.importorskip("zmq")

from suite_common.softramp import SoftRamp                           # noqa: E402

from scan_core import Recipe, run                                    # noqa: E402
from scan_core.instrument import Instrument                          # noqa: E402
from scan_core.manifest import register_manifest                     # noqa: E402
from scan_core.registry import Registry                              # noqa: E402
from test_fly_wire import FakeStreamer                               # noqa: E402

F0_MHZ = 2450.0


def line(f_mhz):
    return float(np.exp(-((f_mhz - F0_MHZ) / 15.0) ** 2))


class FakeGenerator:
    """REP + PUB, a frequency walked by SoftRamp, its history kept so the
    detector can ask what the frequency was at any past moment."""

    def __init__(self, port, readback="stream"):
        self.port = port
        self.hist_t = [time.time()]
        self.hist_v = [2.0e9]
        self.lock = threading.Lock()
        self.walk = SoftRamp(self._set, self.freq, limits=(1e6, 6e9), dt_s=0.005,
                             channel="frequency")
        self.commands = []
        rb = ({"stream": {"group": "ramp", "channel": "frequency"}, "measured": False}
              if readback == "stream" else
              {"read_path": ["frequency_Hz"], "measured": True})
        self.manifest = {"schema": 1, "module": "gen", "revision": 1, "parameters": [
            {"id": "freq", "label": "Frequency", "kind": "control", "type": "float",
             "unit": "MHz", "scale": 1e6, "min": 1, "max": 6000,
             "read_path": ["frequency_Hz"],
             "set": {"verb": "set_frequency", "arg": "frequency_Hz"},
             "settle": {"policy": "echoes", "key": "frequency_Hz", "tol": 1.0},
             "ramp": {"kind": "software",
                      "start": {"verb": "ramp_frequency",
                                "args": {"to": "frequency_Hz", "rate": "rate_Hz_per_s"}},
                      "stop": {"verb": "ramp_stop"},
                      "rate": {"unit": "MHz/s", "min": 0.01, "max": 2000, "default": 50},
                      "readback": rb,
                      "done": {"key": "ramping", "id_key": "ramp_id"}}},
            {"id": "power", "label": "Power", "kind": "control", "type": "float",
             "unit": "dBm", "min": -20, "max": 10, "read_path": ["power"],
             "set": {"verb": "set_power", "arg": "power"},
             "settle": {"policy": "echoes", "key": "power"}},
        ]}
        self._stop = threading.Event()
        self._ctx = zmq.Context.instance()

    def _set(self, v):
        with self.lock:
            self.hist_t.append(time.time())
            self.hist_v.append(float(v))

    def freq(self, t=None):
        with self.lock:
            if t is None:
                return self.hist_v[-1]
            i = max(0, bisect.bisect_right(self.hist_t, t) - 1)
            return self.hist_v[i]

    def status(self):
        st = self.walk.status()
        return {"frequency_Hz": self.freq(), "power": 0.0,
                "ramping": st["ramping"], "ramp_id": st["ramp_id"]}

    def handle(self, msg):
        cmd = msg.get("cmd")
        self.commands.append(msg)
        if cmd == "describe":
            return {"ok": True, "describe": self.manifest}
        if cmd == "status":
            return {"ok": True, "status": self.status()}
        if cmd == "set_frequency":
            self.walk.stop()                 # a set takes the knob over
            self._set(float(msg["frequency_Hz"]))
            return {"ok": True}
        if cmd == "set_power":
            return {"ok": True}
        if cmd == "ramp_frequency":
            rid = self.walk.start(float(msg["frequency_Hz"]), float(msg["rate_Hz_per_s"]))
            return {"ok": True, "ramp_id": rid}
        if cmd == "ramp_stop":
            self.walk.stop()
            return {"ok": True}
        if cmd == "stream_start":
            return {"ok": True, "stream_id": self.walk.stream_start()}
        if cmd == "stream_read":
            return {"ok": True, "stream": self.walk.stream_read()}
        if cmd == "stream_stop":
            return {"ok": True, "stream": self.walk.stream_stop()}
        return {"ok": False, "error": f"unknown {cmd}"}

    def start(self):
        for fn in (self._serve, self._publish):
            threading.Thread(target=fn, daemon=True).start()
        time.sleep(0.15)
        return self

    def stop(self):
        self._stop.set()
        self.walk.stop()
        time.sleep(0.2)

    def _serve(self):
        rep = self._ctx.socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep.bind(f"tcp://127.0.0.1:{self.port}")
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(100):
                rep.send_json(self.handle(rep.recv_json()))
        rep.close(0)

    def _publish(self):
        pub = self._ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(f"tcp://127.0.0.1:{self.port + 1}")
        while not self._stop.is_set():
            pub.send_multipart([b"status", json.dumps(self.status()).encode()])
            time.sleep(0.02)
        pub.close(0)


DET_MANIFEST = {"schema": 1, "module": "det", "revision": 1, "parameters": [
    {"id": "a", "label": "A", "kind": "indicator", "type": "float", "unit": "V",
     "read_path": ["a"], "stream": {"group": "signal", "channel": "a"}}]}


def _rig(port, readback):
    gen = FakeGenerator(port, readback).start()
    det = FakeStreamer(port + 2, DET_MANIFEST,
                       sample=lambda t: {"a": line(gen.freq(t) / 1e6)},
                       status=lambda: {"a": line(gen.freq() / 1e6)}).start()
    insts = [Instrument("gen", host="127.0.0.1", cmd_port=port),
             Instrument("det", host="127.0.0.1", cmd_port=port + 2)]
    time.sleep(0.2)
    reg = Registry()
    for inst in insts:
        register_manifest(reg, inst, inst.command("describe")["describe"], prefix=True)
    return gen, det, insts, reg


def _close(gen, det, insts):
    for inst in insts:
        inst.close()
    gen.stop()
    det.stop()


def test_the_manifest_makes_a_ramp_spec():
    gen, det, insts, reg = _rig(16540, "stream")
    try:
        p = reg.get("gen.freq")
        assert p.ramp is not None and reg.get("gen.power").ramp is None
        assert p.ramp.rate_limits == (0.01, 2000) and p.ramp.rate_default == 50
        assert p.ramp.rate_unit == "MHz/s" and p.ramp.binned_by == "command"
        assert p.ramp.readback.stream.group == "gen.ramp"
        # start/done over the wire, scaled to Hz
        h = p.ramp.start(2010.0, 100.0)                 # 10 MHz at 100 MHz/s
        assert gen.commands[-1] == {"cmd": "ramp_frequency", "frequency_Hz": 2010e6,
                                    "rate_Hz_per_s": 100e6,
                                    "client": gen.commands[-1]["client"]}
        assert h["id"] == 1
        t0 = time.monotonic()
        while not p.ramp.done(h):
            assert time.monotonic() - t0 < 3
            time.sleep(0.02)
        assert gen.freq() == 2010e6
    finally:
        _close(gen, det, insts)


@pytest.mark.parametrize("readback, port", [("stream", 16550), ("status", 16560)])
def test_fly_over_a_swept_frequency(readback, port):
    gen, det, insts, reg = _rig(port, readback)
    try:
        r = Recipe(name="wire", axes=[
            {"type": "array", "param": "gen.power", "values": [0.0, 0.0]},
            {"type": "fly", "param": "gen.freq", "start": 2400, "stop": 2500,
             "num": 51, "speed": 100.0}], detectors=["det.a"], zigzag=True)
        assert r.validate(reg) == []
        ds = run(r, reg)
        f = ds["gen.freq"].values
        want = "command" if readback == "stream" else "measurement"
        assert ds["gen.freq"].attrs["fly_binned_by"] == want
        for row in ds["det.a"].values:                     # forward AND backward
            ok = np.isfinite(row)
            assert ok[2:-2].all()
            assert np.max(np.abs(row[ok] - np.array([line(x) for x in f[ok]]))) < 0.12
            assert abs(f[np.nanargmax(row)] - F0_MHZ) <= 2.0
        # the ramp verbs carried the scale: Hz on the wire
        ramps = [m for m in gen.commands if m["cmd"] == "ramp_frequency"]
        assert len(ramps) == 2 and {m["rate_Hz_per_s"] for m in ramps} == {100e6}
        assert not gen.walk.running
    finally:
        _close(gen, det, insts)
