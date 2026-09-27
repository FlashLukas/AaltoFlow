"""The brain on the simulator, driven by hand (no thread, instant scans)."""

import numpy as np
import pytest

from ccs200.config import Config
from ccs200.sim_system import build_sim_system
from ccs200.spectrometer import analyse


@pytest.fixture
def spec():
    cfg = Config()
    cfg.scan.continuous = False
    s, _ = build_sim_system(cfg, realtime=False, seed=2)
    events = []
    s._on_event = lambda lvl, msg: events.append((lvl, msg))
    s.events = events
    s.start(run=False)
    yield s
    s.shutdown()


def _done(s):
    for _ in range(10000):
        if not s.status().acquiring:
            return
        s.step()
    raise AssertionError("acquisition did not finish")


def test_lifecycle_and_status(spec):
    st = spec.status()
    assert st.connected and st.simulated and st.pixels == 3648
    assert 199 < st.wl_min_nm < 201 and 999 < st.wl_max_nm < 1001
    spec.shutdown()
    assert not spec.status().connected
    spec.shutdown()                                   # twice is fine
    with pytest.raises(ValueError, match="not connected"):
        spec.acquire()


def test_setters_clamp_and_warn(spec):
    spec.set_integration_time(1e-9)
    assert spec.status().integration_time_s == 1e-5
    spec.set_integration_time(500.0)
    assert spec.status().integration_time_s == 60.0
    spec.set_averages(0)
    assert spec.status().averages == 1
    spec.set_averages(10 ** 6)
    assert spec.status().averages == 1000
    assert sum(1 for lvl, m in spec.events if lvl == "warn" and "clamped" in m) == 4
    with pytest.raises(ValueError):
        spec.set_integration_time(float("nan"))
    with pytest.raises(ValueError):
        spec.set_sim("gravity", 1.0)


def test_window_ends_bound_each_other(spec):
    spec.set_window(540.0, 550.0)
    st = spec.status()
    assert (st.window_min_nm, st.window_max_nm) == (540.0, 550.0)
    spec.set_window_min(560.0)                        # past the end: clamped to end - 1 nm
    assert spec.status().window_min_nm == 549.0
    spec.set_window(700.0, 800.0)                     # both moved past the old end
    assert (spec.status().window_min_nm, spec.status().window_max_nm) == (700.0, 800.0)


def test_acquire_finds_the_mercury_line(spec):
    # The sim instrument starts at 5 ms (adopted); the intensity window below
    # is for 10 ms, set explicitly as a user would.
    spec.set_integration_time(0.01)
    n = spec.acquire()
    assert spec.status().acquiring and spec.status().acq_id == n
    _done(spec)
    t = spec.get_trace("sample")
    assert t["acq_id"] == n and t["spectrum"].shape == (3648,)
    assert t["peak_nm"] == pytest.approx(546.07, abs=0.2)
    assert 0.5 < t["peak_intensity"] < 0.75 and not t["saturated"]
    assert spec.status().sample["peak_nm"] == t["peak_nm"]


def test_the_window_picks_another_line(spec):
    spec.set_window(750.0, 780.0)
    spec.acquire()
    _done(spec)
    assert spec.get_trace("sample")["peak_nm"] == pytest.approx(763.51, abs=0.2)


def test_averages_count_only_scans_started_after_the_trigger(spec):
    spec.set_averages(4)
    n = spec.acquire()
    _done(spec)
    t = spec.get_trace("sample")
    assert t["averages"] == 4 and t["acq_id"] == n
    assert spec.status().scans == 4


def test_a_setting_change_restarts_the_acquisition(spec):
    spec.set_averages(3)
    spec.acquire()
    spec.step()
    spec.set_integration_time(0.005)
    assert any("restarted" in m for _, m in spec.events)
    _done(spec)
    t = spec.get_trace("sample")
    assert t["averages"] == 3 and t["integration_time_s"] == 0.005


def test_saturation_is_flagged(spec):
    spec.set_integration_time(0.05)                   # the line at ~3 x full scale
    spec.acquire()
    _done(spec)
    t = spec.get_trace("sample")
    assert t["saturated"] and t["exposure"] >= 0.99
    assert any("SATURATED" in m for _, m in spec.events)


def test_dark_subtraction_needs_a_matching_dark(spec):
    spec.set_dark_subtract(True)
    with pytest.raises(ValueError, match="no dark"):
        spec.acquire()
    spec.set_light(False)
    d = spec.take_dark()
    _done(spec)
    st = spec.status()
    assert st.dark["present"] and st.dark["acq_id"] == d and st.dark["matches"]
    dark = spec.get_trace("dark")["spectrum"]
    assert 0.003 < dark.mean() < 0.006                # offset + a little dark current
    spec.set_light(True)
    spec.acquire()
    _done(spec)
    t = spec.get_trace("sample")
    assert t["dark_applied"]
    # far from every line and in the UV where the lamp is weak: ~0 after subtraction
    assert abs(t["spectrum"][:50].mean()) < 0.002

    spec.set_integration_time(0.02)                   # the dark no longer fits
    assert spec.status().dark["matches"] is False
    with pytest.raises(ValueError, match="take a new dark"):
        spec.acquire()
    spec.set_dark_subtract(False)
    spec.acquire()                                    # fine without subtraction
    spec.clear_dark()
    assert spec.status().dark["present"] is False


def test_abort_latches_and_a_aborted_dark_clears_the_old_one(spec):
    spec.set_light(False)
    spec.take_dark()
    _done(spec)
    spec.set_averages(5)
    n = spec.take_dark()
    spec.step()
    spec.abort()
    st = spec.status()
    assert not st.acquiring and st.acq_id == n and st.sample["aborted"]
    assert st.dark["present"] is False
    with pytest.raises(ValueError, match="aborted"):
        spec.get_trace("sample")


def test_a_new_trigger_abandons_the_running_acquisition(spec):
    spec.set_averages(3)
    a = spec.acquire()
    spec.step()
    b = spec.acquire()
    assert b == a + 1
    _done(spec)
    t = spec.get_trace("sample")
    assert t["acq_id"] == b and t["averages"] == 3


def test_status_never_touches_the_backend(spec):
    calls = []
    real = spec.backend.scan_ready
    spec.backend.scan_ready = lambda: calls.append(1) or real()
    for _ in range(20):
        spec.status()
    assert calls == []


def test_get_trace_refusals(spec):
    with pytest.raises(ValueError, match="no scan"):
        spec.get_trace("last")
    with pytest.raises(ValueError, match="no acquisition"):
        spec.get_trace("sample")
    with pytest.raises(ValueError, match="no dark"):
        spec.get_trace("dark")
    with pytest.raises(ValueError, match="which"):
        spec.get_trace("tomorrow")


def test_continuous_scanning_updates_the_live_readouts():
    cfg = Config()
    s, _ = build_sim_system(cfg, realtime=False, seed=3)
    s.start(run=False)
    for _ in range(3):
        assert s.step()
    st = s.status()
    assert st.scans == 3 and st.peak_nm == pytest.approx(546.07, abs=0.3)
    assert 0 < st.exposure < 1 and st.trace_id == 3
    assert s.get_trace("last")["spectrum"].shape == (3648,)
    s.shutdown()


def test_the_thread_runs_and_stops():
    cfg = Config()
    s, _ = build_sim_system(cfg, realtime=True, seed=4)
    s.start()
    import time
    t0 = time.monotonic()
    while s.status().scans < 3 and time.monotonic() - t0 < 5:
        time.sleep(0.02)
    assert s.status().scans >= 3
    s.shutdown()
    assert s._thread is None


def test_analyse_subpixel_peak_on_a_nonuniform_grid():
    wl = np.linspace(500, 600, 501) + 0.0002 * (np.arange(501) ** 2) / 50
    y = np.exp(-0.5 * ((wl - 547.33) / 0.6) ** 2)
    r = analyse(wl, y, 500, 600)
    assert r["peak_nm"] == pytest.approx(547.33, abs=0.02)
    assert r["integrated"] == pytest.approx(0.6 * np.sqrt(2 * np.pi), rel=1e-3)
    assert np.isnan(analyse(wl, y, 700, 800)["peak_nm"])


# ---- adopt-on-start (Lukas's rule 2026-09-27) ----------------------------------

def test_start_adopts_the_instruments_integration_time():
    """The sim unit was left at 0.3 s; the config says 10 ms. After start the
    brain, the status and the describe revision follow the INSTRUMENT."""
    from ccs200.net.describe import build_manifest
    cfg = Config()
    cfg.scan.continuous = False
    s, backend = build_sim_system(cfg, realtime=False, seed=5, integration_s=0.3)
    rev_before = build_manifest(s)["revision"]
    events = []
    s._on_event = lambda lvl, msg: events.append((lvl, msg))
    s.start(run=False)
    assert s.status().integration_time_s == pytest.approx(0.3)
    assert cfg.scan.integration_time_s == pytest.approx(0.3)
    assert backend.integration_time() == pytest.approx(0.3)      # untouched
    assert any("adopted" in m for _, m in events)
    # the acquire timeout grows with integration: describe must say so
    assert build_manifest(s)["revision"] != rev_before
    n = s.acquire()
    _done(s)
    assert s.get_trace("sample")["integration_time_s"] == pytest.approx(0.3)
    assert backend.integration_time() == pytest.approx(0.3)
    s.shutdown()


def test_default_sim_starts_away_from_the_config_default():
    """The sim is deliberately NOT at the config's 10 ms, or adoption would be
    invisible in every other test."""
    s, _ = build_sim_system(Config(), realtime=False)
    s.start(run=False)
    assert s.status().integration_time_s == pytest.approx(0.005)
    s.shutdown()


def test_out_of_limits_instrument_time_is_clamped_with_a_warning():
    cfg = Config()
    cfg.limits.integration_max_s = 1.0
    s, _ = build_sim_system(cfg, realtime=False, integration_s=5.0)
    events = []
    s._on_event = lambda lvl, msg: events.append((lvl, msg))
    s.start(run=False)
    assert s.status().integration_time_s == pytest.approx(1.0)
    assert any(lvl == "warn" and "outside the limits" in m for lvl, m in events)
    s.shutdown()
