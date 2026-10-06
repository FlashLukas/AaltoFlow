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


def test_start_writes_nothing(rig):
    """Adopt-on-start (Lukas, 2026-09-27): start() only READS the unit."""
    brain, backend = rig
    assert brain.status().connected is True
    assert backend.write_log == []
    assert brain.status().adopted is True


def test_start_adopts_preexisting_state():
    """A unit left at -90 deg, 12.5 dB, RF ON by someone else: status shows
    exactly that, the setpoints equal it, and nothing was written."""
    cfg = Config()
    brain, backend = build_sim_system(cfg, phase_deg=-90.0, attenuation_dB=12.5,
                                      output_on=True)
    events = []
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    brain.start()
    try:
        s = brain.status()
        assert s.adopted is True
        assert s.output_on is True                   # left ON, not switched off
        assert s.phase_deg == -90.0 and s.phase_set_deg == -90.0
        assert s.phase_device_deg == -90.0
        assert s.attenuation_dB == 12.5 and s.attenuation_set_dB == 12.5
        assert backend.write_log == []
        assert backend.read_output() is True
        # RF found ON is never silent
        assert any(lvl == "warn" and "RF output is ON" in m for lvl, m in events)
        assert any("adopted from the unit" in m for _, m in events)
    finally:
        brain.shutdown()
    assert backend._output is False                  # shutdown rule unchanged


def test_adopted_phase_is_expressed_in_the_envelope_branch():
    """Envelope 0..360, unit at -90: adopt as 270 (same physical phase), so an
    echo check and the GUI agree with the limits -- still no write."""
    cfg = Config()
    cfg.limits.phase_min_deg, cfg.limits.phase_max_deg = 0.0, 360.0
    brain, backend = build_sim_system(cfg, phase_deg=-90.0)
    brain.start()
    try:
        s = brain.status()
        assert s.phase_set_deg == 270.0 and s.phase_deg == 270.0
        assert s.phase_device_deg == -90.0
        assert backend.write_log == []
    finally:
        brain.shutdown()


def test_adopted_value_outside_limits_is_announced_not_changed():
    cfg = Config()
    cfg.limits.att_min_dB = 20.0                     # a power ceiling
    brain, backend = build_sim_system(cfg, attenuation_dB=5.0)
    events = []
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    brain.start()
    try:
        assert brain.status().attenuation_dB == 5.0
        assert backend.write_log == []
        assert any(lvl == "warn" and "outside the limits" in m for lvl, m in events)
    finally:
        brain.shutdown()


def test_failed_start_read_adopts_later_and_never_pushes_placeholders():
    """If the first read fails the state is UNKNOWN: nothing is written (not
    even by set_config), and the first good read adopts it."""
    cfg = Config()
    brain, backend = build_sim_system(cfg, phase_deg=45.0, attenuation_dB=7.5,
                                      output_on=True)
    backend.fail_next_read = True
    brain.start()
    try:
        assert brain.status().adopted is False
        brain.apply_config()                         # e.g. Settings > Apply
        assert [w for w in backend.write_log if w[0] in ("phase", "att", "output")] == []
        s = wait_for(brain, lambda s: s.adopted)
        assert s.phase_set_deg == 45.0 and s.attenuation_set_dB == 7.5 and s.output_on
    finally:
        brain.shutdown()


def test_a_user_setpoint_is_not_overwritten_by_a_late_adoption():
    cfg = Config()
    brain, backend = build_sim_system(cfg, phase_deg=45.0, attenuation_dB=7.5)
    backend.fail_next_read = True
    brain.start()
    try:
        brain.set_phase(10.0)                        # user acts before the good read
        s = wait_for(brain, lambda s: s.adopted)
        assert s.phase_set_deg == 10.0 and s.phase_deg == 10.0
        assert s.attenuation_set_dB == 7.5           # the rest is still adopted
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


def test_shutdown_keep_outputs_writes_nothing():
    # a restart for a code update: disconnect, but no write at all
    brain, backend = build_sim_system(Config())
    brain.start()
    brain.set_output(True)
    n = len(backend.write_log)
    brain.shutdown(keep_outputs=True)
    assert backend.write_log[n:] == []
    assert backend._output is True and backend._open is False
    assert brain.status().connected is False


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
