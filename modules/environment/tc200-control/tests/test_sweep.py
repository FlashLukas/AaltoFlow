"""The temperature SWEEP (ramp_temperature): a software ramp for fly scans.

The TC200's own ramps exist only inside its front-panel CYCLE program, so the
service walks the setpoint (softramp.py) in the box's 0.1 degC steps; a fly
scan bins by the MEASURED temperature, which the poll thread streams. Checked
here: the pace, the 0.1 C steps, the end on the target, ramp_stop and a set
taking over, clamping, the faster poll and the stream, and the verbs over the
wire (ports 17372/17373, inside this module's test range 17360..17379).
"""

import time

import pytest

from tc200.config import Config
from tc200.sim_system import build_sim_system


class Recorder:
    """Wraps the simulated box and records every setpoint it is sent."""

    def __init__(self, inner):
        self._inner = inner
        self.sets = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def set_setpoint(self, v):
        self.sets.append((time.monotonic(), v))
        self._inner.set_setpoint(v)


@pytest.fixture
def heater():
    cfg = Config()
    cfg.hardware.poll_s = 0.5
    cfg.hardware.ramp_poll_s = 0.02
    cfg.hardware.ramp_dt_s = 0.01
    # fast enough for a test (the real block could never follow 600 K/min)
    cfg.limits.ramp_rate_max_C_per_s = 10.0
    h, sim = build_sim_system(cfg, temperature_C=30.0, setpoint_C=30.0, enabled=True,
                              seed=3)
    rec = Recorder(sim)
    h.backend = rec
    h.events = []
    h._on_event = lambda lvl, msg: h.events.append((lvl, msg))
    h.start()
    h.rec = rec
    yield h
    h.shutdown()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_sweep_walks_the_setpoint_in_tenth_degree_steps(heater):
    t0 = time.monotonic()
    rid = heater.ramp_temperature(32.0, 4.0)          # 2 C at 4 C/s = 0.5 s
    assert rid == 1
    st = heater.status()
    assert st.ramping and st.ramp_id == 1
    assert st.ramp_target_C == 32.0 and st.ramp_rate_C_per_s == 4.0
    assert _wait(lambda: not heater.status().ramping)
    assert 0.4 < time.monotonic() - t0 < 1.5
    sent = [v for _, v in heater.rec.sets]
    # every 0.1 C step once, upwards, ending exactly on the target
    assert sent == sorted(sent) and len(set(sent)) == len(sent)
    assert sent[-1] == 32.0 and 15 <= len(sent) <= 20
    assert all(abs(v * 10 - round(v * 10)) < 1e-9 for v in sent)
    assert heater.status().setpoint_C == 32.0
    assert any("sweep done" in m for _, m in heater.events)
    # no event per step: a sweep must not flood the log
    assert sum("setpoint ->" in m for _, m in heater.events) == 0


def test_stop_ends_it_where_it_is(heater):
    heater.ramp_temperature(40.0, 2.0)                 # 5 s
    time.sleep(0.4)
    assert heater.ramp_stop() is True
    st = heater.status()
    assert not st.ramping
    assert 30.3 <= st.setpoint_C <= 31.5               # somewhere on the way
    n = len(heater.rec.sets)
    time.sleep(0.3)
    assert len(heater.rec.sets) == n                   # nothing sent after stop
    assert heater.ramp_stop() is False                 # nothing left to stop


def test_a_set_takes_over(heater):
    heater.ramp_temperature(40.0, 2.0)
    time.sleep(0.3)
    heater.set_temperature(35.0)
    assert not heater.status().ramping
    time.sleep(0.3)
    assert heater.status().setpoint_C == 35.0          # the walk did not come back
    assert heater.rec.sets[-1][1] == 35.0
    assert any("stopped by a setpoint" in m for _, m in heater.events)


def test_target_and_rate_are_clamped_and_warned(heater):
    hi = heater.temperature_max()                      # min(100 limit, TMAX 120 - 5, 200)
    heater.ramp_temperature(500.0, 99.0)               # both outside
    st = heater.status()
    assert st.ramp_target_C == hi
    assert st.ramp_rate_C_per_s == heater.cfg.limits.ramp_rate_max_C_per_s
    assert any(lvl == "warn" and "clamped" in m for lvl, m in heater.events)
    heater.ramp_stop()
    with pytest.raises(ValueError):
        heater.ramp_temperature(40.0, 0.0)
    with pytest.raises(ValueError):
        heater.ramp_temperature(float("nan"), 1.0)


def test_refused_in_cycle_mode(heater):
    heater.rec._inner.cycle_mode = True
    heater.poll_once()
    with pytest.raises(RuntimeError, match="CYCLE"):
        heater.ramp_temperature(35.0, 1.0)


def test_stream_records_the_measured_temperature_fast(heater):
    sid = heater.stream_start()
    assert sid == 1
    heater.ramp_temperature(31.0, 2.0)                 # 0.5 s
    assert _wait(lambda: not heater.status().ramping)
    time.sleep(0.1)
    chunk = heater.stream_stop()
    t, vals = chunk["t"], chunk["values"]["temperature"]
    # ramp_poll_s = 20 ms while sweeping / recording: far more than poll_s
    # (0.5 s) would give in the same ~0.6 s
    assert len(t) == len(vals) >= 10
    assert all(28.0 < v < 33.0 for v in vals)          # MEASURED, not commanded
    assert t == sorted(t) and abs(chunk["now"] - t[-1]) < 1.0
    assert chunk["delay_s"] == {"temperature": 0.0}
    # stopped: nothing more is recorded
    assert heater.stream_read()["t"] == []


def test_describe_declares_the_ramp(heater):
    from tc200.net.describe import build_manifest
    m = {p["id"]: p for p in build_manifest(heater)["parameters"]}
    r = m["temperature"]["ramp"]
    assert r["kind"] == "software"
    assert r["start"] == {"verb": "ramp_temperature",
                          "args": {"to": "temperature_C", "rate": "rate_C_per_s"}}
    assert r["stop"] == {"verb": "ramp_stop"}
    assert r["rate"]["unit"] == "C/s"
    assert r["rate"]["min"] <= r["rate"]["default"] <= r["rate"]["max"]
    assert r["readback"] == {"stream": {"group": "temperature", "channel": "temperature"},
                             "measured": True}
    assert m["measured_temperature"]["stream"] == {"group": "temperature",
                                                   "channel": "temperature"}


def test_verbs_over_the_wire():
    from tc200.net.client import Tc200Client
    from tc200.net.service import Tc200Service
    cfg = Config()
    cfg.hardware.poll_s = 0.1
    cfg.hardware.ramp_poll_s = 0.02
    cfg.limits.ramp_rate_max_C_per_s = 10.0
    h, _ = build_sim_system(cfg, temperature_C=30.0, setpoint_C=30.0, enabled=True, seed=4)
    svc = Tc200Service(h, host="127.0.0.1", cmd_port=17372, pub_port=17373, status_hz=20.0)
    svc.start()
    cli = Tc200Client(host="127.0.0.1", cmd_port=17372, pub_port=17373, timeout_ms=2000)
    try:
        cli.start()
        assert cli._cmd({"cmd": "stream_start"})["ok"]
        rid = cli.ramp_temperature(31.0, 4.0)
        assert rid == 1
        assert _wait(lambda: (cli._cmd({"cmd": "status"})["status"]["ramp_id"] >= rid
                              and not cli._cmd({"cmd": "status"})["status"]["ramping"]))
        rep = cli._cmd({"cmd": "stream_stop"})
        assert rep["ok"] and len(rep["stream"]["values"]["temperature"]) >= 3
        assert cli.ramp_stop() is False
        # the stop is a SAFETY verb and stream_read a READ verb: a viewer may
        assert "ramp_stop" in svc.control.safety and "stream_read" in svc.control.read
    finally:
        cli.shutdown()
        svc.stop()
