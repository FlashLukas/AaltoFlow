"""A fly scan OVER THE WIRE: streams declared in `describe`, read with verbs.

Two fake services in their own threads, speaking the suite contract:

  * a STAGE whose `move_to` travels at a set speed and which streams its
    position (group "position");
  * a DETECTOR whose value is a known function of where the stage is, which
    streams it (group "signal") with a declared DELAY -- and whose clock is
    deliberately 3 s WRONG, as a module on another PC could be.

Everything scan-core knows about them comes from their manifests. The test
checks the manifest wiring (streams shared per group, the settle timeout
override), and that the binned line is right despite the clock offset, which
only works if manifest.py puts the module's time stamps on this PC's clock.
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

zmq = pytest.importorskip("zmq")

from scan_core import Recipe, run                                    # noqa: E402
from scan_core.instrument import Instrument                          # noqa: E402
from scan_core.manifest import register_manifest                     # noqa: E402
from scan_core.registry import Registry                              # noqa: E402


class World:
    """The physics both fakes share: one stage position, moved over time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.p0 = self.p1 = 0.0
        self.t0 = self.t1 = time.time()
        self.speed = 100.0

    def move(self, target):
        with self.lock:
            here = self._pos(time.time())
            self.p0, self.p1 = here, float(target)
            self.t0 = time.time()
            self.t1 = self.t0 + abs(self.p1 - self.p0) / self.speed

    def _pos(self, t):
        if t >= self.t1 or self.t1 == self.t0:
            return self.p1
        return self.p0 + (self.p1 - self.p0) * (t - self.t0) / (self.t1 - self.t0)

    def pos(self, t=None):
        with self.lock:
            return self._pos(time.time() if t is None else t)

    def moving(self):
        with self.lock:
            return time.time() < self.t1


def signal(x):
    """What the detector sees at position x: a bump centred on 3."""
    return np.exp(-((x - 3.0) / 2.0) ** 2)


class FakeStreamer:
    """One service: REP commands, PUB status, a recorder thread."""

    def __init__(self, port, manifest, sample, status, handle=None,
                 clock_offset=0.0, rate_hz=400.0, delay_s=None):
        self.port = port
        self.manifest = manifest
        self._sample, self._status, self._handle = sample, status, handle
        self.clock_offset = clock_offset          # this "PC"'s clock error
        self.rate = rate_hz
        self.delay_s = delay_s or {}
        self._buf, self._rec = [], False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ctx = zmq.Context.instance()
        self.commands = []

    def now(self):
        return time.time() + self.clock_offset

    def start(self):
        for fn in (self._serve, self._publish, self._record):
            threading.Thread(target=fn, daemon=True).start()
        time.sleep(0.15)
        return self

    def stop(self):
        self._stop.set()
        time.sleep(0.2)

    def _record(self):
        while not self._stop.is_set():
            t_true = time.time()
            if self._rec:
                row = self._sample(t_true)
                with self._lock:
                    self._buf.append((t_true + self.clock_offset, row))
            time.sleep(1.0 / self.rate)

    def _drain(self):
        with self._lock:
            rows, self._buf = self._buf, []
        chans = list(rows[0][1]) if rows else []
        return {"t": [r[0] for r in rows],
                "values": {c: [r[1][c] for r in rows] for c in chans},
                "delay_s": {c: self.delay_s.get(c, 0.0) for c in chans},
                "overflow": False, "now": self.now()}

    def _serve(self):
        rep = self._ctx.socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)
        rep.bind(f"tcp://127.0.0.1:{self.port}")
        poller = zmq.Poller(); poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if not poller.poll(100):
                continue
            msg = rep.recv_json()
            self.commands.append(msg)
            cmd = msg.get("cmd")
            if cmd == "describe":
                rep.send_json({"ok": True, "describe": self.manifest})
            elif cmd == "status":
                rep.send_json({"ok": True, "status": self._status()})
            elif cmd == "stream_start":
                with self._lock:
                    self._buf, self._rec = [], True
                rep.send_json({"ok": True})
            elif cmd == "stream_read":
                rep.send_json({"ok": True, "stream": self._drain()})
            elif cmd == "stream_stop":
                self._rec = False
                rep.send_json({"ok": True, "stream": self._drain()})
            elif self._handle is not None:
                rep.send_json(self._handle(msg))
            else:
                rep.send_json({"ok": False, "error": f"unknown {cmd}"})
        rep.close(0)

    def _publish(self):
        pub = self._ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(f"tcp://127.0.0.1:{self.port + 1}")
        while not self._stop.is_set():
            pub.send_multipart([b"status", json.dumps(self._status()).encode()])
            time.sleep(0.02)
        pub.close(0)


STAGE_MANIFEST = {"schema": 1, "module": "stage", "revision": 1, "parameters": [
    {"id": "x", "label": "X", "kind": "control", "type": "float", "unit": "um",
     "min": -50, "max": 50, "read_path": ["x"],
     "set": {"verb": "move_to", "arg": "position"},
     "settle": {"policy": "flag_only", "key": "moving", "invert": True},
     "timeout_s": 2,               # far too short for a slow row: the override matters
     "stream": {"group": "position", "channel": "x"}},
    {"id": "speed", "label": "Speed", "kind": "control", "type": "float",
     "unit": "um/s", "min": 0.1, "max": 500, "read_path": ["speed"],
     "set": {"verb": "set_speed", "arg": "value"},
     "settle": {"policy": "echoes", "key": "speed"}},
]}

DET_MANIFEST = {"schema": 1, "module": "det", "revision": 1, "parameters": [
    {"id": "a", "label": "A", "kind": "indicator", "type": "float", "unit": "V",
     "read_path": ["a"], "stream": {"group": "signal", "channel": "a"}},
    {"id": "b", "label": "B", "kind": "indicator", "type": "float", "unit": "V",
     "read_path": ["b"], "stream": {"group": "signal", "channel": "b"}},
    {"id": "plain", "label": "Plain", "kind": "indicator", "type": "float",
     "read_path": ["a"]},
    # offered in mV, streamed (like status) in V: wire = display x scale
    {"id": "b_mV", "label": "B in mV", "kind": "indicator", "type": "float",
     "unit": "mV", "scale": 1e-3, "read_path": ["b"],
     "stream": {"group": "signal", "channel": "b"}},
]}

DELAY = 0.05                        # the detector's declared lag, s


@pytest.fixture
def rig():
    world = World()

    def stage_handle(msg):
        if msg["cmd"] == "move_to":
            world.move(msg["position"])
            return {"ok": True}
        if msg["cmd"] == "set_speed":
            world.speed = float(msg["value"])
            return {"ok": True}
        return {"ok": False, "error": "unknown"}

    stage = FakeStreamer(16500, STAGE_MANIFEST,
                         sample=lambda t: {"x": world.pos(t)},
                         status=lambda: {"x": world.pos(), "moving": world.moving(),
                                         "speed": world.speed},
                         handle=stage_handle).start()
    # The detector reports the signal where the stage was DELAY seconds ago,
    # stamped on a clock that is 3 s off.
    det = FakeStreamer(16502, DET_MANIFEST,
                       sample=lambda t: {"a": float(signal(world.pos(t - DELAY))),
                                         "b": 2.0},
                       status=lambda: {"a": float(signal(world.pos())), "b": 2.0},
                       clock_offset=3.0, delay_s={"a": DELAY, "b": DELAY}).start()
    insts = [Instrument("stage", host="127.0.0.1", cmd_port=16500),
             Instrument("det", host="127.0.0.1", cmd_port=16502)]
    time.sleep(0.2)
    reg = Registry()
    for inst in insts:
        register_manifest(reg, inst, inst.command("describe")["describe"], prefix=True)
    yield world, stage, det, reg
    for inst in insts:
        inst.close()
    stage.stop()
    det.stop()


def test_the_manifest_attaches_one_stream_per_group(rig):
    _, _, _, reg = rig
    a, b, plain = reg.get("det.a"), reg.get("det.b"), reg.get("det.plain")
    assert a.stream is b.stream and a.stream.group == "det.signal"
    assert (a.stream_channel, b.stream_channel) == ("a", "b")
    assert plain.stream is None
    assert reg.get("stage.x").stream.group == "stage.position"


def test_a_fly_scan_over_the_wire_bins_on_this_pcs_clock(rig):
    world, stage, det, reg = rig
    r = Recipe(name="wire", axes=[{"type": "fly", "param": "stage.x", "start": -6,
                                   "stop": 12, "num": 37, "speed": 12,
                                   "speed_param": "stage.speed"}],
               detectors=["det.a", "det.b", "det.b_mV"])
    assert r.validate(reg) == []
    ds = run(r, reg)            # a 1.5 s row with a 2 s settle timeout: needs the override
    x = ds["stage.x"].values
    assert np.allclose(x, np.linspace(-6, 12, 37))
    # right shape AND right place: the bump peaks at 3 um, not 3 s x 12 um/s away
    assert np.max(np.abs(ds["det.a"].values - signal(x))) < 0.03
    assert np.allclose(ds["det.b"].values, 2.0)
    # the descriptor's scale applies to the stream as to a one-value read
    assert np.allclose(ds["det.b_mV"].values, 2000.0)
    assert reg.get("det.b_mV").get() == pytest.approx(2000.0)
    assert np.all(ds["det.a_n"].values >= 3)
    # one start / stop per stream group, and the speed put back
    starts = [m for m in det.commands if m["cmd"] == "stream_start"]
    assert len(starts) == 1
    assert world.speed == 100.0


def test_a_plain_detector_is_refused_before_anything_moves(rig):
    world, stage, _, reg = rig
    r = Recipe(name="wire", axes=[{"type": "fly", "param": "stage.x", "start": 0,
                                   "stop": 5, "num": 6, "speed": 10}],
               detectors=["det.plain"])
    with pytest.raises(ValueError, match="cannot be recorded continuously"):
        run(r, reg)
    assert not [m for m in stage.commands if m["cmd"] == "move_to"]


def test_a_row_ends_on_the_measured_position_not_on_a_stale_moving_flag(rig):
    """The stage's settle rule is `flag_only(moving)`. Right after `move_to`
    the status can still say "not moving" (the fire-and-forget window,
    gotcha #2), so the blocking set may return before the stage has left --
    here it ALWAYS does, because this stage never reports moving at all. The
    row must still be flown to the end: the fly engine ends a row on the
    MEASURED position, not on the settle rule."""
    world, _, _, reg = rig
    world.moving = lambda: False
    r = Recipe(name="wire", axes=[{"type": "fly", "param": "stage.x", "start": -4,
                                   "stop": 8, "num": 25, "speed": 15,
                                   "speed_param": "stage.speed"}],
               detectors=["det.a"])
    ds = run(r, reg)
    assert np.all(ds["det.a_n"].values >= 3)
    assert np.max(np.abs(ds["det.a"].values - signal(ds["stage.x"].values))) < 0.03


def test_the_approach_to_the_first_row_is_waited_for_on_the_measured_position(rig):
    """From the rig, 2026-09-28: KIM's settle rule is flag_only(moving); the
    stale "not moving" frame right after move_to made the APPROACH return at
    once -- the fly speed was set while the stage was still on its way, the fly
    move turned it round, and the first 8 pixels of row 0 stayed empty. Here the
    stage never reports moving and starts 14 um away from the run-in."""
    world, _, _, reg = rig
    world.moving = lambda: False
    world.speed = 12.0
    world.move(10.0)
    time.sleep(1.0)                        # the stage is really at +10
    r = Recipe(name="wire", axes=[{"type": "fly", "param": "stage.x", "start": -4,
                                   "stop": 8, "num": 25, "speed": 15,
                                   "speed_param": "stage.speed"}],
               detectors=["det.a"])
    ds = run(r, reg)
    assert np.all(ds["det.a_n"].values >= 3), ds["det.a_n"].values
    assert world.speed == 12.0
