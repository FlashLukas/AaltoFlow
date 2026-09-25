"""The PowerMeter brain against the simulator, driven by hand with poll_once()
(no thread), so every test is deterministic and fast."""

import math

import pytest

from pm16.config import Config
from pm16.sim_system import build_sim_system


@pytest.fixture
def system():
    cfg = Config()
    meter, sim = build_sim_system(cfg, realtime=False, seed=3, zero_time_s=0.0)
    events = []
    meter._on_event = lambda lvl, msg: events.append((lvl, msg))
    meter.start(poll=False)
    yield meter, sim, cfg, events
    meter.shutdown()


def test_start_adopts_the_meters_stored_settings(system):
    meter, sim, cfg, _ = system
    # the sim meter "remembers" 633 nm; connecting must not overwrite it with 800
    assert cfg.sensor.wavelength_nm == 633.0
    assert meter.status().wavelength_nm == 633.0


def test_push_on_start_pushes_config():
    cfg = Config()
    cfg.hardware.push_on_start = True
    cfg.sensor.wavelength_nm = 1064.0
    meter, sim = build_sim_system(cfg, realtime=False)
    meter.start(poll=False)
    assert sim.get_wavelength() == 1064.0
    meter.shutdown()


def test_limits_come_from_the_device_too(system):
    meter, sim, cfg, _ = system
    cfg.limits.wavelength_max_nm = 2000.0            # config is wider than the head
    assert meter.wavelength_limits() == (400.0, 1100.0)
    lo, hi = meter.range_limits()
    assert (lo, hi) == sim.range_limits()


def test_live_reading_and_wavelength_matter(system):
    meter, sim, _, _ = system
    meter.set_wavelength(sim.laser_nm)
    meter.poll_once()
    right = meter.status().power_W
    assert right == pytest.approx(sim.incident_W, rel=0.03)
    meter.set_wavelength(1064.0)                     # wrong wavelength -> wrong power
    meter.poll_once()
    assert meter.status().power_W > right * 1.1


def test_clamp_emits_warning(system):
    meter, _, cfg, events = system
    meter.set_wavelength(5000.0)
    assert cfg.sensor.wavelength_nm == 1100.0
    assert events[-1][0] == "warn"


def test_nan_refused(system):
    meter, _, _, _ = system
    with pytest.raises(ValueError):
        meter.set_wavelength(float("nan"))


def test_set_range_switches_auto_off_and_snaps_up(system):
    meter, sim, cfg, _ = system
    meter.set_range(5.2e-4)
    s = meter.status()
    assert s.auto_range is False and sim.get_auto_range() is False
    assert s.range_set_W == 5.2e-4                   # what we asked (settle echoes this)
    assert s.range_W == pytest.approx(1.736957e-2)   # what the meter picked (next range up)


def test_auto_off_keeps_the_range_auto_chose(system):
    meter, sim, cfg, _ = system
    meter.poll_once()
    chosen = meter.status().range_W
    meter.set_auto_range(False)
    assert cfg.sensor.range_W == chosen
    assert meter.status().range_W == chosen


def test_overrange_is_flagged(system):
    meter, sim, _, _ = system
    sim.incident_W = 0.1                             # 100 mW into the 0.17 mW range
    meter.set_range(1e-4)
    meter.poll_once()
    assert meter.status().flag == "overrange"


def test_acquire_latches_mean_of_fresh_readings(system):
    meter, sim, cfg, _ = system
    cfg.acquisition.readings = 4
    n = meter.acquire()
    s = meter.status()
    assert s.acq_id == n and s.acquiring            # id and flag move together
    for k in range(4):
        assert meter.status().acquiring
        meter.poll_once()
    s = meter.status()
    assert not s.acquiring
    assert s.sample["acq_id"] == n and s.sample["n"] == 4
    assert s.sample["power_W"] > 0 and s.sample["std_W"] >= 0


def test_reading_started_before_the_trigger_is_not_used(system):
    """The whole point of acquire: a reading that began before the trigger
    describes the previous scan point and must not be averaged in."""
    meter, sim, cfg, _ = system
    cfg.acquisition.readings = 1
    clock = {"t": 0.0}
    meter._clock = lambda: clock["t"]
    real_measure = sim.measure_power

    def trigger_arrives_mid_reading():
        # the reading started at t=0; one second into it a scan triggers
        clock["t"] = 1.0
        meter.acquire()
        return real_measure()

    sim.measure_power = trigger_arrives_mid_reading
    meter.poll_once()
    assert meter.status().acquiring, "a reading that began before the trigger was accepted"

    sim.measure_power = real_measure
    meter.poll_once()                    # starts at t=1.0, after the trigger: counts
    assert not meter.status().acquiring


def test_acquire_refused_when_not_connected():
    meter, _ = build_sim_system(Config(), realtime=False)
    with pytest.raises(ValueError):
        meter.acquire()


def test_zero_blocks_readings_until_done(system):
    meter, sim, _, events = system
    sim.zero_time_s = 10.0
    clock = {"t": 0.0}
    sim._clock = lambda: clock["t"]
    before = meter.status().readings
    meter.zero()
    assert meter.status().zeroing
    with pytest.raises(ValueError):
        meter.acquire()                  # no readings possible while zeroing
    meter.poll_once()
    assert meter.status().readings == before
    clock["t"] = 11.0
    meter.poll_once()
    assert not meter.status().zeroing
    assert "zero adjustment finished" in events[-1][1]


def test_hw_error_is_reported_not_hidden(system):
    meter, sim, _, events = system

    def broken():
        raise OSError("USB gone")

    sim.measure_power = broken
    meter.poll_once()
    s = meter.status()
    assert "USB gone" in s.hw_error
    assert events[-1][0] == "error"


def test_status_never_touches_hardware(system):
    meter, sim, _, _ = system

    def boom(*a, **k):
        raise AssertionError("status() called the backend")

    for name in ("measure_power", "get_range", "get_wavelength", "zero_running"):
        setattr(sim, name, boom)
    s = meter.status()
    assert s.connected and not math.isnan(s.wavelength_nm)
