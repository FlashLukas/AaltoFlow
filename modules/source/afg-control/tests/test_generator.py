"""The brain against the simulated AFG1062.

Each rule of generator.py has a test that fails without it:
  * read-only start (adopt, write nothing, copy into the config);
  * clamps: lab limit, instrument range per waveform/load, the PEAK rule, and
    which knob yields;
  * push ORDER (output off first / on last; waveform vs frequency; the
    amplitude/offset order that never passes the peak on the way);
  * CH2 follows CH1;
  * honesty: settled only after the read-back agrees; a coerced value shows
    as a mismatch; a front-panel change is adopted;
  * outputs_off / align_phase numbered operations; shutdown switches off.
"""

import time

import pytest

from afg.config import Config
from afg.sim_system import build_sim_system
from afg.backends.sim import SimulatedAFG
from afg.generator import Generator, parse_channel


@pytest.fixture
def system():
    cfg = Config()
    gen, sim = build_sim_system(cfg)
    events = []
    gen._on_event = lambda level, msg: events.append((level, msg))
    gen.start()
    yield gen, sim, cfg, events
    gen.shutdown()


def wait(gen, pred, timeout=2.0):
    t_end = time.monotonic() + timeout
    s = gen.status()
    while time.monotonic() < t_end:
        s = gen.status()
        if pred(s):
            return s
        time.sleep(0.01)
    raise AssertionError(f"not reached; last status {s}")


def settled(gen, ch, **echo):
    return wait(gen, lambda s: all(s[f"{ch}_{k}"] == v for k, v in echo.items())
                and s[f"{ch}_settled"])


# ---- start -----------------------------------------------------------------

def test_start_adopts_and_writes_nothing(system):
    gen, sim, cfg, events = system
    s = gen.status()
    assert sim.writes == []
    assert s["ch1_output"] and s["ch1_waveform"] == "sine" and s["ch1_frequency_Hz"] == 30.0
    assert s["ch2_waveform"] == "square" and s["ch2_offset_V"] == 0.75
    assert s["ch1_settled"] and s["ch2_settled"]
    # the config tells the truth now
    assert cfg.channel_1.frequency_Hz == 30.0 and cfg.channel_2.waveform == "square"
    time.sleep(0.2)
    assert sim.writes == [], "the worker sent something after an adopt"


def test_start_warns_about_a_setting_above_the_lab_limit_but_keeps_it():
    cfg = Config()
    cfg.limits_1.peak_max_V = 0.5            # the sim boots CH1 at 2 Vpp -> peak 1 V
    gen, sim = build_sim_system(cfg)
    events = []
    gen._on_event = lambda level, msg: events.append((level, msg))
    gen.start()
    try:
        assert sim.writes == []
        assert gen.status()["ch1_amplitude_Vpp"] == 2.0
        assert any(lvl == "warn" and "ABOVE" in m for lvl, m in events)
    finally:
        gen.shutdown()


def test_start_reports_burst_mode_and_arb():
    boot = {"channels": [dict(output=False, waveform="arb", frequency_Hz=1e3,
                              amplitude_Vpp=1.0, offset_V=0.0, phase_deg=0.0,
                              duty_pct=50.0, symmetry_pct=50.0, load_ohm=50.0),
                         dict(output=False, waveform="sine", frequency_Hz=1e3,
                              amplitude_Vpp=1.0, offset_V=0.0, phase_deg=0.0,
                              duty_pct=50.0, symmetry_pct=50.0, load_ohm=None)]}
    sim = SimulatedAFG(boot=boot)
    sim.ch[1]["mode"] = "burst"
    gen = Generator(sim, Config())
    events = []
    gen._on_event = lambda level, msg: events.append((level, msg))
    gen.start()
    try:
        s = gen.status()
        assert s["ch1_waveform"] == "arb" and s["ch2_mode"] == "burst"
        # high-Z doubles the instrument range to 10 V; the lab limit's default
        # is the full range, so 10 V it is
        assert s["ch2_load"] == "high-Z" and s["ch2_peak_max_V"] == 10.0
        assert gen.backend.envelope("sine", None)["peak_max_V"] == 10.0
        assert any("burst" in m for _, m in events)
        assert sim.writes == []
    finally:
        gen.shutdown()


# ---- clamps ------------------------------------------------------------------

def test_parse_channel():
    assert parse_channel("CH1") == "ch1" and parse_channel(2) == "ch2"
    with pytest.raises(ValueError):
        parse_channel("a")
    with pytest.raises(ValueError):
        parse_channel(0)                      # 0-based numbers are refused


def test_lab_limit_and_instrument_range(system):
    gen, sim, cfg, events = system
    cfg.limits_1.amplitude_max_Vpp = 3.0
    gen.set_amplitude("ch1", 8.0)
    settled(gen, "ch1", amplitude_Vpp=3.0)
    assert any("clamped" in m for _, m in events)
    gen.set_waveform("ch1", "ramp")
    gen.set_frequency("ch1", 5e6)             # ramp stops at 1 MHz
    settled(gen, "ch1", frequency_Hz=1e6, waveform="ramp")


def test_peak_rule_the_knob_being_set_yields(system):
    gen, sim, cfg, events = system
    gen.set_amplitude("ch1", 6.0)
    gen.set_offset("ch1", 4.0)                # 4 + 3 > 5: the OFFSET stops at 2
    s = settled(gen, "ch1", amplitude_Vpp=6.0, offset_V=2.0)
    gen.set_amplitude("ch1", 9.0)             # 2 + 4.5 > 5: the AMPLITUDE stops at 6
    s = settled(gen, "ch1", amplitude_Vpp=6.0, offset_V=2.0)
    assert s["ch1_peak_V"] == pytest.approx(5.0)


def test_high_z_doubles_the_range(system):
    gen, sim, cfg, events = system
    # no limits edited: the default lab limits are the full range
    gen.set_load("ch1", "high-Z")
    wait(gen, lambda s: s["ch1_load"] == "high-Z" and s["ch1_settled"])
    # the AFG rescales the shown volts for the new load: adopted from read-back
    assert gen.status()["ch1_amplitude_Vpp"] == pytest.approx(4.0)
    gen.set_amplitude("ch1", 15.0)
    settled(gen, "ch1", amplitude_Vpp=15.0)
    assert gen.envelope("ch1")["peak_max_V"] == 10.0


def test_lowering_a_limit_pushes_at_once(system):
    gen, sim, cfg, events = system
    cfg.limits_1.peak_max_V = 0.5            # CH1 is driving 2 Vpp
    gen.apply_config()
    s = settled(gen, "ch1", amplitude_Vpp=1.0)
    assert sim.ch[0]["amplitude_Vpp"] == 1.0
    assert any("re-clamped" in m for _, m in events)


def test_unknown_waveform_refused(system):
    gen, *_ = system
    with pytest.raises(ValueError):
        gen.set_waveform("ch1", "arb")
    with pytest.raises(ValueError):
        gen.set_load("ch1", 0.0)


# ---- push order ----------------------------------------------------------------

def test_output_on_is_sent_last(system):
    gen, sim, cfg, events = system
    sim.writes.clear()
    gen._change("ch2", "test", output=True, frequency_Hz=500.0, amplitude_Vpp=0.4)
    settled(gen, "ch2", output=True, frequency_Hz=500.0)
    names = [w[0] for w in sim.writes]
    assert names[-1] == "set_output" and "set_frequency" in names[:-1]


def test_output_off_is_sent_first(system):
    gen, sim, cfg, events = system
    sim.writes.clear()
    gen._change("ch1", "test", output=False, frequency_Hz=500.0)
    settled(gen, "ch1", output=False, frequency_Hz=500.0)
    assert sim.writes[0] == ("set_output", 0, False)


def test_waveform_and_frequency_order(system):
    gen, sim, cfg, events = system
    gen.set_frequency("ch1", 5e6)
    settled(gen, "ch1", frequency_Hz=5e6)
    sim.writes.clear()
    gen._change("ch1", "test", waveform="ramp", frequency_Hz=1e3)   # falls: freq first
    settled(gen, "ch1", waveform="ramp", frequency_Hz=1e3)
    assert [w[0] for w in sim.writes] == ["set_frequency", "set_waveform"]
    assert sim.drain_errors() == [], "the instrument had to coerce something"
    sim.writes.clear()
    gen._change("ch1", "test", waveform="sine", frequency_Hz=20e6)  # rises: shape first
    settled(gen, "ch1", waveform="sine", frequency_Hz=20e6)
    assert [w[0] for w in sim.writes] == ["set_waveform", "set_frequency"]


def test_amplitude_offset_order_never_passes_the_peak(system):
    """From (8 Vpp, +1 V) to (2 Vpp, +3.5 V): both inside the 5 V peak. Offset
    first would pass through (8 Vpp, 3.5 V) = 7.5 V; amplitude first stays at
    1 + 1 = 2 V. The sim refuses an over-peak step with an error."""
    gen, sim, cfg, events = system
    gen._change("ch1", "test", amplitude_Vpp=8.0, offset_V=1.0)
    settled(gen, "ch1", amplitude_Vpp=8.0, offset_V=1.0)
    sim.drain_errors()
    sim.writes.clear()
    gen._change("ch1", "test", amplitude_Vpp=2.0, offset_V=3.5)
    settled(gen, "ch1", amplitude_Vpp=2.0, offset_V=3.5)
    assert [w[0] for w in sim.writes] == ["set_amplitude", "set_offset"]
    assert sim.drain_errors() == []
    # and back: the offset must come down first
    sim.writes.clear()
    gen._change("ch1", "test", amplitude_Vpp=8.0, offset_V=1.0)
    settled(gen, "ch1", amplitude_Vpp=8.0, offset_V=1.0)
    assert [w[0] for w in sim.writes] == ["set_offset", "set_amplitude"]
    assert sim.drain_errors() == []


# ---- coupling --------------------------------------------------------------------

def test_ch2_follows_ch1(system):
    gen, sim, cfg, events = system
    gen.set_follow(True, 90.0)
    gen.set_frequency("ch1", 777.0)
    s = settled(gen, "ch2", frequency_Hz=777.0, phase_deg=90.0)
    assert ("align_phase",) in sim.writes
    gen.set_phase("ch1", 30.0)
    settled(gen, "ch2", phase_deg=120.0)
    with pytest.raises(ValueError, match="follows"):
        gen.set_frequency("ch2", 10.0)
    gen.set_phase_offset(-100.0)
    settled(gen, "ch2", phase_deg=-70.0)
    assert cfg.coupling.ch2_follows_ch1 and cfg.coupling.phase_offset_deg == -100.0
    gen.set_follow(False)
    gen.set_frequency("ch2", 10.0)            # free again
    settled(gen, "ch2", frequency_Hz=10.0)


# ---- honesty ---------------------------------------------------------------------

def test_settled_needs_the_read_back(system):
    """A setting the instrument does not take (here: the sim pretends the
    amplitude stays put) never becomes settled, and says why."""
    gen, sim, cfg, events = system
    sim.set_amplitude = lambda ch, v: sim.writes.append(("ignored", ch, v))
    gen.set_amplitude("ch1", 1.0)
    s = wait(gen, lambda s: s["ch1_mismatch"] != "")
    assert not s["ch1_settled"] and "amplitude_Vpp" in s["ch1_mismatch"]
    # warned once the SAME mismatch is read back a second time
    wait(gen, lambda s: any("does not hold" in m for _, m in events))


def test_a_mismatch_seen_once_is_not_warned(system):
    """Lab PC 2026-10-06: fast output toggles gave "instrument does not hold
    the request -- output: asked True, instrument False", then all was fine:
    the read came before the AFG had applied it. One stale read-back keeps
    the channel unsettled (safe for a scan) but no longer warns."""
    gen, sim, cfg, events = system
    gen.set_output("ch1", False)                   # the sim boots with CH1 on
    wait(gen, lambda s: s["ch1_settled"] and not s["ch1_output"])
    real_read = sim.read_channel
    stale = {"left": 1}

    def lagging(n):
        got = real_read(n)
        if stale["left"] and got.get("output"):
            stale["left"] -= 1
            got = dict(got, output=False)          # the AFG has not applied it yet
        return got
    sim.read_channel = lagging
    gen.set_output("ch1", True)
    s = wait(gen, lambda s: s["ch1_settled"] and s["ch1_output"])
    assert stale["left"] == 0                      # the stale read DID happen
    assert not any("does not hold" in m for _, m in events), events


def test_a_front_panel_change_is_adopted(system):
    gen, sim, cfg, events = system
    sim.ch[0]["frequency_Hz"] = 123.0        # someone turned the knob
    gen._wake.set()
    s = wait(gen, lambda s: s["ch1_frequency_Hz"] == 123.0)
    assert s["ch1_settled"] and s["ch1_mismatch"] == ""
    assert cfg.channel_1.frequency_Hz == 123.0
    assert any("changed at the instrument" in m for _, m in events)


def test_hardware_error_unsettles_and_recovers(system):
    gen, sim, cfg, events = system
    sim.fail_reads = True
    s = wait(gen, lambda s: s["hw_error"] != "")
    assert not s["ch1_settled"]
    sim.fail_reads = False
    wait(gen, lambda s: s["hw_error"] == "" and s["ch1_settled"])


# ---- operations and shutdown -------------------------------------------------------

def test_outputs_off_is_numbered(system):
    gen, sim, cfg, events = system
    n = gen.outputs_off()
    s = wait(gen, lambda s: s["op_id"] == n and s["op_ok"])
    assert s["all_off"] and not sim.ch[0]["output"] and not sim.ch[1]["output"]
    m = gen.align_phase()
    assert m == n + 1
    wait(gen, lambda s: s["op_id"] == m and s["op_ok"])
    assert ("align_phase",) in sim.writes


def test_shutdown_switches_every_output_off():
    gen, sim = build_sim_system(Config())
    gen.start()
    gen.set_output("ch2", True)
    wait(gen, lambda s: s["ch2_output"] and s["ch2_settled"])
    gen.shutdown()
    assert not sim.ch[0]["output"] and not sim.ch[1]["output"]
    assert gen.status()["connected"] is False
    gen.shutdown()                            # twice is fine
