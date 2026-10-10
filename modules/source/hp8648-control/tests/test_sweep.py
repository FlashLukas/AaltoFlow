"""The SWEEPS (ramp_frequency / ramp_power): software ramps for fly scans.

The service walks a knob in small steps (softramp.py) at a set pace and
records every value it sent; a fly scan bins by that COMMANDED value.
Checked here, for each knob: it sweeps to its end at the pace, ramp_stop ends
it, an ordinary set takes it over, the RF output is never touched, the level
ceiling (spec.py) holds over a frequency sweep, the record in the stream
format, clamping, the describe blocks, and the verbs over the wire (ports
18112/18113, this module's own; test_net / test_describe use 18110/18111).
There is no phase sweep: the 8648D has no phase control.
"""

import threading
import time

import pytest

from hp8648.config import Config
from hp8648.sim_system import build_sim_system

#: knob -> (start value in the sim, target, pace in wire units per second).
#: Each sweep lasts ~0.4 s. The sim keeps 10 Hz and 0.1 dB, like the box.
KNOBS = {"frequency": (1.0e9, 1.04e9, 100e6),
         "power": (-30.0, -26.0, 10.0)}
READ = {"frequency": "read_frequency", "power": "read_power"}


class Recorder:
    """Wraps the simulated 8648D and records every set it gets, with the
    thread that sent it (a sweep step runs on the sweep's own thread; the
    worker thread writes ordinary sets)."""

    def __init__(self, inner):
        self._inner = inner
        self.sets = {"frequency": [], "power": []}
        self.outputs = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _rec(self, knob, v):
        self.sets[knob].append((time.monotonic(), v, threading.current_thread().name))
        getattr(self._inner, f"set_{knob}")(v)

    def set_frequency(self, hz):
        self._rec("frequency", hz)

    def set_power(self, dBm):
        self._rec("power", dBm)

    def set_output(self, on):
        self.outputs.append(on)
        self._inner.set_output(on)


@pytest.fixture
def src():
    cfg = Config()
    cfg.hardware.ramp_dt_s = 0.01
    # the fake box is radiating already when the service connects (adopted)
    s, backend = build_sim_system(cfg, initial={"rf_on": True})
    rec = Recorder(backend)
    s.backend = rec
    s.events = []
    s._on_event = lambda lvl, msg: s.events.append((lvl, msg))
    s.start()
    s.rec = rec
    yield s
    s.shutdown()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _from_sweep(entry, knob):
    return entry[2].startswith(f"hp8648-{knob}-sweep")


@pytest.mark.parametrize("knob", list(KNOBS))
def test_each_knob_sweeps_to_its_end_at_the_pace(src, knob):
    start, to, rate = KNOBS[knob]
    src.stream_start()
    t0 = time.monotonic()
    rid = src.ramp(knob, to, rate)
    assert rid == 1
    sw = src.status().sweep
    assert sw[f"{knob}_ramping"] and sw["ramping"] and sw[f"{knob}_ramp_id"] == 1
    assert _wait(lambda: not src.status().sweep[f"{knob}_ramping"])
    took = time.monotonic() - t0
    assert 0.3 < took < 1.5, took                     # |to-start|/rate = 0.4 s
    sets = src.rec.sets[knob]
    vals = [v for _, v, _ in sets]
    assert len(vals) >= 15 and vals == sorted(vals)   # many small steps, one way
    assert vals[-1] == to
    # every write of the knob came from the sweep: the worker did not step
    # the output back to an older setpoint in between
    assert all(_from_sweep(e, knob) for e in sets)
    assert getattr(src.backend, READ[knob])() == pytest.approx(to)
    # the worker's next snapshot echoes the end value (what a scan reads)
    assert _wait(lambda: abs(getattr(src.status(), f"{knob}_Hz" if knob == "frequency"
                                     else "power_dBm") - to) < 1e-6)
    # the record: from rest to the end, one channel per knob, own time stamps
    chunk = src.stream_stop()
    rec = chunk["values"][knob]
    assert rec[0] == start and rec[-1] == to
    assert len(chunk["t_ch"][knob]) == len(rec) >= 15
    for other in KNOBS:                               # the other: at rest
        assert len(chunk["values"][other]) == len(chunk["t_ch"][other]) >= 1
    assert "now" in chunk and chunk["delay_s"][knob] == 0.0
    assert any(f"{knob} sweep done" in m for _, m in src.events)
    # no event per step: a sweep must not flood the log
    assert sum(f"{knob} =" in m for _, m in src.events) == 0


@pytest.mark.parametrize("knob", list(KNOBS))
def test_ramp_stop_ends_it_where_it_is(src, knob):
    start, to, rate = KNOBS[knob]
    src.ramp(knob, start + 10 * (to - start), rate)   # 4 s
    time.sleep(0.15)
    assert src.ramp_stop(knob) is True
    assert not src.status().sweep[f"{knob}_ramping"]
    n = len(src.rec.sets[knob])
    here = src.rec.sets[knob][-1][1]
    assert here != start and abs(here - start) < 3 * abs(to - start)   # part-way
    time.sleep(0.3)                                   # > one worker cycle
    assert len(src.rec.sets[knob]) == n               # nothing after the stop
    assert src.ramp_stop(knob) is False
    # without a knob: every sweep stops
    src.ramp(knob, to, rate / 10)
    assert src.ramp_stop() is True
    assert not src.status().sweep["ramping"]


@pytest.mark.parametrize("knob", list(KNOBS))
def test_an_ordinary_set_takes_the_knob_over(src, knob):
    start, to, rate = KNOBS[knob]
    src.ramp(knob, start + 10 * (to - start), rate)
    time.sleep(0.15)
    getattr(src, f"set_{knob}")(start)
    assert not src.status().sweep[f"{knob}_ramping"]
    assert src.wait_idle()
    n = len(src.rec.sets[knob])
    time.sleep(0.3)
    assert len(src.rec.sets[knob]) == n
    last = src.rec.sets[knob][-1]
    assert last[1] == start and not _from_sweep(last, knob)   # the worker wrote it
    assert any("stopped by a" in m for _, m in src.events)


def test_a_set_of_the_other_knob_leaves_a_sweep_running(src):
    src.ramp("frequency", 2.0e9, 100e6)
    time.sleep(0.05)
    src.set_power(-20.0)
    assert src.wait_idle()
    assert src.status().sweep["frequency_ramping"]
    src.ramp_stop()


def test_rf_output_is_never_touched(src):
    assert src.backend.read_output() is True          # adopted: radiating
    for knob, (start, to, rate) in KNOBS.items():
        src.ramp(knob, to, rate * 4)
    assert _wait(lambda: not src.status().sweep["ramping"])
    src.ramp("power", -40.0, 1.0)
    src.ramp_stop()
    time.sleep(0.3)
    assert src.rec.outputs == []                      # never sent OUTP:STAT
    assert src.status().rf_on is True
    # and with RF OFF a sweep does not switch it on either
    src.set_rf(False)
    assert src.wait_idle()
    src.ramp("frequency", 1.0e9, 1e9)
    assert _wait(lambda: not src.status().sweep["ramping"])
    time.sleep(0.3)
    assert src.rec.outputs == [False] and src.status().rf_on is False


def test_level_is_lowered_once_before_a_frequency_sweep_crosses_2500_MHz(src):
    """+12 dBm is fine at 1 GHz but above the +10 dBm ceiling over 2500 MHz:
    the level comes down to +10 BEFORE the sweep starts, and stays there."""
    src.set_power(12.0)
    assert src.wait_idle()
    n_pow = len(src.rec.sets["power"])
    src.ramp("frequency", 3.0e9, 4e9)                 # the max pace: 0.5 s
    assert _wait(lambda: not src.status().sweep["ramping"])
    pw = src.rec.sets["power"][n_pow:]
    fr = src.rec.sets["frequency"]
    assert [v for _, v, _ in pw] == [10.0]            # once, to the path ceiling
    assert pw[0][0] < fr[0][0]                        # before the first step
    assert src.backend.read_power() == 10.0
    assert any("before the sweep" in m for lvl, m in src.events if lvl == "warn")
    # a level sweep cannot go above the ceiling at the current frequency
    src.ramp("power", 13.0, 100.0)
    assert any("clamped" in m for lvl, m in src.events if lvl == "warn")
    assert src.status().sweep["power_ramp_target_dBm"] == 10.0
    assert _wait(lambda: not src.status().sweep["ramping"])


def test_reverse_power_trip_stops_the_sweeps(src):
    src.ramp("power", -100.0, 1.0)                    # a long sweep down
    time.sleep(0.05)
    src.backend.inject_reverse_power()
    assert _wait(lambda: not src.status().sweep["ramping"], timeout=2.0)
    assert any("reverse power" in m for _, m in src.events)


def test_clamps_and_refusals(src):
    with pytest.raises(ValueError):
        src.ramp_frequency(1.2e9, 0.0)
    with pytest.raises(ValueError):
        src.ramp_power(float("nan"), 1.0)
    with pytest.raises(ValueError):
        src.ramp_frequency(1.2e9, float("inf") - float("inf"))   # NaN pace
    with pytest.raises(ValueError):
        src.ramp("phase", 10.0, 1.0)                  # the 8648D has no phase
    src.ramp_frequency(99e9, 1e15)                    # beyond the band, absurd pace
    assert any(lvl == "warn" and "clamped" in m for lvl, m in src.events)
    sw = src.status().sweep
    assert sw["frequency_ramp_rate_Hz_per_s"] == src.cfg.limits.ramp_rate_max_Hz_per_s
    assert sw["frequency_ramp_target_Hz"] == src.freq_limits()[1]
    src.ramp_stop()
    src.ramp_power(-200.0, 1e-9)                      # below the floor, too slow
    sw = src.status().sweep
    assert sw["power_ramp_target_dBm"] == src.power_floor()
    assert sw["power_ramp_rate_dB_per_s"] == src.cfg.limits.ramp_rate_min_dB_per_s
    src.ramp_stop()


def test_describe_offers_a_ramp_on_each_knob():
    from hp8648.net.describe import build_manifest
    s, _ = build_sim_system(Config())
    by = {p["id"]: p for p in build_manifest(s)["parameters"]}
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
    assert "ramp" not in by["rf_on"]                  # RF is never swept


def test_sweeps_over_the_wire():
    zmq = pytest.importorskip("zmq")
    from hp8648.net.service import Hp8648Service
    cfg = Config()
    cfg.hardware.ramp_dt_s = 0.01
    s, _ = build_sim_system(cfg)
    svc = Hp8648Service(s, host="127.0.0.1", cmd_port=18112, pub_port=18113, status_hz=20.0)
    svc.start()
    sock = zmq.Context.instance().socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, 3000)
    sock.connect("tcp://127.0.0.1:18112")

    def ask(**msg):
        sock.send_json(msg)
        return sock.recv_json()

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
        assert ask(cmd="ramp_stop")["ok"]
        assert ask(cmd="ramp_power", power_dBm=-10.0)["ok"] is False      # no rate
        assert ask(cmd="ramp_power", power_dBm=-10.0, rate_dB_per_s=0)["ok"] is False
        assert ask(cmd="ramp_stop", knob="phase")["ok"] is False          # no phase
        assert ask(cmd="ramp_phase", phase_deg=1.0, rate_deg_per_s=1.0)["ok"] is False
    finally:
        sock.close(0)
        svc.stop()
