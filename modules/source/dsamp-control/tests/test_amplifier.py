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

def test_start_adopts_the_simulators_default_leftover_state(amp):
    """The simulator starts at a plausible leftover (6 dB, off), NOT the device
    minimum, so every test run exercises adoption."""
    s = amp.status()
    assert s.connected is True
    assert s.amp_on is False
    assert s.gain_dB == 6.0 and s.gain_set_dB == 6.0
    assert "SIMULATED" in s.idn


class _NoWriteDuringStart:
    """Wraps a backend and FAILS on any state-changing call while `armed`.
    Only open(), the reads and idn() are allowed at start."""

    def __init__(self, inner):
        self.inner = inner
        self.armed = True
        self.writes = []

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if name.startswith("set_") or name in ("close",):
            def guarded(*a, **kw):
                if self.armed:
                    raise AssertionError(f"start wrote to the amplifier: {name}{a}")
                self.writes.append((name, a))
                return attr(*a, **kw)
            return guarded
        return attr


def _adopting_system(gain=12.5, on=True):
    """A simulator left ON at 12.5 dB -- above the 10 dB safety ceiling."""
    from dsamp.amplifier import Amplifier
    from dsamp.backends.sim import SimulatedGB6000L
    cfg = Config()
    hw = cfg.hardware
    sim = SimulatedGB6000L(hw.gain_min_dB, hw.gain_max_dB, hw.gain_step_dB,
                           initial_gain_dB=gain, initial_on=on)
    guard = _NoWriteDuringStart(sim)
    a = Amplifier(guard, cfg)
    events = []
    a._on_event = lambda lvl, msg: events.append((lvl, msg))
    return a, sim, guard, events


def test_start_issues_no_state_changing_writes_and_adopts():
    a, sim, guard, events = _adopting_system()
    a.start()                                    # the guard raises on any set_*
    try:
        guard.armed = False
        s = a.status()
        # the status tells the truth: ON, 12.5 dB -- even above the ceiling
        assert s.amp_on is True
        assert s.gain_dB == 12.5 and s.gain_set_dB == 12.5
        assert sim._output is True and sim._gain == 12.5      # untouched
        assert guard.writes == []
        msgs = " ".join(m for _, m in events)
        assert "adopted" in msgs and "left as found" in msgs and "already ON" in msgs
        # the ceiling still protects the NEXT explicit setting
        a.set_gain(12.5)
        assert _fresh(a).gain_dB == a.cfg.limits.gain_max_dB
    finally:
        guard.armed = False
        a.shutdown()
    # shutdown behaviour is unchanged: off + minimum gain
    assert sim._output is False and sim._gain == a.cfg.limits.gain_min_dB


def test_start_adopts_off_state_without_writing():
    a, sim, guard, _ = _adopting_system(gain=3.0, on=False)
    a.start()
    try:
        guard.armed = False
        s = a.status()
        assert s.amp_on is False and s.gain_dB == 3.0 and s.gain_set_dB == 3.0
        assert guard.writes == []
    finally:
        guard.armed = False
        a.shutdown()


def test_failed_startup_read_is_retried_and_then_adopted():
    """If the device does not answer at start, nothing is written and nothing is
    guessed into it: the poll adopts the state as soon as reads work."""
    a, sim, guard, events = _adopting_system(gain=4.5, on=True)
    sim.fail_reads = True
    a.start()
    try:
        assert a.status().hw_error
        assert sum("could not read" in m for _, m in events) == 1
        a.poll_once()                            # still failing: no second error event
        assert sum("could not read" in m for _, m in events) == 1
        sim.fail_reads = False
        a.poll_once()
        s = a.status()
        assert s.amp_on is True and s.gain_dB == 4.5 and s.gain_set_dB == 4.5
        assert guard.writes == []
    finally:
        guard.armed = False
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


def test_shutdown_keep_outputs_leaves_stage_and_gain():
    # a restart for a code update: disconnect, no output or gain command
    cfg = Config()
    a, backend = build_sim_system(cfg)
    a.start()
    a.set_gain(8.0)
    a.set_amp(True)
    calls = []
    backend.set_output = lambda on: calls.append(("out", on))   # spies
    backend.set_gain = lambda dB: calls.append(("gain", dB))
    a.shutdown(keep_outputs=True)
    assert calls == []
    assert backend._output is True and backend._gain == 8.0
    assert backend._open is False and a.status().connected is False


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
