"""The SWEEPS (ramp_frequency / ramp_power / ramp_phase): software ramps for
fly scans.

The service walks a knob in small steps (softramp.py) at a set pace and
records every value it sent; a fly scan bins by that COMMANDED value.
Checked here: for each knob the pace and the steps, the end exactly on the
target, ramp_stop and a set taking over, the RF output never touched; fine
power keeping the LEVEL through a frequency sweep and making the delivered
level follow a power sweep; the refusals (power without fine power, phase on
a unit without it); the record in the stream format; the verbs over the wire
(ports 17134/17135, this module's own).
"""

import time

import pytest

from dssg import vernier_cal
from dssg.config import Config
from dssg.sim_system import build_sim_system

#: knob -> (start value in the sim, target, pace in wire units per second)
#: Each sweep lasts ~0.4-0.5 s.
KNOBS = {"frequency": (1.0e9, 1.05e9, 100e6),
         "power": (-10.0, -6.0, 10.0),
         "phase": (0.0, 40.0, 100.0)}


class Recorder:
    """Wraps the simulated unit and records every command it gets."""

    def __init__(self, inner):
        self._inner = inner
        self.sets = {"frequency": [], "power": [], "phase": [], "vernier": [],
                     "output": []}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _rec(self, what, v):
        self.sets[what].append((time.monotonic(), v))
        getattr(self._inner, f"set_{what}")(v)

    def set_frequency(self, hz):
        self._rec("frequency", hz)

    def set_power(self, dBm):
        self._rec("power", dBm)

    def set_phase(self, deg):
        self._rec("phase", deg)

    def set_vernier(self, n):
        self._rec("vernier", n)

    def set_output(self, on):
        self._rec("output", on)

    @property
    def freqs(self):
        return self.sets["frequency"]


def _make(fine=True, rf_on=False, has_phase=True):
    cfg = Config()
    cfg.hardware.poll_hz = 20.0
    cfg.hardware.ramp_dt_s = 0.01
    cfg.hardware.fine_power = fine
    cfg.sim.state_frequency_Hz = 1.0e9
    cfg.sim.state_rf_on = rf_on
    cfg.sim.has_phase = has_phase
    s, backend = build_sim_system(cfg)
    rec = Recorder(backend)
    s.backend = rec
    s.events = []
    s._on_event = lambda lvl, msg: s.events.append((lvl, msg))
    s.start()
    s.rec = rec
    return s


@pytest.fixture
def synth():
    s = _make()
    yield s
    s.shutdown()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_frequency_sweep_walks_at_the_pace_and_ends_on_the_target(synth):
    synth.stream_start()
    t0 = time.monotonic()
    rid = synth.ramp_frequency(1.1e9, 200e6)          # 100 MHz at 200 MHz/s = 0.5 s
    assert rid == 1
    sw = synth.status().sweep
    assert sw["frequency_ramp_id"] == 1 and sw["frequency_ramping"] and sw["ramping"]
    assert _wait(lambda: not synth.status().sweep["frequency_ramping"])
    assert 0.4 < time.monotonic() - t0 < 1.5
    hz = [f for _, f in synth.rec.freqs]
    assert len(hz) >= 20 and hz == sorted(hz)         # many small steps, up only
    assert hz[-1] == 1.1e9
    assert _wait(lambda: synth.status().frequency_Hz == 1.1e9)   # read back
    chunk = synth.stream_stop()
    vals = chunk["values"]["frequency"]
    assert vals[0] == 1.0e9 and vals[-1] == 1.1e9     # from rest to the end
    assert len(chunk["t_ch"]["frequency"]) == len(vals) >= 20
    assert any("frequency sweep done" in m for _, m in synth.events)
    # no event per step: a sweep must not flood the log
    assert sum("frequency =" in m for _, m in synth.events) == 0


def test_power_sweep_delivers_the_ramp_through_fine_power(synth):
    """Fine power: every step re-splits attenuator + vernier, so the
    delivered level follows the ramp -- most steps move the vernier only,
    the attenuator only when the level crosses into its next step."""
    assert synth.fine_power()
    synth.set_power(-10.0)
    n_att0 = len(synth.rec.sets["power"])
    synth.stream_start()
    t0 = time.monotonic()
    assert synth.ramp_power(-6.0, 10.0) == 1          # 4 dB at 10 dB/s = 0.4 s
    delivered = []
    while synth.status().sweep["power_ramping"]:
        delivered.append(synth._delivered(synth._att, synth._vernier, synth._freq))
        time.sleep(0.02)
    assert 0.3 < time.monotonic() - t0 < 1.5
    assert synth._power == -6.0
    assert abs(synth._delivered(synth._att, synth._vernier, synth._freq) + 6.0) <= 0.05
    assert _wait(lambda: abs(synth.status().power_dBm + 6.0) <= 0.05)  # read back
    # the delivered level rose steadily (never by more than a step's worth)
    assert len(delivered) >= 5
    assert all(b >= a - 0.06 for a, b in zip(delivered, delivered[1:]))
    # vernier commands on most steps, few attenuator commands (4 dB / 0.5 dB)
    n_att = len(synth.rec.sets["power"]) - n_att0
    assert len(synth.rec.sets["vernier"]) > n_att and n_att <= 10
    chunk = synth.stream_stop()
    vals = chunk["values"]["power"]
    assert vals[0] == -10.0 and vals[-1] == -6.0
    assert vals == sorted(vals)
    # the calibration-free model agrees with the split it was given
    att, n = vernier_cal.split(-6.0, synth._step(), synth._freq,
                               *synth._power_lims(), cal=synth._cal)
    assert (att, n) == (synth._att, synth._vernier)


def test_phase_sweep(synth):
    synth.stream_start()
    t0 = time.monotonic()
    assert synth.ramp_phase(40.0, 100.0) == 1
    assert _wait(lambda: not synth.status().sweep["phase_ramping"])
    assert 0.3 < time.monotonic() - t0 < 1.5
    deg = [v for _, v in synth.rec.sets["phase"]]
    assert len(deg) >= 15 and deg == sorted(deg) and deg[-1] == 40.0
    assert _wait(lambda: synth.status().phase_deg == 40.0)
    chunk = synth.stream_stop()
    assert chunk["values"]["phase"][0] == 0.0 and chunk["values"]["phase"][-1] == 40.0


@pytest.mark.parametrize("knob", list(KNOBS))
def test_ramp_stop_and_a_set_take_over(synth, knob):
    start, to, rate = KNOBS[knob]
    far = start + 20 * (to - start)
    synth.ramp(knob, far, rate)                       # ~8 s
    time.sleep(0.15)
    assert synth.ramp_stop(knob) is True
    assert not synth.status().sweep[f"{knob}_ramping"]
    here = getattr(synth, {"frequency": "_freq", "power": "_power", "phase": "_phase"}[knob])
    assert here != start and abs(here - start) < abs(far - start) / 2
    n = sum(len(v) for v in synth.rec.sets.values())
    time.sleep(0.1)
    assert sum(len(v) for v in synth.rec.sets.values()) == n    # nothing after the stop
    assert synth.ramp_stop(knob) is False
    # an ordinary set of the knob takes it over
    synth.ramp(knob, far, rate)
    time.sleep(0.1)
    getattr(synth, f"set_{knob}")(start)
    assert not synth.status().sweep[f"{knob}_ramping"]
    assert any(f"{knob} sweep stopped by a {knob} set" in m for _, m in synth.events)
    # without a knob, ramp_stop stops every sweep
    synth.ramp(knob, far, rate)
    assert synth.ramp_stop() is True
    assert not synth.status().sweep["ramping"]


def test_a_set_of_another_knob_leaves_a_sweep_running(synth):
    synth.ramp_frequency(2.0e9, 100e6)
    time.sleep(0.05)
    synth.set_phase(10.0)
    assert synth.status().sweep["frequency_ramping"]
    synth.ramp_stop()


def test_rf_output_is_never_touched():
    for rf_on in (True, False):
        s = _make(rf_on=rf_on)
        try:
            for knob, (start, to, rate) in KNOBS.items():
                s.ramp(knob, to, rate * 4)
            assert _wait(lambda: not s.status().sweep["ramping"])
            s.ramp("power", -15.0, 1.0)
            s.ramp_stop()
            assert s.rec.sets["output"] == []         # never sent OUTPUT
            assert _wait(lambda: s.status().rf_on is rf_on)
        finally:
            s.shutdown()


def test_fine_power_keeps_the_level_through_a_frequency_sweep(synth):
    """With fine power the vernier's dB per count changes with frequency, so
    every step re-splits the SAME asked power: the delivered level stays."""
    synth.set_power(-7.3)
    synth.ramp_frequency(4.0e9, 10e9)                 # 3 GHz in 0.3 s
    assert _wait(lambda: not synth.status().sweep["frequency_ramping"])
    assert _wait(lambda: abs(synth.status().power_dBm - (-7.3)) <= 0.05)
    assert synth._power == -7.3


def test_refusals():
    s = _make(fine=False, has_phase=False)
    try:
        with pytest.raises(ValueError, match="fine power"):
            s.ramp_power(-5.0, 1.0)
        with pytest.raises(ValueError, match="no phase"):
            s.ramp_phase(10.0, 1.0)
        from dssg.net.describe import build_manifest
        by = {p["id"]: p for p in build_manifest(s)["parameters"]}
        assert "ramp" not in by["power"] and "phase" not in by
        assert "ramp" in by["frequency"]
    finally:
        s.shutdown()


def test_clamps_and_bad_requests(synth):
    with pytest.raises(ValueError):
        synth.ramp_frequency(1.2e9, 0.0)
    with pytest.raises(ValueError):
        synth.ramp_phase(float("nan"), 1.0)
    with pytest.raises(ValueError):
        synth.ramp("vernier", 3, 1.0)
    synth.ramp_frequency(99e9, 1e15)                  # beyond the band, absurd pace
    assert any(lvl == "warn" and "clamped" in m for lvl, m in synth.events)
    sw = synth.status().sweep
    assert sw["frequency_ramp_rate_Hz_per_s"] == synth.cfg.limits.ramp_rate_max_Hz_per_s
    assert sw["frequency_ramp_target_Hz"] == synth.limits()["freq_max_Hz"]
    synth.ramp_power(99.0, 1e-9)                      # above the unit's range, too slow
    sw = synth.status().sweep
    assert sw["power_ramp_target_dBm"] == synth.limits()["power_max_dBm"]
    assert sw["power_ramp_rate_dB_per_s"] == synth.cfg.limits.ramp_rate_min_dB_per_s
    synth.ramp_stop()


def test_describe_offers_a_ramp_on_each_knob():
    from dssg.net.describe import build_manifest
    s, _ = build_sim_system(Config())
    by = {p["id"]: p for p in build_manifest(s)["parameters"]}
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
    assert by["frequency"]["scale"] == 1e6


def test_sweep_over_the_wire():
    zmq = pytest.importorskip("zmq")
    from dssg.net.service import DssgService
    cfg = Config()
    cfg.hardware.poll_hz = 20.0
    cfg.hardware.ramp_dt_s = 0.01
    synth, _ = build_sim_system(cfg)
    svc = DssgService(synth, host="127.0.0.1", cmd_port=17134, pub_port=17135,
                      status_hz=20.0)
    svc.start()
    ctx = zmq.Context.instance()
    s = ctx.socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, 3000)
    s.connect("tcp://127.0.0.1:17134")

    def ask(**msg):
        s.send_json(msg)
        return s.recv_json()

    try:
        st = ask(cmd="status")["status"]
        f0 = st["frequency_Hz"]
        assert st["ramping"] is False and st["power_ramp_id"] == 0
        assert ask(cmd="stream_start")["ok"]
        r = ask(cmd="ramp_frequency", frequency_Hz=f0 + 50e6, rate_Hz_per_s=500e6)
        assert r["ok"] and r["ramp_id"] == 1
        assert _wait(lambda: (lambda st: st["frequency_ramp_id"] == 1
                              and not st["frequency_ramping"])(ask(cmd="status")["status"]))
        c = ask(cmd="stream_stop")["stream"]
        assert c["values"]["frequency"][-1] == f0 + 50e6 and "now" in c
        assert set(c["t_ch"]) == {"frequency", "power", "phase"}
        r = ask(cmd="ramp_power", power_dBm=-5.0, rate_dB_per_s=1.0)
        assert r["ok"] and r["ramp_id"] == 1
        assert ask(cmd="ramp_stop", knob="power") == {"ok": True, "stopped": True}
        assert ask(cmd="ramp_phase", phase_deg=10.0, rate_deg_per_s=100.0)["ok"]
        assert ask(cmd="ramp_stop")["ok"]
        assert ask(cmd="ramp_stop") == {"ok": True, "stopped": False}
        assert ask(cmd="ramp_frequency", frequency_Hz=1e9)["ok"] is False   # no rate
        assert ask(cmd="ramp_stop", knob="volume")["ok"] is False
    finally:
        s.close(0)
        svc.stop()
