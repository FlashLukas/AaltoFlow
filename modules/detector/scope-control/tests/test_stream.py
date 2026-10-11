"""The fly-scan RECORD STREAM (scope.py "FLY SCANS", stream.py): one sample per
triggered record, stamped at the record's end with half a record as its
delay, the describe `stream` blocks with `rate_hz`, and the verbs over the
wire. Its own test ports (17636/17637): the module's others are 17640..17649."""

import time

import numpy as np
import pytest

from scope.config import Config
from scope.scope import STREAM_CHANNELS, STREAM_VALUES, _floor125
from scope.sim_system import build_sim_system
from scope.net.describe import build_manifest, _VALUES
from scope.net.service import ScopeService

CMD_PORT = 17636
PUB_PORT = 17637


@pytest.fixture
def scope():
    sc, sim = build_sim_system(Config(), seed=3)
    sc.start()
    yield sc, sim
    sc.shutdown()


def _collect(sc, seconds):
    t_end = time.monotonic() + seconds
    chunks = []
    while time.monotonic() < t_end:
        time.sleep(0.1)
        chunks.append(sc.stream_read())
    return chunks


def test_the_stream_channels_are_the_scalar_detectors():
    """A streamed channel IS a scan detector id: the same five numbers."""
    assert tuple(k for k, _, _ in _VALUES) == STREAM_VALUES
    assert "ch1_pk2pk" in STREAM_CHANNELS and "phase_21" in STREAM_CHANNELS


def test_every_record_gives_one_sample_with_its_numbers(scope):
    sc, sim = scope
    writes = len(sim.writes)
    sid = sc.stream_start()
    t0 = time.time()
    chunks = _collect(sc, 1.5)
    rest = sc.stream_stop()
    assert len(sim.writes) == writes, "streaming must not change the scope"
    rows = [c for c in chunks + [rest] if c["t"]]
    t = np.concatenate([c["t"] for c in rows])
    pp = np.concatenate([c["values"]["ch1_pk2pk"] for c in rows]).astype(float)
    # the sim: 5 ms/div x 14 div = 70 ms records + 50 ms transfer -> ~8 / s
    rate = (len(t) - 1) / (t[-1] - t[0])
    assert 4.0 < rate < 10.0, rate
    assert all(c["id"] == sid for c in rows)
    # the numbers of each RECORD (not an average): the sim's 1 V-peak sine
    assert np.nanmedian(pp) == pytest.approx(2.0, rel=0.05)
    ph = np.concatenate([c["values"]["phase_21"] for c in rows]).astype(float)
    assert np.nanmedian(ph) == pytest.approx(60.0, abs=3.0)
    # stamped at the record's END on the wall clock, start = end - record
    assert t0 - 0.2 < t[0] < time.time()
    c = rows[0]
    span = np.array(c["t_end"]) - np.array(c["t_start"])
    assert np.allclose(span, 0.07, rtol=0.05)
    assert np.allclose(c["t_end"], c["t"])
    # half a record late: scan-core files each record at its middle
    assert c["delay_s"]["ch1_pk2pk"] == pytest.approx(0.035, rel=0.05)
    assert all(not c["overflow"] for c in rows)
    # nothing more once stopped
    time.sleep(0.3)
    assert sc.stream_read()["t"] == []


def test_the_live_view_and_acquire_go_on_while_streaming(scope):
    sc, _ = scope
    sc.stream_start()
    sc.set_averages(2)
    n = sc.acquire()
    t_end = time.monotonic() + 10
    while time.monotonic() < t_end:
        st = sc.status()
        if st["acq_id"] == n and not st["acquiring"]:
            break
        time.sleep(0.02)
    assert st["sample"]["ch1"]["amplitude"] == pytest.approx(1.0, rel=0.05)
    assert st["stream_running"] is True
    assert len(sc.stream_stop()["t"]) >= 1
    assert sc.status()["stream_running"] is False


def test_missed_records_are_counted_not_hidden(scope):
    """A record the module never read (it was busy) leaves a long gap: the
    stream says so in `missed` instead of quietly having fewer samples."""
    sc, _ = scope
    sc.stream_start()
    rec = sc.stream
    # feed the gap rule directly: regular records 0.1 s apart, then a 0.5 s gap
    base = time.time()
    phys = {"ch1": np.zeros(16), "ch2": np.zeros(16)}
    t = np.linspace(0, 0.07, 16)
    sc._stream_last_end = None
    sc._stream_gaps.clear()
    rec.read()
    for end in (0.0, 0.1, 0.2, 0.3, 0.4, 0.9):
        sc._stream_record(t, phys, 0.07, base + 100 + end)
    chunk = rec.read()
    assert chunk["missed"] >= 3
    assert rec.read()["missed"] == 0            # reported once


def test_describe_declares_a_stream_with_its_record_rate(scope):
    sc, _ = scope
    m = build_manifest(sc)
    by_id = {p["id"]: p for p in m["parameters"]}
    for ch in ("ch1", "ch2"):
        for key in STREAM_VALUES:
            s = by_id[f"{ch}_{key}"]["stream"]
            assert s["group"] == "records" and s["channel"] == f"{ch}_{key}"
            # 1 / (70 ms record + 50 ms transfer), two significant digits
            assert s["rate_hz"] == pytest.approx(8.3, abs=0.05)
    assert by_id["phase_21"]["stream"]["channel"] == "phase_21"
    assert "stream" not in by_id["ch1"]            # a whole trace does not stream
    assert sc.status()["stream_rate_Hz"] == pytest.approx(8.3, abs=0.05)


def test_the_rate_follows_the_time_base_and_is_zero_when_stopped(scope):
    sc, _ = scope
    sc.set_tdiv(0.05)                  # 0.7 s records + 0.05 s
    t_end = time.monotonic() + 5
    while time.monotonic() < t_end and not sc.status()["settings_settled"]:
        time.sleep(0.02)
    assert sc.stream_rate_hz() == pytest.approx(1.3, abs=0.05)
    sc.set_trigger_mode("stop")
    t_end = time.monotonic() + 5
    while time.monotonic() < t_end and not sc.status()["settings_settled"]:
        time.sleep(0.02)
    assert sc.stream_rate_hz() == 0.0


def test_floor125():
    assert _floor125(0.93) == 0.5 and _floor125(3.0) == 2.0 and _floor125(12) == 10
    assert _floor125(0) == 0.0


def test_the_analog_discovery_streams_too():
    cfg = Config()
    cfg.sim.model = "ad"
    sc, _ = build_sim_system(cfg, seed=2)
    sc.start()
    try:
        sc.gen.set_output("w1", True)
        sc.stream_start()
        time.sleep(1.0)
        c = sc.stream_stop()
        assert len(c["t"]) >= 5
        # 1 ms/div x 10 div + 5 ms -> ~67 / s, two significant digits
        assert sc.stream_rate_hz() == pytest.approx(67, abs=1)
    finally:
        sc.shutdown()


# ---- over the wire ------------------------------------------------------------------

@pytest.fixture
def service():
    sc, _ = build_sim_system(Config(), seed=4)
    svc = ScopeService(sc, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                       status_hz=20.0)
    svc.start()
    import zmq
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, 3000)
    s.connect(f"tcp://127.0.0.1:{CMD_PORT}")

    def ask(**msg):
        s.send_json(msg)
        return s.recv_json()
    yield ask
    s.close(0)
    svc.stop()
    svc._cmd_t.join(timeout=2.0)
    svc._pub_t.join(timeout=2.0)


def test_stream_verbs_over_the_wire(service):
    ask = service
    r = ask(cmd="stream_start")
    assert r["ok"] and r["stream_id"] >= 1
    time.sleep(0.8)
    r = ask(cmd="stream_read")
    assert r["ok"]
    c = r["stream"]
    assert set(STREAM_CHANNELS) <= set(c["values"])
    assert len(c["t"]) >= 3 and len(c["t"]) == len(c["values"]["ch2_rms"])
    assert len(c["t_start"]) == len(c["t"]) == len(c["t_end"])
    assert "now" in c and c["missed"] == 0 and c["overflow"] is False
    assert c["delay_s"]["ch2_mean"] == pytest.approx(0.035, rel=0.05)
    r = ask(cmd="stream_stop")
    assert r["ok"] and "t" in r["stream"]
    d = ask(cmd="describe")["describe"]
    rates = {p["stream"]["rate_hz"] for p in d["parameters"] if "stream" in p}
    assert len(rates) == 1 and next(iter(rates)) > 0
