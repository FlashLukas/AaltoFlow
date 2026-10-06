"""The brain against the simulated bench.

Each rule of scope.py has a test that fails without it:
  * read-only start (adopt the scope's settings, write nothing);
  * the acquisition: numbered, FRESH traces only (the first after the
    trigger is skipped), latched with "acquiring = False" together;
  * a record-shaping change restarts it; abort latches "aborted";
  * refused when the scope is stopped / single-shot with averaging;
  * settings: queued, written, read back (snapped), echoed as asked;
  * a change made at the scope's front panel is adopted;
  * physical units, the filter, the loop numbers end to end;
  * clipping at the screen edge is reported.
"""

import time

import numpy as np
import pytest

from scope.config import Config
from scope.sim_system import build_sim_system


@pytest.fixture
def system():
    cfg = Config()
    scope, sim = build_sim_system(cfg, seed=5)
    events = []
    scope._on_event = lambda level, msg: events.append((level, msg))
    scope.start()
    yield scope, sim, cfg, events
    scope.shutdown()


def wait(scope, pred, timeout=8.0):
    t_end = time.monotonic() + timeout
    st = scope.status()
    while time.monotonic() < t_end:
        st = scope.status()
        if pred(st):
            return st
        time.sleep(0.01)
    raise AssertionError(f"not reached; last status {st}")


def acquire(scope, timeout=15.0):
    n = scope.acquire()
    return n, wait(scope, lambda s: s["acq_id"] == n and not s["acquiring"], timeout)


def test_start_adopts_and_writes_nothing(system):
    scope, sim, cfg, events = system
    assert sim.writes == []
    st = scope.status()
    # the sim boots CH2 at 0.1 V/div, offset -0.5 V -- not the config's 0.5 / 0
    assert st["ch2_vdiv_V"] == 0.1 and st["ch2_offset_V"] == -0.5
    assert cfg.channel_2.vdiv_V == 0.1                 # the config tells the truth
    time.sleep(0.5)
    assert sim.writes == [], "the trace thread wrote something on its own"


def test_running_average_fills_and_restarts(system):
    scope, sim, cfg, events = system
    scope.set_averages(4)
    wait(scope, lambda s: s["running_n"] == 4)
    tr = scope.get_trace("live")
    assert tr["averages"] == 4 and tr["ch1"].size == cfg.acquisition.points
    scope.restart_average()
    assert scope.status()["running_n"] == 0


def test_acquisition_is_fresh_numbered_and_latched(system):
    scope, sim, cfg, events = system
    scope.set_averages(3)
    t_trigger = time.monotonic()
    n, st = acquire(scope)
    smp = st["sample"]
    assert smp["acq_id"] == n and smp["averages"] == 3
    tr = scope.get_trace("sample")
    assert tr["acq_id"] == n and tr["time_s"].size == cfg.acquisition.points
    # fresh: 3 counted + 1 skipped -> at least ~4 dead times after the trigger
    rate = st["trigger_rate_Hz"]
    assert time.monotonic() - t_trigger >= 3.0 / rate
    n2, st2 = acquire(scope)
    assert n2 == n + 1 and st2["sample"]["acq_id"] == n2


def test_a_setting_change_restarts_the_acquisition(system):
    scope, sim, cfg, events = system
    scope.set_averages(30)
    scope.acquire()
    time.sleep(0.3)
    scope.set_vdiv("ch1", 1.0)
    assert any("restarted" in m for _, m in events)
    scope.abort()
    st = scope.status()
    assert not st["acquiring"] and st["sample"]["aborted"]
    with pytest.raises(ValueError, match="aborted"):
        scope.get_trace("sample")


def test_refused_when_the_scope_cannot_deliver(system):
    scope, sim, cfg, events = system
    scope.set_trigger_mode("stop")
    wait(scope, lambda s: s["trigger_mode"] == "stop" and s["settings_settled"])
    with pytest.raises(ValueError, match="stopped"):
        scope.acquire()
    scope.set_trigger_mode("single")
    wait(scope, lambda s: s["trigger_mode_set"] == "single" and s["settings_settled"])
    with pytest.raises(ValueError, match="single"):
        scope.acquire()


def test_settings_are_written_read_back_and_echoed(system):
    scope, sim, cfg, events = system
    scope.set_vdiv("ch1", 0.3)            # the scope snaps 0.3 -> 0.2 (1-2-5)
    st = wait(scope, lambda s: s["ch1_vdiv_V_set"] == 0.3 and s["settings_settled"])
    assert st["ch1_vdiv_V"] == pytest.approx(0.2)
    assert ("set_channel", "ch1", {"vdiv_V": 0.3}) in sim.writes
    scope.set_tdiv(4e-3)                  # -> 5 ms (1-2.5-5)
    st = wait(scope, lambda s: s["tdiv_s_set"] == 4e-3 and s["settings_settled"])
    assert st["tdiv_s"] == pytest.approx(5e-3)
    scope.set_trigger_level(0.7)
    st = wait(scope, lambda s: s["trigger_level_V_set"] == 0.7 and s["settings_settled"])
    assert st["trigger_level_V"] == 0.7


def test_a_front_panel_change_is_adopted(system, monkeypatch):
    scope, sim, cfg, events = system
    import scope.scope as brain
    monkeypatch.setattr(brain, "_SETTINGS_REREAD_S", 0.1)
    sim.settings["channels"]["ch1"]["vdiv_V"] = 2.0       # someone turned the knob
    st = wait(scope, lambda s: s["ch1_vdiv_V"] == 2.0)
    assert cfg.channel_1.vdiv_V == 2.0
    assert any("changed at the scope" in m for _, m in events)


def test_units_filter_and_loop_end_to_end(system):
    scope, sim, cfg, events = system
    scope.set_physical("ch1", scale=1 / cfg.sim.hall_V_per_mT, unit="mT")
    scope.set_filter(lowpass_Hz=3000.0)
    scope.set_averages(6)
    n, st = acquire(scope)
    smp = st["sample"]
    assert smp["ch1"]["amplitude"] == pytest.approx(cfg.sim.field_amp_mT, rel=0.03)
    assert smp["ch1"]["frequency"] == pytest.approx(cfg.sim.drive_Hz, rel=0.01)
    assert smp["loop"]["hc"] == pytest.approx(cfg.sim.hc_mT, abs=0.5)
    assert smp["loop"]["ms"] == pytest.approx(cfg.sim.ms_V, rel=0.05)
    assert smp["lowpass_Hz"] == 3000.0
    assert scope.status()["ch1_unit"] == "mT"
    # the simulated sample changes, the numbers follow
    scope.set_sim("hc_mT", 25.0)
    n, st = acquire(scope)
    assert st["sample"]["loop"]["hc"] == pytest.approx(25.0, abs=0.5)


def test_clipping_is_reported(system):
    scope, sim, cfg, events = system
    scope.set_vdiv("ch1", 0.05)           # the 1 V field sine goes off the screen
    wait(scope, lambda s: s["ch1_vdiv_V"] == 0.05 and s["settings_settled"])
    scope.set_averages(2)
    n, st = acquire(scope)
    assert "ch1" in st["sample"]["clipped"]
    # the warning is emitted just AFTER the sample is latched: give it a moment
    t_end = time.monotonic() + 2.0
    while not any("CLIPPED" in m for _, m in events) and time.monotonic() < t_end:
        time.sleep(0.01)
    assert any("CLIPPED" in m for _, m in events)


def test_hardware_error_fails_a_running_acquisition(system, monkeypatch):
    scope, sim, cfg, events = system
    scope.set_averages(50)
    scope.acquire()

    def dead():
        raise OSError("USB gone")
    monkeypatch.setattr(sim, "new_trace_ready", dead)
    st = wait(scope, lambda s: not s["acquiring"])
    assert "USB gone" in st["sample"]["error"] and "USB gone" in st["hw_error"]
    with pytest.raises(ValueError, match="failed"):
        scope.get_trace("sample")


def test_bench_scene_triggers_on_the_square():
    cfg = Config()
    cfg.sim.scene = "bench"
    scope, sim = build_sim_system(cfg, seed=2)
    scope.start()
    try:
        scope.set_averages(2)
        n, st = acquire(scope)
        tr = scope.get_trace("sample")
        # CH2 is the AFG's synchronous square, rising at the trigger (t = 0)
        y = tr["ch2_raw"]
        mid = (y.max() + y.min()) / 2
        i0 = int(np.argmin(np.abs(tr["time_s"])))
        assert y[i0 + 5] > mid > y[i0 - 5]
    finally:
        scope.shutdown()
