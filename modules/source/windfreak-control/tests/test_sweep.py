"""The SWEEPS (ramp_frequency / ramp_power / ramp_phase): software ramps for
fly scans, per channel.

The service walks one channel's knob in small steps (softramp.py) at a set
pace and records every value it sent; a fly scan bins by that COMMANDED value.
Checked here, for each knob of each channel: it sweeps to its end at the
pace, ramp_stop ends it, an ordinary set takes it over, the RF output is never
touched, the record in the stream format, clamping, describe's ramp blocks,
and the verbs over the wire (ports 17024/17025, this module's own block).
"""

import time

import pytest

from windfreak.backends.sim import SIM_BOOT_STATE
from windfreak.config import Config
from windfreak.sim_system import build_sim_system

#: knob -> (offset of the target from the start value, pace in wire units
#: per second). Each sweep lasts ~0.4 s.
KNOBS = {"frequency": (40e6, 100e6),
         "power": (4.0, 10.0),
         "phase": (40.0, 100.0)}
KEY = {"frequency": "frequency_Hz", "power": "power_dBm", "phase": "phase_deg"}
CASES = [(ch, knob) for ch in "ab" for knob in KNOBS]


def _start(ch, knob):
    """The value the simulated SynthHD boots with (adopted at start)."""
    return SIM_BOOT_STATE["channels"]["ab".index(ch)][KEY[knob]]


@pytest.fixture
def synth():
    cfg = Config()
    cfg.hardware.ramp_dt_s = 0.01
    s, backend = build_sim_system(cfg)
    s.events = []
    s._on_event = lambda lvl, msg: s.events.append((lvl, msg))
    s.start()
    s.sim = backend
    yield s
    s.shutdown()


def _sent(s, ch, knob):
    """Every value the backend was sent for this channel's knob, in order."""
    i = "ab".index(ch)
    return [w[2] for w in s.sim.writes if w[0] == f"set_{knob}" and w[1] == i]


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("ch,knob", CASES)
def test_each_knob_sweeps_to_its_end_at_the_pace(synth, ch, knob):
    start = _start(ch, knob)
    off, rate = KNOBS[knob]
    to = start + off
    name = f"{ch}_{knob}"
    synth.stream_start()
    t0 = time.monotonic()
    rid = synth.ramp(ch, knob, to, rate)
    assert rid == 1
    st = synth.status()
    assert st[f"{name}_ramping"] and st["ramping"] and st[f"{name}_ramp_id"] == 1
    assert _wait(lambda: not synth.status()[f"{name}_ramping"])
    took = time.monotonic() - t0
    assert 0.3 < took < 1.5, took                     # |to-start|/rate = 0.4 s
    vals = _sent(synth, ch, knob)
    assert len(vals) >= 15 and vals == sorted(vals)   # many small steps, one way
    assert vals[-1] == to
    # the other channel's same knob was not touched
    assert _sent(synth, "ba"[("ab".index(ch))], knob) == []
    # the brain, the config and (after the worker's next poll) the status agree
    assert synth.cfg.channel(ch).__dict__[KEY[knob]] == to
    assert _wait(lambda: synth.status()[f"{ch}_{KEY[knob]}"] == to)
    # the record: from rest to the end, one channel per sweep, own time stamps
    chunk = synth.stream_stop()
    rec = chunk["values"][name]
    assert rec[0] == start and rec[-1] == to
    assert len(chunk["t_ch"][name]) == len(rec) >= 15
    for other in chunk["values"]:                     # the others: at rest
        assert len(chunk["values"][other]) == len(chunk["t_ch"][other]) >= 1
    assert len(chunk["values"]) == 6
    assert "now" in chunk and chunk["delay_s"][name] == 0.0
    assert any(f"{knob} sweep done" in m for _, m in synth.events)
    # no event per step: a sweep must not flood the log
    assert sum(f"{knob} =" in m for _, m in synth.events) == 0


@pytest.mark.parametrize("ch,knob", CASES)
def test_ramp_stop_ends_it_where_it_is(synth, ch, knob):
    start = _start(ch, knob)
    off, rate = KNOBS[knob]
    name = f"{ch}_{knob}"
    synth.ramp(ch, knob, start + 5 * off, rate / 2)      # 4 s
    time.sleep(0.15)
    assert synth.ramp_stop(name) is True
    assert not synth.status()[f"{name}_ramping"]
    vals = _sent(synth, ch, knob)
    here = vals[-1]
    assert here != start and abs(here - start) < 2 * off   # part-way
    time.sleep(0.1)
    assert len(_sent(synth, ch, knob)) == len(vals)      # nothing after the stop
    assert synth.ramp_stop(name) is False
    # without a knob: every sweep stops
    synth.ramp(ch, knob, start + off, rate / 10)
    assert synth.ramp_stop() is True
    assert not synth.status()["ramping"]


@pytest.mark.parametrize("ch,knob", CASES)
def test_an_ordinary_set_takes_the_knob_over(synth, ch, knob):
    start = _start(ch, knob)
    off, rate = KNOBS[knob]
    synth.ramp(ch, knob, start + 5 * off, rate / 2)
    time.sleep(0.15)
    getattr(synth, f"set_{knob}")(ch, start)
    assert not synth.status()[f"{ch}_{knob}_ramping"]
    time.sleep(0.1)
    n = len(_sent(synth, ch, knob))
    time.sleep(0.1)
    assert len(_sent(synth, ch, knob)) == n              # the sweep is gone
    assert _sent(synth, ch, knob)[-1] == start           # the worker pushed the set
    assert any("stopped by a" in m for _, m in synth.events)


def test_a_set_of_another_knob_or_channel_leaves_a_sweep_running(synth):
    synth.ramp("a", "frequency", 3.0e9, 100e6)
    time.sleep(0.05)
    synth.set_power("a", -20.0)
    synth.set_frequency("b", 3.3e9)
    assert synth.status()["a_frequency_ramping"]
    # the worker pushed the other set without disturbing the sweep's knob
    assert _wait(lambda: -20.0 in _sent(synth, "a", "power"))
    assert synth.status()["a_frequency_ramping"]
    synth.ramp_stop()


def test_rf_output_is_never_touched(synth):
    st = synth.status()
    assert st["a_rf_on"] is True and st["b_rf_on"] is False      # adopted
    for ch, knob in CASES:
        off, rate = KNOBS[knob]
        synth.ramp(ch, knob, _start(ch, knob) + off, rate * 4)
    assert _wait(lambda: not synth.status()["ramping"])
    synth.ramp("b", "power", -30.0, 1.0)
    synth.ramp_stop()
    assert not [w for w in synth.sim.writes if w[0] == "set_output"]   # never sent
    st = synth.status()
    assert st["a_rf_on"] is True and st["b_rf_on"] is False
    assert synth.sim.output_on(0) and not synth.sim.output_on(1)


def test_shutdown_stops_every_sweep_first(synth):
    synth.ramp("a", "frequency", 5.0e9, 100e6)
    synth.ramp("b", "phase", 300.0, 10.0)
    time.sleep(0.05)
    synth.shutdown()
    st = synth.status()
    assert not st["ramping"]
    n = len(synth.sim.writes)
    time.sleep(0.1)
    assert len(synth.sim.writes) == n


def test_settings_sent_back_during_a_sweep_do_not_jump_it(synth):
    """The Settings dialog sends the channel values it read when it opened;
    while a knob sweeps that value is stale, not an edit."""
    synth.ramp("a", "frequency", 3.0e9, 100e6)
    time.sleep(0.1)
    synth.cfg.channel_a.frequency_Hz = 2.45e9          # the stale copy
    synth.apply_config()
    assert synth.status()["a_frequency_ramping"]
    assert synth.cfg.channel_a.frequency_Hz > 2.45e9
    synth.ramp_stop()


def test_clamps_and_refusals(synth):
    with pytest.raises(ValueError):
        synth.ramp_frequency("a", 2.5e9, 0.0)
    with pytest.raises(ValueError):
        synth.ramp_power("a", float("nan"), 1.0)
    with pytest.raises(ValueError):
        synth.ramp("a", "amplitude", 1.0, 1.0)
    with pytest.raises(ValueError):
        synth.ramp("c", "power", 1.0, 1.0)
    with pytest.raises(ValueError):
        synth.ramp_stop("frequency")                   # needs the channel
    lim = synth.cfg.limits
    synth.ramp_frequency("b", 99e9, 1e15)              # beyond the band, absurd pace
    assert any(lvl == "warn" and "clamped" in m for lvl, m in synth.events)
    st = synth.status()
    assert st["b_frequency_ramp_rate_Hz_per_s"] == lim.ramp_rate_max_Hz_per_s
    assert st["b_frequency_ramp_target_Hz"] == lim.freq_max_Hz
    synth.ramp_power("a", 50.0, 1e-9)                  # above the level limit, too slow
    st = synth.status()
    assert st["a_power_ramp_target_dBm"] == lim.power_max_dBm
    assert st["a_power_ramp_rate_dB_per_s"] == lim.ramp_rate_min_dB_per_s
    synth.ramp_stop()


def test_describe_offers_a_ramp_on_each_knob_of_each_channel():
    from windfreak.net.describe import build_manifest
    s, _ = build_sim_system(Config())
    by = {p["id"]: p for p in build_manifest(s)["parameters"]}
    for ch in "ab":
        for knob, unit, to_arg, rate_arg in (
                ("frequency", "MHz/s", "frequency_Hz", "rate_Hz_per_s"),
                ("power", "dB/s", "power_dBm", "rate_dB_per_s"),
                ("phase", "deg/s", "phase_deg", "rate_deg_per_s")):
            name = f"{ch}_{knob}"
            r = by[name]["ramp"]
            assert r["kind"] == "software"
            assert r["rate"]["unit"] == unit
            assert r["start"] == {"verb": f"ramp_{knob}",
                                  "args": {"to": to_arg, "rate": rate_arg},
                                  "extra": {"channel": ch}}
            # the same channel argument as the control's own set
            assert r["start"]["extra"] == by[name]["set"]["extra"]
            assert r["stop"] == {"verb": "ramp_stop", "extra": {"knob": name}}
            assert r["readback"] == {"stream": {"group": "ramp", "channel": name},
                                     "measured": False}
            assert r["done"] == {"key": f"{name}_ramping", "id_key": f"{name}_ramp_id"}
            assert r["rate"]["min"] <= r["rate"]["default"] <= r["rate"]["max"]
    # scaled like the set: MHz in the scan, Hz on the wire
    assert by["a_frequency"]["scale"] == 1e6
    assert (by["a_frequency"]["ramp"]["rate"]["max"]
            == Config().limits.ramp_rate_max_Hz_per_s / 1e6)
    assert by["ramping"]["read_path"] == ["ramping"]
    # the RF switch is not sweepable
    assert "ramp" not in by["a_rf_on"]


def test_sweeps_over_the_wire():
    zmq = pytest.importorskip("zmq")
    from windfreak.net.service import WindfreakService
    cfg = Config()
    cfg.hardware.ramp_dt_s = 0.01
    s, _ = build_sim_system(cfg)
    svc = WindfreakService(s, host="127.0.0.1", cmd_port=17024, pub_port=17025,
                           status_hz=20.0)
    svc.start()
    sock = zmq.Context.instance().socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, 3000)
    sock.connect("tcp://127.0.0.1:17024")

    def ask(**msg):
        sock.send_json(msg)
        return sock.recv_json()

    try:
        st = ask(cmd="status")["status"]
        assert st["ramping"] is False and st["a_power_ramp_id"] == 0
        assert ask(cmd="stream_start")["ok"]
        r = ask(cmd="ramp_power", channel="a", power_dBm=st["a_power_dBm"] + 2.0,
                rate_dB_per_s=10.0)
        assert r["ok"] and r["ramp_id"] == 1
        assert _wait(lambda: (lambda st: st["a_power_ramp_id"] == 1
                              and not st["a_power_ramping"])(ask(cmd="status")["status"]))
        assert ask(cmd="stream_read")["ok"]
        c = ask(cmd="stream_stop")["stream"]
        assert c["values"]["a_power"][-1] == st["a_power_dBm"] + 2.0 and "now" in c
        r = ask(cmd="ramp_frequency", channel="b", frequency_Hz=st["b_frequency_Hz"] + 1e6,
                rate_Hz_per_s=1e6)
        assert r["ok"] and r["ramp_id"] == 1
        assert ask(cmd="ramp_stop", knob="b_frequency") == {"ok": True, "stopped": True}
        assert ask(cmd="ramp_phase", channel="b", phase_deg=100.0,
                   rate_deg_per_s=100.0)["ok"]
        assert ask(cmd="ramp_stop")["ok"]
        assert ask(cmd="ramp_power", channel="a", power_dBm=-10.0)["ok"] is False  # no rate
        assert ask(cmd="ramp_power", power_dBm=-10.0, rate_dB_per_s=1.0)["ok"] is False
        assert ask(cmd="ramp_stop", knob="volume")["ok"] is False
        st = ask(cmd="status")["status"]
        assert st["a_rf_on"] is True and st["b_rf_on"] is False
    finally:
        sock.close(0)
        svc.stop()
