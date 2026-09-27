"""The LockIn brain against the simulated 7230.

Most tests run in FAKE TIME: a clock the test advances by hand, and
`poll_once()` called directly instead of the polling thread. That makes the
settling behaviour deterministic -- "is this reading settled?" is exactly the
kind of question a test must not answer by sleeping and hoping.
"""

import math

import pytest

from sr7230 import filters, tables
from sr7230.config import Config
from sr7230.sim_system import build_sim_system


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
    cfg.filter.time_constant_s = 0.01
    cfg.signal.sensitivity_index = 20            # 5 mV: the 2 mV signal sits at 40 %
    li, sim = build_sim_system(cfg, clock=clock, seed=1)
    sim.noise_V_rtHz = sim.noise_fet_V_rtHz = 0.0  # deterministic unless a test wants noise
    sim.floor_fraction = 0.0
    sim.drift = 0.0
    sim.harmonics = {1: 1.0}
    events = []
    li._on_event = lambda lvl, msg: events.append((lvl, msg))
    li.start(poll=False)
    yield li, sim, clock, events
    li.shutdown()


def _run_acquisition(li, clock, step=0.002, max_s=5.0):
    n = li.acquire()
    t = 0.0
    while li.status().acquiring and t < max_s:
        clock.advance(step, li)
        t += step
    return n, li.get_sample()


# ---- start-up, shutdown, OSC OUT safety -------------------------------------------

def test_start_pushes_config_and_reports(rig):
    li, sim, clock, _ = rig
    s = li.status()
    assert s.connected and s.ref_source == "internal"
    assert s.tc_s == 0.01 and sim.tc_index == 9          # TC 9 = 10 ms
    assert sim.sen_index == 20 and s.sensitivity == "5 mV"
    assert s.slope == "12 dB/oct" and sim.slope_index == 1
    assert s.ref_locked is None                           # internal: nothing to lock


def test_osc_out_is_zeroed_at_start_even_if_the_config_says_otherwise():
    cfg = Config()
    cfg.reference.amplitude_V = 0.5
    li, sim = build_sim_system(cfg, seed=0)
    sim.osc_amp = 0.3                                     # someone left it on
    li.start(poll=False)
    try:
        assert sim.osc_amp == 0.0 and li.status().amplitude_V == 0.0
    finally:
        li.shutdown()


def test_osc_out_can_be_kept_across_a_start_if_asked():
    cfg = Config()
    cfg.reference.amplitude_V = 0.5
    cfg.hardware.osc_zero_on_start = False
    li, sim = build_sim_system(cfg, seed=0)
    li.start(poll=False)
    try:
        assert sim.osc_amp == 0.5
    finally:
        li.shutdown()


def test_shutdown_returns_osc_out_to_zero(rig):
    li, sim, *_ = rig
    li.set_amplitude(0.2)
    assert sim.osc_amp == 0.2
    li.shutdown()
    assert sim.osc_amp == 0.0 and not li.status().connected
    li.shutdown()                                         # twice is fine


def test_amplitude_clamps_to_the_envelope(rig):
    li, sim, _, events = rig
    li.set_amplitude(4.0)
    assert li.status().amplitude_V == li.cfg.limits.amplitude_max_V == sim.osc_amp
    assert any(lvl == "warn" and "clamped" in m for lvl, m in events)
    li.set_amplitude(-1.0)
    assert sim.osc_amp == 0.0


# ---- settings: clamps, snapping, mode-dependent limits -------------------------------

def test_time_constant_snaps_to_the_table(rig):
    li, sim, *_ = rig
    li.set_time_constant(0.0123)
    s = li.status()
    assert s.tc_set_s == 0.0123                 # what we asked for (the settle echo)
    assert s.tc_s == 0.01                        # what the instrument applied
    li.set_time_constant(0.07)
    assert li.status().tc_s == 0.05


def test_short_time_constants_need_fast_mode(rig):
    li, sim, _, events = rig
    li.set_time_constant(1e-4)
    assert li.status().tc_s == tables.TC_MIN_NORMAL_S
    assert any("fast mode" in m for _, m in events)
    li.set_fast_mode(True)
    li.set_time_constant(1e-4)
    assert li.status().tc_s == 1e-4 and sim.fast


def test_fast_mode_limits_the_slope_and_leaving_it_raises_the_tc(rig):
    li, sim, _, events = rig
    li.set_slope(24)
    li.set_fast_mode(True)
    assert li.status().slope_db == 12 and sim.slope_index == 1
    li.set_slope("18 dB/oct")
    assert li.status().slope_db == 12
    li.set_time_constant(2e-4)
    li.set_fast_mode(False)                      # 200 us is illegal again
    assert li.status().tc_s == 5e-3 and sim.tc_index == 8


def test_sensitivity_by_label_index_and_value(rig):
    li, sim, *_ = rig
    li.set_sensitivity("1 mV")
    assert sim.sen_index == 18
    li.set_sensitivity(21)
    assert li.status().sensitivity == "10 mV"
    li.set_full_scale(3e-3)                       # smallest range that holds 3 mV
    assert li.status().sensitivity == "5 mV"
    li.set_sensitivity(99)                        # clamped to the table
    assert li.status().sensitivity == "1 V"
    with pytest.raises(ValueError):
        li.set_sensitivity("3 mV")                # not a range the 7230 has


def test_current_mode_changes_the_unit_and_moves_an_illegal_range(rig):
    li, sim, *_ = rig
    li.set_sensitivity(5)
    li.set_input("I low-noise")                   # starts at index 7
    s = li.status()
    assert s.unit == "A" and s.sensitivity_index == 7 and s.sensitivity == "2 fA"
    assert (sim.imode, sim.vmode) == (2, 1)
    li.set_input("A-B")
    assert li.status().unit == "V" and (sim.imode, sim.vmode) == (0, 3)


def test_harmonic_divides_the_frequency_limit(rig):
    li, sim, _, events = rig
    li.set_frequency(50e3)
    li.set_harmonic(3)                            # 150 kHz > 120 kHz: clamp to 2
    assert li.status().harmonic == 2
    assert any(lvl == "warn" and "harmonic" in m for lvl, m in events)
    li.set_frequency(100e3)                       # 2 x 100 kHz > 120 kHz: clamp
    assert li.status().freq_set_Hz == 60e3
    li.cfg.hardware.option_250kHz = True
    assert li.freq_max_Hz() == 125e3


def test_phase_wraps_but_keeps_plus_180(rig):
    li, sim, *_ = rig
    li.set_phase(190.0)
    assert li.status().phase_deg == pytest.approx(-170.0)
    li.set_phase(180.0)
    assert li.status().phase_deg == 180.0


def test_nan_and_nonsense_are_refused(rig):
    li, *_ = rig
    with pytest.raises(ValueError):
        li.set_time_constant(float("nan"))
    with pytest.raises(ValueError):
        li.set_frequency(float("inf"))
    with pytest.raises(ValueError):
        li.set_slope(9)
    with pytest.raises(ValueError):
        li.set_reference("sideways")
    with pytest.raises(ValueError):
        li.set_input("C")
    with pytest.raises(ValueError):
        li.set_coupling("maybe")


# ---- reference -------------------------------------------------------------------------

def test_detuned_internal_reference_sees_nothing(rig):
    """The sim is honest: set the wrong frequency and the signal disappears."""
    li, sim, clock, _ = rig
    li.set_frequency(sim.signal_Hz + 500.0)
    clock.advance(1.0, li, polls=200)
    assert li.status().live["r"] < 1e-3 * abs(sim.signal)


def test_external_reference_locks_and_reads_zero_until_then(rig):
    li, sim, clock, _ = rig
    sim.signal_Hz = 777.0                        # the source is NOT at our oscillator
    li.set_reference("ext_ttl")
    clock.advance(0.05, li)
    s = li.status()
    assert s.ref_locked is False and s.ref_freq_Hz == 0.0     # FRQ reads 0 unlocked
    clock.advance(sim.lock_time_s, li)
    clock.advance(0.5, li, polls=100)
    s = li.status()
    assert s.ref_locked is True and s.ref_freq_Hz == pytest.approx(777.0)
    assert s.live["r"] == pytest.approx(abs(sim.signal), rel=2e-2)


def test_the_oscillator_drives_the_sample(rig):
    """With the source switched off, only OSC OUT excites the sample."""
    li, sim, clock, _ = rig
    sim.signal = 0j
    clock.advance(0.5, li, polls=100)
    assert li.status().live["r"] < 1e-9
    li.set_amplitude(0.5)
    clock.advance(0.5, li, polls=100)
    assert li.status().live["r"] == pytest.approx(0.5 * abs(sim.osc_response), rel=1e-2)


# ---- overloads -----------------------------------------------------------------------------

def test_output_overload_clips_and_is_reported(rig):
    li, sim, clock, _ = rig
    li.set_sensitivity("500 uV")                  # 2 mV signal = 400 % of full scale
    clock.advance(0.5, li, polls=100)
    s = li.status()
    assert s.overload["output"] and s.live["r_fs"] <= 3 * math.sqrt(2) + 1e-9
    _, sample = _run_acquisition(li, clock)
    assert sample["overload"] is True


def test_a_clean_sample_says_so(rig):
    li, sim, clock, _ = rig
    clock.advance(0.5, li, polls=100)
    _, sample = _run_acquisition(li, clock)
    assert sample["overload"] is False and sample["ref_locked"] is True
    assert sample["unit"] == "V"


# ---- auto operations ---------------------------------------------------------------------

def test_auto_measure_sets_range_and_phase(rig):
    li, sim, clock, _ = rig
    li.set_sensitivity("1 V")
    clock.advance(0.5, li, polls=100)
    n = li.auto("auto_measure")                  # no thread: runs at once
    s = li.status()
    assert s.auto_id == n and s.auto_busy is False and s.auto_error == ""
    assert s.sensitivity == "5 mV"               # 2 mV at 40 %
    assert s.phase_deg == pytest.approx(30.0, abs=0.5)
    clock.advance(0.2, li, polls=50)
    assert abs(li.status().live["theta_deg"]) < 0.5


def test_auto_error_is_reported_not_raised(rig):
    li, sim, *_ = rig

    def broken():
        raise RuntimeError("no signal")
    sim.auto_phase = broken
    li.auto("auto_phase")
    s = li.status()
    assert not s.auto_busy and "no signal" in s.auto_error


def test_auto_measure_that_changes_tau_moves_the_settle_time(rig):
    """The manual (6.7.02) says ASM changes the time constant. The brain must
    read it back, or later acquisitions settle on the OLD tau -- too early."""
    li, sim, clock, _ = rig
    before = li.settle_time_s()
    real_asm = sim.auto_measure

    def asm_that_moves_tau():
        real_asm()
        sim.tc_index = tables.TIME_CONSTANTS_S.index(0.2)   # what the box might pick
    sim.auto_measure = asm_that_moves_tau
    li.auto("auto_measure")
    s = li.status()
    assert s.tc_s == pytest.approx(0.2) and s.tc_set_s == pytest.approx(0.2)
    assert li.settle_time_s() == pytest.approx(before * 20.0)   # 0.01 s -> 0.2 s


def test_phase_read_back_after_auto_phase_is_wrapped(rig):
    li, sim, *_ = rig
    sim.auto_phase = lambda: None
    sim.get_phase = lambda: 250.0                # REFP. may report up to +-360
    li.auto("auto_phase")
    assert li.status().phase_deg == pytest.approx(-110.0)


def test_changes_are_refused_while_an_auto_operation_runs(rig):
    """While AS / ASM run, the poll thread holds the instrument; a setter would
    block the command thread for minutes. It is refused instead -- except the
    OSC OUT amplitude, which must always be able to go down."""
    li, *_ = rig
    with li._lock:                                # as `auto()` leaves it with a poll thread
        li._auto_busy, li._auto_op = True, "auto_sensitivity"
    for call in (lambda: li.set_time_constant(0.1), lambda: li.set_sensitivity(20),
                 lambda: li.set_phase(10), lambda: li.set_frequency(500),
                 lambda: li.acquire(), lambda: li.apply_config()):
        with pytest.raises(ValueError, match="auto-sensitivity is still running"):
            call()
    li.set_amplitude(0.0)                         # never refused
    with li._lock:
        li._auto_busy = False
    li.set_time_constant(0.1)                     # and accepted again afterwards


def test_unknown_auto_is_refused(rig):
    li, *_ = rig
    with pytest.raises(ValueError):
        li.auto("auto_everything")


# ---- the settle-then-latch acquisition -----------------------------------------------------

def test_live_reading_matches_signal_once_settled(rig):
    li, sim, clock, _ = rig
    clock.advance(1.0, li, polls=200)
    s = li.status()
    assert s.live["r"] == pytest.approx(abs(sim.signal), rel=1e-3)
    assert s.live["theta_deg"] == pytest.approx(30.0, abs=0.1)
    assert s.live["r_fs"] == pytest.approx(2e-3 / 5e-3, rel=1e-3)


def test_settle_time_follows_tau_and_slope(rig):
    li, *_ = rig
    li.set_time_constant(0.02)
    li.set_slope(24)
    assert li.status().settle_s == pytest.approx(0.02 * filters.settle_tc(4, 99))


def test_acquire_waits_out_the_step_the_live_value_does_not(rig):
    """THE reason `acquire` exists."""
    li, sim, clock, _ = rig
    clock.advance(1.0, li, polls=200)
    old_r = li.status().live["r"]
    sim.set_signal(4e-3, 30.0)                   # step 2 mV -> 4 mV
    n = li.acquire()
    clock.advance(0.005, li)                     # half a tau
    early = li.status()
    assert early.acquiring and early.acq_id == n
    assert early.live["r"] < 0.5 * (old_r + 4e-3), "live value should still lag"
    while li.status().acquiring:
        clock.advance(0.002, li)
    sample = li.get_sample()
    assert sample["acq_id"] == n
    assert sample["r"] == pytest.approx(4e-3, rel=0.011)
    assert sample["settle_s"] == pytest.approx(li.status().settle_s)


def test_acquisition_id_and_flag_change_together(rig):
    li, *_ = rig
    n = li.acquire()
    s = li.status()
    assert s.acq_id == n and s.acquiring is True


def test_averaging_uses_x_and_y_not_r(rig):
    """On pure noise, averaged R must shrink towards zero; averaging R itself
    would converge to a POSITIVE number (R is never negative)."""
    li, sim, clock, _ = rig
    sim.signal = 0j
    sim.noise_V_rtHz = 1e-5
    li.set_time_constant(5e-3)
    li.set_slope(6)
    li.cfg.acquisition.average_tc = 400.0
    clock.advance(0.2, li, polls=50)
    _, sample = _run_acquisition(li, clock, step=0.002, max_s=4.0)
    assert sample["n_avg"] > 50
    sigma = 1e-5 * math.sqrt(filters.enbw_Hz(5e-3, 1))
    assert sample["r"] < 0.5 * sigma


def test_acquire_timeout_grows_with_the_time_constant(rig):
    li, *_ = rig
    li.set_time_constant(10.0)
    li.set_slope(24)
    assert li.acquire_timeout_s() > 3 * 100.0     # 24 dB/oct: ~100 s to settle


def test_acquire_refused_when_disconnected():
    li, _ = build_sim_system(Config())
    with pytest.raises(ValueError):
        li.acquire()


# ---- robustness --------------------------------------------------------------------------

def test_hardware_error_is_visible_not_zeros(rig):
    li, sim, clock, events = rig
    sim.fail_reads = True
    clock.advance(0.01, li)
    s = li.status()
    assert "link failure" in s.hw_error
    assert any(lvl == "error" for lvl, _ in events)
    sim.fail_reads = False
    clock.advance(0.01, li)
    assert li.status().hw_error == ""


def test_apply_config_reclamps_and_repushes(rig):
    li, sim, *_ = rig
    li.cfg.filter.time_constant_s = 1e9
    li.cfg.signal.sensitivity_index = 99
    li.cfg.reference.amplitude_V = 9.0
    li.cfg.filter.slope_db = 24
    li.cfg.filter.fast_mode = True
    li.apply_config()
    s = li.status()
    assert s.tc_s == 1000.0                       # the top of the Limits envelope (1 ks)
    assert s.sensitivity == "1 V" and sim.sen_index == 27
    assert sim.osc_amp == li.cfg.limits.amplitude_max_V
    assert s.slope_db == 12


def test_polling_thread_runs_in_real_time():
    cfg = Config()
    cfg.filter.time_constant_s = 5e-3
    li, sim = build_sim_system(cfg, seed=2)
    li.start()
    try:
        import time
        deadline = time.monotonic() + 3.0
        n = li.acquire()
        while li.status().acquiring and time.monotonic() < deadline:
            time.sleep(0.01)
        assert li.get_sample().get("acq_id") == n
        a = li.auto("auto_sensitivity")          # queued, run by the poll thread
        while li.status().auto_busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert li.status().auto_id == a and not li.status().auto_busy
    finally:
        li.shutdown()
