"""The frequency SWEEP (ramp_frequency): a software ramp for fly scans.

softramp.py walks the synthesiser frequency at the asked pace; the poll
thread -- faster while it runs -- records every REF OUT reading, so a fly scan
bins by the MEASURED wheel frequency (by the commanded one, declared honestly,
while REF OUT is on 'target'). Real time and real threads here (the walk has
its own thread), the simulated MC2000B on the 10/100 blade's inner ring.
Checked: the pace and the end, never "locked" while it moves and locked after,
the stream, ramp_stop, a set and standby taking over, external reference
refusing, clamping, the blind case, describe and the wire.
"""

import time

import pytest

from chopper.config import Config
from chopper.sim_system import build_sim_system

# This module's own test-port block is 17320..17339 (test_net 17320/1,
# 17324/5, 17339); the sweep's wire test takes 17328/9.
WIRE_CMD, WIRE_PUB = 17328, 17329


class Spy:
    """Wraps the simulated MC2000B and records every frequency written."""

    def __init__(self, inner):
        self._inner = inner
        self.freqs = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def set_frequency(self, hz):
        self.freqs.append(hz)
        self._inner.set_frequency(hz)


def _make(**sim):
    cfg = Config()
    for k, v in sim.items():
        setattr(cfg.sim, k, v)
    cfg.sim.jitter_rel = 0.0
    cfg.hardware.ramp_dt_s = 0.02
    cfg.hardware.stream_poll_hz = 20.0
    cfg.settle.hold_s = 0.3
    ch, be = build_sim_system(cfg, seed=1)
    ch.backend = Spy(be)
    ch.events = []
    ch._on_event = lambda lvl, msg: ch.events.append((lvl, msg))
    ch.start()
    return ch


@pytest.fixture
def ch():
    c = _make()
    yield c
    c.shutdown()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_sweep_walks_at_the_pace_ends_on_the_target_and_locks_after(ch):
    assert _wait(lambda: ch.status().locked)
    t0 = time.monotonic()
    rid = ch.ramp_frequency(200.0, 100.0)              # 150 -> 200 Hz at 100 Hz/s
    assert rid == 1
    st = ch.status()
    assert st.ramping and st.ramp_id == 1 and not st.locked
    assert st.ramp_target_Hz == 200.0 and st.ramp_rate_Hz_per_s == 100.0
    saw_locked = False
    while ch.status().ramping and time.monotonic() - t0 < 3.0:
        saw_locked |= ch.status().locked
        time.sleep(0.01)
    assert not saw_locked                              # never locked while it moves
    assert 0.4 < time.monotonic() - t0 < 2.0
    assert ch.status().setpoint_frequency_Hz == 200.0
    f = ch.backend.freqs
    assert len(f) >= 10 and f == sorted(f) and f[-1] == 200.0
    # every write is a NEW grid value (0.1 Hz on this blade): no repeats
    assert len(set(f)) == len(f)
    assert _wait(lambda: ch.status().locked)           # judged afresh at the end
    assert abs(ch.status().frequency_Hz - 200.0) < 1.0


def test_the_stream_records_the_measured_wheel(ch):
    assert ch.stream_start() == 1
    ch.ramp_frequency(250.0, 50.0)                     # 2 s
    assert _wait(lambda: not ch.status().ramping)
    time.sleep(2.5)                                    # the wheel catches up (tau 0.6 s)
    c = ch.stream_stop()
    t, f = c["t"], c["values"]["frequency"]
    assert len(t) == len(f) >= 40 and t == sorted(t) and "now" in c
    assert f[0] < 160.0 and f[-1] == pytest.approx(250.0, abs=1.0)
    # the COMMANDED frequency: every value the sweep sent, on its own stamps
    tc, cmd = c["t_ch"]["commanded"], c["values"]["commanded"]
    assert len(tc) == len(cmd) >= 50 and cmd[-1] == 250.0
    # the wheel LAGS the command (rate x tau ~ 30 Hz): the reading taken when
    # the command passed 240 Hz is well below it
    tk = next(tt for tt, v in zip(tc, cmd) if v >= 240.0)
    fk = next(v for tt, v in zip(t, f) if tt >= tk)
    assert fk < 235.0


def test_ramp_stop_and_a_set_and_standby_take_over(ch):
    ch.ramp_frequency(500.0, 50.0)
    time.sleep(0.4)
    assert ch.ramp_stop() is True
    here = ch.status().setpoint_frequency_Hz
    assert 160.0 < here < 180.0
    time.sleep(0.2)
    assert ch.status().setpoint_frequency_Hz == here and not ch.status().ramping
    assert ch.ramp_stop() is False
    ch.ramp_frequency(500.0, 50.0)
    time.sleep(0.2)
    ch.set_frequency(300.0)
    assert not ch.status().ramping and ch.status().setpoint_frequency_Hz == 300.0
    time.sleep(0.2)
    assert ch.status().setpoint_frequency_Hz == 300.0
    ch.ramp_frequency(500.0, 50.0)
    time.sleep(0.1)
    ch.standby()
    assert not ch.status().ramping
    assert any("stopped by standby" in m for _, m in ch.events)


def test_external_reference_refuses_a_sweep(ch):
    ch.standby()
    refs, _ = ch.mode_options()
    ch.set_ref_mode(next(r for r in refs if r.startswith("ext")))
    with pytest.raises(ValueError):
        ch.ramp_frequency(300.0, 10.0)


def test_clamps_and_refusals(ch):
    with pytest.raises(ValueError):
        ch.ramp_frequency(300.0, 0.0)
    with pytest.raises(ValueError):
        ch.ramp_frequency(float("nan"), 1.0)
    ch.ramp_frequency(1e6, 1e6)
    st = ch.status()
    assert st.ramp_target_Hz == ch.freq_limits()[1]
    assert st.ramp_rate_Hz_per_s == ch.cfg.limits.sweep_rate_max_Hz_per_s
    assert any("clamped" in m for lvl, m in ch.events if lvl == "warn")
    ch.ramp_stop()


def test_describe_bins_by_the_wheel_or_honestly_by_the_command():
    from chopper.net.describe import build_manifest
    c = _make()
    try:
        p = {d["id"]: d for d in build_manifest(c)["parameters"]}
        r = p["frequency"]["ramp"]
        lim = c.cfg.limits
        assert r["kind"] == "software"
        assert r["start"] == {"verb": "ramp_frequency",
                              "args": {"to": "frequency_Hz", "rate": "rate_Hz_per_s"}}
        assert r["rate"]["unit"] == "Hz/s" and r["rate"]["max"] == lim.sweep_rate_max_Hz_per_s
        assert r["readback"] == {"stream": {"group": "wheel", "channel": "frequency"},
                                 "measured": True}
        assert r["done"] == {"key": "ramping", "id_key": "ramp_id"}
        assert p["measured_frequency"]["stream"] == {"group": "wheel", "channel": "frequency"}
        assert p["ramp_stop"]["kind"] == "action"
    finally:
        c.shutdown()
    # REF OUT on 'target': the wheel is invisible, so binned by the command
    c = _make(output_mode="target")
    try:
        p = {d["id"]: d for d in build_manifest(c)["parameters"]}
        assert p["frequency"]["ramp"]["readback"] == {
            "stream": {"group": "wheel", "channel": "commanded"}, "measured": False}
        c.stream_start()
        c.ramp_frequency(160.0, 50.0)
        assert _wait(lambda: not c.status().ramping)
        chunk = c.stream_stop()
        assert chunk["values"]["commanded"][-1] == 160.0
    finally:
        c.shutdown()


def test_sweep_over_the_wire():
    zmq = pytest.importorskip("zmq")
    from chopper.net.service import ChopperService
    cfg = Config()
    cfg.hardware.ramp_dt_s = 0.02
    ch, _ = build_sim_system(cfg, seed=2)
    svc = ChopperService(ch, host="127.0.0.1", cmd_port=WIRE_CMD, pub_port=WIRE_PUB)
    svc.start()
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, 3000)
    s.connect(f"tcp://127.0.0.1:{WIRE_CMD}")

    def ask(**msg):
        s.send_json(msg)
        return s.recv_json()

    try:
        f0 = ask(cmd="status")["status"]["setpoint_frequency_Hz"]
        assert ask(cmd="stream_start")["ok"]
        r = ask(cmd="ramp_frequency", frequency_Hz=f0 + 10.0, rate_Hz_per_s=20.0)
        assert r["ok"] and r["ramp_id"] == 1
        assert _wait(lambda: (lambda st: st["ramp_id"] == 1 and not st["ramping"])(
            ask(cmd="status")["status"]))
        c = ask(cmd="stream_stop")["stream"]
        assert len(c["t"]) == len(c["values"]["frequency"]) >= 2
        assert ask(cmd="status")["status"]["setpoint_frequency_Hz"] == f0 + 10.0
        assert ask(cmd="ramp_stop") == {"ok": True, "stopped": False}
        assert ask(cmd="ramp_frequency", frequency_Hz=200.0)["ok"] is False   # no rate
    finally:
        s.close(0)
        svc.stop()
