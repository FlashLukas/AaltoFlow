"""The brain: clamps, the model envelope, lifecycle and safety, acquisitions,
the thru reference, and the threading rules -- all on the simulator, offline."""

import threading
import time

import numpy as np
import pytest

from signalhound.config import Config
from signalhound.sim_system import build_sim_system


@pytest.fixture
def sa():
    cfg = Config()
    cfg.acquisition.continuous = False
    v, sim = build_sim_system(cfg, realtime=False, seed=1)
    v.events = []
    v._on_event = lambda lvl, msg: v.events.append((lvl, msg))
    v.start(run=False)
    yield v
    v.shutdown()


def _done(v):
    while v.status().acquiring:
        v.step()


def _warned(v, text):
    return any(lvl == "warn" and text in msg for lvl, msg in v.events)


# ---- clamps and the envelope ---------------------------------------------------

def test_center_span_clamp_to_the_model_and_warn(sa):
    assert sa.status().device_model == "SA44B"
    sa.set_center(10e9)                                   # an SA44B stops at 4.4 GHz
    st = sa.status()
    assert st.stop_Hz <= 4.4e9 + 1e-3 and _warned(sa, "clamped")
    sa.set_span(20e9)
    lo, hi = sa.span_limits()
    assert sa.status().span_Hz == hi
    with pytest.raises(ValueError):
        sa.set_center(float("nan"))


def test_start_stop_becomes_center_span(sa):
    sa.set_start_stop(0.5e9, 1.5e9)
    st = sa.status()
    assert st.center_Hz == pytest.approx(1e9) and st.span_Hz == pytest.approx(1e9)
    with pytest.raises(ValueError):
        sa.set_start_stop(2e9, 1e9)


def test_rbw_snaps_vbw_follows_and_cannot_exceed(sa):
    sa.set_rbw(1e6)                                        # SA44B: widest is 250 kHz
    assert sa.status().rbw_Hz == 250e3 and _warned(sa, "RBW")
    sa.set_vbw(250e3)
    sa.set_rbw(10e3)
    assert sa.status().vbw_Hz == 10e3                      # dragged down with the RBW
    sa.set_vbw(1e6)
    assert sa.status().vbw_Hz == 10e3 and _warned(sa, "VBW cannot exceed RBW")


def test_levels_and_counts_clamp(sa):
    sa.set_ref_level(50)
    assert sa.status().ref_level_dBm == 20.0
    sa.set_tg_level(0.0)
    assert sa.status().tg_level_dBm == -10.0              # the TG44A tops out at -10 dBm
    sa.set_tg_level(-99)
    assert sa.status().tg_level_dBm == -30.0
    sa.set_averages(0)
    assert sa.status().averages == 1
    with pytest.raises(ValueError):
        sa.set_detector("quasi-peak")


def test_sa124b_model_widens_the_envelope():
    cfg = Config()
    cfg.hardware.model = "SA124B"
    cfg.acquisition.continuous = False
    v, _ = build_sim_system(cfg, realtime=False)
    v.start(run=False)
    try:
        v.set_center(10e9)
        st = v.status()
        assert st.center_Hz == 10e9 and st.freq_max_Hz == 12.4e9
        v.set_rbw(6e6)
        assert v.status().rbw_Hz == 6e6
    finally:
        v.shutdown()


def test_tg_on_pulls_the_window_into_its_range():
    cfg = Config()
    cfg.hardware.model = "SA124B"
    cfg.acquisition.continuous = False
    v, _ = build_sim_system(cfg, realtime=False)
    v.start(run=False)
    try:
        v.set_center(8e9)
        v.set_tg(True)
        st = v.status()
        assert st.tg_on and st.freq_max_Hz == 4.4e9 and st.stop_Hz <= 4.4e9 + 1e-3
    finally:
        v.shutdown()


# ---- lifecycle and safety -------------------------------------------------------

def test_tg_is_off_at_start_whatever_the_config_said():
    cfg = Config()
    cfg.tracking.on = True
    cfg.acquisition.continuous = False
    v, sim = build_sim_system(cfg, realtime=False)
    v.start(run=False)
    assert v.status().tg_on is False and sim.tg_output_on is False
    v.set_tg(True)
    v.step()
    assert sim.tg_output_on is True
    v.shutdown()
    assert sim.tg_output_on is False                      # shutdown stops the TG output


def test_tg_refused_without_a_tracking_generator():
    cfg = Config()
    cfg.scene.tg_attached = False
    cfg.acquisition.continuous = False
    v, _ = build_sim_system(cfg, realtime=False)
    v.start(run=False)
    try:
        assert v.status().tg_attached is False
        with pytest.raises(ValueError, match="no tracking generator"):
            v.set_tg(True)
    finally:
        v.shutdown()


def test_shutdown_is_safe_twice_and_status_never_touches_hardware(sa):
    calls = []
    orig = sa.backend.finish_sweep
    sa.backend.finish_sweep = lambda: calls.append(1) or orig()
    for _ in range(20):
        sa.status()
    assert calls == []
    sa.shutdown()
    sa.shutdown()
    assert sa.status().connected is False


def test_a_backend_failure_does_not_kill_the_sweep_thread(sa):
    def boom():
        raise RuntimeError("USB unplugged")
    sa.backend.finish_sweep = boom
    sa.cfg.acquisition.continuous = True
    assert sa.step() is False
    assert "USB unplugged" in sa.status().hw_error


# ---- acquisitions ---------------------------------------------------------------

def test_acquire_latches_a_fresh_averaged_trace(sa):
    sa.set_averages(4)
    n = sa.acquire()
    assert sa.status().acquiring and sa.status().acq_id == n
    _done(sa)
    t = sa.get_trace("sample")
    assert t["acq_id"] == n and t["averages"] == 4
    assert t["trace"].shape == (sa.status().points,)
    assert t["freqs_Hz"][0] == pytest.approx(t["start_Hz"])
    assert abs(t["peak_Hz"] - sa.cfg.scene.tone_Hz) <= t["bin_Hz"]
    assert t["peak_dBm"] == pytest.approx(sa.cfg.scene.tone_dBm, abs=0.3)


def test_averaging_is_in_power_not_in_db(sa):
    """Four identical noise levels must average to themselves; in dB the log of
    fluctuating noise would read low. Checked against the power mean."""
    sa.cfg.scene.tone_on = False
    sa.set_averages(8)
    sa.acquire()
    traces = []
    orig = sa.backend.finish_sweep

    def spy():
        z, m = orig()
        traces.append(z.copy())
        return z, m
    sa.backend.finish_sweep = spy
    _done(sa)
    mean = sa.get_trace("sample")["trace"]
    expect = 10 * np.log10(np.mean([10 ** (x / 10) for x in traces], axis=0))
    assert np.allclose(mean, expect)


def test_a_setting_change_restarts_the_acquisition(sa):
    sa.set_averages(3)
    n = sa.acquire()
    sa.step()
    sa.set_rbw(30e3)
    assert _warned(sa, f"acquisition #{n} restarted")
    _done(sa)
    t = sa.get_trace("sample")
    assert t["rbw_Hz"] == 30e3 and t["averages"] == 3


def test_abort_latches_aborted(sa):
    sa.set_averages(5)
    n = sa.acquire()
    sa.abort()
    st = sa.status()
    assert not st.acquiring and st.sample == {**st.sample, "acq_id": n, "aborted": True}
    with pytest.raises(ValueError, match="aborted"):
        sa.get_trace("sample")


def test_frequencies_are_the_next_grid(sa):
    sa.set_span(10e6)
    sa.set_rbw(10e3)
    f = sa.frequencies()
    assert f.size == 2001 and f[1] - f[0] == pytest.approx(5e3)
    assert sa.status().points == 2001


def test_idle_thread_keeps_the_grid_honest(sa):
    sa.set_span(1e6)
    sa.step()                                   # not continuous, no acquisition: idle
    assert sa.status().points == int(1e6 / (sa.cfg.sweep.rbw_Hz / 2)) + 1


# ---- the thru reference and transmission -----------------------------------------

def test_take_reference_needs_the_tg(sa):
    with pytest.raises(ValueError, match="tracking generator"):
        sa.take_reference()


def test_transmission_against_a_thru(sa):
    sa.set_tg(True)
    sa.set_scene("dut_inserted", False)
    r = sa.take_reference()
    _done(sa)
    ref = sa.status().reference
    assert ref["present"] and ref["acq_id"] == r and ref["tg_level_dBm"] == -20.0
    sa.set_scene("dut_inserted", True)
    sa.acquire()
    _done(sa)
    t = sa.get_trace("sample", "transmission")
    assert "trace" not in t and t["reference_acq_id"] == r
    i = np.argmin(np.abs(t["freqs_Hz"] - 1e9))
    assert t["transmission"][i] == pytest.approx(-1.5, abs=0.2)
    assert sa.status().sample["tx_center_dB"] == pytest.approx(-1.5, abs=0.2)


def test_transmission_refused_on_mismatch(sa):
    with pytest.raises(ValueError, match="no sweep"):
        sa.get_trace("last", "transmission")
    sa.set_tg(True)
    sa.take_reference(); _done(sa)
    sa.set_tg_level(-25.0)
    sa.acquire(); _done(sa)
    with pytest.raises(ValueError, match="TG level -25 dBm vs reference -20 dBm"):
        sa.get_trace("sample", "transmission")
    sa.set_tg_level(-20.0)
    sa.set_tg_points(201)
    sa.acquire(); _done(sa)
    with pytest.raises(ValueError, match="201 points vs reference 401"):
        sa.get_trace("sample", "transmission")
    sa.set_tg(False)
    sa.acquire(); _done(sa)
    with pytest.raises(ValueError, match="spectrum, not a tracking-generator sweep"):
        sa.get_trace("sample", "transmission")


def test_aborting_a_reference_clears_the_old_one(sa):
    sa.set_tg(True)
    sa.take_reference(); _done(sa)
    assert sa.status().reference["present"]
    sa.take_reference()
    sa.abort()
    assert sa.status().reference["present"] is False


def test_scene_is_simulation_only_and_clamped(sa):
    sa.set_scene("dut_bandwidth_Hz", -5)
    assert sa.cfg.scene.dut_bandwidth_Hz == 1e3
    sa.set_scene("tone_on", "off")
    assert sa.cfg.scene.tone_on is False
    with pytest.raises(ValueError):
        sa.set_scene("warp_factor", 9)


# ---- threads (gotcha #1 / #28) ---------------------------------------------------------

def test_a_sample_is_never_announced_before_it_exists():
    """Poll status from another thread while the sweep thread runs: whenever a
    frame says 'acq n finished', the sample in that SAME frame must be n."""
    cfg = Config()
    cfg.acquisition.continuous = True
    v, _ = build_sim_system(cfg, realtime=False, seed=2)
    v.start(run=True)
    bad = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            st = v.status()
            if not st.acquiring and st.acq_id and st.sample.get("acq_id") != st.acq_id:
                bad.append((st.acq_id, st.sample.get("acq_id")))

    t = threading.Thread(target=watch)
    t.start()
    try:
        for _ in range(30):
            n = v.acquire()
            end = time.monotonic() + 5
            while time.monotonic() < end:
                st = v.status()
                if st.acq_id == n and not st.acquiring:
                    break
    finally:
        stop.set()
        t.join()
        v.shutdown()
    assert bad == []


# ---- start-up: read, never write (Lukas's rule, 2026-09-27) -----------------------

def test_start_leaves_a_busy_analyser_as_it_found_it():
    """The simulated TG was left EMITTING by whatever used the analyser last,
    and the saved config wants continuous sweeps. Start must neither silence
    the TG nor configure anything; status tells the truth about both."""
    from signalhound.backends.sim import SimulatedAnalyzer
    from signalhound.spectrum import SpectrumAnalyzer
    cfg = Config()
    cfg.hardware.model = "SA124B"
    cfg.acquisition.continuous = True
    sim = SimulatedAnalyzer(cfg, seed=1, time_scale=0.0, tg_output_on=True)
    v = SpectrumAnalyzer(sim, cfg)
    v.start(run=False)
    for _ in range(3):
        v.step()
    st = v.status()
    assert sim.configure_calls == 0 and sim.tg_output_on is True
    assert st.configured is False and st.continuous is False and st.sweeps == 0
    assert st.device_model == "SA124B" and st.freq_max_Hz == pytest.approx(12.4e9)
    v.shutdown()
    assert sim.tg_output_on is False                      # shutdown behaviour unchanged


def test_sweep_on_start_restores_the_old_behaviour():
    cfg = Config()
    cfg.acquisition.sweep_on_start = True
    v, sim = build_sim_system(cfg, realtime=False, seed=1)
    v.start(run=False)
    assert v.status().continuous is True
    assert v.step() is True and sim.configure_calls == 1
    v.shutdown()


def test_continuous_on_is_a_deliberate_request_that_configures(sa):
    assert sa.status().configured is False
    sa.set_continuous(True)
    assert sa.step() is True and sa.status().configured is True
