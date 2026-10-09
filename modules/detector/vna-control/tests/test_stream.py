"""The fly-scan STREAM (2026-10-09): every sweep a sample, whole traces and
single frequency points, each with honest time stamps.

Lukas: "streaming on vna both the complete trace or individual frequency
points". What a fly scan needs from the module, and what is checked here:
  * a whole trace per sweep, stamped at the sweep's MIDDLE (t_start + T/2);
  * point channels, point i stamped at t_start + (i + 0.5) T / n -- the
    moment that point was measured, not the middle;
  * u and ln refused (with the reason, in `errors`) exactly as get_trace
    refuses them; a sweep change mid-stream makes the stream fail;
  * the sweeps run back to back while streaming, also with continuous off;
  * the simulator computes a sweep under a MOVING field segment by segment;
  * the wire: verbs, describe (stream blocks, point_<k>), get_point.
Non-default ports for the network part (17730/17731).
"""

import math
import time

import numpy as np
import pytest

from vna import model
from vna.analyzer import STREAM_ABANDON_S, Analyzer, parse_points
from vna.backends.sim import SimulatedVna
from vna.config import Config
from vna.field import FieldReading
from vna.net.client import VnaClient
from vna.net.describe import build_manifest
from vna.net.service import VnaService
from vna.sim_system import build_sim_system

CMD_PORT = 17730
PUB_PORT = 17731


@pytest.fixture
def vna():
    cfg = Config()
    cfg.field.source = "manual"
    cfg.acquisition.continuous = False
    cfg.sweep.points = 201
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz = 1e9, 5e9
    cfg.stream.points_Hz = "2.0e9, 3.01e9, 9e9"      # the last one is outside the sweep
    events = []
    v, _sim = build_sim_system(cfg, realtime=False, seed=3)
    v._on_event = lambda lvl, msg: events.append((lvl, msg))
    v.start(run=False)
    v.events = events
    yield v
    v.shutdown()


def test_points_are_parsed_and_snapped_to_the_grid(vna):
    assert parse_points("1.5e9; 2e9  3e9,") == [1.5e9, 2e9, 3e9]
    with pytest.raises(ValueError):
        parse_points("1.5e9, two")
    pts = vna.stream_points()
    grid = vna.frequencies()
    assert [p["channel"] for p in pts] == ["p1", "p2"]          # 9 GHz is not swept
    assert pts[0]["freq_Hz"] == pytest.approx(2.0e9)           # on the grid exactly
    assert pts[1]["freq_Hz"] == grid[pts[1]["index"]]
    assert abs(pts[1]["freq_Hz"] - 3.01e9) <= (grid[1] - grid[0]) / 2
    out = vna.set_stream_points([1.234e9])
    assert [p["channel"] for p in out] == ["p1"] and vna.cfg.stream.points_Hz == "1234000000"


def test_every_sweep_is_one_sample_with_its_points(vna):
    vna.set_manual_field(80.0)
    sid = vna.stream_start()
    assert vna.status().streaming
    for _ in range(3):
        assert vna.step()             # sweeps although `continuous` is off
    c = vna.stream_read()
    assert c["id"] == sid and c["values"]["s"].shape == (3, 201)
    assert np.allclose(c["t"], c["t_start"] + 0.5 * (c["t_end"] - c["t_start"]))
    for p in vna.stream_points():
        i = p["index"]
        assert np.allclose(c["values"][p["channel"]], c["values"]["s"][:, i])
        assert np.allclose(c["t_ch"][p["channel"]],
                           c["t_start"] + (i + 0.5) / 201 * (c["t_end"] - c["t_start"]))
    assert c["settings"]["points"] == 201 and c["settings"]["channels"] == {
        p["channel"]: p["index"] for p in vna.stream_points()}
    assert all(v == 0.0 for v in c["delay_s"].values())
    # u and ln: refused with the reason, not silently missing
    assert "u" not in c["values"] and "needs a reference" in c["errors"]["u"]
    assert "needs a reference" in c["errors"]["ln"]
    # nothing more until the next sweep; stop ends it
    assert len(vna.stream_read()["t"]) == 0
    vna.step()
    rest = vna.stream_stop()
    assert len(rest["t"]) == 1 and not vna.status().streaming
    assert vna.step() is False        # continuous off and no stream: idle again


def test_u_and_ln_stream_against_the_reference_and_refuse_a_new_one(vna):
    vna.set_manual_field(250.0)
    vna.take_reference()
    while vna.status().acquiring:
        vna.step()
    ref = vna.get_trace("reference")["s"]
    vna.set_manual_field(80.0)
    vna.stream_start()
    vna.step(); vna.step()
    c = vna.stream_read()
    assert c["errors"] == {}
    assert np.allclose(c["values"]["u"], (c["values"]["s"] - ref) / ref)
    assert np.allclose(c["values"]["ln"], np.log(c["values"]["s"] / ref))
    # a new reference during the stream: u would mix two references -> refused
    vna.take_reference()
    while vna.status().acquiring:
        vna.step()
    vna.step()
    c = vna.stream_read()
    assert "reference changed while streaming" in c["errors"]["u"]
    vna.stream_stop()


def test_a_sweep_change_mid_stream_makes_the_stream_fail(vna):
    vna.stream_start()
    vna.step()
    vna.set_points(301)
    vna.step()
    with pytest.raises(ValueError, match="sweep changed while streaming"):
        vna.stream_read()
    with pytest.raises(ValueError):
        vna.stream_stop()             # still the reason; and the stream is gone
    assert not vna.status().streaming
    # IFBW / power may change: the trace still means the same thing
    vna.stream_start()
    vna.step()
    vna.set_ifbw(1e3)
    vna.step()
    assert len(vna.stream_read()["t"]) == 2
    vna.stream_stop()


def test_an_abandoned_stream_is_dropped():
    now = [0.0]
    cfg = Config()
    cfg.field.source = "manual"
    cfg.acquisition.continuous = False
    cfg.sweep.points = 101
    v = Analyzer(SimulatedVna(cfg, seed=1, time_scale=0.0), cfg, clock=lambda: now[0])
    v.start(run=False)
    try:
        v.stream_start()
        assert v.step()
        now[0] += STREAM_ABANDON_S + 1
        assert v.step() is False and not v.status().streaming
    finally:
        v.shutdown()


def test_sweeps_take_their_time_and_follow_each_other():
    """Real time: points x dwell / IFBW per sweep, back to back."""
    cfg = Config()
    cfg.field.source = "manual"
    cfg.acquisition.continuous = False
    cfg.sweep.points, cfg.sweep.ifbw_Hz = 101, 2000.0
    cfg.line.point_dwell_ifbw = 1.0                   # 101 / 2000 = 50 ms a sweep
    v, sim = build_sim_system(cfg, realtime=True, seed=2)
    v.start()
    try:
        v.stream_start()
        time.sleep(0.6)
        c = v.stream_stop()
    finally:
        v.shutdown()
    T = model.sweep_time_s(101, 2000.0, 1.0)
    assert T == pytest.approx(0.0505)
    n = len(c["t"])
    assert 6 <= n <= 13, n
    assert np.allclose(c["t_end"] - c["t_start"], T)
    assert np.all(np.diff(c["t_start"]) >= T - 1e-3)   # one after the other, no overlap
    assert np.all(np.diff(c["t_start"]) < 3 * T)       # ... and back to back


def test_the_simulator_sees_a_moving_field_point_by_point():
    cfg = Config()
    cfg.line.noise = 0.0
    sim = SimulatedVna(cfg, seed=0, time_scale=0.0)
    sim.open()
    f = np.linspace(1e9, 6e9, 640)
    sim.start_sweep(f, 10e3, -10.0, "S21", FieldReading(40.0, True, "manual", 0.0))
    z = sim.finish_sweep(field_end=FieldReading(160.0, True, "manual", 0.0))
    seg = len(f) // SimulatedVna.FIELD_SEGMENTS                   # 10 points a segment
    first = model.s_clean(f[:seg], 40.0 + 120.0 * 0.5 / 64, cfg.sample, cfg.line)
    last = model.s_clean(f[-seg:], 40.0 + 120.0 * 63.5 / 64, cfg.sample, cfg.line)
    assert np.allclose(z[0][:seg], first) and np.allclose(z[0][-seg:], last)
    # a steady field: the whole sweep at that field, as before
    sim.start_sweep(f, 10e3, -10.0, "S21", FieldReading(70.0, True, "manual", 0.0))
    z2, _ = sim.finish_sweep(field_end=FieldReading(70.0, True, "manual", 0.0))
    assert np.allclose(z2, model.s_clean(f, 70.0, cfg.sample, cfg.line))


# ───────────────────────────────── the wire ──────────────────────────────────

@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.field.source = "manual"
    cfg.acquisition.continuous = False
    cfg.sweep.points = 201
    cfg.stream.points_Hz = "2e9, 4e9"
    v, _sim = build_sim_system(cfg, realtime=False, seed=5)
    svc = VnaService(v, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, status_hz=20.0)
    svc.start()
    cli = VnaClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=3000)
    time.sleep(0.3)
    yield svc, cli
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def test_describe_declares_the_streams_and_the_points(service_and_client):
    svc, cli = service_and_client
    m = {d["id"]: d for d in cli.describe()["parameters"]}
    for pid, ch in (("s", "s"), ("u", "u"), ("ln_ratio", "ln"),
                    ("point_1", "p1"), ("point_2", "p2")):
        assert m[pid]["stream"] == {"group": "trace", "channel": ch}
    assert m["point_1"]["dtype"] == "complex" and "dims" not in m["point_1"]
    assert m["point_1"]["label"].endswith("GHz") and "2 GHz" in m["point_1"]["label"]
    assert m["point_2"]["read"]["verb"] == "get_point"
    rev = cli.describe()["revision"]
    cli.set_stream_points([3e9])
    m2 = {d["id"]: d for d in cli.describe()["parameters"]}
    assert "point_2" not in m2 and "3 GHz" in m2["point_1"]["label"]
    assert cli.describe()["revision"] != rev


def test_the_stream_verbs_over_the_wire(service_and_client):
    svc, cli = service_and_client
    cli.stream_start()
    time.sleep(0.4)
    c = cli.stream_read()
    c2 = cli.stream_stop()
    n = len(c["t"]) + len(c2["t"])
    assert n >= 3
    assert len(c["values"]["s"]["re"]) == len(c["t"])
    assert len(c["values"]["s"]["re"][0]) == 201                # one trace a sample
    assert len(c["values"]["p1"]["re"]) == len(c["t_ch"]["p1"]) == len(c["t"])
    assert "needs a reference" in c["errors"]["u"]
    assert isinstance(c["now"], float) and c["settings"]["points"] == 201


def test_get_point_is_the_sample_at_that_frequency(service_and_client):
    svc, cli = service_and_client
    t = cli.acquire_blocking()
    i = int(np.argmin(np.abs(t["freqs_Hz"] - 2e9)))
    z = cli.get_point("p1")
    assert z == pytest.approx(complex(t["s"][i]))
    with pytest.raises(ValueError, match="no stream point"):
        cli.get_point("p9")
