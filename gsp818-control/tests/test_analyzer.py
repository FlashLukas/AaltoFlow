"""The brain: clamps, lifecycle, safety of the tracking generator, the
scan-safe acquisition and the thru reference. Driven by step(), no thread."""

import threading

import numpy as np
import pytest

from gsp818.analyzer import SpectrumAnalyzer
from gsp818.config import Config
from gsp818.sim_system import build_sim_system


@pytest.fixture
def sa():
    cfg = Config()
    cfg.sweep.points = 401
    cfg.acquisition.continuous = False
    a, sim = build_sim_system(cfg, realtime=False, seed=7)
    events = []
    a._on_event = lambda lvl, msg: events.append((lvl, msg))
    a.start(run=False)
    a.events, a.sim = events, sim
    yield a
    a.shutdown()


def _finish(a):
    while a.status().acquiring:
        a.step()


def _acquire(a):
    n = a.acquire()
    _finish(a)
    return n


# ---- clamps ----------------------------------------------------------------------

def test_setters_clamp_and_warn(sa):
    sa.set_stop(5e9)
    assert sa.status().stop_Hz == 1.8e9 and sa.events[-1][0] == "warn"
    sa.set_rbw(10e6)
    assert sa.status().rbw_set_Hz == 3e6 and sa.status().rbw_auto is False
    sa.set_ref_level(99)
    assert sa.status().ref_level_dBm == 30.0
    sa.set_atten(12.6)
    assert sa.status().atten_set_dB == 13.0              # whole dB
    sa.set_atten(-5)
    assert sa.status().atten_set_dB == 0.0
    sa.set_tg_level(5)
    assert sa.status().tg_level_dBm == 0.0 and "clamped" in sa.events[-1][1]
    sa.set_points(3)
    assert sa.status().points == Config().limits.points_min
    with pytest.raises(ValueError):
        sa.set_start(float("nan"))
    with pytest.raises(ValueError):
        sa.set_detector("rms")
    with pytest.raises(ValueError):
        sa.set_dut("amplifier")
    with pytest.raises(ValueError):
        sa.set_carriers("100 MHz")


def test_start_stop_center_span_bound_each_other(sa):
    sa.set_start(100e6); sa.set_stop(200e6)
    s = sa.status()
    assert (s.center_Hz, s.span_Hz) == (150e6, 100e6)
    sa.set_start(300e6)                                   # would cross stop: clamped
    assert sa.status().start_Hz == 200e6 - Config().limits.min_span_Hz
    sa.set_start(100e6)
    sa.set_span(5e9)                                      # centre 150 MHz stays
    s = sa.status()
    assert s.center_Hz == pytest.approx(150e6) and s.start_Hz == pytest.approx(9e3)
    sa.set_span(10e6); sa.set_center(1.799e9)             # near the edge: the span shrinks
    s = sa.status()
    assert s.center_Hz == pytest.approx(1.799e9) and s.stop_Hz == pytest.approx(1.8e9)
    assert s.span_Hz == pytest.approx(2e6)


def test_auto_and_readback_in_status(sa):
    sa.set_start(99e6); sa.set_stop(101e6)
    sa.step()
    s = sa.status()
    assert s.rbw_auto and s.rbw_Hz == 10e3 and s.detector_in_use == "normal"
    sa.set_rbw(1e3)
    s = sa.status()
    assert s.rbw_Hz == 1e3 and s.rbw_set_Hz == 1e3 and not s.rbw_auto
    sa.set_rbw_auto(True)
    assert sa.status().rbw_Hz == 10e3


# ---- lifecycle and safety -------------------------------------------------------------

def test_tracking_generator_is_off_at_start_even_if_the_ini_says_on():
    cfg = Config()
    cfg.tracking.tg_on = True
    a, sim = build_sim_system(cfg, realtime=False)
    a.start(run=False)
    a.step()
    assert a.status().tg_on is False and sim.tg_output is False
    a.shutdown()


def test_tg_switches_without_a_sweep_and_goes_off_on_shutdown(sa):
    sa.set_tg(True)
    sa.step()                                   # continuous off: no sweep, but applied
    assert sa.sim.tg_output is True and sa.status().tg_on is True
    sa.shutdown()
    assert sa.sim.tg_output is False and sa.cfg.tracking.tg_on is False
    assert sa.status().connected is False
    sa.shutdown()                               # twice is fine


def test_the_sweep_thread_survives_a_backend_failure(sa):
    boom = {"n": 1}

    def finish():
        if boom["n"]:
            boom["n"] -= 1
            raise IOError("USB hiccup")
        return np.full(401, -80.0), {}
    sa.backend.finish_sweep = finish
    sa.acquire()
    assert sa.step() is False and "USB hiccup" in sa.status().hw_error
    _finish(sa)
    assert sa.status().hw_error == "" and sa.get_trace("sample")["points"] == 401


def test_status_never_touches_the_backend(sa):
    def forbidden(*a, **k):
        raise AssertionError("status() must not call the backend")
    for name in ("configure", "start_sweep", "finish_sweep", "idn"):
        setattr(sa.backend, name, forbidden)
    s = sa.status()
    assert s.connected


# ---- acquisition ----------------------------------------------------------------------------

def test_acquire_latches_a_power_average(sa):
    sa.set_averages(4)
    n = _acquire(sa)
    t = sa.get_trace("sample")
    assert t["acq_id"] == n and t["averages"] == 4
    assert t["power_dBm"].shape == (401,) and t["freqs_Hz"][-1] == 1.8e9
    assert sa.status().sample["acq_id"] == n


def test_a_change_restarts_a_running_acquisition(sa):
    sa.set_averages(3)
    n = sa.acquire()
    sa.step()
    sa.set_rbw(100e3)
    assert any("restarted" in m for _, m in sa.events)
    _finish(sa)
    t = sa.get_trace("sample")
    assert t["acq_id"] == n and t["rbw_Hz"] == 100e3 and t["averages"] == 3


def test_abort_latches_an_aborted_sample(sa):
    n = sa.acquire()
    sa.abort()
    s = sa.status()
    assert s.acq_id == n and not s.acquiring and s.sample["aborted"]
    with pytest.raises(ValueError, match="aborted"):
        sa.get_trace("sample")


def test_busy_cleared_and_sample_published_together(sa):
    """Gotcha #28: no status frame may say "#n done" while sample is still #n-1."""
    sa.set_averages(2)
    stop, bad = threading.Event(), []

    def watch():
        while not stop.is_set():
            s = sa.status()
            if not s.acquiring and s.acq_id and s.sample.get("acq_id") != s.acq_id:
                bad.append((s.acq_id, s.sample.get("acq_id")))
    t = threading.Thread(target=watch); t.start()
    for _ in range(15):
        _acquire(sa)
    stop.set(); t.join()
    assert bad == []


# ---- the thru reference -------------------------------------------------------------------

def test_thru_reference_normalises_the_dut(sa):
    sa.set_start(100e6); sa.set_stop(1.7e9)
    sa.set_tg(True); sa.set_dut("thru")
    r = sa.take_reference(); _finish(sa)
    assert sa.status().reference["present"] and sa.status().reference["acq_id"] == r
    # the thru against itself: flat 0 dB, ripple and cables gone
    _acquire(sa)
    flat = sa.get_trace("sample", "norm")["norm_dB"]
    assert np.abs(flat).max() < 0.2
    sa.set_dut("bandpass"); _acquire(sa)
    t = sa.get_trace("sample", "norm")
    f, y = t["freqs_Hz"], t["norm_dB"]
    assert y[np.argmin(abs(f - 900e6))] == pytest.approx(-1.5, abs=0.2)
    assert y[np.argmin(abs(f - 300e6))] < -30
    assert t["reference_acq_id"] == r and "power_dBm" not in t


def test_norm_is_refused_with_a_reason(sa):
    _acquire(sa)
    with pytest.raises(ValueError, match="there is none"):
        sa.get_trace("sample", "norm")
    sa.take_reference(); _finish(sa)                  # TG off: useless reference
    assert any("tracking generator OFF" in m for _, m in sa.events)
    with pytest.raises(ValueError, match="tracking generator off"):
        sa.get_trace("sample", "norm")
    sa.set_tg(True); sa.take_reference(); _finish(sa)
    sa.set_points(201); _acquire(sa)
    with pytest.raises(ValueError, match="201 points vs reference 401"):
        sa.get_trace("sample", "norm")
    sa.set_points(401); sa.set_tg_level(-20); _acquire(sa)
    with pytest.raises(ValueError, match="TG level -20 dBm vs reference -10 dBm"):
        sa.get_trace("sample", "norm")


def test_aborting_a_reference_clears_the_old_one(sa):
    sa.set_tg(True)
    sa.take_reference(); _finish(sa)
    assert sa.status().reference["present"]
    sa.take_reference(); sa.abort()
    assert sa.status().reference["present"] is False
    sa.take_reference(); _finish(sa)
    sa.clear_reference()
    assert sa.status().reference["present"] is False


def test_real_mode_hides_the_bench(sa):
    class Real:
        simulated = False
    b = SpectrumAnalyzer(Real(), Config())
    s = b.status()
    assert s.dut == "" and s.simulated is False


# ---- review additions ------------------------------------------------------------

def test_a_change_between_configure_and_sweep_is_not_averaged_in(sa):
    """A setter that lands AFTER the backend was configured but BEFORE the sweep
    starts must void that sweep: the backend still has the OLD settings. (Found
    in review: the revision used to be read after configuring.)"""
    n = sa.acquire()
    real_configure = sa.backend.configure
    fired = []

    def configure_then_user_types(s):
        rb = real_configure(s)
        if not fired:                  # only the first time
            fired.append(s.rbw_Hz)
            sa.set_rbw(10e3)           # the user types a new RBW right now
        return rb

    sa.backend.configure = configure_then_user_types
    sa.set_rbw(1e6)                    # forces a configure on the next step
    assert sa.step() is False          # the old-settings sweep was thrown away
    assert sa.status().acquiring
    _finish(sa)
    t = sa.get_trace("sample")
    assert t["acq_id"] == n and t["rbw_Hz"] == 10e3


def test_status_never_writes_to_backend(sa):
    """status() must not touch the hardware (gotcha #1 and the service's 10 Hz
    publisher): count backend calls around many status() calls."""
    calls = []
    for name in ("configure", "start_sweep", "finish_sweep", "abort_sweep", "idn"):
        orig = getattr(sa.backend, name)
        setattr(sa.backend, name, lambda *a, _o=orig, _n=name, **k: (calls.append(_n), _o(*a, **k))[1])
    for _ in range(50):
        sa.status()
    assert calls == []
