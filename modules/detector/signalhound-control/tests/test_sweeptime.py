"""The sweep-time estimate learns from real sweeps (lab PC 2026-10-06: an
SA124B swept 0.9-12 GHz at RBW 6 MHz in 4.65 s, the SA44B-based estimate
said 82 s). Offline: the fake sa_api.dll, and a fake clock that a fake sweep
advances, so the "measured" durations are exact."""

import pytest

from fake_sa_api import FakeSaApi
from signalhound.backends.sa_api import SaApiAnalyzer
from signalhound.config import Config
from signalhound.instruments import SweepSettings
from signalhound.spectrum import SpectrumAnalyzer
from signalhound.sweeptime import MAX_ENTRIES, SweepTimeLearner


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TimedSaApi(FakeSaApi):
    """saGetSweep takes `seconds_per_GHz` x span on the fake clock -- i.e. a
    sweep whose duration really scales with the span, like the instrument."""

    def __init__(self, clock, seconds_per_GHz, **kw):
        super().__init__(**kw)
        self.clock = clock
        self.seconds_per_GHz = seconds_per_GHz

    def _saGetSweep_32f(self, h, mn, mx):
        self.clock.t += self.seconds_per_GHz * self.span / 1e9
        return super()._saGetSweep_32f(h, mn, mx)


def _brain(seconds_per_GHz, bins=None):
    clock = FakeClock()
    cfg = Config()
    cfg.acquisition.continuous = False
    dll = TimedSaApi(clock, seconds_per_GHz, device_type=4, bins=bins)     # SA124B
    v = SpectrumAnalyzer(SaApiAnalyzer(cfg, dll=dll), cfg, clock=clock)
    v.start(run=False)
    return v


def _acquire(v):
    n = v.acquire()
    for _ in range(50):
        if not v.status().acquiring:
            return n
        v.step()
    raise AssertionError("acquisition never finished")


def _settle(v):
    """One idle pass: configures the analyser with the new settings, so the
    reported grid (and with it the points of the estimate) is current."""
    v.step()


def test_the_learned_time_replaces_the_a_priori_one_after_one_sweep():
    v = _brain(seconds_per_GHz=0.3, bins=11101)
    try:
        v.set_start_stop(0.9e9, 12e9)
        v.set_rbw(6e6)
        _settle(v)
        a_priori = v.status().sweep_time_s
        assert a_priori == pytest.approx(4.79, abs=0.05)       # the SA124B constants
        _acquire(v)
        # 11.1 GHz x 0.3 s/GHz on the fake clock = what the sweep "took"
        assert v.status().sweep_time_s == pytest.approx(3.33)
    finally:
        v.shutdown()


def test_a_changed_span_scales_the_learned_time():
    v = _brain(seconds_per_GHz=0.3)
    try:
        v.set_start_stop(1e9, 11e9)
        v.set_rbw(6e6)
        _settle(v)
        _acquire(v)
        assert v.status().sweep_time_s == pytest.approx(3.0)
        v.set_start_stop(1e9, 6e9)                    # half the span (and the points)
        _settle(v)
        assert v.status().sweep_time_s == pytest.approx(1.5, rel=0.01)
        _acquire(v)                                   # ...and once timed, exact again
        assert v.status().sweep_time_s == pytest.approx(1.5)
        v.set_start_stop(1e9, 11e9)                   # the first setting is still known
        _settle(v)
        assert v.status().sweep_time_s == pytest.approx(3.0)
    finally:
        v.shutdown()


def test_the_sa124b_a_priori_estimate_is_near_the_measured_sweep():
    """Measured 2026-10-06: 0.9-12 GHz, RBW 6 MHz, 11101 points, 4.65 s."""
    cfg = Config()
    b = SaApiAnalyzer(cfg, dll=FakeSaApi(device_type=4))
    b.open()
    try:
        s = SweepSettings.from_config(cfg)
        s = SweepSettings(**{**s.__dict__, "center_Hz": 6.45e9, "span_Hz": 11.1e9,
                             "rbw_Hz": 6e6, "vbw_Hz": 6e6, "tg_on": False})
        est = b.sweep_time_s(s, 11101)
        assert 4.65 / 2 <= est <= 4.65 * 2, est
    finally:
        b.close()


def _s(start, stop, rbw=6e6, tg=False):
    s = SweepSettings.from_config(Config())
    return SweepSettings(**{**s.__dict__, "center_Hz": (start + stop) / 2,
                            "span_Hz": stop - start, "rbw_Hz": rbw, "tg_on": tg})


def test_learner_keeps_models_and_modes_apart():
    L = SweepTimeLearner()
    L.record("SA124B", _s(1e9, 2e9), 1001, 0.5)
    assert L.estimate("SA44B", _s(1e9, 2e9), 1001) is None          # other model
    assert L.estimate("SA124B", _s(1e9, 2e9, tg=True), 1001) is None  # other mode
    assert L.estimate("SA124B", _s(1e9, 2e9), 1001) == 0.5


def test_a_tg_sweep_scales_by_points():
    L = SweepTimeLearner()
    L.record("SA44B", _s(1e9, 2e9, tg=True), 101, 0.4)
    assert L.estimate("SA44B", _s(1e9, 2e9, tg=True), 202) == pytest.approx(0.8)


def test_the_last_measurement_wins_and_the_table_is_bounded():
    L = SweepTimeLearner()
    L.record("SA124B", _s(1e9, 2e9), 1001, 0.5)
    L.record("SA124B", _s(1e9, 2e9), 1001, 0.7)
    assert L.estimate("SA124B", _s(1e9, 2e9), 1001) == 0.7
    for i in range(MAX_ENTRIES + 10):
        L.record("SA124B", _s(1e9, 2e9 + i * 1e6), 1001, 1.0)
    assert len(L) == MAX_ENTRIES
    L.record("SA124B", _s(1e9, 2e9), 1001, float("nan"))           # ignored
    L.record("SA124B", _s(1e9, 2e9), 1001, 0.0)                    # ignored
    assert len(L) == MAX_ENTRIES
