"""The SWEEPS (ramp_frequency / ramp_power): software ramps for fly scans.

The service walks a knob in small steps (softramp.py) at a set pace, one
tg_cw per step through the backend, and records every value it sent; a fly
scan bins by that COMMANDED value. Checked here, for each knob: it sweeps to
its end at the pace, ramp_stop ends it, an ordinary set takes it over, the CW
is never switched (and a parked TG never unparked), the record in the stream
format, a refusing / silent owner ends the sweep with an error, clamping,
describe, and the verbs over the wire.

Ports (this module's own, never the real 5587/5588 or 5625/5626): 17670/17671
for the shsg service, 17672/17673 for the fake signalhound owner.
"""

from __future__ import annotations

import time

import pytest

from fake_owner import FakeOwner
from shsg.config import Config
from shsg.generator import Generator, Refused
from shsg.sim_system import build_sim_system

SVC_CMD, SVC_PUB = 17670, 17671
OWN_CMD, OWN_PUB = 17672, 17673

#: knob -> (start value in the sim, target, pace in wire units per second,
#: the backend's set_cw argument). Each sweep lasts ~0.4 s.
KNOBS = {"frequency": (1.0e9, 1.04e9, 100e6, "freq_hz"),
         "power": (-20.0, -16.0, 10.0, "level_dbm")}
ECHO = {"frequency": "frequency_Hz", "power": "power_dBm"}


class Recorder:
    """Wraps the simulated TG44A and records every set_cw it gets, with time."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = []                       # (monotonic, kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def set_cw(self, **kw):
        self._inner.set_cw(**kw)              # raises like the owner would
        self.calls.append((time.monotonic(), dict(kw)))

    def sets(self, key):
        return [(t, kw[key]) for t, kw in self.calls if kw.get(key) is not None]

    def switched(self):
        """Every call that switched the output (an `on` argument)."""
        return [kw for _, kw in self.calls if kw.get("on") is not None]


def _make(rf_on=True):
    cfg = Config()
    cfg.hardware.ramp_dt_s = 0.01
    cfg.signal.rf_on = rf_on                # the fake TG's state at connect
    g, backend = build_sim_system(cfg)
    rec = Recorder(backend)
    g.backend = rec
    g.events = []
    g._on_event = lambda lvl, msg: g.events.append((lvl, msg))
    g.start()
    g.rec = rec
    g.sim = backend
    return g


@pytest.fixture
def gen():
    g = _make(rf_on=True)
    yield g
    g.cfg.hardware.off_on_shutdown = False
    g.shutdown()


@pytest.fixture
def parked():
    g = _make(rf_on=False)
    yield g
    g.cfg.hardware.off_on_shutdown = False
    g.shutdown()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("knob", list(KNOBS))
def test_each_knob_sweeps_to_its_end_at_the_pace(gen, knob):
    start, to, rate, key = KNOBS[knob]
    gen.stream_start()
    t0 = time.monotonic()
    rid = gen.ramp(knob, to, rate)
    assert rid == 1
    sw = gen.status().sweep
    assert sw[f"{knob}_ramping"] and sw["ramping"] and sw[f"{knob}_ramp_id"] == 1
    assert _wait(lambda: not gen.status().sweep[f"{knob}_ramping"])
    took = time.monotonic() - t0
    assert 0.3 < took < 1.5, took                     # |to-start|/rate = 0.4 s
    vals = [v for _, v in gen.rec.sets(key)]
    assert len(vals) >= 15 and vals == sorted(vals)   # many small steps, one way
    assert vals[-1] == to
    # one knob per step: never the other knob, never the output
    assert all(set(kw) == {key} for _, kw in gen.rec.calls)
    assert getattr(gen.status(), ECHO[knob]) == to
    # the record: from rest to the end, one channel per knob, own time stamps
    chunk = gen.stream_stop()
    rec = chunk["values"][knob]
    assert rec[0] == start and rec[-1] == to
    assert len(chunk["t_ch"][knob]) == len(rec) >= 15
    for other in KNOBS:                               # the other: at rest
        assert len(chunk["values"][other]) == len(chunk["t_ch"][other]) >= 1
    assert "now" in chunk and chunk["delay_s"][knob] == 0.0
    assert any(f"{knob} sweep done" in m for _, m in gen.events)
    # no event per step: a sweep must not flood the log
    assert sum("requested" in m for _, m in gen.events) == 0


@pytest.mark.parametrize("knob", list(KNOBS))
def test_ramp_stop_ends_it_where_it_is(gen, knob):
    start, to, rate, key = KNOBS[knob]
    gen.ramp(knob, start + 3 * (to - start), rate / 4)   # ~4.8 s
    time.sleep(0.15)
    assert gen.ramp_stop(knob) is True
    assert not gen.status().sweep[f"{knob}_ramping"]
    n = len(gen.rec.calls)
    here = gen.rec.sets(key)[-1][1]
    assert here != start and abs(here - start) < abs(to - start)   # part-way
    time.sleep(0.1)
    assert len(gen.rec.calls) == n                    # nothing after the stop
    assert gen.ramp_stop(knob) is False
    # without a knob: every sweep stops
    gen.ramp(knob, to, rate / 10)
    assert gen.ramp_stop() is True
    assert not gen.status().sweep["ramping"]


@pytest.mark.parametrize("knob", list(KNOBS))
def test_an_ordinary_set_takes_the_knob_over(gen, knob):
    start, to, rate, key = KNOBS[knob]
    gen.ramp(knob, start + 3 * (to - start), rate / 4)
    time.sleep(0.15)
    getattr(gen, f"set_{knob}")(start)
    assert not gen.status().sweep[f"{knob}_ramping"]
    n = len(gen.rec.calls)
    time.sleep(0.1)
    assert len(gen.rec.calls) == n
    assert gen.rec.sets(key)[-1][1] == start
    assert any("stopped by a" in m for _, m in gen.events)


def test_a_set_of_the_other_knob_leaves_a_sweep_running(gen):
    gen.ramp("frequency", 2.0e9, 100e6)
    time.sleep(0.05)
    gen.set_power(-18.0)
    assert gen.status().sweep["frequency_ramping"]
    gen.ramp_stop()


def test_the_cw_is_never_switched_by_a_sweep(gen):
    assert gen.status().rf_on is True                 # adopted: CW on
    for knob, (start, to, rate, key) in KNOBS.items():
        gen.ramp(knob, to, rate * 2)
    assert _wait(lambda: not gen.status().sweep["ramping"])
    gen.ramp("power", -25.0, 1.0)
    gen.ramp_stop()
    assert gen.rec.switched() == []                   # never an `on`
    assert gen.status().rf_on is True
    # a CW on/off is a new instruction: it ends a running sweep first
    gen.ramp("frequency", 2.0e9, 100e6)
    time.sleep(0.05)
    gen.rf_off()
    assert not gen.status().sweep["ramping"]
    assert gen.rec.switched() == [{"on": False}]


def test_a_parked_tg_stays_parked_and_only_the_setting_walks(parked):
    g = parked
    st = g.status()
    assert st.parked and not st.rf_on
    g.stream_start()
    g.ramp("frequency", 1.04e9, 100e6)
    assert any(lvl == "warn" and "parked" in m for lvl, m in g.events)
    assert _wait(lambda: not g.status().sweep["ramping"])
    # the record walked ...
    rec = g.stream_stop()["values"]["frequency"]
    assert rec[0] == 1.0e9 and rec[-1] == 1.04e9 and len(rec) >= 15
    # ... but nothing was sent step by step, only the end value ONCE (so the
    # CW setting is right when CW goes on), and never an `on`
    assert [kw for _, kw in g.rec.calls] == [{"freq_hz": 1.04e9}]
    st = g.status()
    assert st.parked and not st.rf_on and st.frequency_Hz == 1.04e9
    assert g.rec.switched() == []


def test_refused_while_busy_or_unknown_and_busy_mid_sweep_ends_it(gen):
    gen.sim.simulate_sweep(True)                      # an SNA sweep holds the TG
    with pytest.raises(Refused):
        gen.ramp("frequency", 1.1e9, 100e6)
    gen.sim.simulate_sweep(False)
    gen.sim.simulate_unknown()
    with pytest.raises(Refused):
        gen.ramp("power", -15.0, 1.0)
    gen.set_frequency(1.0e9)                          # an explicit set: known again
    assert not gen.status().sweep["ramping"]
    # busy in the middle of a sweep: the walk ends with an error, no hang
    gen.events.clear()
    gen.ramp("frequency", 1.5e9, 10e6)
    time.sleep(0.1)
    gen.sim.simulate_sweep(True)
    assert _wait(lambda: not gen.status().sweep["frequency_ramping"], 2.0)
    assert any(lvl == "error" and "frequency sweep ended" in m and "tg_busy" in m
               for lvl, m in gen.events)
    gen.sim.simulate_sweep(False)


def test_clamps_and_refusals(gen):
    with pytest.raises(ValueError):
        gen.ramp_frequency(1.2e9, 0.0)
    with pytest.raises(ValueError):
        gen.ramp_power(float("nan"), 1.0)
    with pytest.raises(ValueError):
        gen.ramp_power(-15.0, float("inf"))
    with pytest.raises(ValueError):
        gen.ramp("phase", 1.0, 1.0)                   # the TG has no phase
    gen.ramp_frequency(99e9, 1e15)                    # beyond the band, absurd pace
    assert any(lvl == "warn" and "clamped" in m for lvl, m in gen.events)
    sw = gen.status().sweep
    assert sw["frequency_ramp_rate_Hz_per_s"] == gen.cfg.limits.ramp_rate_max_Hz_per_s
    assert sw["frequency_ramp_target_Hz"] == gen.cfg.limits.freq_max_Hz
    gen.ramp_power(50.0, 1e-9)                        # above the level limit, too slow
    sw = gen.status().sweep
    assert sw["power_ramp_target_dBm"] == gen.cfg.limits.power_max_dBm
    assert sw["power_ramp_rate_dB_per_s"] == gen.cfg.limits.ramp_rate_min_dB_per_s
    gen.ramp_stop()


def test_describe_offers_a_ramp_on_each_knob():
    from shsg.net.describe import build_manifest
    g, _ = build_sim_system(Config())
    by = {p["id"]: p for p in build_manifest(g)["parameters"]}
    for knob, unit, to_arg, rate_arg in (("frequency", "MHz/s", "frequency_Hz", "rate_Hz_per_s"),
                                         ("power", "dB/s", "power_dBm", "rate_dB_per_s")):
        r = by[knob]["ramp"]
        assert r["kind"] == "software"
        assert r["rate"]["unit"] == unit
        assert r["start"] == {"verb": f"ramp_{knob}", "args": {"to": to_arg, "rate": rate_arg}}
        assert r["stop"] == {"verb": "ramp_stop", "extra": {"knob": knob}}
        assert r["readback"] == {"stream": {"group": "ramp", "channel": knob},
                                 "measured": False}
        assert r["done"] == {"key": f"{knob}_ramping", "id_key": f"{knob}_ramp_id"}
        assert r["rate"]["min"] <= r["rate"]["default"] <= r["rate"]["max"]
    # scaled like the set: MHz in the scan, Hz on the wire
    assert by["frequency"]["scale"] == 1e6
    assert by["frequency"]["ramp"]["rate"]["max"] == Config().limits.ramp_rate_max_Hz_per_s / 1e6
    assert by["ramping"]["read_path"] == ["ramping"]
    assert "ramp" not in by["rf_on"]                  # switching is never swept


def test_sweeps_over_the_wire():
    zmq = pytest.importorskip("zmq")
    from shsg.net.service import ShsgService
    cfg = Config()
    cfg.hardware.ramp_dt_s = 0.01
    cfg.hardware.off_on_shutdown = False
    cfg.signal.rf_on = True
    g, _ = build_sim_system(cfg)
    svc = ShsgService(g, host="127.0.0.1", cmd_port=SVC_CMD, pub_port=SVC_PUB,
                      status_hz=20.0)
    svc.start()
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, 3000)
    s.connect(f"tcp://127.0.0.1:{SVC_CMD}")

    def ask(**msg):
        s.send_json(msg)
        return s.recv_json()

    try:
        st = ask(cmd="status")["status"]
        assert st["ramping"] is False and st["power_ramp_id"] == 0
        assert ask(cmd="stream_start")["ok"]
        r = ask(cmd="ramp_power", power_dBm=st["power_dBm"] + 2.0, rate_dB_per_s=10.0)
        assert r["ok"] and r["ramp_id"] == 1
        assert _wait(lambda: (lambda st: st["power_ramp_id"] == 1 and not st["power_ramping"])(
            ask(cmd="status")["status"]))
        c = ask(cmd="stream_read")["stream"]
        assert c["values"]["power"][-1] == st["power_dBm"] + 2.0 and "t_ch" in c
        assert ask(cmd="stream_stop")["ok"]
        r = ask(cmd="ramp_frequency", frequency_Hz=st["frequency_Hz"] + 1e6,
                rate_Hz_per_s=1e5)
        assert r["ok"] and r["ramp_id"] == 1
        assert ask(cmd="ramp_stop", knob="frequency") == {"ok": True, "stopped": True}
        assert ask(cmd="ramp_power", power_dBm=-15.0)["ok"] is False      # no rate
        assert ask(cmd="ramp_stop", knob="phase")["ok"] is False
        assert ask(cmd="ramp_stop")["ok"]
        assert ask(cmd="status")["status"]["rf_on"] is True               # untouched
    finally:
        s.close(0)
        svc.stop()


# ---- through the REAL backend, against a fake signalhound owner -------------

def _remote_gen(**limits):
    from shsg.backends.remote_sa import RemoteTG
    cfg = Config()
    cfg.hardware.ramp_dt_s = 0.02
    cfg.hardware.off_on_shutdown = False
    for k, v in limits.items():
        setattr(cfg.limits, k, v)
    backend = RemoteTG("127.0.0.1", OWN_CMD, OWN_PUB, timeout_ms=300, wait_s=3.0)
    g = Generator(backend, cfg)
    g.events = []
    g._on_event = lambda lvl, msg: g.events.append((lvl, msg))
    g.start()
    return g


def test_through_the_owner_each_step_is_a_tg_cw_and_deferred_steps_are_reported():
    owner = FakeOwner(OWN_CMD, OWN_PUB, on=True, freq_hz=1e9, level_dbm=-20.0).start()
    g = _remote_gen()
    try:
        g.stream_start()
        g.ramp("frequency", 1.01e9, 50e6)                 # 0.2 s
        assert _wait(lambda: not g.status().sweep["frequency_ramping"])
        reqs = owner.tg_cw_requests()
        assert len(reqs) >= 5
        assert all(set(r) == {"cmd", "freq_hz"} for r in reqs)   # never `on`
        assert reqs[-1]["freq_hz"] == 1.01e9
        assert g.stream_stop()["values"]["frequency"][-1] == 1.01e9
        # a step the owner accepted but applied later is counted and reported
        owner.defer = True
        g.ramp("power", -19.0, 10.0)
        assert _wait(lambda: not g.status().sweep["power_ramping"])
        assert any(lvl == "warn" and "DEFERRED" in m for lvl, m in g.events)
    finally:
        g.shutdown()
        owner.stop()


def test_an_owner_that_refuses_ends_the_sweep_with_an_error():
    owner = FakeOwner(OWN_CMD, OWN_PUB, on=True, freq_hz=1e9, level_dbm=-12.0).start()
    # our limit wider than the TG's -10 dBm: the OWNER refuses past it
    g = _remote_gen(power_max_dBm=0.0)
    try:
        g.ramp("power", -5.0, 20.0)
        assert _wait(lambda: not g.status().sweep["power_ramping"], 3.0)
        assert any(lvl == "error" and "power sweep ended" in m and "refused" in m
                   for lvl, m in g.events)
    finally:
        g.shutdown()
        owner.stop()


def test_an_owner_that_goes_silent_ends_the_sweep_not_hangs_it():
    owner = FakeOwner(OWN_CMD, OWN_PUB, on=True, freq_hz=1e9, level_dbm=-20.0).start()
    g = _remote_gen()
    try:
        g.ramp("frequency", 1.5e9, 10e6)                  # 50 s
        time.sleep(0.15)
        owner.stop()                                      # the owner dies
        assert _wait(lambda: not g.status().sweep["frequency_ramping"], 5.0)
        assert any(lvl == "error" and "frequency sweep ended" in m for lvl, m in g.events)
    finally:
        g.shutdown()
