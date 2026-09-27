"""The DspLockIn brain against the simulated SR830.

Most tests run in FAKE TIME: a clock the test advances by hand, and
`poll_once()` called directly instead of the polling thread. That makes the
settling behaviour deterministic -- "is this reading settled?" is exactly the
kind of question a test must not answer by sleeping and hoping.
"""

import math

import pytest

from sr830 import filters, tables
from sr830.config import Config
from sr830.sim_system import build_sim_system


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt, lockin=None, polls=1):
        """Move time forward in `polls` equal steps, polling after each."""
        for _ in range(polls):
            self.t += dt / polls
            if lockin is not None:
                lockin.poll_once()


@pytest.fixture
def rig():
    clock = FakeClock()
    cfg = Config()
    li, sim = build_sim_system(cfg, clock=clock, seed=1)
    sim.noise_V_rtHz = 0.0            # deterministic unless a test wants noise
    sim.drift = 0.0
    events = []
    li._on_event = lambda lvl, msg: events.append((lvl, msg))
    li.start(poll=False)
    yield li, sim, clock, events
    li.shutdown()


def _run_acquisition(li, clock, step=0.005, max_s=10.0):
    n = li.acquire()
    t = 0.0
    while li.status().acquiring and t < max_s:
        clock.advance(step, li)
        t += step
    return n, li.get_sample()


def _run_auto(li, clock, fn, step=0.01, max_s=10.0):
    n = fn()
    t = 0.0
    while li.status().auto_busy and t < max_s:
        clock.advance(step, li)
        t += step
    return n


# ---- lifecycle and settings --------------------------------------------------

def test_start_pushes_the_whole_config_and_reads_it_back(rig):
    li, sim, clock, _ = rig
    cfg = li.cfg
    assert sim.internal and sim.osc_hz == cfg.reference.frequency_Hz
    assert sim.sens == tables.sens_index(cfg.demod.sensitivity)
    assert sim.tc == tables.tc_index(cfg.demod.time_constant)
    assert sim.slope == 3 and sim.sine_V == 0.004
    s = li.status()
    assert s.connected and s.sensitivity == "10 mV" and s.time_constant == "30 ms"
    assert s.order == 4 and s.unit == "V"
    assert s.full_scale == pytest.approx(0.01)


def test_time_constant_snaps_and_the_200_Hz_rule(rig):
    li, sim, clock, events = rig
    li.set_time_constant(0.025)                   # nearest step
    assert li.status().time_constant == "30 ms"
    li.set_time_constant("100 s")                 # 1 kHz detection: 30 s at most
    assert li.status().time_constant == "30 s" and sim.tc == 13
    assert any(lvl == "warn" and "200 Hz" in m for lvl, m in events)
    li.set_frequency(50.0)                        # below 200 Hz the long ones are legal
    li.set_time_constant("100 s")
    assert li.status().time_constant == "100 s"
    li.cfg.limits.tc_max = "1 s"
    li.set_time_constant("10 s")
    assert li.status().time_constant == "1 s"


def test_the_instrument_changing_tau_on_its_own_is_read_back(rig):
    """Above 200 Hz the SR830 cuts a long time constant to 30 s by itself and
    sets LIA status bit 5; the brain must notice and report what is applied."""
    li, sim, clock, events = rig
    li.set_frequency(50.0)
    li.set_time_constant("300 s")
    sim.set_frequency(1000.0)                     # someone turns the knob
    clock.advance(0.05, li)
    assert li.status().time_constant == "30 s"
    assert any("on its own" in m for _, m in events)


def test_sensitivity_labels_numbers_and_current_mode(rig):
    li, sim, *_ = rig
    li.set_sensitivity(3e-3)                      # snaps UP
    assert li.status().sensitivity == "5 mV" and sim.sens == 19
    li.set_input_source("I1M")
    s = li.status()
    assert s.unit == "A" and s.sensitivity == "5 nA"
    assert s.full_scale == pytest.approx(5e-9)
    li.set_sensitivity(3e-9)                      # a number in AMPS in current mode
    assert li.status().sensitivity == "5 nA"
    li.set_sensitivity("100 nA")
    assert li.status().sensitivity == "100 nA" and sim.sens == 23


def test_frequency_and_harmonic_limits_depend_on_each_other(rig):
    li, sim, _, events = rig
    li.set_frequency(2e5)
    assert li.status().freq_set_Hz == 102000.0
    li.set_frequency(30000.0)
    li.set_harmonic(10)                           # 10 x 30 kHz > 102 kHz
    assert li.status().harmonic == 3
    li.set_frequency(50000.0)                     # 3 x 50 kHz > 102 kHz
    assert li.status().freq_set_Hz == pytest.approx(34000.0)
    assert sum(lvl == "warn" for lvl, _ in events) >= 3


def test_frequency_refused_on_external_reference(rig):
    li, *_ = rig
    li.set_reference_source("external")
    with pytest.raises(ValueError, match="EXTERNAL"):
        li.set_frequency(5000.0)


def test_external_reference_locks_and_is_measured(rig):
    li, sim, clock, _ = rig
    sim.ext_ref_Hz = 777.0
    li.set_reference_source("external")
    clock.advance(0.05, li)
    assert li.status().unlocked is True
    clock.advance(0.5, li, polls=10)
    s = li.status()
    assert s.unlocked is False
    assert s.ref_freq_Hz == pytest.approx(777.0)


def test_sine_out_rounds_to_2_mV_and_clamps(rig):
    li, sim, _, events = rig
    li.set_sine_out(0.1233)
    assert li.status().sine_out_set_V == pytest.approx(0.124)
    assert sim.sine_V == pytest.approx(0.124)
    li.set_sine_out(99)
    assert li.status().sine_out_set_V == li.cfg.limits.sine_max_V
    li.set_sine_out(0)
    assert li.status().sine_out_set_V == li.cfg.limits.sine_min_V
    assert any(lvl == "warn" and "clamped" in m for lvl, m in events)


def test_phase_and_aux_out(rig):
    li, sim, *_ = rig
    li.set_phase(200.0)
    assert li.status().phase_deg == 180.0
    li.set_aux_out(2, 12.0)
    assert li.status().aux_out_set_V[1] == 10.5 and sim.aux_out[1] == 10.5
    with pytest.raises(ValueError):
        li.set_aux_out(5, 1.0)


def test_nan_and_bad_enums_are_refused(rig):
    li, *_ = rig
    with pytest.raises(ValueError):
        li.set_frequency(float("nan"))
    with pytest.raises(ValueError):
        li.set_sine_out(float("inf"))
    with pytest.raises(ValueError):
        li.set_reserve("huge")
    with pytest.raises(ValueError):
        li.set_input_source("B")


def test_detuned_internal_reference_sees_nothing(rig):
    li, sim, clock, _ = rig
    li.set_frequency(sim.ext_ref_Hz + 500.0)        # 500 Hz off, tau = 30 ms
    clock.advance(1.0, li, polls=100)
    assert li.status().live["r"] < 1e-3 * abs(sim.signal_A)


# ---- overloads --------------------------------------------------------------

def test_output_overload_when_the_range_is_too_small(rig):
    li, sim, clock, _ = rig
    li.set_sensitivity("1 mV")                      # a 2 mV signal
    clock.advance(1.0, li, polls=50)
    s = li.status()
    assert s.overload["output"] is True
    assert s.live["r"] <= 1.1 * 1e-3 * math.sqrt(2) + 1e-12      # clipped outputs
    n, sample = _run_acquisition(li, clock)
    assert sample["overload"] == 1                  # the sample says so


def test_input_overload_with_low_reserve(rig):
    li, sim, clock, _ = rig
    sim.set_signal(0.5, 0.0)                        # 500 mV on a 2 nV range, low noise
    li.set_sensitivity("2 nV")
    li.set_reserve("low_noise")
    clock.advance(0.1, li)
    assert li.status().overload["input"] is True


# ---- the settle-then-latch acquisition -----------------------------------------

def test_live_reading_matches_signal_once_settled(rig):
    li, sim, clock, _ = rig
    clock.advance(2.0, li, polls=100)
    s = li.status()
    assert s.live["r"] == pytest.approx(abs(sim.signal_A), rel=1e-3)
    assert s.live["theta_deg"] == pytest.approx(30.0, abs=0.1)


def test_settle_time_follows_tau_slope_and_sync_filter(rig):
    li, *_ = rig
    li.set_time_constant("100 ms")
    li.set_slope("12 dB/oct")
    assert li.status().settle_s == pytest.approx(0.1 * filters.settle_tc(2, 99))
    li.set_frequency(50.0)
    li.set_sync_filter(True)                         # + one period below 200 Hz
    assert li.status().settle_s == pytest.approx(0.1 * filters.settle_tc(2, 99) + 1 / 50.0)


def test_acquire_waits_out_the_step_the_live_value_does_not(rig):
    """THE reason `acquire` exists: step the input, read at once -> the live
    value lags; an acquisition started at the same moment lands on the new
    signal because it waits the computed settle time first."""
    li, sim, clock, _ = rig
    clock.advance(2.0, li, polls=100)
    old_r = li.status().live["r"]
    sim.set_signal(5e-3, 30.0)                       # step 2 mV -> 5 mV
    n = li.acquire()
    clock.advance(0.015, li)                         # half a tau
    early = li.status()
    assert early.acquiring and early.acq_id == n
    assert early.live["r"] < 0.5 * (old_r + 5e-3), "live value should still lag"
    while li.status().acquiring:
        clock.advance(0.005, li)
    sample = li.get_sample()
    assert sample["acq_id"] == n
    assert sample["r"] == pytest.approx(5e-3, rel=0.011)
    assert sample["settle_s"] == pytest.approx(li.status().settle_s)
    assert sample["overload"] == 0 and sample["unit"] == "V"


def test_acquisition_id_and_flag_change_together(rig):
    li, *_ = rig
    n = li.acquire()
    s = li.status()
    assert s.acq_id == n and s.acquiring is True


def test_averaging_uses_x_and_y_not_r(rig):
    """On pure noise, averaged R must shrink towards zero. Averaging R itself
    would converge to a POSITIVE number (R is never negative)."""
    li, sim, clock, _ = rig
    sim.signal_A = 0j
    sim.noise_V_rtHz = 1e-4
    li.set_time_constant("1 ms")
    li.set_slope("6 dB/oct")
    li.cfg.acquisition.average_tc = 400.0
    clock.advance(0.2, li, polls=50)
    _, sample = _run_acquisition(li, clock, step=0.001, max_s=2.0)
    assert sample["n_avg"] > 50
    sigma = 1e-4 * math.sqrt(filters.enbw_Hz(1e-3, 1))
    assert sample["r"] < 0.5 * sigma


def test_acquire_refused_when_disconnected():
    li, _ = build_sim_system(Config())
    with pytest.raises(ValueError):
        li.acquire()


# ---- auto functions ------------------------------------------------------------

def test_auto_gain_blocks_setters_and_picks_a_range(rig):
    li, sim, clock, _ = rig
    li.set_sensitivity("1 V")
    clock.advance(1.0, li, polls=20)
    n = li.auto_gain()
    s = li.status()
    assert s.auto_busy and s.auto_id == n and s.auto_name == "gain"
    with pytest.raises(ValueError, match="auto gain"):
        li.set_sensitivity("1 mV")                   # the SR830 is busy
    with pytest.raises(ValueError):
        li.acquire()
    while li.status().auto_busy:
        clock.advance(0.05, li)
    s = li.status()
    assert s.sensitivity == "5 mV"                   # 2 mV with headroom
    assert "1 V -> 5 mV" in s.auto_note


def test_auto_phase_waits_for_the_outputs_to_settle(rig):
    li, sim, clock, _ = rig
    clock.advance(1.0, li, polls=20)
    n = li.auto_phase()
    clock.advance(sim.auto_phase_s + 0.01, li)       # the command itself is done ...
    assert li.status().auto_busy                     # ... but the outputs are not yet
    _run_auto(li, clock, lambda: n)
    s = li.status()
    assert not s.auto_busy and s.phase_deg == pytest.approx(30.0, abs=0.05)
    assert abs(s.live["theta_deg"]) < 0.5


def test_auto_reserve(rig):
    li, sim, clock, _ = rig
    li.set_reserve("high")
    _run_auto(li, clock, li.auto_reserve)
    assert li.status().reserve == "low_noise"


# ---- robustness and safety ---------------------------------------------------------

def test_hardware_error_is_visible_not_zeros(rig):
    li, sim, clock, events = rig

    def boom():
        raise RuntimeError("GPIB gone")
    sim.read_outputs = boom
    clock.advance(0.01, li)
    assert "GPIB gone" in li.status().hw_error
    assert any(lvl == "error" for lvl, _ in events)


def test_apply_config_repairs_bad_values(rig):
    li, sim, _, events = rig
    li.cfg.demod.reserve = "gigantic"               # a typo in the .ini
    li.cfg.reference.sine_out_V = 50.0
    li.cfg.demod.time_constant = "0.1"              # seconds, as text: snapped
    li.apply_config()
    s = li.status()
    assert s.reserve == "normal"
    assert s.sine_out_set_V == li.cfg.limits.sine_max_V
    assert s.time_constant == "100 ms"
    assert any("not valid" in m for _, m in events)


def test_shutdown_makes_the_outputs_safe():
    li, sim = build_sim_system(Config(), seed=2)
    li.start(poll=False)
    li.set_sine_out(1.0)
    li.set_aux_out(1, 5.0)
    li.shutdown()
    assert sim.sine_V == pytest.approx(0.004) and sim.aux_out == [0.0] * 4
    assert li.status().connected is False


def test_shutdown_leaves_outputs_when_told_to():
    cfg = Config()
    cfg.safety.sine_min_on_stop = False
    cfg.safety.aux_out_zero_on_stop = False
    li, sim = build_sim_system(cfg, seed=2)
    li.start(poll=False)
    li.set_sine_out(1.0)
    li.set_aux_out(1, 5.0)
    li.shutdown()
    assert sim.sine_V == pytest.approx(1.0) and sim.aux_out[0] == 5.0


def test_polling_thread_runs_in_real_time():
    cfg = Config()
    cfg.demod.time_constant = "1 ms"
    li, sim = build_sim_system(cfg, seed=2)
    li.start()
    try:
        import time
        deadline = time.monotonic() + 3.0
        n = li.acquire()
        while li.status().acquiring and time.monotonic() < deadline:
            time.sleep(0.01)
        assert li.get_sample().get("acq_id") == n
    finally:
        li.shutdown()


# ---- review additions: the waits a scan relies on -----------------------------------

def test_auto_refused_while_acquiring_and_polls_never_query_a_busy_unit(rig):
    """An auto function in the middle of an averaging window would mix two
    gains/phases in one sample; and the poller must only serial-poll while
    the SR830 is busy (the sim raises on a query then, as GPIB would time out)."""
    li, sim, clock, events = rig
    li.acquire()
    with pytest.raises(ValueError, match="acquisition is running"):
        li.auto_phase()
    while li.status().acquiring:
        clock.advance(0.01, li)
    _run_auto(li, clock, li.auto_gain)
    assert li.status().hw_error == ""
    assert not [m for lvl, m in events if lvl == "error"]


def test_status_frame_never_shows_a_new_id_as_finished(rig):
    """Invariant a scan's wait depends on (gotchas #17 / #28): a frame that says
    'not acquiring' must not carry the id of an acquisition that has only just
    been triggered. Deterministic: a lock that fires acquire() the instant
    status() releases it -- the worst possible moment for the other thread."""
    li, sim, clock, _ = rig
    import threading

    class HookLock:
        def __init__(self):
            self._l = threading.Lock()
            self.armed = False

        def __enter__(self):
            self._l.acquire()

        def __exit__(self, *exc):
            self._l.release()
            if self.armed:
                self.armed = False
                li.acquire()              # "the command thread", right now

    li._lock = HookLock()
    _run_acquisition(li, clock)
    li._lock.armed = True
    s = li.status()
    assert not (s.acquiring is False and s.acq_id == li._acq_id),         "frame shows the new id as already finished"
    assert s.acq_id == s.sample["acq_id"]


def test_auto_started_under_a_running_poller_gives_no_hardware_error():
    """The auto command and the 'busy' mark go out in one locked section, so
    the real-time poller never slips a query in between (on the rig that
    query would time out)."""
    import time
    cfg = Config()
    cfg.hardware.poll_hz = 500.0
    li, sim = build_sim_system(cfg, seed=4)
    sim.auto_gain_s = sim.auto_reserve_s = sim.auto_phase_s = 0.02
    errors = []
    li._on_event = lambda lvl, msg: errors.append(msg) if lvl == "error" else None
    li.start()
    try:
        for _ in range(10):
            li.auto_reserve()
            deadline = time.monotonic() + 3.0
            while li.status().auto_busy and time.monotonic() < deadline:
                time.sleep(0.002)
        assert li.status().hw_error == "" and not errors, errors
    finally:
        li.shutdown()


def test_bools_sent_as_text_over_the_wire_are_parsed(rig):
    li, sim, clock, _ = rig
    from sr830.net.protocol import apply_config_dict
    apply_config_dict(li.cfg, {"demod": {"sync_filter": "False"},
                               "safety": {"aux_out_zero_on_stop": "no"}})
    li.apply_config()
    assert li.cfg.demod.sync_filter is False
    assert li.cfg.safety.aux_out_zero_on_stop is False
    assert sim.sync is False
