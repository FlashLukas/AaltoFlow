"""The owner-side tracking-generator contract (Lukas's decision 2026-09-28).

The Signal Hound kit is three modules: this one (the spectrum analyser, the
ONLY owner of the USB devices), `shsg` (the TG as a CW source) and `shsna`
(TG sweeps: a scalar network analyser). The TG can only be driven through the
analyser's API handle, so the two client modules ask THIS service with four
verbs -- tg_cw, tg_sweep_acquire, get_tg_trace, tg_abort -- and read the tg_*
status keys. These tests pin that contract down, in the simulator and over
the wire (ports 17600/17601, reserved for this file).

Facts measured on the real SA44B + TG44A the same day and built in here: the
TG has NO off (it is PARKED instead), its state cannot be read at start
("unknown"), a CW survives spectrum sweeps, a TG sweep ignores the level and
returns dB relative to the TG's output, at most 1001 points."""

import math
import threading
import time

import numpy as np
import pytest

from fake_sa_api import FakeSaApi
from signalhound.backends.sa_api import SaApiAnalyzer
from signalhound.config import Config
from signalhound.net.protocol import status_to_dict
from signalhound.sim_system import build_sim_system
from signalhound.spectrum import TG_MODES, SpectrumAnalyzer

TG_KEYS = ("tg_attached", "tg_mode", "tg_cw_on", "tg_cw_freq_hz", "tg_cw_level_dbm",
           "tg_park_hz", "tg_park_level_dbm", "tg_acq_id", "tg_acquiring", "tg_sample_id",
           "tg_error", "hw_error", "spectrum_paused")
PARK = (10e3, -30.0)                          # the default hardware.tg_park_hz / _dbm


@pytest.fixture
def sa():
    cfg = Config()
    cfg.acquisition.continuous = False
    v, sim = build_sim_system(cfg, realtime=False, seed=1)
    v.sim = sim
    v.events = []
    v._on_event = lambda lvl, msg: v.events.append((lvl, msg))
    v.start(run=False)
    yield v
    v.shutdown()


def _tg_done(v, limit=50):
    for _ in range(limit):
        if not v.status().tg_acquiring:
            return
        v.step()
    raise AssertionError("TG sweep did not finish")


def _f(t):
    return t["start_hz"] + t["bin_hz"] * np.arange(t["points"])


# ---- status keys ----------------------------------------------------------------

def test_every_tg_status_key_is_in_status(sa):
    st = status_to_dict(sa.status())
    for k in TG_KEYS:
        assert k in st, k
    assert TG_MODES == ("unknown", "parked", "cw", "sweep")
    assert st["tg_attached"] is True and st["tg_mode"] == "unknown"
    assert st["tg_cw_on"] is False and st["tg_acquiring"] is False
    assert (st["tg_park_hz"], st["tg_park_level_dbm"]) == PARK
    assert st["tg_acq_id"] == 0 and st["tg_sample_id"] == 0 and st["tg_error"] == ""


def test_old_tracking_features_are_gone(sa):
    """Tracking mode, the thru and transmission now live in shsna."""
    assert not hasattr(sa.cfg, "tracking")
    for name in ("set_tg", "set_tg_level", "set_tg_points", "take_reference",
                 "clear_reference"):
        assert not hasattr(sa, name), name
    st = status_to_dict(sa.status())
    assert "reference" not in st and "tg_on" not in st
    with pytest.raises(ValueError):
        sa.get_trace("last", "transmission")


# ---- start-up and shutdown safety -------------------------------------------------

def test_start_sends_the_tg_nothing_and_says_unknown():
    """A TG left emitting by another program (the lab found one on 2026-09-28)
    is left alone, and its state -- which cannot be read -- is "unknown"."""
    from signalhound.backends.sim import SimulatedAnalyzer
    cfg = Config()
    sim = SimulatedAnalyzer(cfg, seed=1, time_scale=0.0, tg_output_on=True,
                            tg_cw=(1.0e9, -30.0))
    v = SpectrumAnalyzer(sim, cfg)
    v.start(run=False)
    try:
        st = v.status()
        assert st.tg_cw_on is False and st.tg_mode == "unknown"
        assert sim.log == []                      # no set_tg_cw, no idle, no configure
        assert sim.tg_cw == (1.0e9, -30.0)        # untouched
    finally:
        v.shutdown()


def test_shutdown_parks_the_tg_before_closing(sa):
    """The TG44A has no off and keeps emitting after close (measured): PARK it."""
    sa.tg_cw(True, 1e9, -15.0)
    sa.sim.log.clear()
    sa.shutdown()
    assert sa.sim.log[-2:] == ["set_tg_cw", "close"]
    assert sa.sim.tg_cw == PARK


def test_the_simulated_tg_has_no_off_either(sa):
    """Like the real one: idle (saAbort) and close leave it emitting."""
    sa.tg_cw(True, 1e9, -15.0)
    sa.backend.idle()
    sa.backend.close()
    assert sa.sim.tg_cw == (1e9, -15.0) and sa.sim.tg_output_on


# ---- tg_cw ---------------------------------------------------------------------------

def test_tg_cw_applies_and_echoes(sa):
    r = sa.tg_cw(True, 2.0e9, -15.0)
    assert r == {"on": True, "freq_hz": 2.0e9, "level_dbm": -15.0, "deferred": False}
    st = sa.status()
    assert (st.tg_cw_on, st.tg_cw_freq_hz, st.tg_cw_level_dbm) == (True, 2.0e9, -15.0)
    assert st.tg_mode == "cw" and sa.sim.tg_cw == (2.0e9, -15.0)
    # a missing frequency / level is KEPT
    assert sa.tg_cw(True, level_dbm=-20.0)["freq_hz"] == 2.0e9
    assert sa.tg_cw(True, freq_hz=1.5e9)["level_dbm"] == -20.0


def test_off_is_a_park_that_keeps_the_cw_for_next_time(sa):
    sa.tg_cw(True, 1.5e9, -20.0)
    off = sa.tg_cw(False)
    assert off == {"on": False, "freq_hz": 1.5e9, "level_dbm": -20.0, "deferred": False}
    st = sa.status()
    assert st.tg_mode == "parked" and st.tg_cw_on is False
    assert sa.sim.tg_cw == PARK                   # physically: at the park
    assert sa.tg_cw(True)["freq_hz"] == 1.5e9     # "on" brings the kept CW back
    assert sa.sim.tg_cw == (1.5e9, -20.0)


def test_after_unknown_any_accepted_call_makes_the_state_known(sa):
    """Nothing to keep after start: the missing values are the PARK values."""
    assert sa.status().tg_mode == "unknown"
    r = sa.tg_cw(True, freq_hz=1e9)               # frequency only
    assert r["level_dbm"] == PARK[1] and sa.status().tg_mode == "cw"


def test_a_frequency_or_level_alone_keeps_on_off_as_it_is(sa):
    """Found by the live test of the three processes (2026-09-28): shsg sends
    a frequency change WITHOUT `on` while its RF is off. That must not switch
    the TG on, and must be kept for the next "on" -- not refused."""
    sa.tg_cw(False)                                   # parked
    r = sa.tg_cw(None, freq_hz=1e9)
    assert r["on"] is False and sa.status().tg_mode == "parked"
    assert sa.sim.tg_cw == PARK                       # still physically parked
    sa.tg_cw(None, level_dbm=-20.0)
    on = sa.tg_cw(True)
    assert (on["freq_hz"], on["level_dbm"]) == (1e9, -20.0)
    assert sa.sim.tg_cw == (1e9, -20.0)
    sa.tg_cw(None, freq_hz=1.1e9)                     # while ON: retunes, stays on
    assert sa.status().tg_mode == "cw" and sa.sim.tg_cw == (1.1e9, -20.0)


def test_off_after_unknown_parks(sa):
    sa.tg_cw(False)
    assert sa.status().tg_mode == "parked" and sa.sim.tg_cw == PARK


def test_tg_cw_refusals(sa):
    with pytest.raises(ValueError, match="4.4 GHz"):
        sa.tg_cw(True, 5e9, -20.0)
    with pytest.raises(ValueError, match="10 Hz"):
        sa.tg_cw(True, 1.0, -20.0)
    with pytest.raises(ValueError, match="-30"):
        sa.tg_cw(True, 1e9, -40.0)
    with pytest.raises(ValueError, match="-10"):
        sa.tg_cw(True, 1e9, 0.0)
    with pytest.raises(ValueError):
        sa.tg_cw(True, float("nan"), -20.0)
    # nothing was applied by a refused call
    assert sa.status().tg_mode == "unknown" and sa.sim.log == []


def test_tg_refused_without_a_tg():
    cfg = Config()
    cfg.scene.tg_attached = False
    v, _ = build_sim_system(cfg, realtime=False)
    v.start(run=False)
    try:
        assert v.status().tg_attached is False
        with pytest.raises(ValueError, match="no tracking generator"):
            v.tg_cw(True, 1e9, -20.0)
        with pytest.raises(ValueError, match="no tracking generator"):
            v.tg_sweep_acquire(0.9e9, 1.1e9)
    finally:
        v.shutdown()


def test_echo_is_stored_after_the_backend_call(sa):
    """gotcha #40: a frame showing the new CW must come from AFTER the TG got it."""
    seen = []
    orig = sa.backend.set_tg_cw

    def spy(f, lvl):
        seen.append(sa.status().tg_cw_freq_hz)     # what status says DURING the call
        return orig(f, lvl)
    sa.backend.set_tg_cw = spy
    sa.tg_cw(True, 1.2e9, -20.0)
    assert seen[0] != 1.2e9
    assert sa.status().tg_cw_freq_hz == 1.2e9


def test_a_failing_tg_cw_keeps_the_old_values_and_reports(sa):
    sa.tg_cw(True, 1e9, -20.0)

    def boom(f, lvl):
        raise RuntimeError("USB unplugged")
    sa.backend.set_tg_cw = boom
    with pytest.raises(ValueError, match="USB unplugged"):
        sa.tg_cw(True, 2e9, -20.0)
    st = sa.status()
    assert st.tg_cw_freq_hz == 1e9 and "USB unplugged" in st.hw_error


def test_the_cw_shows_on_the_simulated_spectrum(sa):
    sa.cfg.scene.tone_on = False
    sa.set_span(20e6)
    sa.tg_cw(True, 1.002e9, -15.0)
    sa.acquire()
    while sa.status().acquiring:
        sa.step()
    t = sa.get_trace("sample")
    assert abs(t["peak_Hz"] - 1.002e9) <= t["bin_Hz"]
    # TG level minus cable and filter loss: well above the floor, below the TG level
    assert -25.0 < t["peak_dBm"] < -15.0


# ---- CW while the spectrum sweeps: both hardware answers -------------------------------

def test_cw_during_sweep_allowed_keeps_sweeping(sa):
    """The measured answer (2026-09-28): the TG44A keeps a CW while it sweeps."""
    assert sa.cfg.hardware.tg_cw_during_sweep is True
    sa.set_continuous(True)
    sa.tg_cw(True, 1e9, -20.0)
    n0 = sa.status().sweeps
    assert sa.step() is True and sa.status().sweeps == n0 + 1
    st = sa.status()
    assert st.spectrum_paused == "" and st.tg_cw_on is True and sa.sim.tg_output_on


def test_cw_during_sweep_not_allowed_pauses_spectrum(sa):
    sa.cfg.hardware.tg_cw_during_sweep = False
    sa.set_continuous(True)
    assert sa.step() is True
    sa.tg_cw(True, 1e9, -20.0)
    n0 = sa.status().sweeps
    assert sa.step() is False and sa.status().sweeps == n0
    st = sa.status()
    assert "CW" in st.spectrum_paused and st.sweeping is False and st.tg_mode == "cw"
    sa.tg_cw(False)
    assert sa.status().spectrum_paused == ""
    assert sa.step() is True and sa.status().sweeps == n0 + 1


# ---- tg_sweep_acquire / get_tg_trace ----------------------------------------------------

def test_tg_sweep_acquire_id_sample_and_trace(sa):
    n = sa.tg_sweep_acquire(0.9e9, 1.1e9)
    st = sa.status()
    assert n == 1 and st.tg_acq_id == 1 and st.tg_acquiring is True and st.tg_mode == "sweep"
    assert "TG sweep" in st.spectrum_paused
    _tg_done(sa)
    st = sa.status()
    assert st.tg_acquiring is False and st.tg_sample_id == 1 and st.tg_error == ""
    assert st.tg_mode == "parked"                 # no CW before: parked after (no off)
    t = sa.get_tg_trace()
    assert t["id"] == 1 and t["overload"] is False
    assert t["unit"] == "dB" and t["level_dbm"] is None and t["level_applied"] is False
    assert "dbm" not in t
    assert t["start_hz"] == pytest.approx(0.9e9)
    assert t["points"] == sa.cfg.hardware.tg_sweep_points == len(t["db"])
    assert t["start_hz"] + t["bin_hz"] * (t["points"] - 1) == pytest.approx(1.1e9)
    f = _f(t)
    at = lambda hz: t["db"][int(np.argmin(np.abs(f - hz)))]      # noqa: E731
    # dB relative to the TG output: the pass band ~ -(cable + filter loss) ...
    assert at(1e9) == pytest.approx(-2.5, abs=0.8)
    assert at(1e9) > at(0.9e9) + 10               # ... and the band-pass skirt below it
    assert sa.get_tg_trace(1)["id"] == 1


def test_the_level_is_accepted_but_not_applied(sa):
    """Measured: the TG sweep ignores the saSetTg level (-30 and -20 identical)."""
    sa.tg_sweep_acquire(0.9e9, 1.1e9, level_dbm=-30.0)
    assert sa.tg_request(1) == {"id": 1, "points": 401, "averages": 1,
                                "level_applied": False}
    _tg_done(sa)
    a = np.asarray(sa.get_tg_trace()["db"])
    sa.tg_sweep_acquire(0.9e9, 1.1e9, level_dbm=-20.0)
    _tg_done(sa)
    b = np.asarray(sa.get_tg_trace()["db"])
    band = a > -30
    assert np.abs(a[band] - b[band]).max() < 0.3  # the same, within the noise
    with pytest.raises(ValueError, match="-30"):
        sa.tg_sweep_acquire(0.9e9, 1.1e9, level_dbm=-35.0)   # still a TG level


def test_tg_sweep_thru_and_dut_give_the_filter(sa):
    """What shsna will do: a thru, then the device; the difference is the filter."""
    sa.set_scene("dut_inserted", False)
    sa.tg_sweep_acquire(0.9e9, 1.1e9, averages=2)
    _tg_done(sa)
    thru = np.asarray(sa.get_tg_trace()["db"])
    sa.set_scene("dut_inserted", True)
    sa.tg_sweep_acquire(0.9e9, 1.1e9, averages=2)
    _tg_done(sa)
    t = sa.get_tg_trace(2)
    tx = np.asarray(t["db"]) - thru
    assert tx[int(np.argmin(np.abs(_f(t) - 1e9)))] == pytest.approx(-1.5, abs=0.3)


def test_tg_sweep_averages_in_power(sa):
    traces = []
    orig = sa.backend.finish_sweep

    def spy():
        z, m = orig()
        traces.append(z.copy())
        return z, m
    sa.backend.finish_sweep = spy
    sa.tg_sweep_acquire(0.9e9, 1.1e9, averages=4)
    _tg_done(sa)
    assert len(traces) == 4
    expect = 10 * np.log10(np.mean([10 ** (x / 10) for x in traces], axis=0))
    assert np.allclose(sa.get_tg_trace()["db"], expect)
    assert sa.get_tg_trace()["averages"] == 4


def test_tg_sweep_optional_rbw_and_points(sa):
    sa.tg_sweep_acquire(0.95e9, 1.05e9, rbw_hz=10e3, points=101)
    _tg_done(sa)
    t = sa.get_tg_trace()
    assert t["points"] == 101 and t["rbw_hz"] == 10e3


def test_points_are_clamped_to_1001_and_reported(sa):
    """The API silently clamps a TG sweep to 1001 points (measured); here the
    clamp is done first, announced, and reported in the request."""
    n = sa.tg_sweep_acquire(0.9e9, 1.1e9, points=5000)
    assert sa.tg_request(n)["points"] == 1001
    assert any(lvl == "warn" and "clamped to 1001" in msg for lvl, msg in sa.events)
    _tg_done(sa)
    assert sa.get_tg_trace()["points"] == 1001


def test_tg_sweep_refusals(sa):
    with pytest.raises(ValueError, match="stop"):
        sa.tg_sweep_acquire(1.1e9, 0.9e9)
    with pytest.raises(ValueError, match="4.4 GHz"):
        sa.tg_sweep_acquire(4e9, 5e9)
    with pytest.raises(ValueError, match="averages"):
        sa.tg_sweep_acquire(0.9e9, 1.1e9, averages=0)
    assert sa.status().tg_acq_id == 0            # a refused call starts nothing
    sa.tg_sweep_acquire(0.9e9, 1.1e9)
    with pytest.raises(ValueError, match="busy"):
        sa.tg_sweep_acquire(0.9e9, 1.1e9)         # exclusive: one TG sweep at a time
    _tg_done(sa)
    sa.tg_sweep_acquire(0.9e9, 1.1e9)             # accepted again afterwards


def test_get_tg_trace_refusals(sa):
    with pytest.raises(ValueError, match="no TG sweep"):
        sa.get_tg_trace()
    n = sa.tg_sweep_acquire(0.9e9, 1.1e9)
    with pytest.raises(ValueError, match="not finished"):
        sa.get_tg_trace(n)
    _tg_done(sa)
    sa.get_tg_trace(n)
    with pytest.raises(ValueError, match="not available"):
        sa.get_tg_trace(n + 5)


# ---- a CW request during a TG sweep: accepted, deferred ------------------------------------

def test_tg_cw_during_a_tg_sweep_is_deferred_and_applied_after(sa):
    sa.tg_cw(True, 1.5e9, -12.0)
    sa.tg_sweep_acquire(0.9e9, 1.1e9)
    r = sa.tg_cw(True, 2.0e9, -18.0)
    assert r == {"on": True, "freq_hz": 2.0e9, "level_dbm": -18.0, "deferred": True}
    st = sa.status()                               # the echo changes only when applied
    assert st.tg_mode == "sweep" and st.tg_cw_freq_hz == 1.5e9
    _tg_done(sa)
    st = sa.status()
    assert (st.tg_mode, st.tg_cw_freq_hz, st.tg_cw_level_dbm) == ("cw", 2.0e9, -18.0)
    assert sa.sim.tg_cw == (2.0e9, -18.0)


def test_off_during_a_tg_sweep_parks_after_it(sa):
    """shsg shutting down while shsna sweeps: the owner must not restore a CW
    that nobody owns any more."""
    sa.tg_cw(True, 1.5e9, -12.0)
    sa.tg_sweep_acquire(0.9e9, 1.1e9)
    assert sa.tg_cw(False)["deferred"] is True
    _tg_done(sa)
    assert sa.status().tg_mode == "parked" and sa.sim.tg_cw == PARK


def test_a_deferred_cw_survives_aborting_a_queued_sweep(sa):
    sa.tg_sweep_acquire(0.9e9, 1.1e9)
    sa.tg_cw(True, 1.2e9, -20.0)                  # deferred
    sa.tg_abort()                                  # the sweep never ran
    st = sa.status()
    assert st.tg_mode == "cw" and sa.sim.tg_cw == (1.2e9, -20.0)


# ---- tg_grid: the axis before any sweep (for shsna's scans) ------------------------------

def test_tg_grid_predicts_the_grid_without_touching_the_analyser(sa):
    g = sa.tg_grid(0.9e9, 1.1e9, points=201)
    assert sa.sim.log == [] and sa.status().tg_acquiring is False
    assert g["points"] == 201 and g["start_hz"] == pytest.approx(0.9e9)
    assert g["bin_hz"] == pytest.approx(0.2e9 / 200)
    assert g["predicted"] is False                # the simulator knows its grid
    sa.tg_sweep_acquire(0.9e9, 1.1e9, points=201)
    _tg_done(sa)
    t = sa.get_tg_trace()
    assert (t["start_hz"], t["bin_hz"], t["points"]) == (g["start_hz"], g["bin_hz"],
                                                         g["points"])
    assert sa.tg_grid(0.9e9, 1.1e9, points=5000)["points"] == 1001       # the same clamp
    assert sa.tg_grid(0.9e9, 1.1e9)["points"] == sa.cfg.hardware.tg_sweep_points
    with pytest.raises(ValueError, match="4.4 GHz"):
        sa.tg_grid(4e9, 5e9)


def test_tg_grid_on_the_real_analyser_is_marked_predicted():
    cfg = Config()
    dll = FakeSaApi(readonly=True)
    v = SpectrumAnalyzer(SaApiAnalyzer(cfg, dll=dll), cfg)
    v.start(run=False)
    try:
        g = v.tg_grid(0.9e9, 1.1e9, points=101)
        assert g["predicted"] is True and g["points"] == 101
        assert dll.writes() == []                 # a query: nothing configured
    finally:
        dll.readonly = False
        v.shutdown()


# ---- tg_abort with an id -------------------------------------------------------------------

def test_tg_abort_with_an_id_aborts_only_that_one(sa):
    n = sa.tg_sweep_acquire(0.9e9, 1.1e9)
    assert sa.tg_abort(n + 1) is False            # not the running one: left alone
    assert sa.status().tg_acquiring is True
    assert sa.tg_abort(n) is True
    assert sa.status().tg_acquiring is False and "aborted" in sa.status().tg_error
    assert sa.tg_abort() is False                 # nothing left to abort


# ---- the SA is restored afterwards ---------------------------------------------------

def test_the_cw_comes_back_first_and_the_spectrum_after_the_result(sa):
    """2026-09-28 (lab PC: ~1.2 s overhead per windowed acquisition): the TG
    result is released once the TG is back (CW or park); the spectrum is
    reconfigured on the sweep thread's NEXT pass -- off the SNA's critical
    path, and not at all between back-to-back TG sweeps."""
    sa.set_span(20e6)
    sa.set_continuous(True)
    sa.step()
    grid0 = sa.status().points
    sa.tg_cw(True, 1.5e9, -12.0)
    sa.sim.log.clear()
    sa.tg_sweep_acquire(0.9e9, 1.1e9, points=201)
    _tg_done(sa)
    assert sa.sim.log == ["configure:tg", "idle", "set_tg_cw"]
    st = sa.status()
    assert st.tg_cw_on is True and st.tg_mode == "cw" and sa.sim.tg_cw == (1.5e9, -12.0)
    t = sa.get_tg_trace()
    assert set(t["timing_s"]) == {"queued", "configure", "sweep", "restore", "total"}
    assert sa.step() is True and sa.status().points == grid0   # sweeping as before
    assert sa.sim.log[3] == "configure:spectrum"


def test_back_to_back_tg_sweeps_do_not_reconfigure_the_spectrum(sa):
    sa.set_continuous(True)
    sa.step()
    sa.sim.log.clear()
    sa.tg_sweep_acquire(0.9e9, 1.1e9, points=101)
    _tg_done(sa)
    sa.tg_sweep_acquire(0.95e9, 1.05e9, points=51)       # queued before the next pass
    _tg_done(sa)
    assert "configure:spectrum" not in sa.sim.log
    assert sa.status().spectrum_paused == ""


def test_without_a_cw_the_tg_is_parked_after_the_sweep(sa):
    """After a TG sweep the TG sits at the last swept frequency (measured):
    with no CW to give back it is parked, never left there."""
    sa.set_continuous(True)
    sa.step()
    sa.sim.log.clear()
    sa.tg_sweep_acquire(0.9e9, 1.1e9)
    _tg_done(sa)
    assert sa.sim.log == ["configure:tg", "idle", "set_tg_cw"]
    assert sa.sim.tg_cw == PARK


def test_an_untouched_analyser_is_left_unconfigured_after_a_tg_sweep(sa):
    """Start-up rule: the spectrum was never configured, so it is not configured
    afterwards either -- the TG sweep is only stopped (idle), then parked."""
    assert sa.status().configured is False
    sa.tg_sweep_acquire(0.9e9, 1.1e9)
    _tg_done(sa)
    assert sa.sim.log == ["configure:tg", "idle", "set_tg_cw"]
    assert sa.status().configured is False and sa.sim.tg_cw == PARK


def test_a_spectrum_acquisition_waits_for_the_tg_sweep(sa):
    sa.tg_sweep_acquire(0.9e9, 1.1e9)
    n = sa.acquire()
    _tg_done(sa)
    while sa.status().acquiring:
        sa.step()
    t = sa.get_trace("sample")
    assert t["acq_id"] == n and t["center_Hz"] == sa.cfg.sweep.center_Hz


def test_frequencies_during_a_tg_sweep_do_not_disturb_it(sa):
    f0 = sa.frequencies()                         # configures the spectrum grid
    sa.tg_sweep_acquire(0.9e9, 1.1e9)
    assert np.array_equal(sa.frequencies(), f0)   # served from the known grid
    sa.set_span(10e6)
    with pytest.raises(ValueError, match="busy"):
        sa.frequencies()                          # a new grid would need the analyser
    _tg_done(sa)
    assert sa.frequencies().size != f0.size


# ---- abort ----------------------------------------------------------------------------

def test_tg_abort_of_a_queued_sweep(sa):
    n = sa.tg_sweep_acquire(0.9e9, 1.1e9)
    sa.tg_abort()
    st = sa.status()
    assert st.tg_acquiring is False and st.tg_acq_id == n and st.tg_sample_id == 0
    assert "aborted" in st.tg_error and st.tg_mode == "unknown"   # nothing touched it
    with pytest.raises(ValueError, match="aborted"):
        sa.get_tg_trace(n)
    assert sa.sim.log == []                       # never started: nothing to restore
    sa.tg_abort()                                 # nothing running: harmless


def test_tg_abort_of_a_running_sweep_restores(sa):
    sa.set_continuous(True)
    sa.step()
    sa.tg_cw(True, 1.5e9, -12.0)
    n = sa.tg_sweep_acquire(0.9e9, 1.1e9, averages=5)
    orig = sa.backend.finish_sweep
    calls = []

    def abort_on_second():
        calls.append(1)
        if len(calls) == 2:
            sa.tg_abort()                         # someone clicks Abort mid-acquisition
        return orig()
    sa.backend.finish_sweep = abort_on_second
    _tg_done(sa)
    st = sa.status()
    assert len(calls) == 2                        # it stopped at the next chance
    assert st.tg_acquiring is False and st.tg_sample_id == 0 and "aborted" in st.tg_error
    assert st.tg_cw_on is True and sa.sim.tg_cw == (1.5e9, -12.0)   # CW back
    with pytest.raises(ValueError, match="aborted"):
        sa.get_tg_trace(n)
    assert sa.step() is True                      # spectrum sweeping again


def test_a_failing_tg_sweep_reports_and_still_restores(sa):
    sa.tg_cw(True, 1.5e9, -12.0)

    def boom():
        raise RuntimeError("USB unplugged")
    orig = sa.backend.finish_sweep
    sa.backend.finish_sweep = boom
    n = sa.tg_sweep_acquire(0.9e9, 1.1e9)
    _tg_done(sa)
    st = sa.status()
    assert st.tg_acquiring is False and "USB unplugged" in st.tg_error
    assert "USB unplugged" in st.hw_error and st.tg_sample_id == 0
    assert st.tg_cw_on is True and sa.sim.tg_cw == (1.5e9, -12.0)
    with pytest.raises(ValueError, match="USB unplugged"):
        sa.get_tg_trace(n)
    sa.backend.finish_sweep = orig


# ---- threads (gotcha #28) ----------------------------------------------------------

def test_a_tg_sample_is_never_announced_before_it_exists():
    cfg = Config()
    cfg.acquisition.sweep_on_start = True
    v, _ = build_sim_system(cfg, realtime=False, seed=2)
    v.start(run=True)
    bad, stop = [], threading.Event()

    def watch():
        while not stop.is_set():
            st = v.status()
            if st.tg_acq_id and not st.tg_acquiring and st.tg_sample_id != st.tg_acq_id:
                bad.append((st.tg_acq_id, st.tg_sample_id))
            if st.tg_acquiring and st.tg_mode != "sweep":
                bad.append(("mode", st.tg_mode))

    t = threading.Thread(target=watch)
    t.start()
    try:
        for i in range(15):
            n = v.tg_sweep_acquire(0.9e9, 1.1e9)
            v.tg_cw(bool(i % 2), 1e9, -20.0)       # CW requests racing the sweeps
            end = time.monotonic() + 5
            while time.monotonic() < end:
                st = v.status()
                if st.tg_acq_id == n and not st.tg_acquiring:
                    break
            assert v.get_tg_trace(n)["id"] == n
    finally:
        stop.set()
        t.join()
        v.shutdown()
    assert bad == []


# ---- the real backend against the fake DLL ----------------------------------------------

def test_real_backend_tg_calls_and_restore_order():
    cfg = Config()
    cfg.acquisition.continuous = False
    dll = FakeSaApi(bins=None)
    v = SpectrumAnalyzer(SaApiAnalyzer(cfg, dll=dll), cfg)
    v.start(run=False)
    try:
        assert v.status().tg_mode == "unknown"
        v.set_span(20e6)
        v.set_continuous(True)
        v.step()                                  # spectrum configured
        v.tg_cw(True, 1.5e9, -12.0)
        assert ("saSetTg", 0, 1.5e9, -12.0) in dll.calls
        dll.calls.clear()
        v.tg_sweep_acquire(0.9e9, 1.1e9, points=201)
        for _ in range(10):
            if not v.status().tg_acquiring:
                break
            v.step()
        names = dll.names()
        assert "saConfigTgSweep" in names and names.count("saGetSweep_32f") == 1
        after = names[names.index("saGetSweep_32f") + 1:]
        # the TG sweep is stopped (abort), the CW set, and the spectrum is
        # reconfigured only on the next pass (after the result is out)
        assert after.index("saAbort") < after.index("saSetTg")
        assert "saInitiate" not in after
        v.step()
        assert "saInitiate" in dll.names()[len(names):]
        assert dll.mode == 0 and dll.tg_level == -12.0
        t = v.get_tg_trace()
        assert t["points"] == 201 and len(t["db"]) == 201
        dll.calls.clear()
        v.tg_cw(False)                            # a PARK, not an abort
        assert dll.calls == [("saSetTg", 0, PARK[0], PARK[1])]
        dll.calls.clear()
    finally:
        v.shutdown()
    # shutdown parks BEFORE the device is closed
    names = dll.names()
    assert names.index("saSetTg") < names.index("saCloseDevice")


def test_real_backend_without_tg_refuses_cw():
    cfg = Config()
    b = SaApiAnalyzer(cfg, dll=FakeSaApi(tg=False))
    b.open()
    try:
        with pytest.raises(RuntimeError, match="no tracking generator"):
            b.set_tg_cw(1e9, -20.0)
    finally:
        b.close()


def test_real_backend_start_writes_nothing_to_the_tg():
    cfg = Config()
    dll = FakeSaApi(readonly=True)
    v = SpectrumAnalyzer(SaApiAnalyzer(cfg, dll=dll), cfg)
    v.start(run=False)
    try:
        assert v.status().tg_mode == "unknown" and dll.writes() == []
    finally:
        dll.readonly = False
        v.shutdown()


# ---- over the wire -------------------------------------------------------------------------

CMD_PORT, PUB_PORT = 17600, 17601


@pytest.fixture
def wire():
    from signalhound.net.client import SignalhoundClient
    from signalhound.net.service import SignalhoundService
    cfg = Config()
    v, sim = build_sim_system(cfg, realtime=False, seed=4)
    svc = SignalhoundService(v, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                             status_hz=20.0)
    svc.start()
    cli = SignalhoundClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                            timeout_ms=2000)
    time.sleep(0.3)
    yield svc, cli
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _wait(cli, pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = cli._cmd({"cmd": "status"})["status"]
        if pred(st):
            return st
        time.sleep(0.03)
    return cli._cmd({"cmd": "status"})["status"]


def test_the_contract_over_the_wire(wire):
    svc, cli = wire
    st = cli._cmd({"cmd": "status"})["status"]
    for k in TG_KEYS + ("describe_rev",):
        assert k in st, k
    assert st["tg_mode"] == "unknown"

    r = cli._cmd({"cmd": "tg_cw", "on": True, "freq_hz": 1.2e9, "level_dbm": -15.0})
    assert r == {"ok": True, "tg_cw": {"on": True, "freq_hz": 1.2e9, "level_dbm": -15.0},
                 "deferred": False}
    st = _wait(cli, lambda s: s["tg_cw_on"])
    assert st["tg_mode"] == "cw" and st["tg_cw_freq_hz"] == 1.2e9
    r = cli._cmd({"cmd": "tg_cw", "on": "false"})                              # gotcha #3
    assert r["tg_cw"]["on"] is False and _wait(cli, lambda s: s["tg_mode"] == "parked")
    r = cli._cmd({"cmd": "tg_cw", "on": True, "freq_hz": 9e9})
    assert r["ok"] is False and "4.4 GHz" in r["error"]
    r = cli._cmd({"cmd": "tg_cw", "freq_hz": 1.3e9})                           # no `on`: keep off
    assert r["ok"] is True and r["tg_cw"]["on"] is False and r["tg_cw"]["freq_hz"] == 1.3e9

    r = cli._cmd({"cmd": "tg_sweep_acquire", "start_hz": 0.9e9, "stop_hz": 1.1e9,
                  "averages": 2, "points": 2000})
    assert r["ok"] is True and r["points"] == 1001 and r["level_applied"] is False
    n = r["tg_acq_id"]
    st = _wait(cli, lambda s: s["tg_acq_id"] == n and not s["tg_acquiring"])
    assert st["tg_sample_id"] == n and st["tg_error"] == ""
    t = cli._cmd({"cmd": "get_tg_trace", "id": n})
    assert t["ok"] is True
    for k in ("id", "start_hz", "bin_hz", "points", "db", "unit", "level_dbm", "overload"):
        assert k in t, k
    assert t["id"] == n and len(t["db"]) == t["points"] == 1001
    assert t["unit"] == "dB" and t["level_dbm"] is None
    local = svc.signalhound.get_tg_trace(n)
    assert np.array_equal(np.asarray(t["db"], dtype=float), local["db"])
    assert cli._cmd({"cmd": "get_tg_trace", "id": n + 1})["ok"] is False
    assert cli._cmd({"cmd": "tg_abort"}) == {"ok": True, "aborted": False}    # nothing running
    g = cli._cmd({"cmd": "tg_grid", "start_hz": 0.9e9, "stop_hz": 1.1e9, "points": 2000})
    assert g["ok"] and g["points"] == 1001 and g["start_hz"] == t["start_hz"]
    assert g["bin_hz"] == pytest.approx(t["bin_hz"]) and g["predicted"] is False
    r = cli._cmd({"cmd": "tg_sweep_acquire", "start_hz": 0.9e9, "stop_hz": 1.1e9})
    assert cli._cmd({"cmd": "tg_abort", "id": r["tg_acq_id"] + 7})["aborted"] is False
    _wait(cli, lambda s: not s["tg_acquiring"])

    bad = cli._cmd({"cmd": "tg_sweep_acquire", "start_hz": 2e9, "stop_hz": 1e9})
    assert bad["ok"] is False

    # the client facade wraps the same verbs
    assert cli.tg_cw(True, 1e9, -20.0)["on"] is True
    tr = cli.tg_sweep_blocking(0.95e9, 1.05e9, timeout_s=5)
    assert isinstance(tr["db"], np.ndarray) and tr["freqs_hz"].shape == tr["db"].shape
    assert math.isclose(tr["freqs_hz"][0], tr["start_hz"])
    s = cli.status()
    assert s.tg_cw_on is True and s.tg_mode == "cw"                            # CW restored


# --------------------------------------------------------------------------- #
# Found on the lab PC (2026-09-28): a tg_cw during a LONG spectrum sweep waited
# for the hardware lock, the client timed out after 1.5 s -- and the change was
# applied anyway. A CW change must answer at once (queued) and be applied as
# soon as the sweep lets go of the hardware.
# --------------------------------------------------------------------------- #
def _hold_hw(sa, release):
    """Hold the hardware lock from another thread, like saGetSweep on a 7.5 s
    sweep does, until `release` is set."""
    got = threading.Event()

    def holder():
        with sa._hw:
            got.set()
            release.wait(5.0)
    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert got.wait(1.0)
    return t


def test_a_cw_change_during_a_long_sweep_answers_at_once_and_applies_after(sa):
    sa.tg_cw(True, 1e9, -20.0)
    release = threading.Event()
    t = _hold_hw(sa, release)
    t0 = time.monotonic()
    r = sa.tg_cw(None, freq_hz=950e6)
    assert time.monotonic() - t0 < 1.0, "tg_cw waited for the sweep"
    assert r["deferred"] is True and r["freq_hz"] == 950e6
    assert sa.status().tg_cw_freq_hz == 1e9       # the echo moves only when applied
    release.set(); t.join(2.0)
    sa.step()                                     # the sweep thread's next pass
    st = sa.status()
    assert st.tg_cw_freq_hz == 950e6 and sa.sim.tg_cw == (950e6, -20.0)


def test_a_newer_cw_command_wins_over_an_older_queued_one(sa):
    sa.tg_cw(True, 1e9, -20.0)
    release = threading.Event()
    t = _hold_hw(sa, release)
    sa.tg_cw(None, freq_hz=950e6)                 # queued
    sa.tg_cw(None, level_dbm=-25.0)               # queued too: builds on the queued one
    release.set(); t.join(2.0)
    sa.tg_cw(None, freq_hz=900e6)                 # applied directly, newest
    sa.step()
    assert sa.sim.tg_cw == (900e6, -25.0) and sa.status().tg_cw_freq_hz == 900e6
