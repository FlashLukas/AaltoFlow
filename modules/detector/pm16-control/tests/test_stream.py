"""The fly-scan STREAM: every PM16 reading, time stamped, while a scan wants it.

scan-core's fly scan moves a stage continuously and bins the power readings by
the stage's measured position. What it needs from this module: each reading
recorded once, stamped at the MIDDLE of its ~60 ms averaging window (so its
delay is 0), an overrange reading as NaN rather than a clipped number, the
stream declared on `power`, and the verbs on the wire (ports 15722/15723).
"""

import math
import time

import pytest

from pm16.config import Config
from pm16.meter import STREAM_CHANNELS
from pm16.net.describe import build_manifest
from pm16.sim_system import build_sim_system

CMD, PUB = 15722, 15723


def test_each_reading_is_recorded_at_the_middle_of_its_window():
    meter, _ = build_sim_system(realtime=False, sample_period_s=0.05, seed=1)
    meter.start(poll=False)
    try:
        meter.stream.start()
        t0 = time.time()
        meter.poll_once()
        t1 = time.time()
        c = meter.stream.stop()
        assert len(c["t"]) == 1 and list(c["values"]) == list(STREAM_CHANNELS)
        # the stamp is the middle of a 50 ms reading, not its start or end
        assert t0 + 0.02 < c["t"][0] < t1 - 0.02
        assert c["values"]["power"][0] == pytest.approx(meter.status().power_W)
        assert c["delay_s"] == {"power": 0.0}
    finally:
        meter.shutdown()


def test_an_overrange_reading_is_streamed_as_nan():
    meter, sim = build_sim_system(realtime=False, seed=1)
    meter.start(poll=False)
    try:
        meter.set_auto_range(False)
        meter.set_range(1e-4)                 # 0.17 mW range vs 1.2 mW of light
        meter.stream.start()
        meter.poll_once()
        c = meter.stream.stop()
        assert c["values"]["power"] == [None]  # NaN -> null on the wire
    finally:
        meter.shutdown()


def test_the_power_detector_declares_its_stream():
    meter, _ = build_sim_system(realtime=False)
    params = {p["id"]: p for p in build_manifest(meter)["parameters"]}
    assert params["power"]["stream"] == {"group": "power", "channel": "power"}
    assert params["power"]["scale"] == 1e-3     # the stream is W, the scan records mW
    assert "stream" not in params["power_std"]


def test_the_poll_loop_runs_as_fast_as_the_meter_allows():
    """60 ms readings -> ~16 a second. It used to wait with Event.wait between
    readings, which on Windows costs a whole 15.6 ms tick (~13 a second)."""
    meter, _ = build_sim_system(realtime=True, seed=1)
    meter.start()
    try:
        meter.stream.start()
        time.sleep(1.0)
        n = len(meter.stream.stop()["t"])
    finally:
        meter.shutdown()
    assert 14 <= n <= 17


def test_the_stream_verbs_over_the_wire():
    zmq = pytest.importorskip("zmq")
    from pm16.net.service import Pm16Service
    meter, _ = build_sim_system(realtime=False, sample_period_s=0.01, seed=3)
    svc = Pm16Service(meter, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20.0)
    svc.start()
    req = zmq.Context.instance().socket(zmq.REQ)
    req.setsockopt(zmq.RCVTIMEO, 3000)
    req.setsockopt(zmq.LINGER, 0)
    req.connect(f"tcp://127.0.0.1:{CMD}")

    def cmd(verb):
        req.send_json({"cmd": verb})
        return req.recv_json()

    try:
        assert cmd("stream_start")["stream_id"] >= 1
        time.sleep(0.3)
        first = cmd("stream_read")["stream"]
        time.sleep(0.2)
        rest = cmd("stream_stop")["stream"]
        assert len(first["t"]) >= 5 and len(rest["t"]) >= 3
        assert first["t"][-1] < rest["t"][0]
        assert all(math.isfinite(v) and v > 0 for v in first["values"]["power"])
        assert "now" in first
        assert cmd("stream_read")["stream"]["t"] == []
    finally:
        req.close(0)
        svc.stop()
        time.sleep(0.2)
