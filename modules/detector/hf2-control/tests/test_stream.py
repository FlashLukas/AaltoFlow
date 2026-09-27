"""The fly-scan STREAM: every reading the poll thread takes, time stamped.

scan-core's fly scan records the lock-in continuously while a stage moves,
then bins the samples by the stage's measured position. What it needs from
this module is tested here: the recorder (clears on start, drains on read,
bounded, JSON-safe), that the poll thread feeds it only while it runs, that
the declared delay is the filter's group delay with the APPLIED tau, and the
three verbs over the wire on non-default ports.
"""

import math
import time

import pytest

from hf2.config import Config
from hf2.lockin import STREAM_CHANNELS
from hf2.net.describe import build_manifest
from hf2.sim_system import build_sim_system
from hf2.stream import StreamRecorder

CMD_PORT = 15896
PUB_PORT = 15897


def test_the_recorder_clears_on_start_and_drains_on_read():
    rec = StreamRecorder(["a", "b"])
    rec.append(1.0, (1, 2))                        # not running: ignored
    assert rec.read()["t"] == []
    sid = rec.start()
    rec.append(1.0, (1, 2))
    rec.append(2.0, (3, float("nan")))
    c = rec.read()
    assert c["id"] == sid and c["t"] == [1.0, 2.0]
    assert c["values"] == {"a": [1.0, 3.0], "b": [2.0, None]}   # NaN -> null
    assert rec.read()["t"] == []                   # drained
    rec.append(3.0, (5, 6))
    assert rec.stop()["t"] == [3.0] and not rec.running
    rec.append(4.0, (7, 8))
    assert rec.start() == sid + 1 and rec.read()["t"] == []     # start clears


def test_the_recorder_is_bounded_and_says_so():
    rec = StreamRecorder(["a"], max_samples=3)
    rec.start()
    for i in range(5):
        rec.append(float(i), (i,))
    c = rec.read()
    assert c["t"] == [2.0, 3.0, 4.0] and c["overflow"] is True
    assert rec.read()["overflow"] is False


def test_the_reply_carries_now_and_the_delays():
    rec = StreamRecorder(["a"], delay_fn=lambda: {"a": 0.04})
    rec.start()
    c = rec.read()
    assert c["delay_s"] == {"a": 0.04}
    assert abs(c["now"] - time.time()) < 1.0


def test_the_poll_thread_feeds_the_stream_only_while_it_runs():
    li, _ = build_sim_system(seed=1)
    li.start(poll=False)
    try:
        li.poll_once()
        li.stream.start()
        t0 = time.time()
        for _ in range(3):
            li.poll_once()
        c = li.stream.stop()
        li.poll_once()
        assert len(c["t"]) == 3 and all(t >= t0 for t in c["t"])
        assert list(c["values"]) == list(STREAM_CHANNELS)
        # the stream IS the live reading: r = |x + iy|
        x, y, r = c["values"]["x1"][0], c["values"]["y1"][0], c["values"]["r1"][0]
        assert r == pytest.approx(math.hypot(x, y))
        assert li.stream.read()["t"] == []
    finally:
        li.shutdown()


def test_the_delay_is_order_times_the_applied_time_constant():
    cfg = Config()
    li, sim = build_sim_system(cfg, seed=1)
    li.start(poll=False)
    try:
        li.set_time_constant(1, 0.02)
        li.set_order(1, 3)
        d = li.stream_delays()
        tau = li.status().tc_s[0]                  # what the HARDWARE applied
        assert d["x1"] == d["r1"] == d["theta1"] == pytest.approx(3 * tau)
        assert d["aux1"] == 0.0
    finally:
        li.shutdown()


def test_every_scan_detector_declares_its_stream_channel():
    li, _ = build_sim_system(seed=1)
    m = build_manifest(li)
    streamed = {p["id"]: p["stream"] for p in m["parameters"] if "stream" in p}
    assert set(streamed) == set(STREAM_CHANNELS)
    assert all(s == {"group": "demod", "channel": pid} for pid, s in streamed.items())


def test_the_stream_verbs_over_the_wire():
    zmq = pytest.importorskip("zmq")
    from hf2.net.service import Hf2Service
    cfg = Config()
    cfg.hardware.poll_hz = 100.0
    li, _ = build_sim_system(cfg, seed=3)
    svc = Hf2Service(li, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                     status_hz=20.0)
    svc.start()
    ctx = zmq.Context.instance()
    req = ctx.socket(zmq.REQ)
    req.setsockopt(zmq.RCVTIMEO, 2000)
    req.setsockopt(zmq.LINGER, 0)
    req.connect(f"tcp://127.0.0.1:{CMD_PORT}")

    def cmd(verb):
        req.send_json({"cmd": verb})
        return req.recv_json()

    try:
        start = cmd("stream_start")
        assert start["ok"] and start["stream_id"] >= 1
        time.sleep(0.3)
        first = cmd("stream_read")["stream"]
        time.sleep(0.2)
        rest = cmd("stream_stop")["stream"]
        assert len(first["t"]) >= 10 and len(rest["t"]) >= 5
        assert first["t"][-1] < rest["t"][0]          # no overlap: a read drains
        assert set(first["values"]) == set(STREAM_CHANNELS)
        assert first["delay_s"]["x1"] > 0 and "now" in first
        assert cmd("stream_read")["stream"]["t"] == []   # stopped
    finally:
        req.close(0)
        svc.stop()
        time.sleep(0.2)


def test_the_poll_thread_keeps_its_configured_rate():
    """poll_hz = 50 must mean ~50 readings a second. It used to wait with
    Event.wait, which Windows rounds up to its 15.6 ms tick: ~32 Hz, and a
    fly scan got a third fewer samples per pixel than it was told."""
    cfg = Config()
    cfg.hardware.poll_hz = 50.0
    li, _ = build_sim_system(cfg, seed=2)
    li.start()
    try:
        li.stream.start()
        time.sleep(1.0)
        n = len(li.stream.stop()["t"])
    finally:
        li.shutdown()
    assert 44 <= n <= 52
