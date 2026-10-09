"""The field SWEEP (ramp_field): a HARDWARE ramp for fly scans.

MultiVu sweeps the magnet at the asked rate itself (linear approach); the
brain numbers the sweep, says when it ARRIVED, and records every field
reading for the fly scan to bin by. Fake clock, as in test_cryostat.py.
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
    assert r["readback"]["stream"] == {"group": "field", "channel": "field"}
    assert r["rate"]["min"] <= r["rate"]["default"] <= r["rate"]["max"]
    m = next(p for p in params if p["id"] == "measured_field")
    assert m["stream"] == {"group": "field", "channel": "field"}
