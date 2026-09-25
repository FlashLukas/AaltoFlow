"""The LockIn brain against the simulator.

Most tests run in FAKE TIME: a clock the test advances by hand, and
`poll_once()` called directly instead of the polling thread. That makes the
settling behaviour deterministic -- "is this reading settled?" is exactly the
kind of question a test must not answer by sleeping and hoping.
"""

import math

import pytest

from hf2 import filters
from hf2.config import Config
from hf2.sim_system import build_sim_system


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


def _run_acquisition(li, clock, step=0.002, max_s=5.0):
    n = li.acquire()
    t = 0.0
    while li.status().acquiring and t < max_s:
        clock.advance(step, li)
        t += step
    return n, li.get_sample()


# ---- settings ----------------------------------------------------------------

def test_start_pushes_config_and_reports(rig):
    li, sim, clock, _ = rig
    s = li.status()
    assert s.connected
    assert s.reference == ["internal", "internal"]      # frequency set from software
    assert s.freq_set_Hz == [li.cfg.ch1.frequency_Hz, li.cfg.ch2.frequency_Hz]
    assert s.order == [4, 4]
    assert sim.demods[0].enabled and sim.demods[3].enabled
    assert sim.demods[3].input == 1 and sim.demods[3].osc == 1


def test_time_constant_clamps_and_reports_hardware_value(rig):
    li, sim, _, events = rig
    li.set_time_constant(1, 0.0123456)
    s = li.status()
    assert s.tc_set_s[0] == 0.0123456          # what we asked for (the settle echo)
    assert s.tc_s[0] == 0.01235                # what the "hardware" applied
    li.set_time_constant(2, 1e6)
    assert li.status().tc_set_s[1] == li.cfg.limits.tc_max_s
    assert any(lvl == "warn" and "clamped" in m for lvl, m in events)


def test_nan_is_refused_not_passed_through(rig):
    li, *_ = rig
    with pytest.raises(ValueError):
        li.set_time_constant(1, float("nan"))
    with pytest.raises(ValueError):
        li.set_frequency(1, float("inf"))


def test_bad_channel_is_refused(rig):
    li, *_ = rig
    for bad in (0, 3, "x"):
        with pytest.raises(ValueError):
            li.set_order(bad, 2)


def test_order_is_clamped_to_1_to_8(rig):
    li, *_ = rig
    li.set_order(1, 12)
    assert li.status().order[0] == 8
    li.set_order(1, 0)
    assert li.status().order[0] == 1


def test_frequency_refused_on_external_reference(rig):
    li, *_ = rig
    li.set_reference(1, "external")
    with pytest.raises(ValueError, match="EXTERNAL"):
        li.set_frequency(1, 5000.0)


def test_internal_reference_uses_our_frequency(rig):
    li, sim, clock, _ = rig
    li.set_reference(1, "int")
    li.set_frequency(1, 4321.0)
    clock.advance(0.01, li)
    s = li.status()
    assert s.reference[0] == "internal"
    assert s.freq_set_Hz[0] == 4321.0
    assert s.ref_freq_Hz[0] == pytest.approx(4321.0)
    assert s.pll_locked[0] is None


def test_external_reference_locks_to_the_source(rig):
    li, sim, clock, _ = rig
    li.set_reference(2, "external")
    clock.advance(0.05, li)
    assert li.status().pll_locked[1] is False            # still locking
    clock.advance(sim.lock_time_s, li)
    s = li.status()
    assert s.pll_locked[1] is True
    assert s.ref_freq_Hz[1] == pytest.approx(sim.ext_ref_Hz[1])


def test_detuned_internal_reference_sees_nothing(rig):
    """The sim is honest: set the wrong frequency and the signal disappears."""
    li, sim, clock, _ = rig
    li.set_reference(1, "internal")
    li.set_frequency(1, sim.ext_ref_Hz[0] + 500.0)       # 500 Hz off, tau = 10 ms
    clock.advance(1.0, li, polls=200)
    assert li.status().live["r"][0] < 1e-3 * abs(sim.signal[0])


# ---- the settle-then-latch acquisition -----------------------------------------

def test_live_reading_matches_signal_once_settled(rig):
    li, sim, clock, _ = rig
    clock.advance(1.0, li, polls=200)
    s = li.status()
    assert s.live["r"][0] == pytest.approx(abs(sim.signal[0]), rel=1e-3)
    assert s.live["theta_deg"][0] == pytest.approx(30.0, abs=0.1)
    assert s.live["r"][1] == pytest.approx(abs(sim.signal[1]), rel=1e-3)


def test_settle_time_follows_tau_and_order(rig):
    li, *_ = rig
    li.set_time_constant(1, 0.02)
    li.set_order(1, 4)
    assert li.status().settle_s[0] == pytest.approx(0.02 * filters.settle_tc(4, 99))


def test_acquire_waits_out_the_step_the_live_value_does_not(rig):
    """THE reason `acquire` exists.

    Step the input, then read at once: the live value is still near the OLD
    signal. An acquisition started at the same moment only latches after the
    computed settle time, and lands on the NEW signal.
    """
    li, sim, clock, _ = rig
    clock.advance(1.0, li, polls=200)               # settle on the initial signal
    old_r = li.status().live["r"][0]

    sim.set_signal(0, 5e-3, 30.0)                   # step 2 mV -> 5 mV
    n = li.acquire()
    clock.advance(0.005, li)                        # 5 ms: half a tau
    early = li.status()
    assert early.acquiring and early.acq_id == n
    assert early.live["r"][0] < 0.5 * (old_r + 5e-3), "live value should still lag"

    while li.status().acquiring:
        clock.advance(0.002, li)
    sample = li.get_sample()
    assert sample["acq_id"] == n
    # 99 % settled by construction; the rest is filter tail
    assert sample["r"][0] == pytest.approx(5e-3, rel=0.011)
    assert sample["settle_s"] == pytest.approx(max(li.status().settle_s))


def test_acquisition_id_and_flag_change_together(rig):
    """No snapshot may show the new id with a stale acquiring=False."""
    li, sim, clock, _ = rig
    n = li.acquire()
    s = li.status()
    assert s.acq_id == n and s.acquiring is True


def test_averaging_uses_x_and_y_not_r(rig):
    """On pure noise, averaged R must shrink towards zero. Averaging R itself
    would converge to a POSITIVE number (R is never negative)."""
    li, sim, clock, _ = rig
    sim.signal = [0j, 0j]
    sim.noise_V_rtHz = 1e-3
    for ch in (1, 2):                 # the window follows the SLOWER channel
        li.set_time_constant(ch, 1e-3)
        li.set_order(ch, 1)
    li.cfg.acquisition.average_tc = 400.0
    clock.advance(0.2, li, polls=50)
    _, sample = _run_acquisition(li, clock, step=0.001, max_s=2.0)
    assert sample["n_avg"] > 50
    sigma = 1e-3 * math.sqrt(filters.enbw_Hz(1e-3, 1))
    assert sample["r"][0] < 0.5 * sigma


def test_average_window_is_measured_in_time_constants(rig):
    li, sim, clock, _ = rig
    li.set_time_constant(1, 0.01)
    li.set_time_constant(2, 0.02)
    li.cfg.acquisition.average_tc = 5
    _, sample = _run_acquisition(li, clock, step=0.002)
    assert sample["avg_s"] == pytest.approx(5 * 0.02)     # the slower channel


def test_acquire_refused_when_disconnected():
    li, _ = build_sim_system(Config())
    with pytest.raises(ValueError):
        li.acquire()


# ---- robustness ------------------------------------------------------------------

def test_hardware_error_is_visible_not_zeros(rig):
    li, sim, clock, events = rig

    def boom(_):
        raise RuntimeError("USB gone")
    sim.read_demods = boom
    clock.advance(0.01, li)
    s = li.status()
    assert "USB gone" in s.hw_error
    assert any(lvl == "error" for lvl, _ in events)


def test_apply_config_reclamps_and_repushes(rig):
    li, sim, *_ = rig
    li.cfg.ch1.time_constant_s = 1e9
    li.cfg.ch2.order = 99
    li.apply_config()
    s = li.status()
    assert s.tc_set_s[0] == li.cfg.limits.tc_max_s
    assert s.order[1] == 8
    assert sim.demods[3].order == 8


def test_polling_thread_runs_in_real_time():
    li, sim = build_sim_system(Config(), seed=2)
    li.cfg.ch1.time_constant_s = 1e-3
    li.cfg.ch2.time_constant_s = 1e-3
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
