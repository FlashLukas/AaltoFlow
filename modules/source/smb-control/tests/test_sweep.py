"""The SWEEPS (ramp_frequency / ramp_power / ramp_phase): software ramps for
fly scans.

The service walks a knob in small steps (softramp.py) at a set pace and
records every value it sent; a fly scan bins by that COMMANDED value.
Checked here, for each knob: it sweeps to its end at the pace, ramp_stop ends
it, an ordinary set takes it over, the RF output is never touched, the record
in the stream format, clamping, and the verbs over the wire (ports
18102/18103, this module's own).
"""

import time

import pytest

from smb.config import Config
from smb.sim_system import build_sim_system

#: knob -> (start value in the sim, target, pace in wire units per second)
#: Each sweep lasts ~0.4 s.
KNOBS = {"frequency": (1.0e9, 1.04e9, 100e6),
         "power": (-30.0, -26.0, 10.0),
         "phase": (0.0, 40.0, 100.0)}
READ = {"frequency": "read_frequency", "power": "read_power", "phase": "read_phase"}


class Recorder:
    """Wraps the simulated SMB100A and records every set it gets (and checks
    a sweep step never asks for the settle pause)."""

    def __init__(self, inner):
        self._inner = inner
        self.sets = {"frequency": [], "power": [], "phase": []}
        self.outputs = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _rec(self, knob, v, settle):
        self.sets[knob].append((time.monotonic(), v, settle))
        getattr(self._inner, f"set_{knob}")(v)

    def set_frequency(self, hz, settle=True):
        self._rec("frequency", hz, settle)

    def set_power(self, dBm, settle=True):
        self._rec("power", dBm, settle)

    def set_phase(self, deg, settle=True):
        self._rec("phase", deg, settle)

    def set_output(self, on):
        self.outputs.append(on)
        self._inner.set_output(on)


@pytest.fixture
def gen():
    cfg = Config()
    cfg.hardware.ramp_dt_s = 0.01
    cfg.signal.rf_on = True             # the fake box is radiating already
    g, backend = build_sim_system(cfg)
    rec = Recorder(backend)
    g.backend = rec
    g.events = []
    g._on_event = lambda lvl, msg: g.events.append((lvl, msg))
    g.start()
    g.rec = rec
    yield g
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
    start, to, rate = KNOBS[knob]
    gen.stream_start()
    t0 = time.monotonic()
    rid = gen.ramp(knob, to, rate)
    assert rid == 1
    sw = gen.status().sweep
    assert sw[f"{knob}_ramping"] and sw["ramping"] and sw[f"{knob}_ramp_id"] == 1
    assert _wait(lambda: not gen.status().sweep[f"{knob}_ramping"])
    took = time.monotonic() - t0
    assert 0.3 < took < 1.5, took                     # |to-start|/rate = 0.4 s
    vals = [v for _, v, _ in gen.rec.sets[knob]]
    assert len(vals) >= 15 and vals == sorted(vals)   # many small steps, one way
    assert vals[-1] == to
    assert all(settle is False for *_, settle in gen.rec.sets[knob])
    assert getattr(gen.backend, READ[knob])() == to
    # the record: from rest to the end, one channel per knob, own time stamps
    chunk = gen.stream_stop()
    rec = chunk["values"][knob]
    assert rec[0] == start and rec[-1] == to
    assert len(chunk["t_ch"][knob]) == len(rec) >= 15
    for other in KNOBS:                               # the others: at rest
        assert len(chunk["values"][other]) == len(chunk["t_ch"][other]) >= 1
    assert "now" in chunk and chunk["delay_s"][knob] == 0.0
    assert any(f"{knob} sweep done" in m for _, m in gen.events)
    # no event per step: a sweep must not flood the log
    assert sum(f"{knob} =" in m for _, m in gen.events) == 0


@pytest.mark.parametrize("knob", list(KNOBS))
def test_ramp_stop_ends_it_where_it_is(gen, knob):
    start, to, rate = KNOBS[knob]
    gen.ramp(knob, start + 10 * (to - start), rate)   # 4 s
    time.sleep(0.15)
    assert gen.ramp_stop(knob) is True
    assert not gen.status().sweep[f"{knob}_ramping"]
    n = len(gen.rec.sets[knob])
    here = gen.rec.sets[knob][-1][1]
    assert here != start and abs(here - start) < 3 * abs(to - start)   # part-way
    time.sleep(0.1)
    assert len(gen.rec.sets[knob]) == n               # nothing after the stop
    assert gen.ramp_stop(knob) is False
    # without a knob: every sweep stops
    gen.ramp(knob, to, rate / 10)
    assert gen.ramp_stop() is True
    assert not gen.status().sweep["ramping"]


@pytest.mark.parametrize("knob", list(KNOBS))
def test_an_ordinary_set_takes_the_knob_over(gen, knob):
    start, to, rate = KNOBS[knob]
    gen.ramp(knob, start + 10 * (to - start), rate)
    time.sleep(0.15)
    getattr(gen, f"set_{knob}")(start)
    assert not gen.status().sweep[f"{knob}_ramping"]
    n = len(gen.rec.sets[knob])
    time.sleep(0.1)
    assert len(gen.rec.sets[knob]) == n
    assert gen.rec.sets[knob][-1][1] == start and gen.rec.sets[knob][-1][2] is True
    assert any("stopped by a" in m for _, m in gen.events)


def test_a_set_of_another_knob_leaves_a_sweep_running(gen):
    gen.ramp("frequency", 2.0e9, 100e6)
    time.sleep(0.05)
    gen.set_power(-20.0)
    assert gen.status().sweep["frequency_ramping"]
    gen.ramp_stop()


def test_rf_output_is_never_touched(gen):
    assert gen.backend.read_output() is True          # adopted: radiating
    for knob, (start, to, rate) in KNOBS.items():
        gen.ramp(knob, to, rate * 4)
    assert _wait(lambda: not gen.status().sweep["ramping"])
    gen.ramp("power", -40.0, 1.0)
    gen.ramp_stop()
    assert gen.rec.outputs == []                      # never sent OUTP:STAT
    assert gen.status().rf_on is True
    # and with RF OFF a sweep does not switch it on either
    gen.set_rf(False)
    gen.ramp("frequency", 1.0e9, 1e9)
    assert _wait(lambda: not gen.status().sweep["ramping"])
    assert gen.rec.outputs == [False] and gen.status().rf_on is False


def test_clamps_and_refusals(gen):
    with pytest.raises(ValueError):
        gen.ramp_frequency(1.2e9, 0.0)
    with pytest.raises(ValueError):
        gen.ramp_power(float("nan"), 1.0)
    with pytest.raises(ValueError):
        gen.ramp("amplitude", 1.0, 1.0)
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
    from smb.net.describe import build_manifest
    g, _ = build_sim_system(Config())
    by = {p["id"]: p for p in build_manifest(g)["parameters"]}
    for knob, unit, to_arg, rate_arg in (("frequency", "MHz/s", "frequency_Hz", "rate_Hz_per_s"),
                                         ("power", "dB/s", "power_dBm", "rate_dB_per_s"),
                                         ("phase", "deg/s", "phase_deg", "rate_deg_per_s")):
        r = by[knob]["ramp"]
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


def test_sweeps_over_the_wire():
    zmq = pytest.importorskip("zmq")
    from smb.net.service import SmbService
    cfg = Config()
    cfg.hardware.ramp_dt_s = 0.01
    g, _ = build_sim_system(cfg)
    svc = SmbService(g, host="127.0.0.1", cmd_port=18102, pub_port=18103, status_hz=20.0)
    svc.start()
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, 3000)
    s.connect("tcp://127.0.0.1:18102")

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
        c = ask(cmd="stream_stop")["stream"]
        assert c["values"]["power"][-1] == st["power_dBm"] + 2.0 and "now" in c
        r = ask(cmd="ramp_frequency", frequency_Hz=st["frequency_Hz"] + 1e6,
                rate_Hz_per_s=1e6)
        assert r["ok"] and r["ramp_id"] == 1
        assert ask(cmd="ramp_stop", knob="frequency") == {"ok": True, "stopped": True}
        assert ask(cmd="ramp_phase", phase_deg=10.0, rate_deg_per_s=100.0)["ok"]
        assert ask(cmd="ramp_stop")["ok"]
        assert ask(cmd="ramp_power", power_dBm=-10.0)["ok"] is False     # no rate
        assert ask(cmd="ramp_stop", knob="volume")["ok"] is False
    finally:
        s.close(0)
        svc.stop()
