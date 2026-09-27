"""Amplifier brain against the simulated backend: lifecycle and safety, the
gain envelope and step, the poll thread, the estimate, and read failures."""

import time

import pytest

from dsamp import model
from dsamp.config import Config
from dsamp.sim_system import build_sim_system


@pytest.fixture
def amp():
    cfg = Config()
    a, backend = build_sim_system(cfg)
    events = []
    a._on_event = lambda lvl, msg: events.append((lvl, msg))
    a.events = events
    a.sim = backend
    a.start()
    yield a
    a.shutdown()


def _fresh(a):
    """Force a poll so the snapshot reflects the last command (tests only)."""
    a.poll_once()
    return a.status()


# ---- lifecycle and safety ----------------------------------------------------

def test_start_leaves_the_stage_off_and_gain_at_startup_value(amp):
    s = amp.status()
    assert s.connected is True
    assert s.amp_on is False
    assert s.gain_dB == 0.0
    assert "SIMULATED" in s.idn


def test_start_forces_off_even_if_the_device_was_left_on():
    cfg = Config()
    a, backend = build_sim_system(cfg)
    backend._output = True            # someone left it on from the front panel
    backend.open = lambda: None       # an open() that does NOT switch it off
    a.start()
    try:
        assert backend.read_output() is False
    finally:
        a.shutdown()


def test_startup_gain_is_clamped_to_the_ceiling():
    cfg = Config()
    cfg.amp.startup_gain_dB = 30.0    # above the 10 dB safety ceiling
    a, backend = build_sim_system(cfg)
    a.start()
    try:
        assert backend.read_gain() == cfg.limits.gain_max_dB
    finally:
        a.shutdown()


def test_shutdown_switches_off_and_goes_to_minimum_gain():
    cfg = Config()
    a, backend = build_sim_system(cfg)
    a.start()
    a.set_gain(8.0)
    a.set_amp(True)
    assert backend.read_output() is True
    a.shutdown()
    assert backend._output is False
    assert backend._gain == cfg.limits.gain_min_dB
    assert a.status().connected is False
    a.shutdown()                      # twice must be harmless


def test_amp_on_warns_about_termination(amp):
    amp.set_amp(True)
    assert any(lvl == "warn" and "terminated" in m for lvl, m in amp.events)
    assert _fresh(amp).amp_on is True
    amp.amp_off()
    assert _fresh(amp).amp_on is False


# ---- gain envelope and step ----------------------------------------------------

def test_gain_clamped_to_the_safety_ceiling_with_a_warning(amp):
    amp.set_gain(25.0)
    assert _fresh(amp).gain_dB == amp.cfg.limits.gain_max_dB
    assert any(lvl == "warn" and "clamped" in m for lvl, m in amp.events)


def test_gain_clamped_low(amp):
    amp.set_gain(-5.0)
    assert _fresh(amp).gain_dB == 0.0


def test_gain_snaps_to_the_device_step(amp):
    amp.set_gain(6.2)
    s = _fresh(amp)
    assert s.gain_dB == 6.0 and s.gain_set_dB == 6.0
    amp.set_gain(6.3)
    assert _fresh(amp).gain_dB == 6.5


def test_snapping_never_crosses_the_ceiling(amp):
    amp.cfg.limits.gain_max_dB = 7.3  # not on the 0.5 dB grid
    amp.apply_config()
    amp.set_gain(7.3)                 # nearest step 7.5 would exceed the ceiling
    assert _fresh(amp).gain_dB == 7.0


def test_envelope_is_the_intersection_of_device_and_limits(amp):
    amp.cfg.limits.gain_max_dB = 50.0
    assert amp.gain_range() == (0.0, amp.cfg.hardware.gain_max_dB)
    amp.cfg.limits.gain_max_dB = 12.0
    assert amp.gain_range() == (0.0, 12.0)
    amp.cfg.limits.gain_min_dB = 20.0  # nonsense: must collapse, not invert
    lo, hi = amp.gain_range()
    assert lo <= hi


def test_apply_config_reclamps_a_standing_gain(amp):
    amp.set_gain(9.0)
    amp.cfg.limits.gain_max_dB = 4.0
    amp.apply_config()
    assert _fresh(amp).gain_dB == 4.0


# ---- the poll thread (gotcha #1) --------------------------------------------------

def test_status_does_not_touch_the_hardware(amp):
    """status() returns the worker's snapshot: a read failure must not raise
    out of status(), and the setter must not rewrite the snapshot."""
    amp._stop.set()                   # park the poll thread so it cannot swap
    amp._thread.join(timeout=2.0)     # the snapshot while we look
    before = amp.status()
    amp.set_gain(5.0)
    assert amp.status() is before     # no poll yet -> the same object
    after = _fresh(amp)
    assert after is not before and after.gain_dB == 5.0


def test_poll_thread_picks_up_a_command_by_itself(amp):
    amp.set_gain(3.0)
    t0 = time.monotonic()
    while amp.status().gain_dB != 3.0 and time.monotonic() - t0 < 3.0:
        time.sleep(0.02)
    assert amp.status().gain_dB == 3.0


def test_read_failure_becomes_hw_error_not_a_crash(amp):
    amp.sim.fail_reads = True
    s = _fresh(amp)
    assert s.hw_error and s.connected is True
    assert any(lvl == "error" for lvl, _ in amp.events)
    amp.sim.fail_reads = False
    assert _fresh(amp).hw_error == ""


def test_temperature_rises_with_the_stage_on(amp):
    amp.sim.TAU_S = 0.05              # speed the thermal model up for the test
    t_off = _fresh(amp).temperature_C
    amp.set_amp(True)
    time.sleep(0.3)
    assert _fresh(amp).temperature_C > t_off + 5.0


# ---- operating point and estimate ----------------------------------------------------

def test_estimate_follows_the_datasheet_rolloff(amp):
    amp.set_gain(10.0)
    amp.set_frequency(1e9)
    g1 = _fresh(amp).est_gain_dB
    amp.set_frequency(6e9)
    g6 = _fresh(amp).est_gain_dB
    assert g1 == pytest.approx(10.0)
    assert g6 == pytest.approx(10.0 - (31.0 - 20.0))   # datasheet: 20 dB at 6 GHz


def test_frequency_and_input_are_clamped(amp):
    amp.set_frequency(20e9)
    amp.set_input_power(25.0)
    s = _fresh(amp)
    assert s.frequency_Hz == amp.cfg.limits.freq_max_Hz
    assert s.input_dBm == amp.cfg.limits.input_max_dBm


def test_output_warning_when_the_estimate_is_too_hot(amp):
    amp.cfg.limits.gain_max_dB = 31.0
    amp.set_frequency(1e9)
    amp.set_input_power(0.0)
    amp.set_gain(25.0)
    amp.set_amp(True)
    s = _fresh(amp)
    assert s.output_warning is True
    assert s.compression_dB > 0.5
    assert any(lvl == "warn" and "estimated output" in m for lvl, m in amp.events)


def test_compression_model_is_one_dB_at_p1db():
    p1 = 22.0
    out = model.compressed_output_dBm(p1 + 1.0, p1)
    assert out == pytest.approx(p1, abs=1e-6)
    # far below compression the amplifier is linear
    assert model.compressed_output_dBm(-20.0, p1) == pytest.approx(-20.0, abs=1e-3)


def test_droop_is_monotonic_across_the_band():
    ds = [model.droop_dB(f * 1e9) for f in (0.05, 1, 2, 3, 4, 5, 6)]
    assert ds == sorted(ds)
    assert ds[0] == 0.0
