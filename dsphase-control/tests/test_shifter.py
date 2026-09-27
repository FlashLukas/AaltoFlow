"""The PhaseShifter brain against the simulated backend: rounding, wrapping,
clamping, the read-back worker, safety on start/shutdown, and a hardware read
failure."""

import threading
import time

import pytest

from dsphase.config import Config
from dsphase.sim_system import build_sim_system


def wait_for(brain, pred, timeout=2.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        s = brain.status()
        if pred(s):
            return s
        time.sleep(0.01)
    return brain.status()


@pytest.fixture
def rig():
    cfg = Config()
    brain, backend = build_sim_system(cfg)
    events = []
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    brain.events = events
    brain.start()
    yield brain, backend
    brain.shutdown()


def test_start_leaves_output_off(rig):
    brain, backend = rig
    s = brain.status()
    assert s.connected is True
    assert s.output_on is False
    assert backend.read_output() is False


def test_start_output_off_even_if_backend_was_on():
    cfg = Config()
    brain, backend = build_sim_system(cfg)
    backend._output = True              # a unit left on by someone else
    brain.start()
    try:
        assert backend.read_output() is False
    finally:
        brain.shutdown()


def test_phase_is_rounded_to_the_step(rig):
    brain, backend = rig
    brain.set_phase(33.3)
    s = wait_for(brain, lambda s: s.phase_deg == 33.5)
    assert s.phase_deg == 33.5
    assert s.phase_set_deg == 33.5
    assert backend.read_phase() == 33.5


def test_phase_wraps_but_reports_in_callers_branch(rig):
    brain, backend = rig
    brain.set_phase(270.0)
    s = wait_for(brain, lambda s: s.phase_deg == 270.0)
    assert s.phase_deg == 270.0                 # the branch the scan asked for
    assert s.phase_device_deg == -90.0          # what the unit really holds
    assert backend.read_phase() == -90.0


def test_a_full_turn_sweep_echoes_every_point(rig):
    brain, _ = rig
    for target in (0, 90, 180, 270, 360):
        brain.set_phase(target)
        s = wait_for(brain, lambda s: s.phase_deg == target)
        assert s.phase_deg == target


def test_status_is_a_readback_not_memory(rig):
    """If the unit disagrees with what we asked, status must show the unit."""
    brain, backend = rig
    brain.set_phase(40.0)
    wait_for(brain, lambda s: s.phase_deg == 40.0)
    backend._phase = 12.0                       # someone pressed the front panel
    s = wait_for(brain, lambda s: s.phase_device_deg == 12.0)
    assert s.phase_device_deg == 12.0
    assert s.phase_deg == 12.0 and s.phase_set_deg == 40.0


def test_phase_clamped_with_warning(rig):
    brain, _ = rig
    brain.set_phase(1000.0)
    s = wait_for(brain, lambda s: s.phase_set_deg == brain.cfg.limits.phase_max_deg)
    assert s.phase_set_deg == brain.cfg.limits.phase_max_deg
    assert any("clamped" in m for lvl, m in brain.events if lvl == "warn")


def test_attenuation_rounded_and_clamped(rig):
    brain, _ = rig
    brain.set_attenuation(6.1)
    assert wait_for(brain, lambda s: s.attenuation_dB == 6.0).attenuation_dB == 6.0
    brain.set_attenuation(99.0)
    assert wait_for(brain, lambda s: s.attenuation_dB == 30.0).attenuation_dB == 30.0
    brain.set_attenuation(-3.0)
    assert wait_for(brain, lambda s: s.attenuation_dB == 0.0).attenuation_dB == 0.0


def test_attenuation_floor_caps_power(rig):
    brain, _ = rig
    brain.cfg.limits.att_min_dB = 10.0
    brain.set_attenuation(0.0)
    assert wait_for(brain, lambda s: s.attenuation_dB == 10.0).attenuation_dB == 10.0


def test_frequency_clamped_and_selects_accuracy(rig):
    brain, _ = rig
    brain.set_frequency(10_000.0)
    s = wait_for(brain, lambda s: s.frequency_MHz == 6000.0)
    assert s.frequency_MHz == 6000.0
    brain.set_phase(45.0)
    s = wait_for(brain, lambda s: s.phase_deg == 45.0)
    assert s.accuracy_deg == 4.0


def test_frequency_not_sent_without_a_command(rig):
    brain, backend = rig
    brain.set_frequency(3000.0)
    # the sim records what it was told; the brain always calls set_frequency,
    # but the REAL backend drops it when freq_command is empty (test_backend)
    assert backend._freq == 3000.0


def test_output_on_off(rig):
    brain, backend = rig
    brain.set_output(True)
    assert wait_for(brain, lambda s: s.output_on).output_on is True
    brain.set_output(False)
    assert wait_for(brain, lambda s: not s.output_on).output_on is False


def test_shutdown_switches_output_off():
    cfg = Config()
    brain, backend = build_sim_system(cfg)
    brain.start()
    brain.set_output(True)
    assert backend.read_output() is True
    brain.shutdown()
    assert backend._output is False
    assert brain.status().connected is False
    assert brain.status().output_on is False
    brain.shutdown()                             # twice is harmless


def test_apply_config_rerounds_to_a_new_step(rig):
    brain, _ = rig
    brain.set_phase(10.0)
    wait_for(brain, lambda s: s.phase_deg == 10.0)
    brain.cfg.device.phase_step_deg = 5.625      # a PS6000P-style unit
    brain.apply_config()
    s = wait_for(brain, lambda s: s.phase_deg == 11.25)
    assert s.phase_deg == 11.25 and s.phase_step_deg == 5.625


def test_read_failure_is_reported_and_recovers(rig):
    brain, backend = rig
    brain.set_phase(20.0)
    wait_for(brain, lambda s: s.phase_deg == 20.0)
    backend.fail_next_read = True
    s = wait_for(brain, lambda s: bool(s.hw_error))
    assert "read failed" in s.hw_error
    assert s.phase_deg == 20.0                   # last good readback kept
    s = wait_for(brain, lambda s: not s.hw_error)
    assert s.hw_error == ""
    assert any(lvl == "error" for lvl, _ in brain.events)


def test_status_never_touches_hardware(rig):
    """status() returns the worker's snapshot -- no backend call (gotcha #1)."""
    brain, backend = rig
    calls = []
    orig = backend.read_phase
    backend.read_phase = lambda: calls.append(1) or orig()
    brain._stop.set()
    brain._kick.set()                            # park the worker
    time.sleep(0.3)
    calls.clear()
    for _ in range(50):
        brain.status()
    assert calls == []


def test_concurrent_setters_do_not_lose_updates(rig):
    """Hammer set_phase from threads while the worker polls: the final
    snapshot must agree with the last setpoint (gotcha #1 lost-update race)."""
    brain, _ = rig

    def worker(base):
        for i in range(40):
            brain.set_phase(base + i * 0.5)

    ts = [threading.Thread(target=worker, args=(b,)) for b in (0.0, 100.0)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    brain.set_phase(123.5)
    s = wait_for(brain, lambda s: s.phase_deg == 123.5)
    assert s.phase_deg == 123.5 and s.phase_set_deg == 123.5


def test_rf_at_start_is_allowed_but_announced():
    cfg = Config()
    cfg.signal.output_on = True
    brain, backend = build_sim_system(cfg)
    events = []
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    brain.start()
    try:
        assert backend.read_output() is True
        assert any(lvl == "warn" and "RF output" in msg for lvl, msg in events)
    finally:
        brain.shutdown()
    assert backend._output is False
