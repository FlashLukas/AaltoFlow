"""The wavelength SWEEP (ramp_wavelength): a software ramp of one AOTF line,
for fly scans.

The service walks one line's wavelength register (softramp.py) and records
every value it sent, all 8 lines per row (forward-filled); a fly scan bins by
that COMMANDED wavelength. Checked here: the pace and the end on the target,
one sweep at a time with one ramp_id counter, ramp_stop and a set taking
over, clamping, emission never touched, the record, describe, and the verbs
over the wire (ports 17350/17351, inside this module's test range).
"""

import time

import pytest

from superk.config import Config
from superk.sim_system import build_sim_system


@pytest.fixture
def rig():
    cfg = Config()
    cfg.hardware.sim_warmup_s = 0.2
    cfg.hardware.poll_hz = 20.0
    cfg.hardware.ramp_dt_s = 0.01
    laser, backend = build_sim_system(cfg)
    events = []
    laser._on_event = lambda lvl, msg: events.append((lvl, msg))
    laser.start()
    yield laser, backend, events
    laser.shutdown()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _wl_writes(backend, ch):
    return [w[2] for w in backend.writes if w[0] == "set_wavelength" and w[1] == ch]


def test_sweep_walks_one_line_at_the_pace_and_ends_on_the_target(rig):
    laser, backend, events = rig
    start = laser.status().wavelength_set_nm[2]            # line 3: 750 nm
    backend.writes.clear()
    t0 = time.monotonic()
    rid = laser.ramp_wavelength(3, start + 50.0, 100.0)    # 50 nm at 100 nm/s = 0.5 s
    assert rid == 1
    st = laser.status()
    assert st.ramping and st.ramp_id == 1 and st.ramp_line == 3       # live
    assert st.ramp_target_nm == start + 50.0 and st.ramp_rate_nm_per_s == 100.0
    assert _wait(lambda: not laser.status().ramping)
    assert 0.4 < time.monotonic() - t0 < 1.5
    wl = _wl_writes(backend, 2)
    assert len(wl) >= 20 and wl == sorted(wl) and wl[-1] == start + 50.0
    # only line 3 was written, and nothing that switches light
    assert {w[1] for w in backend.writes if w[0] == "set_wavelength"} == {2}
    assert not [w for w in backend.writes if w[0] in ("set_emission", "set_rf",
                                                     "set_amplitude", "set_power")]
    assert _wait(lambda: laser.status().wavelength_nm[2] == start + 50.0)
    assert any("sweep done" in m for _, m in events)


def test_emission_is_never_switched_by_a_sweep(rig):
    laser, backend, _ = rig
    assert not laser.status().emission_set
    laser.ramp_wavelength(1, 700.0, 100.0)
    assert _wait(lambda: not laser.status().ramping)
    st = laser.status()
    assert not st.emission_set and not st.emission_on


def test_one_sweep_at_a_time_and_one_counter(rig):
    laser, backend, _ = rig
    r1 = laser.ramp_wavelength(1, 850.0, 20.0)             # long
    time.sleep(0.2)
    r2 = laser.ramp_wavelength(2, 750.0, 100.0)            # replaces it
    assert r2 == r1 + 1
    held = laser.status().wavelength_set_nm
    assert _wait(lambda: not laser.status().ramping)
    st = laser.status()
    assert st.ramp_id == r2 and st.ramp_line == 2
    # (the set values travel in the worker's snapshot: wait one poll for it)
    assert _wait(lambda: laser.status().wavelength_set_nm[1] == 750.0)
    st = laser.status()
    # line 1 stopped where the first sweep had got to
    assert 650.0 < st.wavelength_set_nm[0] < 850.0
    assert st.wavelength_set_nm[0] == pytest.approx(held[0], abs=1.0)


def test_stop_ends_it_where_it_is(rig):
    laser, backend, _ = rig
    laser.ramp_wavelength(1, 850.0, 50.0)                  # 4 s
    time.sleep(0.4)
    assert laser.ramp_stop() is True
    assert not laser.status().ramping
    n = len(backend.writes)
    time.sleep(0.2)
    assert len(backend.writes) == n                        # nothing after the stop
    wl = laser.status().wavelength_set_nm[0]
    assert 655.0 < wl < 700.0
    assert laser.ramp_stop() is False


def test_a_set_of_the_swept_line_takes_over_another_line_does_not(rig):
    laser, backend, events = rig
    laser.ramp_wavelength(1, 850.0, 20.0)
    time.sleep(0.2)
    laser.set_wavelength(2, 720.0)                         # another line
    assert laser.status().ramping
    laser.set_wavelength(1, 600.0)                         # the swept one
    assert not laser.status().ramping
    time.sleep(0.2)
    assert laser.status().wavelength_set_nm[0] == 600.0
    assert any("stopped by a set" in m for _, m in events)
    # a crystal change stops a sweep too (it moves every line's range)
    laser.ramp_wavelength(1, 850.0, 20.0)
    laser.set_filter("nIR2")
    assert not laser.status().ramping


def test_target_and_rate_are_clamped_and_warned(rig):
    laser, backend, events = rig
    lo, hi = laser.wavelength_range()
    laser.ramp_wavelength(1, 5000.0, 1e6)
    st = laser.status()
    assert st.ramp_target_nm == hi
    assert st.ramp_rate_nm_per_s == laser.cfg.limits.ramp_rate_max_nm_per_s
    assert any(lvl == "warn" and "clamped" in m for lvl, m in events)
    laser.ramp_stop()
    with pytest.raises(ValueError):
        laser.ramp_wavelength(1, 700.0, 0.0)
    with pytest.raises(ValueError):
        laser.ramp_wavelength(9, 700.0, 1.0)
    with pytest.raises(ValueError):
        laser.ramp_wavelength(1, float("nan"), 1.0)


def test_the_record_holds_every_line_forward_filled(rig):
    laser, backend, _ = rig
    before = list(laser.status().wavelength_set_nm)
    sid = laser.stream_start()
    assert sid == 1
    laser.ramp_wavelength(2, before[1] + 20.0, 100.0)      # 0.2 s
    assert _wait(lambda: not laser.status().ramping)
    c = laser.stream_stop()
    v = c["values"]
    assert sorted(v) == [f"wavelength_{n}" for n in range(1, 9)]
    n = len(c["t"])
    assert n >= 10 and all(len(x) == n for x in v.values())
    assert v["wavelength_2"][0] == before[1]               # at rest first
    assert v["wavelength_2"][-1] == before[1] + 20.0
    assert v["wavelength_2"] == sorted(v["wavelength_2"])
    assert v["wavelength_1"] == [before[0]] * n            # forward-filled
    assert c["t"] == sorted(c["t"])
    assert laser.stream_read()["t"] == []                  # stopped: nothing more


def test_describe_declares_a_ramp_per_line(rig):
    from superk.net.describe import build_manifest
    laser, *_ = rig
    m = {p["id"]: p for p in build_manifest(laser)["parameters"]}
    for n in range(1, 9):
        r = m[f"wavelength_{n}"]["ramp"]
        assert r["kind"] == "software"
        assert r["start"] == {"verb": "ramp_wavelength",
                              "args": {"to": "wavelength_nm", "rate": "rate_nm_per_s"},
                              "extra": {"line": n}}
        assert r["stop"] == {"verb": "ramp_stop"}
        assert r["readback"] == {"stream": {"group": "ramp", "channel": f"wavelength_{n}"},
                                 "measured": False}
        assert r["rate"]["unit"] == "nm/s"
        assert r["rate"]["min"] <= r["rate"]["default"] <= r["rate"]["max"]


def test_verbs_over_the_wire():
    from superk.net.client import SuperkClient
    from superk.net.service import SuperkService
    cfg = Config()
    cfg.hardware.sim_warmup_s = 0.2
    cfg.hardware.poll_hz = 20.0
    laser, _ = build_sim_system(cfg)
    svc = SuperkService(laser, host="127.0.0.1", cmd_port=17350, pub_port=17351)
    svc.start()
    cli = SuperkClient(host="127.0.0.1", cmd_port=17350, pub_port=17351, timeout_ms=2000)
    try:
        cli.start()
        assert cli._cmd({"cmd": "stream_start"})["ok"]
        rid = cli.ramp_wavelength(1, 660.0, 50.0)
        assert rid == 1

        def done():
            st = cli._cmd({"cmd": "status"})["status"]
            return st["ramp_id"] >= rid and not st["ramping"]
        assert _wait(done)
        rep = cli._cmd({"cmd": "stream_stop"})
        assert rep["ok"] and rep["stream"]["values"]["wavelength_1"][-1] == 660.0
        assert cli.ramp_stop() is False
        assert "ramp_stop" in svc.control.safety and "stream_read" in svc.control.read
    finally:
        cli.shutdown()
        svc.stop()
