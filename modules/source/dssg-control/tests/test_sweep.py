"""The frequency SWEEP (ramp_frequency): a software ramp for fly scans.

The service walks the frequency in small steps (softramp.py) at a set pace and
records every value it sent; a fly scan bins by that COMMANDED frequency.
Checked here: the pace and the steps, the end exactly on the target, a set or
ramp_stop taking over, fine power keeping the LEVEL through the sweep, the
record in the stream format, and the verbs over the wire.
"""

import time

import pytest

from dssg.config import Config
from dssg.sim_system import build_sim_system


class Recorder:
    """Wraps the simulated unit and records every set_frequency it gets."""

    def __init__(self, inner):
        self._inner = inner
        self.freqs = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def set_frequency(self, hz):
        self.freqs.append((time.monotonic(), hz))
        self._inner.set_frequency(hz)


@pytest.fixture
def synth():
    cfg = Config()
    cfg.hardware.poll_hz = 20.0
    cfg.hardware.ramp_dt_s = 0.01
    cfg.sim.state_frequency_Hz = 1.0e9
    s, backend = build_sim_system(cfg)
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


def test_sweep_walks_at_the_pace_and_ends_on_the_target(synth):
    synth.stream_start()
    t0 = time.monotonic()
    rid = synth.ramp_frequency(1.1e9, 200e6)          # 100 MHz at 200 MHz/s = 0.5 s
    assert rid == 1
    st = synth.status()
    assert st.ramp_id == 1 and st.ramping            # live, not a poll later
    assert _wait(lambda: not synth.status().ramping)
    assert 0.4 < time.monotonic() - t0 < 1.5
    hz = [f for _, f in synth.rec.freqs]
    assert len(hz) >= 20 and hz == sorted(hz)         # many small steps, up only
    assert hz[-1] == 1.1e9
    assert _wait(lambda: synth.status().frequency_Hz == 1.1e9)   # read back
    chunk = synth.stream_stop()
    vals = chunk["values"]["frequency"]
    assert vals[0] == 1.0e9 and vals[-1] == 1.1e9     # from rest to the end
    assert len(chunk["t"]) == len(vals) >= 20
    assert any("sweep done" in m for _, m in synth.events)
    # no event per step: a sweep must not flood the log
    assert sum("frequency =" in m for _, m in synth.events) == 0


def test_set_frequency_and_ramp_stop_take_over(synth):
    synth.ramp_frequency(2.0e9, 100e6)                # 10 s
    time.sleep(0.2)
    synth.set_frequency(1.5e9)
    assert not synth.status().ramping
    n = len(synth.rec.freqs)
    time.sleep(0.1)
    assert len(synth.rec.freqs) == n                  # nothing after the set
    assert synth.rec.freqs[-1][1] == 1.5e9
    synth.ramp_frequency(1.0e9, 100e6)
    time.sleep(0.2)
    assert synth.ramp_stop() is True
    here = synth.rec.freqs[-1][1]
    assert 1.45e9 < here < 1.5e9
    time.sleep(0.1)
    assert synth.rec.freqs[-1][1] == here
    assert synth.ramp_stop() is False


def test_fine_power_keeps_the_level_through_a_sweep(synth):
    """With fine power the vernier's dB per count changes with frequency, so
    every step re-splits the SAME asked power: the delivered level stays."""
    if not synth.fine_power():
        pytest.skip("fine power is off in this configuration")
    synth.set_power(-7.3)
    synth.ramp_frequency(4.0e9, 10e9)                 # 3 GHz in 0.3 s
    assert _wait(lambda: not synth.status().ramping)
    assert _wait(lambda: abs(synth.status().power_dBm - (-7.3)) <= 0.05)
    assert synth._power == -7.3


def test_clamps_and_refusals(synth):
    with pytest.raises(ValueError):
        synth.ramp_frequency(1.2e9, 0.0)
    synth.ramp_frequency(99e9, 1e15)                  # beyond the band, absurd pace
    assert any("clamped" in m for _, m in synth.events)
    st = synth.status()
    assert st.ramp_rate_Hz_per_s == synth.cfg.limits.ramp_rate_max_Hz_per_s
    assert st.ramp_target_Hz == synth.limits()["freq_max_Hz"]
    synth.ramp_stop()


def test_describe_offers_the_ramp():
    from dssg.net.describe import build_manifest
    s, _ = build_sim_system(Config())
    f = next(p for p in build_manifest(s)["parameters"] if p["id"] == "frequency")
    r = f["ramp"]
    assert f["scale"] == 1e6 and r["rate"]["unit"] == "MHz/s"
    assert r["readback"] == {"stream": {"group": "ramp", "channel": "frequency"},
                             "measured": False}
    assert r["start"]["args"] == {"to": "frequency_Hz", "rate": "rate_Hz_per_s"}
    assert r["rate"]["min"] <= r["rate"]["default"] <= r["rate"]["max"]


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
        f0 = ask(cmd="status")["status"]["frequency_Hz"]
        assert ask(cmd="stream_start")["ok"]
        r = ask(cmd="ramp_frequency", frequency_Hz=f0 + 50e6, rate_Hz_per_s=500e6)
        assert r["ok"] and r["ramp_id"] == 1
        assert _wait(lambda: (lambda st: st["ramp_id"] == 1 and not st["ramping"])(
            ask(cmd="status")["status"]))
        c = ask(cmd="stream_stop")["stream"]
        assert c["values"]["frequency"][-1] == f0 + 50e6 and "now" in c
        assert ask(cmd="ramp_stop") == {"ok": True, "stopped": False}
        assert ask(cmd="ramp_frequency", frequency_Hz=1e9)["ok"] is False   # no rate
    finally:
        s.close(0)
        svc.stop()
