"""The current SWEEP (ramp_current): a software ramp for fly scans.

softramp.py walks the setpoint at the asked pace and programs every step; the
worker measures V and I faster meanwhile and records them, so a fly scan bins
by the MEASURED current (a coil lags the programmed value by L/R). These run
on REAL time with the worker thread running (the walk has its own thread).
Checked: the pace and the end, the stream, ramp_stop, a set taking over,
output off and the watchdog stopping it, a sweep never switching the output
on, the voltage limit stopping it, clamping, describe and the wire.
"""

import time

import pytest

from kepco.backends.sim import SimulatedBOP
from kepco.config import Config
from kepco.supply import BipolarSupply

from conftest import Spy

# This module's own test-port block is 17000..17019 (test_net 17000/1,
# 17010/1, 17018/9); the sweep's wire test takes 17004/5.
WIRE_CMD, WIRE_PUB = 17004, 17005


def _make(cfg=None):
    cfg = cfg or Config()
    cfg.ramp.step_hz = 40.0            # 25 ms sweep steps
    cfg.hardware.stream_poll_hz = 20.0
    sim = SimulatedBOP(load=cfg.sim, clock=time.monotonic, seed=0)
    spy = Spy(sim)
    supply = BipolarSupply(spy, cfg)
    events = []
    supply._on_event = lambda lvl, msg: events.append((lvl, msg))
    supply.start()
    return supply, spy, events


@pytest.fixture
def live():
    supply, spy, events = _make()
    yield supply, spy, events
    supply.shutdown()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _mains(spy):
    return [c[1] for c in spy.calls if c[0] == "program_current"]


def _output_on(supply):
    supply.set_output(True)
    assert _wait(lambda: supply.status().output and not supply.status().ramping)


def test_sweep_walks_at_the_pace_and_ends_on_the_target(live):
    supply, spy, events = live
    _output_on(supply)
    spy.calls.clear()
    t0 = time.monotonic()
    rid = supply.ramp_current(1.0, 2.0)                # 1 A at 2 A/s = 0.5 s
    assert rid == 1
    st = supply.status()
    assert st.ramp_id == 1 and st.ramping and st.sweeping
    assert st.sweep_target_A == 1.0 and st.sweep_rate_A_per_s == 2.0
    assert _wait(lambda: not supply.status().ramping)
    assert 0.4 < time.monotonic() - t0 < 2.0
    st = supply.status()
    assert st.programmed == 1.0 and st.current_set_A == 1.0 and not st.sweeping
    vals = _mains(spy)
    assert len(vals) >= 10 and vals == sorted(vals) and vals[-1] == 1.0
    # the ordinary ramp rate is untouched by a sweep at its own pace
    assert supply.cfg.ramp.rate_A_per_s == Config().ramp.rate_A_per_s
    assert supply.cfg.output.current_A == 1.0
    assert sum("current =" in m for _, m in events) == 0      # no event per step


def test_the_stream_records_the_measured_current_with_its_time(live):
    supply, spy, events = live
    _output_on(supply)
    assert supply.stream_start() == 1
    supply.ramp_current(1.0, 2.0)
    assert _wait(lambda: not supply.status().ramping)
    time.sleep(0.3)
    c = supply.stream_stop()
    t, cur = c["t"], c["values"]["current"]
    assert len(t) == len(cur) == len(c["values"]["voltage"]) >= 10
    assert t == sorted(t) and "now" in c and c["delay_s"]["current"] == 0.0
    assert cur[0] < 0.2 and cur[-1] == pytest.approx(1.0, abs=0.05)
    prog = c["values"]["programmed"]
    assert prog == sorted(prog) and prog[-1] == 1.0


def test_ramp_stop_ends_it_where_it_is(live):
    supply, spy, events = live
    _output_on(supply)
    supply.ramp_current(5.0, 1.0)
    time.sleep(0.5)
    assert supply.ramp_stop() is True
    here = supply.status().current_set_A
    assert 0.2 < here < 1.0
    assert _wait(lambda: not supply.status().ramping)
    time.sleep(0.2)
    st = supply.status()
    assert st.programmed == here and st.current_set_A == here
    assert supply.ramp_stop() is False


def test_a_current_set_takes_over_and_ramps_from_where_the_sweep_got(live):
    supply, spy, events = live
    _output_on(supply)
    supply.ramp_current(5.0, 1.0)
    time.sleep(0.4)
    supply.set_current(0.0)
    assert not supply.status().sweeping
    # the snapshot shows a set one worker step later (as for any set)
    assert _wait(lambda: supply.status().current_set_A == 0.0)
    assert _wait(lambda: not supply.status().ramping)
    vals = _mains(spy)
    peak = max(vals)
    i = vals.index(peak)
    # up during the sweep, then down at the ordinary rate: never a step
    assert vals[:i + 1] == sorted(vals[:i + 1]) and vals[i:] == sorted(vals[i:], reverse=True)
    steps = [abs(b - a) for a, b in zip(vals, vals[1:])]
    assert max(steps) < 0.1
    assert any("stopped by a current set" in m for _, m in events)


def test_output_off_stops_a_sweep_and_a_sweep_never_switches_it_on(live):
    supply, spy, events = live
    # output OFF: only the stored setpoint walks; nothing is switched on
    supply.ramp_current(0.5, 2.0)
    assert _wait(lambda: not supply.status().ramping)
    st = supply.status()
    assert st.current_set_A == 0.5 and not st.output and not st.output_request
    assert ("set_output", True) not in spy.calls
    assert not any(c[0] == "program_current" and c[1] > 0 for c in spy.calls)
    # output ON, sweep, then off: the sweep stops and the worker ramps down
    _output_on(supply)
    supply.ramp_current(5.0, 1.0)
    time.sleep(0.3)
    supply.output_off()
    assert not supply.status().sweeping
    assert _wait(lambda: not supply.status().output, 10.0)
    vals = _mains(spy)
    assert vals[-1] == 0.0


def test_the_voltage_limit_stops_a_sweep():
    """A coil of 2 ohm at a 1 V compliance: above ~0.5 A the limit takes over
    and the current no longer follows -- the sweep stops there."""
    cfg = Config()
    supply, spy, events = _make(cfg)
    try:
        supply.set_voltage_limit(1.0)
        _output_on(supply)
        supply.ramp_current(3.0, 1.0)
        assert _wait(lambda: not supply.status().sweeping, 6.0)
        assert supply.status().current_set_A < 1.5
        assert any("voltage limit" in m for lvl, m in events if lvl == "warn")
    finally:
        supply.shutdown()


def test_the_watchdog_stops_a_sweep():
    cfg = Config()
    cfg.safety.watchdog_s = 0.4
    supply, spy, events = _make(cfg)
    try:
        _output_on(supply)
        supply.touch()
        supply.ramp_current(5.0, 0.5)
        assert _wait(lambda: not supply.status().sweeping, 3.0)
        assert _wait(lambda: not supply.status().output, 10.0)
        assert any("no client" in m for _, m in events)
    finally:
        supply.shutdown()


def test_clamps_and_refusals(live):
    supply, spy, events = live
    with pytest.raises(ValueError):
        supply.ramp_current(1.0, 0.0)
    with pytest.raises(ValueError):
        supply.ramp_current(float("nan"), 1.0)
    supply.ramp_current(100.0, 1e6)
    st = supply.status()
    assert st.sweep_target_A == supply.cfg.limits.current_max_A
    assert st.sweep_rate_A_per_s == supply.cfg.limits.rate_max_A_per_s
    assert any("clamped" in m for lvl, m in events if lvl == "warn")
    supply.ramp_stop()
    supply.set_mode("voltage")
    with pytest.raises(ValueError):
        supply.ramp_current(1.0, 1.0)


def test_describe_offers_the_ramp_in_current_mode_only(live):
    from kepco.net.describe import build_manifest
    supply, spy, events = live
    p = {d["id"]: d for d in build_manifest(supply)["parameters"]}
    r = p["current"]["ramp"]
    lim = supply.cfg.limits
    assert r["kind"] == "software"
    assert r["start"] == {"verb": "ramp_current",
                          "args": {"to": "current_A", "rate": "rate_A_per_s"}}
    assert r["stop"] == {"verb": "ramp_stop"}
    assert r["rate"]["unit"] == "A/s"
    assert r["rate"]["min"] == lim.sweep_rate_min_A_per_s
    assert r["rate"]["max"] == lim.rate_max_A_per_s
    assert r["readback"] == {"stream": {"group": "measure", "channel": "current"},
                             "measured": True}
    assert r["done"] == {"key": "ramping", "id_key": "ramp_id"}
    assert p["live_current"]["stream"] == {"group": "measure", "channel": "current"}
    supply.set_mode("voltage")
    p = {d["id"]: d for d in build_manifest(supply)["parameters"]}
    assert not any("ramp" in d for d in p.values())


def test_sweep_over_the_wire():
    zmq = pytest.importorskip("zmq")
    from kepco.net.service import KepcoService
    cfg = Config()
    cfg.ramp.step_hz = 40.0
    sim = SimulatedBOP(load=cfg.sim, clock=time.monotonic, seed=0)
    svc = KepcoService(BipolarSupply(sim, cfg), host="127.0.0.1",
                       cmd_port=WIRE_CMD, pub_port=WIRE_PUB)
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
        r = ask(cmd="ramp_current", current_A=0.2, rate_A_per_s=0.5)
        assert r["ok"] and r["ramp_id"] == 1
        assert _wait(lambda: (lambda st: st["ramp_id"] == 1 and not st["ramping"])(
            ask(cmd="status")["status"]))
        c = ask(cmd="stream_stop")["stream"]
        assert len(c["t"]) == len(c["values"]["current"]) >= 2
        assert ask(cmd="status")["status"]["current_set_A"] == 0.2
        assert ask(cmd="ramp_stop") == {"ok": True, "stopped": False}
        assert ask(cmd="ramp_current", current_A=1.0)["ok"] is False      # no rate
    finally:
        s.close(0)
        svc.stop()
