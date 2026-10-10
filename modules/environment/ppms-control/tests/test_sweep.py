"""The field and temperature SWEEPS (ramp_field, ramp_temperature): HARDWARE
ramps for fly scans.

MultiVu sweeps the magnet (linear approach) and the temperature (fast_settle)
at the asked rate itself; the brain numbers each sweep, says when it ARRIVED,
and records every field and temperature reading for the fly scan to bin by
(one stream group, "cryostat"). Fake clock, as in test_cryostat.py.
"""

import time

import pytest

from test_cryostat import RecordingSim, Clock, make, run
from ppms.config import Config
from ppms.cryostat import Cryostat


def test_sweep_at_its_own_rate_then_arrives():
    clock = Clock()
    sim = RecordingSim(field_mT=0.0, clock=clock, noise=False)
    cryo = Cryostat(sim, Config(), clock=clock)
    cryo.start(poll=False)
    rate_setting = cryo.cfg.field.rate_mT_per_s
    rid = cryo.ramp_field(100.0, 10.0)
    assert rid == 1
    assert sim.commands[-1] == ("field", 100.0, 10.0, "linear")
    st = cryo.status()
    assert st.ramping and st.ramp_id == 1 and st.setpoint_field_mT == 100.0
    assert not st.field_stable
    run(cryo, clock, 5.0)
    st = cryo.status()
    assert st.ramping and 45 < st.measured_field_mT < 55       # 10 mT/s
    run(cryo, clock, 5.5)
    st = cryo.status()
    assert not st.ramping and st.ramp_id == 1                   # arrived
    assert cryo.cfg.field.rate_mT_per_s == rate_setting         # setting untouched


def test_ramp_stop_holds_where_it_is_and_set_takes_over():
    cryo, sim, clock, events = make(field_mT=0.0)
    cryo.ramp_field(200.0, 20.0)
    run(cryo, clock, 2.0)
    assert cryo.ramp_stop() is True
    st = cryo.status()
    assert not st.ramping and 35 < st.setpoint_field_mT < 45
    held = st.setpoint_field_mT
    run(cryo, clock, 3.0)
    assert abs(cryo.status().measured_field_mT - held) < 0.5
    assert cryo.ramp_stop() is False
    cryo.ramp_field(-100.0, 20.0)
    cryo.set_field(0.0)
    assert not cryo.status().ramping


def test_clamps_and_refusals():
    cryo, sim, clock, events = make()
    with pytest.raises(ValueError):
        cryo.ramp_field(10.0, 0.0)
    with pytest.raises(ValueError):
        cryo.ramp_field(float("nan"), 1.0)
    cryo.ramp_field(1e9, 1e9)
    st = cryo.status()
    assert st.ramp_target_mT == cryo.cfg.limits.field_max_mT
    assert st.ramp_rate_mT_per_s == cryo.cfg.limits.field_rate_max_mT_per_s
    assert any("clamped" in m for _, m in events)


def test_the_stream_records_the_field_readings():
    cryo, sim, clock, events = make()
    cryo.stream_start()
    cryo.ramp_field(10.0, 5.0)
    run(cryo, clock, 3.0, step=0.1)
    c = cryo.stream_stop()
    f = c["values"]["field"]
    assert len(c["t"]) == len(f) == 30
    assert f[0] < 1.0 and f[-1] == pytest.approx(10.0)
    assert f == sorted(f) and "now" in c
    # the temperature rides along in the same rows (one group, one drain)
    assert c["values"]["temperature"] == [pytest.approx(300.0)] * 30


def test_poll_thread_reads_fast_while_sweeping():
    cfg = Config()
    cfg.hardware.poll_s = 0.5
    cfg.hardware.ramp_poll_s = 0.02
    from ppms.backends.sim import SimulatedDynaCool
    cryo = Cryostat(SimulatedDynaCool(noise=False), cfg)
    cryo.start()
    try:
        cryo.stream_start()
        cryo.ramp_field(20.0, 20.0)                 # 1 s
        time.sleep(1.5)
        c = cryo.stream_stop()
        assert len(c["t"]) >= 25                    # ~50 Hz, not 2 Hz
        assert not cryo.status().ramping
    finally:
        cryo.shutdown()


def test_describe_offers_a_hardware_ramp():
    from ppms.net.describe import build_manifest
    cryo, *_ = make()
    params = build_manifest(cryo)["parameters"]
    f = next(p for p in params if p["id"] == "field")
    r = f["ramp"]
    assert r["kind"] == "hardware" and r["readback"]["measured"] is True
    assert r["readback"]["stream"] == {"group": "cryostat", "channel": "field"}
    assert r["rate"]["min"] <= r["rate"]["default"] <= r["rate"]["max"]
    m = next(p for p in params if p["id"] == "measured_field")
    assert m["stream"] == {"group": "cryostat", "channel": "field"}


# ---- the temperature sweep (2026-10-10) ------------------------------------------

def test_temperature_sweep_converts_the_rate_and_arrives_on_near():
    clock = Clock()
    sim = RecordingSim(temperature_K=300.0, clock=clock, noise=False)
    cryo = Cryostat(sim, Config(), clock=clock)
    cryo.start(poll=False)
    rate_setting = cryo.cfg.temperature.rate_K_per_min
    rid = cryo.ramp_temperature(295.0, 0.25)              # 5 K at 0.25 K/s = 20 s
    assert rid == 1
    # MultiVu takes K/min: 0.25 K/s = 15 K/min, fast_settle
    assert sim.commands[-1] == ("temperature", 295.0, 15.0, "fast_settle")
    st = cryo.status()
    assert st.temp_ramping and st.temp_ramp_id == 1
    assert st.temp_ramp_target_K == 295.0 and st.temp_ramp_rate_K_per_s == 0.25
    assert st.setpoint_temperature_K == 295.0 and not st.temperature_stable
    assert not st.ramping and st.ramp_id == 0             # the field's are separate
    run(cryo, clock, 10.0)
    st = cryo.status()
    assert st.temp_ramping and 297 < st.temperature_K < 298
    run(cryo, clock, 10.5)
    st = cryo.status()
    # arrived on "Near", with no hold time -- before temperature_stable
    assert st.temperature_status == "Near"
    assert not st.temp_ramping and st.temp_ramp_id == 1
    assert not st.temperature_stable
    assert cryo.cfg.temperature.rate_K_per_min == rate_setting   # setting untouched


def test_a_reading_from_before_the_sweep_does_not_end_it():
    """MultiVu says "Stable" at 300 K; a sweep to 299.8 K is within tolerance
    of that reading -- but the reading is older than the command and must not
    count (the generation guard), or the row would end before it began."""
    cryo, sim, clock, _ = make(temperature_K=300.0)
    run(cryo, clock, 5.0)                                  # settled: "Stable"
    cryo.ramp_temperature(299.8, 0.01)                     # 20 s
    run(cryo, clock, 1.0)
    st = cryo.status()
    assert st.temperature_status == "Chasing" and st.temp_ramping


def test_temperature_stop_holds_and_set_takes_over():
    cryo, sim, clock, events = make(temperature_K=300.0)
    cryo.ramp_temperature(200.0, 0.25)
    run(cryo, clock, 4.0)
    assert cryo.ramp_temperature_stop() is True
    st = cryo.status()
    assert not st.temp_ramping and 298.5 < st.setpoint_temperature_K < 299.5
    held = st.setpoint_temperature_K
    run(cryo, clock, 3.0)
    assert abs(cryo.status().temperature_K - held) < 0.1
    assert cryo.ramp_temperature_stop() is False
    cryo.ramp_temperature(250.0, 0.25)
    cryo.set_temperature(280.0)
    assert not cryo.status().temp_ramping
    # a FIELD stop does not stop a temperature sweep, and the other way round
    cryo.ramp_temperature(250.0, 0.25)
    assert cryo.ramp_stop() is False
    assert cryo.status().temp_ramping


def test_temperature_sweep_clamps_and_refuses():
    cryo, sim, clock, events = make()
    with pytest.raises(ValueError):
        cryo.ramp_temperature(10.0, 0.0)
    with pytest.raises(ValueError):
        cryo.ramp_temperature(float("inf"), 0.1)
    cryo.ramp_temperature(1e6, 1e6)
    st = cryo.status()
    lim = cryo.cfg.limits
    assert st.temp_ramp_target_K == lim.temperature_max_K
    assert st.temp_ramp_rate_K_per_s == pytest.approx(lim.temperature_rate_max_K_per_min / 60)
    cryo.ramp_temperature(10.0, 1e-9)
    assert cryo.status().temp_ramp_rate_K_per_s == pytest.approx(
        lim.temperature_rate_min_K_per_min / 60)
    assert sum("clamped" in m for _, m in events) == 2


def test_poll_thread_reads_temperature_fast_while_it_sweeps():
    cfg = Config()
    cfg.hardware.poll_s = 0.5
    cfg.hardware.ramp_poll_s = 0.02
    from ppms.backends.sim import SimulatedDynaCool
    cryo = Cryostat(SimulatedDynaCool(noise=False, temperature_K=300.0), cfg)
    cryo.start()
    try:
        cryo.stream_start()
        cryo.ramp_temperature(299.0, 1.0 / 3.0)            # 3 s at the max rate
        time.sleep(1.0)
        c = cryo.stream_read()
        assert len(c["t"]) >= 25                           # ~50 Hz, not 2 Hz
        tv = c["values"]["temperature"]
        assert tv[0] > tv[-1] and 299.0 <= tv[-1] < 300.0  # moving, MEASURED
        cryo.stream_stop()
    finally:
        cryo.shutdown()


def test_describe_offers_a_temperature_ramp():
    from ppms.net.describe import build_manifest
    cryo, *_ = make()
    params = {p["id"]: p for p in build_manifest(cryo)["parameters"]}
    r = params["temperature"]["ramp"]
    assert r["kind"] == "hardware"
    assert r["start"] == {"verb": "ramp_temperature",
                          "args": {"to": "temperature_K", "rate": "rate_K_per_s"}}
    assert r["stop"] == {"verb": "ramp_temperature_stop"}
    assert r["done"] == {"key": "temp_ramping", "id_key": "temp_ramp_id"}
    assert r["rate"]["unit"] == "K/s"
    assert r["rate"]["min"] == pytest.approx(0.01 / 60)
    assert r["rate"]["max"] == pytest.approx(20.0 / 60)
    assert r["rate"]["default"] == pytest.approx(2.0 / 60)
    assert r["readback"] == {"stream": {"group": "cryostat", "channel": "temperature"},
                             "measured": True}
    assert params["measured_temperature"]["stream"] == {"group": "cryostat",
                                                        "channel": "temperature"}


def test_the_stop_verbs_are_safety_verbs():
    from ppms.net.service import PpmsService
    cryo, *_ = make()
    svc = PpmsService(cryo, host="127.0.0.1", cmd_port=17390, pub_port=17391)
    assert {"ramp_stop", "ramp_temperature_stop"} <= svc.control.safety
    assert "stream_read" in svc.control.read
