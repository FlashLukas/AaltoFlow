"""The level SWEEPS (ramp_voltage / ramp_current): software ramps for fly scans.

softramp.py walks the level of the active source function at the asked pace
(one level write per step); the poll thread records every reading with its
time, so a fly scan bins by the MEASURED value (the source readback). Real
time, real threads, the simulated 2450 on a 1 kohm resistor. Checked: the pace
and the end (voltage and current), the stream, ramp_stop, a set taking over,
output off and a function change stopping it, compliance stopping it, a sweep
never switching the output on, clamping, describe and the wire.
"""

import time

import pytest

from k2450.config import Config
from k2450.sim_system import build_sim_system

# This module's own test-port block is 17040..17059 (test_net 17040/1, 17044/5,
# 17059); the sweep's wire test takes 17048/9.
WIRE_CMD, WIRE_PUB = 17048, 17049


def _make(cfg=None):
    cfg = cfg or Config()
    cfg.hardware.ramp_dt_s = 0.02
    # The sim's 2450 is FOUND (adopted at start) with its own power-up limits;
    # these only apply to the sim's remembered state below. A 1 kohm sample
    # at up to 5 V needs 5 mA of compliance, so the tests set 10 mA.
    smu, sim = build_sim_system(cfg, realtime=False, seed=1)
    smu.events = []
    smu._on_event = lambda lvl, msg: smu.events.append((lvl, msg))
    smu.start()
    smu.set_current_limit(10e-3)
    smu.set_voltage_limit(20.0)
    return smu, sim


@pytest.fixture
def smu():
    s, _ = _make()
    yield s
    s.shutdown()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _levels(sim, fn="voltage"):
    return [w[2] for w in sim.writes if w[0] == "level" and w[1] == fn]


def test_voltage_sweep_walks_at_the_pace_and_ends_on_the_target(smu):
    smu.set_output(True)
    sim = smu.backend
    sim.writes.clear()
    t0 = time.monotonic()
    rid = smu.ramp_voltage(2.0, 4.0)                   # 2 V at 4 V/s = 0.5 s
    assert rid == 1
    st = smu.status()
    assert st.ramping and st.ramp_id == 1 and st.sweep_function == "voltage"
    assert st.sweep_target == 2.0 and st.sweep_rate == 4.0
    assert _wait(lambda: not smu.status().ramping)
    assert 0.4 < time.monotonic() - t0 < 2.0
    st = smu.status()
    assert st.source_voltage_set_V == 2.0 and st.output
    vals = _levels(sim)
    assert len(vals) >= 10 and vals == sorted(vals) and vals[-1] == 2.0
    assert _wait(lambda: smu.status().settled)
    assert sum("voltage level =" in m for _, m in smu.events) == 0   # quiet steps


def test_current_sweep_in_current_mode(smu):
    smu.set_source_function("current")
    smu.set_output(True)
    rid = smu.ramp_current(1e-3, 4e-3)                 # 1 mA at 4 mA/s
    assert rid == 1 and smu.status().sweep_function == "current"
    assert _wait(lambda: not smu.status().ramping)
    st = smu.status()
    assert st.source_current_set_A == pytest.approx(1e-3, abs=1e-15)
    assert st.source_current_set_uA == pytest.approx(1000.0)
    with pytest.raises(ValueError):
        smu.ramp_voltage(1.0, 1.0)                     # not sourcing voltage


def test_the_stream_records_the_readback_with_its_time(smu):
    smu.set_output(True)
    assert smu.stream_start() == 1
    smu.ramp_voltage(1.0, 2.0)
    assert _wait(lambda: not smu.status().ramping)
    time.sleep(0.2)
    c = smu.stream_stop()
    t, v = c["t"], c["values"]["voltage"]
    assert len(t) == len(v) == len(c["values"]["current_uA"]) >= 10
    assert t == sorted(t) and "now" in c and c["delay_s"]["voltage"] == 0.0
    assert v[0] < 0.4 and v[-1] == pytest.approx(1.0, abs=0.01)
    # 1 kohm: the current follows in uA
    assert c["values"]["current_uA"][-1] == pytest.approx(1000.0, rel=0.02)


def test_ramp_stop_and_a_set_take_over(smu):
    smu.set_output(True)
    smu.ramp_voltage(5.0, 2.0)
    time.sleep(0.4)
    assert smu.ramp_stop() is True
    here = smu.status().source_voltage_set_V
    assert 0.4 < here < 1.5
    time.sleep(0.2)
    assert smu.status().source_voltage_set_V == here and not smu.status().ramping
    assert smu.ramp_stop() is False
    smu.ramp_voltage(5.0, 2.0)
    time.sleep(0.2)
    smu.set_voltage(0.25)
    st = smu.status()
    assert not st.ramping and st.source_voltage_set_V == 0.25
    time.sleep(0.2)
    assert smu.status().source_voltage_set_V == 0.25
    assert any("stopped by a voltage set" in m for _, m in smu.events)


def test_output_off_and_a_function_change_stop_it_and_it_never_switches_on(smu):
    sim = smu.backend
    # output OFF: the level walks, the output stays off
    smu.ramp_voltage(0.5, 5.0)
    assert _wait(lambda: not smu.status().ramping)
    assert smu.status().source_voltage_set_V == 0.5 and not smu.status().output
    assert ("output", True) not in sim.writes
    smu.set_output(True)
    smu.ramp_voltage(5.0, 1.0)
    time.sleep(0.2)
    smu.output_off()
    assert not smu.status().ramping and not smu.status().output
    smu.ramp_voltage(5.0, 1.0)
    time.sleep(0.1)
    smu.set_source_function("current")
    assert not smu.status().ramping


def test_compliance_stops_a_sweep():
    """1 kohm, 1 mA current limit: past ~1 V the SMU is in compliance and the
    swept voltage no longer applies -- the sweep stops there."""
    cfg = Config()
    smu, sim = _make(cfg)
    try:
        smu.set_current_limit(1e-3)
        smu.set_output(True)
        smu.ramp_voltage(5.0, 1.0)
        assert _wait(lambda: not smu.status().ramping, 10.0), smu.events
        st = smu.status()
        assert st.source_voltage_set_V < 2.0, (st, smu.events)
        assert any("in compliance" in m for lvl, m in smu.events if lvl == "warn"), smu.events
    finally:
        smu.shutdown()


def test_clamps_and_refusals(smu):
    with pytest.raises(ValueError):
        smu.ramp_voltage(1.0, 0.0)
    with pytest.raises(ValueError):
        smu.ramp_voltage(float("nan"), 1.0)
    smu.ramp_voltage(1e6, 1e9)
    st = smu.status()
    assert st.sweep_target == smu.level_limits("voltage")[1]
    assert st.sweep_rate == smu.cfg.limits.sweep_rate_max_V_per_s
    assert any("clamped" in m for lvl, m in smu.events if lvl == "warn")
    smu.ramp_stop()


def test_describe_offers_the_ramp_of_the_active_function(smu):
    from k2450.net.describe import build_manifest
    lim = smu.cfg.limits
    p = {d["id"]: d for d in build_manifest(smu)["parameters"]}
    r = p["source_voltage"]["ramp"]
    assert r["kind"] == "software"
    assert r["start"] == {"verb": "ramp_voltage",
                          "args": {"to": "voltage_V", "rate": "rate_V_per_s"}}
    assert r["rate"]["unit"] == "V/s" and r["rate"]["max"] == lim.sweep_rate_max_V_per_s
    assert r["readback"] == {"stream": {"group": "read", "channel": "voltage"},
                             "measured": True}
    assert r["done"] == {"key": "ramping", "id_key": "ramp_id"}
    assert p["ramp_stop"]["kind"] == "action"
    assert p["live_voltage"]["stream"] == {"group": "read", "channel": "voltage"}
    smu.set_source_function("current")
    p = {d["id"]: d for d in build_manifest(smu)["parameters"]}
    r = p["source_current"]["ramp"]
    assert r["start"] == {"verb": "ramp_current",
                          "args": {"to": "current_uA", "rate": "rate_uA_per_s"}}
    assert r["rate"]["unit"] == "uA/s"
    assert r["readback"]["stream"] == {"group": "read", "channel": "current_uA"}
    assert "ramp" not in p["source_voltage"]


def test_sweep_over_the_wire():
    zmq = pytest.importorskip("zmq")
    from k2450.net.service import K2450Service
    cfg = Config()
    cfg.hardware.ramp_dt_s = 0.02
    smu, _ = build_sim_system(cfg, realtime=False, seed=2)
    svc = K2450Service(smu, host="127.0.0.1", cmd_port=WIRE_CMD, pub_port=WIRE_PUB)
    svc.start()
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, 3000)
    s.connect(f"tcp://127.0.0.1:{WIRE_CMD}")

    def ask(**msg):
        s.send_json(msg)
        return s.recv_json()

    try:
        assert ask(cmd="stream_start")["ok"]
        r = ask(cmd="ramp_voltage", voltage_V=0.5, rate_V_per_s=1.0)
        assert r["ok"] and r["ramp_id"] == 1
        assert _wait(lambda: (lambda st: st["ramp_id"] == 1 and not st["ramping"])(
            ask(cmd="status")["status"]))
        c = ask(cmd="stream_stop")["stream"]
        # the output is OFF (adopted): the readings still go to the stream
        assert len(c["t"]) == len(c["values"]["voltage"]) >= 2
        assert ask(cmd="status")["status"]["source_voltage_set_V"] == 0.5
        assert ask(cmd="set_source_function", function="current")["ok"]
        r = ask(cmd="ramp_current", current_uA=10.0, rate_uA_per_s=100.0)
        assert r["ok"] and r["ramp_id"] == 2
        assert _wait(lambda: not ask(cmd="status")["status"]["ramping"])
        assert ask(cmd="status")["status"]["source_current_set_uA"] == pytest.approx(10.0)
        assert ask(cmd="ramp_stop") == {"ok": True, "stopped": False}
        assert ask(cmd="ramp_voltage", voltage_V=1.0)["ok"] is False      # no rate
    finally:
        s.close(0)
        svc.stop()
