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
    assert any("limited" in m for _, m in events)
    gen.set_waveform("ch1", "ramp")
    gen.set_frequency("ch1", 5e6)             # ramp stops at 1 MHz
    settled(gen, "ch1", frequency_Hz=1e6, waveform="ramp")


def test_peak_rule_depends_only_on_what_was_asked(system):
    """The offset is kept, the amplitude yields to the peak limit -- fitted
    from the pair AS ASKED, so the order of two requests cannot matter (lab PC
    2026-10-07: 1 Vpp at +9.5 V, then 20 Vpp and +1 V, ended at 1 Vpp)."""
    gen, sim, cfg, events = system
    cfg.limits_1.peak_max_V = 10.0
    gen.set_load("ch1", "high-Z")                      # the lab's case: 10 V peak
    wait(gen, lambda s: s["ch1_load"] == "high-Z" and s["ch1_settled"])
    gen.set_amplitude("ch1", 1.0)
    gen.set_offset("ch1", 9.5)
    settled(gen, "ch1", amplitude_Vpp=1.0, offset_V=9.5)
    gen.set_amplitude("ch1", 20.0)                     # cut to 1 against +9.5 V ...
    gen.set_offset("ch1", 1.0)                         # ... and back up: 18 Vpp at +1 V
    s = settled(gen, "ch1", amplitude_Vpp=18.0, offset_V=1.0)
    assert s["ch1_peak_V"] == pytest.approx(10.0)
    # the other order gives the same
    gen.set_offset("ch1", 9.5); gen.set_amplitude("ch1", 1.0)
    settled(gen, "ch1", amplitude_Vpp=1.0, offset_V=9.5)
    gen.set_offset("ch1", 1.0); gen.set_amplitude("ch1", 20.0)
    settled(gen, "ch1", amplitude_Vpp=18.0, offset_V=1.0)
    # a lone offset change cuts the amplitude, and gives it back when it returns
    gen.set_offset("ch1", 6.0)
    settled(gen, "ch1", amplitude_Vpp=8.0, offset_V=6.0)
    gen.set_offset("ch1", 1.0)
    settled(gen, "ch1", amplitude_Vpp=18.0, offset_V=1.0)


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

    def lagging(n, full=True):
        got = real_read(n, full=full)
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


def test_shutdown_keep_outputs_changes_nothing():
    # a restart for a code update: disconnect, but send no write at all
    gen, sim = build_sim_system(Config())
    gen.start()
    gen.set_output("ch2", True)
    wait(gen, lambda s: s["ch2_output"] and s["ch2_settled"])
    n = len(sim.writes)
    gen.shutdown(keep_outputs=True)
    assert sim.writes[n:] == []
    assert sim.ch[0]["output"] and sim.ch[1]["output"]
    assert gen.status()["connected"] is False and not sim._open


# ---- the phase, as measured on the lab's AFG1062 (2026-10-07) -------------------
# The simulator behaves like the unit: negative phases rejected (-201), whole
# degrees, truncated. The brain must keep the user's setpoint, send the same
# angle in 0..360 rounded to whole degrees, and compare modulo 360.

def _sent_phases(sim, ch):
    return [w[2] for w in sim.writes if w[0] == "set_phase" and w[1] == ch]


@pytest.mark.parametrize("asked, sent", [(-90.0, 270.0), (-180.0, 180.0), (31.0, 31.0),
                                         (90.0, 90.0), (179.5, 180.0), (31.4, 31.0),
                                         (359.7, 0.0)])
def test_phase_is_kept_as_asked_and_sent_in_range(system, asked, sent):
    gen, sim, cfg, events = system
    sim.writes.clear()
    gen.set_phase("ch1", asked)
    s = settled(gen, "ch1", phase_deg=asked)          # the echo is what was ASKED
    assert _sent_phases(sim, 0) == [sent]
    assert s["ch1_mismatch"] == ""
    assert sim.drain_errors() == []                   # nothing the unit rejected


def test_a_scan_of_phase_minus180_to_180_never_hangs(system):
    """The scan "TestOscilloscope" (ch1_phase -180..180) stopped at its first
    point: -180 was wrapped to +180 and the echo never matched."""
    gen, sim, cfg, events = system
    for asked in (-180.0, -135.0, -90.0, -45.0, 0.0, 45.0, 90.0, 135.0, 180.0):
        gen.set_phase("ch1", asked)
        settled(gen, "ch1", phase_deg=asked)


def test_an_equivalent_phase_is_not_a_front_panel_change(system):
    gen, sim, cfg, events = system
    gen.set_phase("ch1", -90.0)
    settled(gen, "ch1", phase_deg=-90.0)
    time.sleep(0.3)                                   # several read-backs of 270
    assert gen.status()["ch1_phase_deg"] == -90.0
    assert not any("changed at the instrument" in m for _, m in events)
    sim.ch[0]["phase_deg"] = 100.0                    # a REAL change at the panel
    gen._wake.set()
    wait(gen, lambda s: s["ch1_phase_deg"] == 100.0)


def test_phase_follows_can_be_switched_off(system):
    """Lukas 2026-10-07: "select if also the phase follows or not"."""
    gen, sim, cfg, events = system
    gen.set_follow(True, 90.0)
    settled(gen, "ch2", phase_deg=90.0, frequency_Hz=gen.status()["ch1_frequency_Hz"])
    with pytest.raises(ValueError, match="phase follows"):
        gen.set_phase("ch2", 10.0)
    gen.set_phase_follow(False)
    st = wait(gen, lambda s: not s["phase_follow"])   # the snapshot is rebuilt by the worker
    assert st["follow"] and st["phase_follow_set"] is False
    gen.set_phase("ch2", -45.0)                       # its own setting again
    settled(gen, "ch2", phase_deg=-45.0)
    with pytest.raises(ValueError, match="frequency follows"):
        gen.set_frequency("ch2", 10.0)                # the frequency still follows
    gen.set_frequency("ch1", 500.0)
    settled(gen, "ch2", frequency_Hz=500.0, phase_deg=-45.0)   # phase untouched
    assert cfg.coupling.ch2_phase_follows is False
    gen.set_phase_follow(True)
    settled(gen, "ch2", phase_deg=90.0)


def test_status_shows_a_pending_request_at_once(system):
    """Lab PC 2026-10-07: right after a set_* the status showed the OLD
    setpoints with settled True for 0.5-1.6 s. Now: the new setpoint and
    settled False at once, until it is pushed AND read back."""
    gen, sim, cfg, events = system
    settled(gen, "ch1", frequency_Hz=30.0)
    gen._lock.acquire()                     # hold the worker off this channel
    try:
        gen._want["ch1"]["frequency_Hz"] = 777.0
        gen._gen["ch1"] += 1
    finally:
        gen._lock.release()
    s = gen.status()
    assert s["ch1_frequency_Hz"] == 777.0 and s["ch1_settled"] is False
    settled(gen, "ch1", frequency_Hz=777.0)


def test_a_push_reads_back_only_its_channel_and_mode_rarely(system):
    """Speed (lab PC: ~2 s per change): after a push only the pushed channel
    is read back, and the load / mode queries only every few seconds."""
    gen, sim, cfg, events = system
    time.sleep(0.3)
    sim.reads.clear()
    gen.set_frequency("ch1", 1234.0)
    settled(gen, "ch1", frequency_Hz=1234.0)
    first = sim.reads[0] if sim.reads else None
    assert first == (0, False), sim.reads[:4]            # CH1, not a full read
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.2:
        time.sleep(0.05)
    fulls = [r for r in sim.reads if r[1]]
    assert len(fulls) <= 2                               # not every poll


def test_no_ramp_symmetry_where_the_instrument_has_none():
    """Lab PC 2026-10-07: FUNC:RAMP:SYMM is not a command of the AFG1062
    firmware (-102, the ramp stayed symmetric): not offered, not sent."""
    from afg.net.describe import build_manifest
    gen, sim = build_sim_system(Config())
    sim_caps = sim.capabilities
    sim.capabilities = lambda: dict(sim_caps(), ramp_symmetry=False)
    gen = Generator(sim, Config())
    gen.start()
    try:
        gen.set_waveform("ch1", "ramp")
        settled(gen, "ch1", waveform="ramp")
        ids = {p["id"] for p in build_manifest(gen)["parameters"]}
        assert "ch1_symmetry" not in ids
        with pytest.raises(ValueError, match="symmetry"):
            gen.set_symmetry("ch1", 80.0)
        assert not any(w[0] == "set_symmetry" for w in sim.writes)
    finally:
        gen.shutdown()


def test_the_limit_message_says_what_happened(system):
    """Lab PC 2026-10-07: amplitude-first logged "amplitude 20 Vpp clamped ->
    amplitude 1 Vpp" and then "offset 1 V clamped -> amplitude 18 Vpp" -- the
    final state right, the words misleading. Now the note says it is for now
    and why."""
    gen, sim, cfg, events = system
    cfg.limits_1.peak_max_V = 10.0
    gen.set_load("ch1", "high-Z")
    wait(gen, lambda s: s["ch1_load"] == "high-Z" and s["ch1_settled"])
    gen.set_offset("ch1", 9.5)
    gen.set_amplitude("ch1", 20.0)
    msgs = [m for _, m in events if "limited" in m]
    assert msgs and "for now, of 20 asked" in msgs[-1] and "offset 9.5 V" in msgs[-1]
    assert "clamped" not in msgs[-1]


def test_a_slow_field_seen_first_is_not_a_front_panel_change(system):
    """Lab PC: "CH2: changed at the instrument: mode = continuous" while nobody
    touched CH2 -- the 5-s read saw `mode` for the first time after quick
    reads had left it out."""
    gen, sim, cfg, events = system
    for _ in range(3):
        gen.set_duty("ch1", 30.0); time.sleep(0.4)
        gen.set_duty("ch1", 60.0); time.sleep(0.4)
    gen._last_full = {ch: -1e9 for ch in gen.channels}  # force full reads now
    gen._wake.set()
    time.sleep(0.5)
    assert not any("changed at the instrument" in m for _, m in events)


def test_quick_and_full_reads_alternating_report_no_change(system, monkeypatch):
    """Lab PC: "changed at the instrument: mode = continuous" every ~5 s on an
    untouched unit -- a quick read REPLACED the stored read-back, so each full
    read found mode/load "new". Read-backs are merged now; a REAL change of
    mode or load at the panel is still adopted."""
    import afg.generator as brain
    monkeypatch.setattr(brain, "_FULL_READ_S", 1.0)     # a full read every 2nd poll
    gen, sim, cfg, events = system
    time.sleep(2.6)                                     # quick and full reads alternate
    assert sum(1 for r in sim.reads if r[1]) >= 2 and sum(1 for r in sim.reads if not r[1]) >= 2
    assert not any("changed at the instrument" in m for _, m in events)
    sim.ch[1]["mode"] = "burst"                         # someone switched burst on
    sim.ch[1]["load_ohm"] = None                        # and the load to high-Z
    st = wait(gen, lambda s: s["ch2_mode"] == "burst" and s["ch2_load"] == "high-Z")
    assert any("changed at the instrument" in m and "mode" in m for _, m in events)


def test_module_settings_survive_a_restart(tmp_path):
    """Lab PC 2026-10-07: after a keep_outputs restart CH2 no longer followed
    CH1 (follow / phase follow are module settings; the AFG cannot be asked
    for them). They are saved to afg.ini at every change and loaded again."""
    from afg.config import Config
    ini = tmp_path / "afg.ini"
    gen, _ = build_sim_system(Config())
    gen.persist_path = str(ini)
    gen.start()
    try:
        gen.set_follow(True, 30.0, phase=False)
    finally:
        gen.shutdown(keep_outputs=True)
    assert ini.is_file() and not (tmp_path / "afg.ini.tmp").exists()
    gen2, _ = build_sim_system(Config.load(str(ini)))
    gen2.start()
    try:
        st = gen2.status()
        assert st["follow"] is True and st["phase_follow_set"] is False
        assert st["phase_offset_deg"] == pytest.approx(30.0)
    finally:
        gen2.shutdown(keep_outputs=True)


def test_nothing_is_saved_without_a_persist_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gen, _ = build_sim_system()
    gen.start()
    try:
        gen.set_follow(True)
    finally:
        gen.shutdown()
    assert list(tmp_path.iterdir()) == []


def test_a_panel_change_of_ch1_keeps_ch2_following(system):
    """Lab PC 2026-10-08: CH1 set to 118 Hz at the panel with "CH2 follows
    CH1" on; the module adopted it, CH2 stayed at 114 Hz."""
    gen, sim, cfg, events = system
    gen.set_follow(True, phase=False)
    wait(gen, lambda s: s["ch2_settled"] and s["ch2_frequency_Hz"] == s["ch1_frequency_Hz"])
    sim.ch[0]["frequency_Hz"] = 118.0                 # the knob on CH1
    gen._wake.set()
    s = wait(gen, lambda s: s["ch2_frequency_Hz"] == 118.0 and s["ch2_settled"], 4.0)
    assert sim.ch[1]["frequency_Hz"] == 118.0 and s["follow"] is True
    assert any("CH2 follows: 118 Hz" in m for _, m in events), events


def test_a_panel_change_of_ch2_switches_follow_off(system, tmp_path):
    """CH2 changed at the panel while it follows: the user overrode it there
    -- follow off, with a warn, saved; no tug of war with the panel."""
    gen, sim, cfg, events = system
    gen.persist_path = str(tmp_path / "afg.ini")
    gen.set_follow(True, phase=False)
    wait(gen, lambda s: s["ch2_settled"] and s["ch2_frequency_Hz"] == s["ch1_frequency_Hz"])
    sim.ch[1]["frequency_Hz"] = 777.0                 # the knob on CH2
    gen._wake.set()
    # (the adopted value shows with the worker's next snapshot, a moment
    # after the follow flag: wait for both)
    s = wait(gen, lambda s: s["follow"] is False and s["ch2_frequency_Hz"] == 777.0, 4.0)
    assert sim.ch[1]["frequency_Hz"] == 777.0
    assert any(lvl == "warn" and "follow" in m and "OFF" in m for lvl, m in events)
    assert "ch2_follows_ch1 = False" in (tmp_path / "afg.ini").read_text(encoding="utf-8")
