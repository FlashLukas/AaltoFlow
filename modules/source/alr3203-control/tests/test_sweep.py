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

from alr3203.backends.sim import SimRelay, SimulatedALR3203
from alr3203.config import Config
from alr3203.supply import BipolarSupply

from conftest import Spy

# This module's own test-port block is 17000..17019 (test_net 17000/1,
# 17010/1, 17018/9); the sweep's wire test takes 17004/5.
WIRE_CMD, WIRE_PUB = 17004, 17005


def _make(cfg=None):
    cfg = cfg or Config()
    cfg.ramp.step_hz = 20.0            # 50 ms sweep steps (the serial link's cap)
    cfg.hardware.stream_poll_hz = 20.0
    sim = SimulatedALR3203(load=cfg.sim, clock=time.monotonic, seed=0)
    spy = Spy(sim)
    supply = BipolarSupply(spy, cfg, relay=SimRelay(sim))
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
    # the ordinary ramp walks 2 A/s at 20 Hz = 0.1 A a step, plus timer jitter
    # (each step is rate x the real time since the last one): no jump beyond
    assert max(steps) < 0.15
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
        # The worker stops the sweep FIRST (sweeping goes False) and emits the
        # warning right after, so wait for it rather than look at once: checking
        # in between failed 1 run in 6 on the Blue Box PC.
        assert _wait(lambda: any("voltage limit" in m for lvl, m in events
                                 if lvl == "warn"), 2.0)
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
    from alr3203.net.describe import build_manifest
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
    from alr3203.net.service import Alr3203Service
    cfg = Config()
    cfg.ramp.step_hz = 20.0
    sim = SimulatedALR3203(load=cfg.sim, clock=time.monotonic, seed=0)
    svc = Alr3203Service(BipolarSupply(sim, cfg, relay=SimRelay(sim)), host="127.0.0.1",
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



# --------------------------------------------------------------------------- #
# ACROSS ZERO (Lukas, 2026-11-10: "I just want to send the sweep of - current
# to + current and the software handles the relay automatically")
# --------------------------------------------------------------------------- #
def _at(supply, amps):
    supply.set_current(amps)
    assert _wait(lambda: supply.status().current_set_A == amps
                 and not supply.status().ramping, 10.0)


def test_a_sweep_from_minus_to_plus_flips_the_relay_by_itself(live):
    supply, spy, events = live
    _output_on(supply)
    _at(supply, -1.0)
    relay = supply.relay
    assert relay.inverted is True
    flips_before = len(relay.flips)
    assert supply.stream_start() >= 1
    spy.calls.clear()
    rid = supply.ramp_current(1.0, 4.0)                 # 2 A at 4 A/s, plus the flip
    seen = []
    t_end = time.monotonic() + 10.0
    while time.monotonic() < t_end:
        st = supply.status()
        seen.append((st.ramp_id, st.sweeping))
        if not st.sweeping:
            break
        time.sleep(0.01)
    st = supply.status()
    # ONE sweep to a scan: its number, "sweeping" all the way, then done
    assert all(r == rid for r, _ in seen)
    assert [sw for _, sw in seen[:-1]] == [True] * (len(seen) - 1)
    assert st.ramp_id == rid and not st.sweeping
    assert st.current_set_A == 1.0 and st.programmed == 1.0
    assert relay.inverted is False
    assert len(relay.flips) == flips_before + 1          # flipped ONCE
    assert relay.hot_switches == 0                       # never under current
    assert any("through 0" in m for _, m in events)
    assert any(f"sweep #{rid} done" in m for _, m in events)
    # the MEASURED current went from -1 to +1 through 0 (signed by the relay)
    time.sleep(0.3)
    c = supply.stream_stop()
    cur = c["values"]["current"]
    assert cur[0] < -0.8 and cur[-1] == pytest.approx(1.0, abs=0.05)
    assert min(abs(x) for x in cur) < 0.05
    # magnitudes programmed: down to 0, then up -- never a jump
    mags = _mains(spy)
    i0 = mags.index(0.0)
    assert mags[:i0 + 1] == sorted(mags[:i0 + 1], reverse=True)
    assert mags[i0:] == sorted(mags[i0:])


def test_back_from_plus_to_minus_too(live):
    supply, spy, events = live
    _output_on(supply)
    _at(supply, 0.5)
    rid = supply.ramp_current(-0.5, 2.0)
    assert _wait(lambda: not supply.status().sweeping, 10.0)
    st = supply.status()
    assert st.ramp_id == rid and st.current_set_A == -0.5
    assert supply.relay.inverted is True and supply.relay.hot_switches == 0


def test_a_stop_during_the_crossing_ends_it_at_zero_or_before(live):
    supply, spy, events = live
    _output_on(supply)
    _at(supply, -1.0)
    supply.ramp_current(1.0, 1.0)                        # 1 s to reach 0
    time.sleep(0.6)
    assert supply.ramp_stop() is True
    assert _wait(lambda: not supply.status().sweeping, 5.0)
    time.sleep(0.3)
    st = supply.status()
    assert -1.0 < st.current_set_A <= 0.0                # stopped where it was
    assert supply.relay.inverted is True                 # never got to the flip
    assert supply.relay.hot_switches == 0


def test_with_the_output_off_only_the_setpoint_walks_across_zero(live):
    supply, spy, events = live
    supply.set_current(-0.5)
    flips = len(supply.relay.flips)
    rid = supply.ramp_current(0.5, 2.0)
    assert _wait(lambda: not supply.status().sweeping, 5.0)
    st = supply.status()
    assert st.ramp_id == rid and st.current_set_A == 0.5 and not st.output
    assert len(supply.relay.flips) == flips              # set when the output goes on


def test_one_wait_and_one_flip_even_when_the_current_takes_its_time():
    """Blue Box, 2026-11-10: "polarity change ... waiting for the current to
    stop" was said TWICE before one flip. The worker, seeing its own setpoint
    at 0 ("no flip needed"), cleared the flip timer under the sweep's flip --
    and in an unlucky moment the sweep would have ended at 0 with "the relay
    did not switch". A slow coil makes the wait long enough to catch it."""
    cfg = Config()
    cfg.sim.load_L_H = 0.6                              # tau = L/R ~ 0.3 s: a long zero wait
    supply, spy, events = _make(cfg)
    try:
        _output_on(supply)
        _at(supply, -0.3)
        events.clear()
        rid = supply.ramp_current(0.3, 1.0)
        assert _wait(lambda: not supply.status().sweeping, 15.0)
        st = supply.status()
        assert st.ramp_id == rid and st.current_set_A == 0.3
        assert supply.relay.inverted is False and supply.relay.hot_switches == 0
        waits = [m for _, m in events if "waiting for the current to stop" in m]
        flips = [m for _, m in events if "polarity relay ->" in m]
        assert len(waits) == 1, waits
        assert len(flips) == 1, flips
        assert not [m for lvl, m in events if lvl == "error"]
    finally:
        supply.shutdown()
