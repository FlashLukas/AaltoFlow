"""Sweeps (ramp_start): a knob walked at a set pace, for fly scans over any
knob (INSTRUMENT_MODULE_GUIDE 6b "Ramps", Lukas 2026-10-10 "fly for the
remaining modules"). Against the simulated AFG1062. Each rule has a test:

  * the sweep reaches its target, one command per step, its number finished
    in status; the output is never switched;
  * the stream records every value SENT (measured false), each knob its own
    channel with its own time stamps;
  * CH2 following CH1 follows each step; sweeping the follower is refused;
  * clamped like a set (peak rule for amplitude / offset), with a warn;
  * a set of the knob, ramp_stop and outputs_off end it where it is;
  * no "changed at the instrument" while (or because) a sweep moved a knob.
"""

import time

import pytest

from afg.config import Config
from afg.sim_system import build_sim_system


@pytest.fixture
def system():
    cfg = Config()
    gen, sim = build_sim_system(cfg)
    events = []
    gen._on_event = lambda level, msg: events.append((level, msg))
    gen.start()
    yield gen, sim, cfg, events
    gen.shutdown()


def wait(gen, pred, timeout=5.0):
    t_end = time.monotonic() + timeout
    s = gen.status()
    while time.monotonic() < t_end:
        s = gen.status()
        if pred(s):
            return s
        time.sleep(0.01)
    raise AssertionError(f"not reached; last status {s}")


def done(gen, rid):
    return wait(gen, lambda s: s["ramp_id"] >= rid and not s["ramping"])


def test_a_sweep_reaches_its_target_and_records_it(system):
    gen, sim, cfg, events = system
    out0 = [c["output"] for c in sim.ch]
    gen.stream_start()
    rid = gen.ramp_start("ch1", "frequency", 40.0, 20.0)      # 30 -> 40 Hz in 0.5 s
    assert gen.status()["ramping"] and gen.status()["ramp_knob"] == "ch1_frequency"
    s = done(gen, rid)
    assert s["ch1_frequency_Hz"] == 40.0 and sim.ch[0]["frequency_Hz"] == 40.0
    steps = [w for w in sim.writes if w[0] == "set_frequency" and w[1] == 0]
    assert len(steps) >= 5                                     # a walk, not a jump
    assert [c["output"] for c in sim.ch] == out0               # never switched
    assert not any(w[0] == "set_output" for w in sim.writes)
    chunk = gen.stream_stop()
    vals = chunk["values"]["ch1_frequency"]
    assert len(vals) == len(chunk["t_ch"]["ch1_frequency"]) >= 5
    assert vals[-1] == 40.0 and vals == sorted(vals)
    assert set(chunk["values"]) >= {"ch1_amplitude", "ch2_phase"}   # every knob
    assert not any("changed at the instrument" in m for _, m in events), events


def test_the_follower_follows_and_cannot_be_swept(system):
    gen, sim, cfg, events = system
    gen.set_follow(True, 45.0, True)
    wait(gen, lambda s: s["ch2_settled"] and s["ch2_frequency_Hz"] == s["ch1_frequency_Hz"])
    with pytest.raises(ValueError, match="follows"):
        gen.ramp_start("ch2", "frequency", 50.0, 10.0)
    with pytest.raises(ValueError, match="follows"):
        gen.ramp_start("ch2", "phase", 50.0, 10.0)
    gen.ramp_start("ch2", "amplitude", 1.0, 1.0)              # its own knobs: fine
    gen.ramp_stop()
    n_align = sum(1 for w in sim.writes if w[0] == "align_phase")
    rid = gen.ramp_start("ch1", "frequency", 36.0, 30.0)
    done(gen, rid)
    assert sim.ch[1]["frequency_Hz"] == 36.0                   # followed every step
    wait(gen, lambda s: sum(1 for w in sim.writes if w[0] == "align_phase") > n_align)
    rid = gen.ramp_start("ch1", "phase", 20.0, 100.0)
    done(gen, rid)
    assert sim.ch[1]["phase_deg"] == 65.0                      # 20 + 45, followed


def test_clamped_like_a_set(system):
    gen, sim, cfg, events = system
    cfg.limits_1.peak_max_V = 2.0
    gen.set_offset("ch1", 1.0)
    wait(gen, lambda s: s["ch1_offset_V"] == 1.0 and s["ch1_settled"])
    # the peak rule: amplitude may reach 2 x (2 - 1) = 2 Vpp
    rid = gen.ramp_start("ch1", "amplitude", 8.0, 100.0)
    s = done(gen, rid)
    assert s["ch1_amplitude_Vpp"] == pytest.approx(2.0)
    assert any(lvl == "warn" and "limited to 2" in m for lvl, m in events)
    rid = gen.ramp_start("ch1", "frequency", 41.0, 1e12)       # rate beyond the max
    assert any("limited" in m and "1e+07" in m for _, m in events)
    done(gen, rid)


def test_a_set_or_a_stop_ends_it_where_it_is(system):
    gen, sim, cfg, events = system
    gen.ramp_start("ch1", "frequency", 1000.0, 10.0)          # would take ~100 s
    wait(gen, lambda s: s["ch1_frequency_Hz"] > 30.5)
    gen.set_frequency("ch1", 50.0)                             # the set takes over
    s = wait(gen, lambda s: not s["ramping"] and s["ch1_frequency_Hz"] == 50.0
             and s["ch1_settled"])
    assert sim.ch[0]["frequency_Hz"] == 50.0
    time.sleep(0.2)
    assert sim.ch[0]["frequency_Hz"] == 50.0                   # no step after the set
    gen.ramp_start("ch1", "offset", 0.5, 0.01)
    wait(gen, lambda s: s["ramping"])
    assert gen.ramp_stop() is True
    v = sim.ch[0]["offset_V"]
    time.sleep(0.2)
    assert sim.ch[0]["offset_V"] == v and not gen.status()["ramping"]
    gen.ramp_start("ch1", "offset", -0.5, 0.01)
    wait(gen, lambda s: s["ramping"])
    gen.outputs_off()
    wait(gen, lambda s: not s["ramping"] and s["all_off"])


def test_a_dc_channel_has_no_frequency_to_sweep(system):
    gen, sim, cfg, events = system
    gen.set_waveform("ch2", "dc")
    wait(gen, lambda s: s["ch2_waveform"] == "dc" and s["ch2_settled"])
    with pytest.raises(ValueError, match="no frequency"):
        gen.ramp_start("ch2", "frequency", 100.0, 10.0)
    with pytest.raises(ValueError, match="cannot sweep"):
        gen.ramp_start("ch1", "duty", 10.0, 1.0)
