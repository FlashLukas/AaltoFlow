"""The Synthesizer brain against the simulated SynthHD: lifecycle and safety,
clamping, the settle flag, lock behaviour, and the threading rule."""

import threading
import time

import pytest

from windfreak.config import Config
from windfreak.sim_system import build_sim_system
from windfreak.synthesizer import parse_channel
from windfreak.backends.sim import max_leveled_power


def wait_for(pred, synth, timeout=2.0):
    """Poll status until pred(status) is true; return that status."""
    t_end = time.monotonic() + timeout
    s = synth.status()
    while time.monotonic() < t_end:
        s = synth.status()
        if pred(s):
            return s
        time.sleep(0.005)
    raise AssertionError(f"condition not reached; last status {s}")


@pytest.fixture
def rig():
    cfg = Config()
    synth, backend = build_sim_system(cfg)
    events = []
    synth._on_event = lambda lvl, msg: events.append((lvl, msg))
    synth.start()
    yield synth, backend, events
    synth.shutdown()


# ---- safety ---------------------------------------------------------------

def test_outputs_start_off_even_if_the_box_booted_radiating(rig):
    synth, backend, _ = rig
    s = wait_for(lambda s: s["a_settled"] and s["b_settled"], synth)
    assert s["connected"] is True
    assert s["a_rf_on"] is False and s["b_rf_on"] is False
    assert not backend.output_on(0) and not backend.output_on(1)


def test_shutdown_turns_both_outputs_off():
    synth, backend = build_sim_system(Config())
    synth.start()
    synth.set_rf("a", True); synth.set_rf("b", True)
    wait_for(lambda s: s["a_rf_on"] and s["b_rf_on"], synth)
    assert backend.output_on(0) and backend.output_on(1)
    synth.shutdown()
    assert not backend.output_on(0) and not backend.output_on(1)
    assert synth.status()["connected"] is False
    synth.shutdown()                           # twice is harmless


def test_all_rf_off(rig):
    synth, backend, _ = rig
    synth.set_rf("a", True); synth.set_rf("b", True)
    wait_for(lambda s: s["a_rf_on"] and s["b_rf_on"], synth)
    synth.all_rf_off()
    wait_for(lambda s: not s["a_rf_on"] and not s["b_rf_on"], synth)
    assert not backend.output_on(0) and not backend.output_on(1)


# ---- set / read back / settle ----------------------------------------------

def test_channels_are_independent(rig):
    synth, backend, _ = rig
    synth.set_frequency("a", 2.0e9)
    synth.set_frequency("b", 5.5e9)
    synth.set_power("b", -3.0)
    s = wait_for(lambda s: s["b_frequency_Hz"] == 5.5e9 and s["b_settled"]
                 and s["a_frequency_Hz"] == 2.0e9 and s["a_settled"], synth)
    assert s["a_power_dBm"] == Config().channel_a.power_dBm
    assert s["b_power_dBm"] == -3.0
    assert backend.read_frequency(0) == 2.0e9 and backend.read_frequency(1) == 5.5e9


def test_echo_is_the_request_and_readback_is_snapped_to_the_grid(rig):
    """The echo must equal the request EXACTLY (scan-core adopts on it), while
    the readback shows what the PLL really makes (100 Hz grid in the sim)."""
    synth, _, _ = rig
    f = 1234.56789e6
    synth.set_frequency("a", f)
    s = wait_for(lambda s: s["a_frequency_Hz"] == f and s["a_settled"], synth)
    assert s["a_frequency_actual_Hz"] == pytest.approx(round(f / 100) * 100)


def test_settled_is_false_until_the_new_request_is_pushed():
    """Gotcha #2: right after a set, the snapshot still describes the previous
    request -- its echo differs, so an adopt-then-flag wait cannot be fooled."""
    synth, backend = build_sim_system(Config())
    synth.start()
    gate = threading.Event()
    orig = backend.set_frequency

    def slow_set(ch, hz):                       # the hardware "takes a while"
        gate.wait(2.0)
        orig(ch, hz)

    try:
        old = wait_for(lambda s: s["a_settled"], synth)["a_frequency_Hz"]
        backend.set_frequency = slow_set
        synth.set_frequency("a", 3e9)
        time.sleep(0.3)                         # worker is stuck inside the push
        s = synth.status()
        # the stale frame still says "settled" -- but for the OLD frequency, so
        # a caller that first waits for the echo cannot be fooled
        assert s["a_frequency_Hz"] == old != 3e9
        gate.set()
        wait_for(lambda s: s["a_frequency_Hz"] == 3e9 and s["a_settled"], synth)
        assert backend.read_frequency(0) == 3e9
    finally:
        gate.set()
        synth.shutdown()


def test_phase_is_per_channel(rig):
    synth, backend, _ = rig
    synth.set_phase("b", 90.0)
    s = wait_for(lambda s: s["b_phase_deg"] == 90.0 and s["b_settled"], synth)
    assert s["a_phase_deg"] == 0.0
    assert backend.ch[1].phase_deg == 90.0


def test_parse_channel():
    assert parse_channel("A") == "a" and parse_channel(1) == "b" and parse_channel("0") == "a"
    with pytest.raises(ValueError):
        parse_channel("c")


# ---- clamps ------------------------------------------------------------------

def test_power_clamped_high_with_warn(rig):
    synth, _, events = rig
    synth.set_power("a", 1000.0)
    s = wait_for(lambda s: s["a_settled"] and s["a_power_dBm"] == 20.0, synth)
    assert s["a_power_dBm"] == synth.cfg.limits.power_max_dBm
    assert any(lvl == "warn" and "clamped" in m for lvl, m in events)


def test_frequency_and_phase_clamped(rig):
    synth, _, _ = rig
    synth.set_frequency("b", 1e15)
    synth.set_phase("b", 1000.0)
    s = wait_for(lambda s: s["b_settled"] and s["b_frequency_Hz"] == 24e9, synth)
    assert s["b_phase_deg"] == synth.cfg.limits.phase_max_deg
    synth.set_frequency("b", 0.0)
    wait_for(lambda s: s["b_frequency_Hz"] == synth.cfg.limits.freq_min_Hz, synth)


def test_apply_config_reclamps_to_new_limits(rig):
    synth, _, _ = rig
    synth.set_power("a", 15.0)
    wait_for(lambda s: s["a_power_dBm"] == 15.0, synth)
    synth.cfg.limits.power_max_dBm = 5.0
    synth.apply_config()
    wait_for(lambda s: s["a_power_dBm"] == 5.0 and s["a_settled"], synth)


def test_bad_reference_is_refused(rig):
    synth, _, _ = rig
    with pytest.raises(ValueError):
        synth.set_reference("gps")


# ---- physics of the simulator, seen through the brain -------------------------

def test_leveling_fails_above_the_frequency_dependent_maximum(rig):
    synth, _, _ = rig
    f = 22e9
    assert max_leveled_power(f) < 15.0
    synth.set_frequency("a", 5e9)
    synth.set_power("a", 15.0)
    synth.set_rf("a", True)
    s = wait_for(lambda s: s["a_settled"] and s["a_rf_on"] and s["a_power_dBm"] == 15.0,
                 synth)
    s = wait_for(lambda s: s["a_leveled"], synth)
    synth.set_frequency("a", f)
    s = wait_for(lambda s: s["a_frequency_Hz"] == f and s["a_settled"], synth)
    s = wait_for(lambda s: not s["a_leveled"], synth)
    assert s["a_locked"] is True                # locked, just not at that power


def test_external_reference_missing_unlocks_both_and_blocks_settle(rig):
    synth, _, events = rig
    synth.set_reference("external", 10.0)
    s = wait_for(lambda s: s["reference"] == "external" and not s["a_locked"]
                 and not s["b_locked"], synth)
    synth.set_rf("a", True)
    synth.set_frequency("a", 2e9)
    time.sleep(0.6)                             # several polls
    s = synth.status()
    assert s["a_frequency_Hz"] == 2e9 and s["a_rf_on"]   # echoed ...
    assert s["a_settled"] is False              # ... but NOT settled: a scan waits
    assert s["ref_settled"] is False
    assert any("NOT LOCKED" in m for _, m in events)
    # switching OFF must settle even without a lock: the safe action may not
    # hang a scan (an after_scan "RF off" with the reference cable pulled)
    synth.set_rf("a", False)
    wait_for(lambda s: not s["a_rf_on"] and s["a_settled"] and not s["a_locked"], synth)
    synth.set_rf("a", True)
    synth.set_reference("internal_10MHz")
    wait_for(lambda s: s["a_locked"] and s["a_settled"] and s["ref_settled"], synth)


def test_all_rf_off_waits_for_the_instrument(rig):
    """`rf_all_off` (the all_rf_off action's wait flag) goes true only once the
    worker has actually switched BOTH outputs off -- not when the command is
    merely accepted."""
    synth, backend, _ = rig
    wait_for(lambda s: s["rf_all_off"], synth)          # starts off
    synth.set_rf("a", True)
    synth.set_rf("b", True)
    wait_for(lambda s: s["a_rf_on"] and s["b_rf_on"] and not s["rf_all_off"], synth)
    assert backend.output_on(0) and backend.output_on(1)
    synth.all_rf_off()
    wait_for(lambda s: s["rf_all_off"], synth)
    assert not backend.output_on(0) and not backend.output_on(1)


def test_hardware_group_change_does_not_desync_the_snapshot(rig):
    """The backend was built with pll_off_when_rf_off; flipping it in cfg at
    runtime must not make the snapshot claim a PLL state the hardware lacks."""
    synth, backend, _ = rig
    synth.cfg.hardware.pll_off_when_rf_off = True
    synth.apply_config()
    s = wait_for(lambda s: s["a_settled"], synth)
    assert s["a_pll_on"] is True and backend.ch[0].pll_on is True


def test_external_reference_present_locks():
    synth, _ = build_sim_system(Config(), external_ref_MHz=10.0)
    synth.start()
    try:
        synth.set_reference("external", 10.0)
        wait_for(lambda s: s["reference"] == "external" and s["ref_settled"]
                 and s["a_locked"], synth)
        synth.set_ext_ref(20.0)                 # declared != connected -> no lock
        wait_for(lambda s: s["ext_ref_MHz"] == 20.0 and not s["a_locked"], synth)
    finally:
        synth.shutdown()


def test_pll_off_mode_still_settles_with_rf_off():
    """With the PLL powered down while off, a frequency set cannot lock -- and
    must not block a scan that sets frequency before switching RF on."""
    cfg = Config()
    cfg.hardware.pll_off_when_rf_off = True
    synth, backend = build_sim_system(cfg)
    synth.start()
    try:
        synth.set_frequency("a", 3e9)
        s = wait_for(lambda s: s["a_frequency_Hz"] == 3e9 and s["a_settled"], synth)
        assert s["a_locked"] is False and s["a_pll_on"] is False
        synth.set_rf("a", True)                 # 20 ms PLL boot, then lock
        wait_for(lambda s: s["a_rf_on"] and s["a_locked"] and s["a_settled"], synth)
    finally:
        synth.shutdown()


# ---- threading ---------------------------------------------------------------

def test_no_lost_updates_under_concurrent_setters(rig):
    """Gotcha #1: many setters racing the worker; the LAST value must win."""
    synth, backend, _ = rig

    def hammer(ch, base):
        for i in range(200):
            synth.set_power(ch, base + (i % 10))
        synth.set_power(ch, -7.0 if ch == "a" else -9.0)

    ts = [threading.Thread(target=hammer, args=(ch, -30.0)) for ch in "ab"]
    [t.start() for t in ts]
    [t.join() for t in ts]
    s = wait_for(lambda s: s["a_power_dBm"] == -7.0 and s["b_power_dBm"] == -9.0
                 and s["a_settled"] and s["b_settled"], synth)
    assert backend.ch[0].power_dBm == -7.0 and backend.ch[1].power_dBm == -9.0


def test_status_never_touches_hardware(rig):
    synth, backend, _ = rig
    calls = []
    orig = backend.read_locked
    backend.read_locked = lambda ch: calls.append(threading.current_thread().name) or orig(ch)
    time.sleep(0.5)
    for _ in range(50):
        synth.status()
    assert calls and all(name == "synth-worker" for name in calls)


def test_hardware_error_is_reported_not_fatal(rig):
    synth, backend, events = rig
    orig = backend.read_temperature
    backend.read_temperature = lambda: (_ for _ in ()).throw(IOError("USB unplugged"))
    s = wait_for(lambda s: "USB unplugged" in s["hw_error"], synth)
    assert s["a_settled"] is False
    assert any(lvl == "error" for lvl, _ in events)
    backend.read_temperature = orig
    wait_for(lambda s: s["hw_error"] == "" and s["a_settled"], synth)
