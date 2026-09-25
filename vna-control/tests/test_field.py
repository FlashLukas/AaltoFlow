"""The field subscription, against FAKE magnet services that publish status
frames the way clMag and mag2d do. Scratch ports, so a running magnet is never
touched."""

import json
import math
import threading
import time

import pytest
import zmq

from vna import model
from vna.config import Config
from vna.field import RemoteField
from vna.sim_system import build_sim_system

PUB_PORT = 15741


class FakeMagnet:
    """Publishes {"measured_field_mT": ...} at 20 Hz, like clMag's PUB socket."""

    def __init__(self, port):
        self.field = 0.0
        self.running = True
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, args=(port,), daemon=True)
        self._t.start()

    def _run(self, port):
        pub = zmq.Context.instance().socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(f"tcp://127.0.0.1:{port}")
        while not self._stop.is_set():
            if self.running:
                frame = {"state": "STABLE", "setpoint_field_mT": self.field,
                         "measured_field_mT": self.field, "field_stable": True}
                pub.send_multipart([b"status", json.dumps(frame).encode()])
            time.sleep(0.05)
        pub.close(0)

    def close(self):
        self._stop.set()
        self._t.join(1.0)


@pytest.fixture
def magnet():
    m = FakeMagnet(PUB_PORT)
    yield m
    m.close()
    time.sleep(0.1)


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_remote_field_follows_goes_stale_and_recovers(magnet):
    rf = RemoteField("127.0.0.1", PUB_PORT, stale_s=0.4, fallback_mT=lambda: -1.0, kind="clMag")
    try:
        magnet.field = 42.5
        assert _wait(lambda: rf.read().ok and rf.read().field_mT == 42.5)
        magnet.field = -12.0
        assert _wait(lambda: rf.read().field_mT == -12.0)
        magnet.running = False                       # the magnet goes silent
        assert _wait(lambda: not rf.read().ok)
        r = rf.read()
        assert r.field_mT == -12.0 and "stale" in r.source   # last value, flagged
        magnet.running = True
        assert _wait(lambda: rf.read().ok)
    finally:
        rf.close()


def test_the_vna_resonance_follows_the_magnet(magnet):
    """The point of the module: move the magnet, the dip moves along Kittel."""
    cfg = Config()
    cfg.field.source = "clMag"                      # the default is mag2d now
    cfg.field.clMag_pub_port = PUB_PORT
    cfg.acquisition.continuous = False
    vna, _ = build_sim_system(cfg, realtime=False, seed=2)
    vna.start(run=False)
    try:
        for field in (25.0, 80.0):
            magnet.field = field
            assert _wait(lambda: vna._refresh_live() or vna.status().field_mT == field)
            n = vna.acquire()
            while vna.status().acquiring:
                vna.step()
            t = vna.get_trace("sample")
            assert t["acq_id"] == n and t["field_ok"] and t["field_mT"] == field
            assert t["dip_Hz"] == pytest.approx(model.kittel_Hz(field, cfg.sample), abs=2e6)
    finally:
        vna.shutdown()


# ---- the vector magnet (mag2d) ------------------------------------------------------

MAG2D_PORT = 15742


class FakeVectorMagnet:
    """Publishes the measured VECTOR the way the mag2d service's status does:
    {"measured_bx_mT", "measured_by_mT", ...} at 20 Hz."""

    def __init__(self, port):
        self.bx, self.by = 0.0, 0.0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, args=(port,), daemon=True)
        self._t.start()

    def _run(self, port):
        pub = zmq.Context.instance().socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(f"tcp://127.0.0.1:{port}")
        while not self._stop.is_set():
            frame = {"state": "STABLE", "measured_bx_mT": self.bx, "measured_by_mT": self.by,
                     "measured_field_mT": 999.0,     # NOT what the VNA must use
                     "field_stable": True}
            pub.send_multipart([b"status", json.dumps(frame).encode()])
            time.sleep(0.05)
        pub.close(0)

    def close(self):
        self._stop.set()
        self._t.join(1.0)


@pytest.fixture
def vector_magnet():
    m = FakeVectorMagnet(MAG2D_PORT)
    yield m
    m.close()
    time.sleep(0.1)


def test_mag2d_source_gives_hypot_and_atan2(vector_magnet):
    rf = RemoteField("127.0.0.1", MAG2D_PORT, stale_s=1.0, kind="mag2d")
    try:
        vector_magnet.bx, vector_magnet.by = 30.0, 40.0
        assert _wait(lambda: rf.read().ok and rf.read().field_mT == pytest.approx(50.0))
        r = rf.read()
        assert r.angle_deg == pytest.approx(53.13010235415598) and r.source == "mag2d"
        # a field pointing along -x is 50 mT at 180 deg, never "-50 mT"
        vector_magnet.bx, vector_magnet.by = -50.0, 0.0
        assert _wait(lambda: rf.read().angle_deg == pytest.approx(180.0))
        assert rf.read().field_mT == pytest.approx(50.0)
    finally:
        rf.close()


def test_mag2dcal_reads_the_same_frames_from_its_own_port(vector_magnet):
    """The parallel magnet module (calibration + freeze + stabilizer) publishes
    the SAME status keys as mag2d, so it shares the parser -- only the endpoint
    and the name differ. Two magnet modules must never share one port."""
    from vna.config import Config
    from vna.field import FIELD_SOURCES, make_field_source

    assert "mag2dcal" in FIELD_SOURCES
    cfg = Config()
    # the two vector magnets are separate services: separate default ports
    assert cfg.field.mag2dcal_pub_port != cfg.field.mag2d_pub_port
    cfg.field.source = "mag2dcal"
    cfg.field.mag2dcal_pub_port = MAG2D_PORT        # the fake publisher stands in for it
    src = make_field_source(cfg.field)
    try:
        vector_magnet.bx, vector_magnet.by = 0.0, 25.0
        assert _wait(lambda: src.read().ok and src.read().field_mT == pytest.approx(25.0))
        assert src.read().angle_deg == pytest.approx(90.0)
        assert src.read().source == "mag2dcal"      # which magnet it is, in the status line
    finally:
        src.close()


def test_the_default_source_is_mag2d_and_the_angle_reaches_the_sample(vector_magnet):
    """With an in-plane uniaxial anisotropy the line depends on the angle, so
    the dip itself proves the angle got into the physics, and the sample must
    carry it as metadata."""
    cfg = Config()
    assert cfg.field.source == "mag2d"
    cfg.field.mag2d_pub_port = MAG2D_PORT
    cfg.acquisition.continuous = False
    cfg.sample.hk_mT, cfg.sample.easy_axis_deg = 20.0, 0.0
    vna, _ = build_sim_system(cfg, realtime=False, seed=6)
    vna.start(run=False)
    try:
        dips = {}
        for angle in (0.0, 90.0):
            b = 60.0
            vector_magnet.bx = b * math.cos(math.radians(angle))
            vector_magnet.by = b * math.sin(math.radians(angle))
            assert _wait(lambda: vna._refresh_live() or (
                vna.status().field_ok and vna.status().angle_deg == pytest.approx(angle, abs=1e-9)))
            n = vna.acquire()
            while vna.status().acquiring:
                vna.step()
            t = vna.get_trace("sample")
            assert t["acq_id"] == n and t["field_ok"]
            assert t["field_mT"] == pytest.approx(b) and t["angle_deg"] == pytest.approx(angle, abs=1e-9)
            expect = model.kittel_Hz(b, cfg.sample, angle)
            assert t["dip_Hz"] == pytest.approx(expect, abs=3e6)
            assert t["f_res_model_Hz"] == pytest.approx(expect)
            dips[angle] = t["dip_Hz"]
        assert dips[0.0] - dips[90.0] > 200e6       # easy axis resonates well above hard
    finally:
        vna.shutdown()


def test_the_field_feeds_samples_in_real_mode_too(vector_magnet):
    """The subscription belongs to the brain, not the simulator: a real
    analyser's samples carry the magnet's field and angle."""
    from fake_visa import FakePna
    from vna.analyzer import Analyzer
    from vna.backends.pna import PnaVna
    cfg = Config()
    cfg.field.mag2d_pub_port = MAG2D_PORT
    cfg.acquisition.continuous = False
    cfg.sweep.points = 11
    vna = Analyzer(PnaVna(cfg, resource=FakePna(sweep_polls=0), sleep=lambda s: None), cfg)
    vna.start(run=False)
    try:
        vector_magnet.bx, vector_magnet.by = 0.0, -25.0
        assert _wait(lambda: vna._refresh_live() or vna.status().field_ok)
        n = vna.acquire()
        while vna.status().acquiring:
            vna.step()
        s = vna.status().sample
        assert s["acq_id"] == n and s["field_ok"] is True
        assert s["field_mT"] == pytest.approx(25.0) and s["angle_deg"] == pytest.approx(-90.0)
    finally:
        vna.shutdown()


def test_a_silent_mag2d_is_flagged_not_trusted():
    cfg = Config()
    cfg.field.mag2d_pub_port = 15743                # nothing publishes there
    cfg.acquisition.continuous = False
    vna, _ = build_sim_system(cfg, realtime=False, seed=1)
    vna.start(run=False)
    try:
        st = vna.status()
        assert st.field_ok is False and "mag2d not heard" in st.field_source
    finally:
        vna.shutdown()
