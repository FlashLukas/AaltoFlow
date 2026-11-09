"""The fly-scan STREAM: the focus position recorded continuously.

scan-core's fly scan moves a knob slowly without stopping and bins a detector
by where it measurably was. What it needs from kim001: a sampler that records
the focus axis, time stamped, in the same micrometres the `position_z` control
reads -- including DURING a move -- and the three verbs over the wire
(non-default ports 19442/19443). Rows keep kim's [x, y, z] channels; x and y
are the constant 0 of the axes a KIM001 does not have, and only z is declared
in describe.
"""

import time

import pytest

from kim001.config import Config
from kim001.kim001 import STREAM_CHANNELS
from kim001.net.describe import build_manifest
from kim001.sim_system import build_sim_system

CMD, PUB = 19442, 19443
Z = 2


def _brain():
    brain, backend = build_sim_system(Config())     # the plain 20 nm config step
    brain.start()
    return brain, backend


def test_the_stream_follows_a_move():
    brain, _ = _brain()
    try:
        brain.set_velocity_um(Z, 20.0)             # 20 um/s = 1000 steps/s
        brain.stream_start(rate_hz=100)
        brain.move_to_um(Z, 10.0)
        time.sleep(0.8)
        c = brain.stream_stop()
        z = c["values"]["z"]
        assert list(c["values"]) == list(STREAM_CHANNELS)
        assert len(c["t"]) >= 50                   # ~100 Hz for 0.8 s
        assert z[0] < 1.0 and z[-1] == pytest.approx(10.0, abs=0.05)
        # it MOVED through the middle, not in one jump
        assert sum(1 for v in z if 2.0 < v < 8.0) >= 10
        assert all(b >= a for a, b in zip(z, z[1:]))   # monotonic on the way up
        assert all(t2 > t1 for t1, t2 in zip(c["t"], c["t"][1:]))
        assert set(c["values"]["x"]) == {0.0} and set(c["values"]["y"]) == {0.0}
    finally:
        brain.shutdown()


def test_the_stream_reads_what_status_reads():
    brain, _ = _brain()
    try:
        brain.move_to_um(Z, -3.0)
        time.sleep(0.6)
        brain.stream_start(rate_hz=50)
        time.sleep(0.1)
        c = brain.stream_stop()
        assert c["values"]["z"][-1] == pytest.approx(brain.status().position_um[Z])
    finally:
        brain.shutdown()


def test_stop_ends_the_sampler_and_shutdown_stops_it_too():
    brain, _ = _brain()
    brain.stream_start()
    th = brain._stream_thread
    brain.stream_stop()
    assert not th.is_alive() and not brain.stream.running
    brain.stream_start()
    th = brain._stream_thread
    brain.shutdown()
    assert not th.is_alive()


def test_only_the_focus_position_declares_a_stream():
    brain, _ = _brain()
    try:
        m = build_manifest(brain)
        streams = {p["id"]: p.get("stream") for p in m["parameters"]
                   if p["id"].startswith("position_")}
        assert streams == {"position_z": {"group": "position", "channel": "z"}}
    finally:
        brain.shutdown()


def test_the_stream_verbs_over_the_wire():
    zmq = pytest.importorskip("zmq")
    from kim001.net.service import Kim001Service
    brain, _ = build_sim_system(Config())
    svc = Kim001Service(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    req = zmq.Context.instance().socket(zmq.REQ)
    req.setsockopt(zmq.RCVTIMEO, 3000)
    req.setsockopt(zmq.LINGER, 0)
    req.connect(f"tcp://127.0.0.1:{CMD}")

    def cmd(verb, **kw):
        req.send_json({"cmd": verb, **kw})
        return req.recv_json()

    try:
        assert cmd("stream_start")["stream_id"] >= 1
        time.sleep(0.3)
        first = cmd("stream_read")["stream"]
        time.sleep(0.2)
        rest = cmd("stream_stop")["stream"]
        assert len(first["t"]) >= 8 and len(rest["t"]) >= 4    # 50 Hz default
        assert first["t"][-1] < rest["t"][0]
        assert first["delay_s"] == {"x": 0.0, "y": 0.0, "z": 0.0}
        assert "now" in first
        assert cmd("stream_read")["stream"]["t"] == []
    finally:
        req.close(0)
        svc.stop()
        time.sleep(0.1)
