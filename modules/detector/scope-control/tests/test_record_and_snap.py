"""Lab PC 2026-10-07 (re-test of 2453dc0), driven by hand with step():

  * requests in quick succession were each compared with the FINAL read-back
    ("asked 0.5, the scope set 0.01"): only the last request per setting is
    compared, against the read-back of that push;
  * the record length is per time/div (41 div at 1 ms/div, not at 0.5 s/div),
    so a length seen at one time/div must not be carried to another;
  * a record longer than the time since the previous one cannot be one fresh
    record: warned, with the numbers.
"""

import numpy as np
import pytest

from scope.config import Config
from scope.sim_system import build_sim_system


@pytest.fixture
def manual():
    cfg = Config()
    scope, sim = build_sim_system(cfg, seed=3)
    events = []
    scope._on_event = lambda level, msg: events.append((level, msg))
    scope.start(run=False)
    yield scope, events
    scope.shutdown()


def test_quick_requests_compare_only_the_last(manual):
    scope, events = manual
    scope.set_tdiv(0.5)
    scope.set_tdiv(0.01)                      # before the worker pushed the first
    scope.step()
    assert scope.status()["tdiv_s"] == pytest.approx(0.01)
    assert not any("asked 0.5" in m for _, m in events), events


def test_record_length_is_per_time_div(manual):
    scope, events = manual
    scope.set_tdiv(1e-3)
    scope.step()
    n = 20480                                  # 41 ms at 500 kSa/s: 41 div
    t = np.arange(n) * 2e-6 - 0.02048
    y = np.sin(2 * np.pi * 50 * t)
    scope._take(t, {"ch1": y, "ch2": y}, scope._rev, now=10.0)
    assert scope.status()["record_s"] == pytest.approx(0.041, rel=0.01)
    scope.set_tdiv(0.5)
    scope.step()
    # the sim's record is the screen (SANU / SARA = 14 div): 7 s, not 41 x 0.5
    assert scope.status()["record_s"] == pytest.approx(7.0, rel=0.01)


def test_a_record_longer_than_the_trigger_gap_is_flagged(manual):
    scope, events = manual
    scope.set_tdiv(0.5)
    scope.step()
    t = np.linspace(-16, 16, 20000)            # a 32 s block ...
    y = np.sin(2 * np.pi * 50 * t)
    scope._take(t, {"ch1": y, "ch2": y}, scope._rev, now=100.0)
    scope._take(t, {"ch1": y, "ch2": y}, scope._rev, now=108.0)   # ... every 8 s
    assert any("cannot be ONE fresh record" in m for lvl, m in events if lvl == "warn")
    assert scope.status()["last_record"]["span_s"] == pytest.approx(32.0)


def test_numbers_come_from_the_full_record(manual):
    """Lab PC 2026-10-07, 0.5 s/div: 20480 points of a 1 Vpp 50 Hz sine at
    1250 Sa/s read 0.26 Vpp at "11 Hz" -- the record had been averaged down
    to 1000 points (bins of 0.8 period) BEFORE the numbers. Now the numbers
    come from the full record, the stored trace keeps the amplitude, and the
    trace's alias is warned about."""
    scope, events = manual
    scope.set_tdiv(0.5)
    scope.set_averages(1)
    scope.step()
    n = 20480
    t = np.arange(n) / 1250.0 - n / 2500.0
    v = 0.52 * np.sin(2 * np.pi * 50 * t)
    scope._take(t, {"ch1": v, "ch2": v}, scope._rev, now=1.0)
    live = scope.status()["live"]
    assert live["ch1"]["pk2pk"] == pytest.approx(1.04, rel=0.01)
    assert live["ch1"]["frequency"] == pytest.approx(50.0, rel=1e-3)
    assert live["phase_21_deg"] == pytest.approx(0.0, abs=0.1)
    tr = scope.get_trace("live")
    assert tr["ch1"].size == scope.cfg.acquisition.points
    assert np.ptp(tr["ch1"]) > 0.9                 # sampled, not washed out
    assert any("alias" in m for lvl, m in events if lvl == "warn")


def test_boxcar_still_used_when_bins_are_short(manual):
    """At a fast time/div the neighbour averaging stays (it lowers noise)."""
    from scope import analysis as A
    t = np.arange(20480) * 2e-6                    # 41 ms at 500 kSa/s
    y = np.sin(2 * np.pi * 50 * t)
    noisy = y + np.random.default_rng(1).normal(0, 0.1, t.size)
    tr, red = A.reduce_points(t, {"y": noisy}, 1000, max_bin_s=1 / (20 * 50))
    assert np.std(red["y"] - np.sin(2 * np.pi * 50 * tr)) < 0.04   # averaged: ~0.1/sqrt(20)
