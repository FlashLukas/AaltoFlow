"""The brain on the simulator, driven step by step (no thread): acquisitions,
the thru reference, transmission and every refusal."""

import math

import numpy as np
import pytest

from shsna.config import Config
from shsna.sim_system import build_sim_system


@pytest.fixture
def sna():
    cfg = Config()
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz, cfg.sweep.points = 700e6, 1300e6, 601
    v, sim = build_sim_system(cfg, realtime=False, seed=2)
    events = []
    v._on_event = lambda level, msg: events.append((level, msg))
    v.start(run=False)
    v.events, v.sim = events, sim
    yield v
    v.shutdown()


def _finish(v, n=None, limit=50):
    for _ in range(limit):
        if not v.status().acquiring:
            break
        v.step()
    st = v.status()
    assert not st.acquiring
    if n is not None:
        assert st.acq_id == n
    return st


def _thru_then_dut(v):
    v.set_sim("dut_inserted", False)
    _finish(v, v.take_reference())
    v.set_sim("dut_inserted", True)
    _finish(v, v.acquire())


def test_start_writes_nothing_and_does_not_sweep(sna):
    st = sna.status()
    assert st.connected and st.continuous is False and st.hw_error == ""
    assert sna.step() is False and sna.status().sweeps == 0     # nothing asked for


def test_acquire_latches_a_sample_on_the_analysers_grid(sna):
    n = sna.acquire()
    assert sna.status().acquiring and sna.status().acq_id == n
    st = _finish(sna, n)
    assert st.acq_error == "" and st.sample["acq_id"] == n and not st.sample["failed"]
    t = sna.get_trace("raw")
    assert t["raw"].shape == (601,) and t["acq_id"] == n
    assert t["freqs_Hz"][0] == 700e6 and t["freqs_Hz"][-1] == pytest.approx(1300e6)
    # the DUT is in by default: its pass band at 1 GHz, the pad below it
    assert t["raw"].max() == pytest.approx(-20 - 1.5 - 1.0, abs=1.0)


def test_transmission_is_raw_minus_the_thru_and_shows_the_filter(sna):
    _thru_then_dut(sna)
    t = sna.get_trace("transmission")
    raw = sna.get_trace("raw")["raw"]
    ref = sna.get_trace("reference")["reference"]
    np.testing.assert_allclose(t["transmission"], raw - ref)
    r = sna.get_result()
    c = sna.cfg.sim
    assert r["peak_freq_hz"] == pytest.approx(c.dut_center_Hz, abs=5e6)
    assert r["peak_transmission_db"] == pytest.approx(-c.dut_loss_dB, abs=0.3)
    assert r["bw3_hz"] == pytest.approx(c.dut_bandwidth_Hz, abs=3e6)
    # the same numbers in status, for a GUI
    assert sna.status().sample["peak_transmission_db"] == pytest.approx(r["peak_transmission_db"])
    raw_r = sna.get_result("raw")
    assert raw_r["peak_db"] == pytest.approx(raw.max()) and "peak_transmission_db" not in raw_r


def test_the_thru_cancels_ripple_cable_and_pad(sna):
    """Without the DUT, transmission is 0 dB everywhere (up to the jitter),
    although the raw trace wanders by the TG ripple and the cable slope."""
    sna.set_sim("dut_inserted", False)
    _finish(sna, sna.take_reference())
    _finish(sna, sna.acquire())
    t = sna.get_trace("transmission")["transmission"]
    raw = sna.get_trace("raw")["raw"]
    assert np.ptp(raw) > 0.8 and np.abs(t).max() < 0.15


def test_transmission_is_refused_without_a_reference(sna):
    _finish(sna, sna.acquire())
    with pytest.raises(ValueError, match="needs a thru reference"):
        sna.get_trace("transmission")
    with pytest.raises(ValueError, match="needs a thru reference"):
        sna.get_result("transmission")
    with pytest.raises(ValueError, match="no reference"):
        sna.get_trace("reference")


def test_transmission_is_refused_on_another_grid(sna):
    _thru_then_dut(sna)
    sna.set_points(301)
    _finish(sna, sna.acquire())
    with pytest.raises(ValueError, match="301 points vs reference 601"):
        sna.get_trace("transmission")
    sna.set_points(601)
    sna.set_start(750e6)
    _finish(sna, sna.acquire())
    with pytest.raises(ValueError, match="first bin .* Take a new reference"):
        sna.get_result()
    assert sna.status().sample["peak_transmission_db"] != sna.status().sample["peak_transmission_db"]  # NaN


def test_a_failed_acquisition_is_latched_with_its_reason(sna):
    _finish(sna, sna.acquire())
    sna.sim.fail_next = "USB hiccup"
    n = sna.acquire()
    st = _finish(sna, n)
    assert st.sample == {**st.sample, "acq_id": n, "failed": True, "error": "USB hiccup"}
    assert st.acq_error == "USB hiccup" and st.hw_error == "USB hiccup"
    # the PREVIOUS trace is not handed out as if it were this one
    with pytest.raises(ValueError, match="#%d failed: USB hiccup" % n):
        sna.get_trace("raw")
    with pytest.raises(ValueError, match="failed"):
        sna.get_result("raw")
    assert ("error", f"acquisition #{n} failed: USB hiccup") in sna.events
    # a new trigger is a fresh attempt: the old error no longer describes the present
    m = sna.acquire()
    assert sna.status().hw_error == ""
    st = _finish(sna, m)
    assert st.acq_error == "" and st.sample["failed"] is False


def test_a_failed_reference_clears_the_old_one(sna):
    _thru_then_dut(sna)
    sna.sim.fail_next = "TG not answering"
    _finish(sna, sna.take_reference())
    assert sna.status().reference["present"] is False
    with pytest.raises(ValueError, match="needs a thru reference"):
        sna.get_trace("transmission", "last")


def test_no_tg_is_hw_error_and_a_failed_acquisition(sna):
    sna.set_sim("tg_attached", False)
    sna.step()
    assert "no tracking generator" in sna.status().hw_error
    st = _finish(sna, sna.acquire())
    assert "no tracking generator" in st.acq_error


def test_abort_latches_aborted_and_clears_a_reference_being_taken(sna):
    _thru_then_dut(sna)
    n = sna.take_reference()
    sna.abort()
    st = sna.status()
    assert not st.acquiring and st.acq_id == n and st.acq_error == "aborted"
    assert st.sample["failed"] and st.reference["present"] is False
    sna.abort()                                          # nothing running: harmless


def test_a_settings_change_restarts_the_acquisition(sna):
    """The sweep in flight was for the old band: it is thrown away and the SAME
    acquisition id is measured again under the new settings."""
    sna.sim.time_scale = 1.0                             # sweeps take real time
    n = sna.acquire()
    sna.backend.start_sweep = _spy(sna.backend.start_sweep, calls := [])
    import threading
    t = threading.Thread(target=sna.step)
    t.start()
    import time
    time.sleep(0.05)
    sna.set_stop(1200e6)
    t.join(timeout=5)
    assert sna.status().acquiring and sna.status().acq_id == n     # restarted, not latched
    sna.sim.time_scale = 0.0
    st = _finish(sna, n)
    assert st.sample["stop_Hz"] == pytest.approx(1200e6)
    assert [c[1] for c in calls] == [1300e6, 1200e6]


def _spy(fn, calls):
    def wrapped(*a):
        calls.append(a)
        return fn(*a)
    return wrapped


def test_a_new_trigger_during_a_reference_clears_the_old_reference(sna):
    _thru_then_dut(sna)
    sna.take_reference()
    sna.acquire()
    assert sna.status().reference["present"] is False
    assert any("interrupted by a new trigger" in m for _l, m in sna.events)


def test_setters_clamp_and_say_so(sna):
    sna.set_points(5000)
    assert sna.cfg.sweep.points == 1001                   # the SA API's maximum
    assert sna.events[-1] == ("warn", "1001 points (clamped)")
    sna.set_stop(10e9)
    assert sna.cfg.sweep.stop_Hz == 4.4e9
    sna.set_start(5e9)
    assert sna.cfg.sweep.start_Hz == pytest.approx(4.4e9 - 1e3)
    sna.set_rbw(-5)
    assert sna.cfg.sweep.rbw_Hz == 0.0                    # auto
    sna.set_rbw(1e6)
    assert sna.cfg.sweep.rbw_Hz == 250e3
    sna.set_averages(0)
    assert sna.cfg.sweep.averages == 1
    with pytest.raises(ValueError, match="finite"):
        sna.set_start(float("nan"))
    with pytest.raises(ValueError, match="unknown sim parameter"):
        sna.set_sim("level", 3)
    sna.set_sim("dut_order", 4.6)
    assert sna.cfg.sim.dut_order == 5


def test_averages_reduce_the_noise_in_power(sna):
    """More averages, less scatter of the floor (averaged in linear power)."""
    sna.set_sim("pad_dB", 80.0)                            # the tone 40 dB below ...
    sna.set_sim("floor_dB", -40.0)                         # ... the noise floor
    _finish(sna, sna.acquire())
    one = np.std(sna.get_trace("raw")["raw"])
    sna.set_averages(16)
    _finish(sna, sna.acquire())
    sixteen = sna.get_trace("raw")
    assert sixteen["averages"] == 16 and np.std(sixteen["raw"]) < one / 2


def test_the_frequency_grid_for_a_scan(sna):
    f = sna.frequencies()                                 # the simulator knows its grid
    assert f.size == 601 and f[0] == 700e6
    sna.set_sim("dut_inserted", False)
    _finish(sna, sna.take_reference())
    np.testing.assert_allclose(sna.frequencies(), sna.get_trace("reference")["freqs_Hz"])


def test_the_grid_of_an_unknowing_backend_is_refused_not_guessed(sna):
    real = sna.backend.predicted_grid
    sna.backend.predicted_grid = lambda *a: None          # like the remote before a sweep
    with pytest.raises(ValueError, match="not known yet"):
        sna.frequencies()
    assert sna.grid_points() is None
    sna.backend.predicted_grid = real
    _finish(sna, sna.acquire())
    sna.backend.predicted_grid = lambda *a: None
    assert sna.frequencies().size == 601                  # the last sweep of this band
    sna.set_points(301)
    with pytest.raises(ValueError, match="not known yet"):
        sna.frequencies()                                 # ... but not of another count


def test_continuous_sweeps_are_not_latched_as_samples(sna):
    sna.set_continuous(True)
    assert sna.step() and sna.step()
    st = sna.status()
    assert st.sweeps == 2 and st.sample == {} and math.isfinite(st.last_peak_db)
    assert sna.get_trace("raw", "last")["trace_id"] == st.trace_id
    with pytest.raises(ValueError, match="no acquisition latched"):
        sna.get_trace("raw")


def test_set_sim_is_refused_on_the_real_backend():
    from shsna.analyzer import Analyzer
    from shsna.backends.remote_sa import RemoteSa
    cfg = Config()
    real = Analyzer(RemoteSa(cfg), cfg)
    with pytest.raises(ValueError, match="real analyser"):
        real.set_sim("dut_inserted", False)


def test_bad_arguments_are_value_errors(sna):
    with pytest.raises(ValueError):
        sna.get_trace("s21")
    with pytest.raises(ValueError):
        sna.get_trace("raw", "later")
    with pytest.raises(ValueError):
        sna.get_result("reference")
