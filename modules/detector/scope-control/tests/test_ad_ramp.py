"""Sweeps of the Analog Discovery's W1/W2 (fly scans over any knob, Lukas
2026-10-10): afg-control's ramps, in the copied generator brain, on the
simulated AD. In the scope's service the sweep's verbs, status keys and
record are the generator's (gen_ramp_start, gen_ramping, gen_stream_read).
Test ports 17646/17647."""

import time

import pytest

from scope.config import Config
from scope.sim_system import build_sim_system
from scope.net.describe import build_manifest
from scope.net.service import ScopeService
from scope.net.client import ScopeClient

CMD, PUB = 17646, 17647


def wait(fn, pred, timeout=6.0):
    t_end = time.monotonic() + timeout
    s = fn()
    while time.monotonic() < t_end:
        s = fn()
        if pred(s):
            return s
        time.sleep(0.02)
    raise AssertionError(f"not reached; last {s}")


@pytest.fixture
def ad():
    cfg = Config()
    cfg.sim.model = "ad"
    scope, sim = build_sim_system(cfg, seed=4)
    scope.start()
    yield scope, sim
    scope.shutdown()


def test_w1_sweep_reaches_target_and_w2_follows(ad):
    scope, sim = ad
    g = scope.gen
    g.set_output("w1", True)
    g.set_follow(True, 90.0, True)
    wait(scope.status, lambda s: s["w1_settled"] and s["w2_settled"] and s["gen_follow"])
    with pytest.raises(ValueError, match="follows"):
        g.ramp_start("w2", "frequency", 2000.0, 100.0)
    g.stream_start()
    rid = g.ramp_start("w1", "frequency", 1200.0, 1000.0)      # 1000 -> 1200 Hz, 0.2 s
    st = wait(scope.status, lambda s: s["gen_ramp_id"] >= rid and not s["gen_ramping"])
    assert st["w1_frequency_Hz"] == 1200.0
    assert sim.gen.ch[1]["frequency_Hz"] == 1200.0             # followed
    assert sim.gen.ch[1]["output"] is False                    # never switched on
    rec = g.stream_stop()
    v = rec["values"]["w1_frequency"]
    assert v[-1] == 1200.0 and len(v) == len(rec["t_ch"]["w1_frequency"]) >= 3


def test_describe_namespaces_the_ramp_blocks(ad):
    scope, sim = ad
    byid = {p["id"]: p for p in build_manifest(scope)["parameters"]}
    r = byid["w1_amplitude"]["ramp"]
    assert r["start"]["verb"] == "gen_ramp_start"
    assert r["start"]["extra"] == {"channel": "w1", "knob": "amplitude"}
    assert r["stop"]["verb"] == "gen_ramp_stop"
    assert r["done"] == {"key": "gen_ramping", "id_key": "gen_ramp_id"}
    rb = r["readback"]
    assert rb["measured"] is False and rb["stream"]["channel"] == "w1_amplitude"
    assert rb["stream"]["start_verb"] == "gen_stream_start"
    assert rb["stream"]["read_verb"] == "gen_stream_read"
    assert "ramp" in byid["w2_phase"] and "gen_ramp_stop" in byid


def test_a_sweep_over_the_wire_as_check_modules_runs_it():
    """What tools/check_modules.py --live does for a declared ramp, on the AD
    simulator (its default scratch service is the Siglent sim): start the
    record, a short sweep, wait for its number, read the record, stop."""
    cfg = Config()
    cfg.sim.model = "ad"
    scope, sim = build_sim_system(cfg, seed=5)
    svc = ScopeService(scope, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20.0)
    svc.start()
    cli = ScopeClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    try:
        cli.start()
        byid = {p["id"]: p for p in cli.describe()["parameters"]}
        for pid in ("w1_frequency", "w1_amplitude", "w2_offset", "w2_phase"):
            d = byid[pid]
            r = d["ramp"]
            here = cli._cmd({"cmd": "status"})["status"][d["read_path"][0]]
            rate = r["rate"]["default"]
            assert cli._cmd({"cmd": r["readback"]["stream"]["start_verb"]})["ok"]
            rep = cli._cmd({"cmd": r["start"]["verb"], "to": here + rate * 0.3,
                            "rate": rate, **r["start"]["extra"]})
            assert rep["ok"], rep
            wait(lambda: cli._cmd({"cmd": "status"})["status"],
                 lambda s: s[r["done"]["id_key"]] >= rep["ramp_id"]
                 and not s[r["done"]["key"]])
            chunk = cli._cmd({"cmd": r["readback"]["stream"]["stop_verb"]})["stream"]
            assert len(chunk["values"][r["readback"]["stream"]["channel"]]) >= 2, pid
            assert cli._cmd({"cmd": r["stop"]["verb"]})["ok"]
        # the remote stand-in the Generator tab uses
        assert cli.gen.ramp_start("w1", "offset", 0.2, 1.0)["ok"]
        wait(lambda: cli._cmd({"cmd": "status"})["status"], lambda s: s["gen_ramping"])
        assert cli.gen.ramp_stop()["stopped"] is True
        assert cli.gen.cfg.hardware.ramp_dt_s > 0
    finally:
        cli.shutdown()
        svc.stop()
        svc._cmd_t.join(timeout=2.0)
        svc._pub_t.join(timeout=2.0)
