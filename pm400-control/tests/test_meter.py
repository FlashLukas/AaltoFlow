"""The Pm400Meter brain against the simulator, driven by hand with poll_once()
(no thread), so every test is deterministic and fast."""

import math

import pytest

from pm400.config import Config
from pm400.sim_system import build_sim_system


@pytest.fixture
def system():
    cfg = Config()
    meter, sim = build_sim_system(cfg, realtime=False, seed=3, zero_time_s=0.0)
    events = []
    meter._on_event = lambda lvl, msg: events.append((lvl, msg))
    meter.start(poll=False)
    yield meter, sim, cfg, events
    meter.shutdown()


def _swap(meter, cfg, head):
    cfg.sim.head = head
    meter.check_head()


# ---- start-up and the head ---------------------------------------------------

def test_start_adopts_the_consoles_settings(system):
    meter, sim, cfg, _ = system
    # the photodiode head loads 635 nm; connecting must not overwrite it with 800
    assert cfg.sensor.wavelength_nm == 635.0
    s = meter.status()
    assert s.wavelength_nm == 635.0 and s.head == "photodiode"
    assert s.quantity == "power" and s.unit == "W"


def test_push_on_start_pushes_config():
    cfg = Config()
    cfg.hardware.push_on_start = True
    cfg.sensor.wavelength_nm = 1064.0
    cfg.sensor.avg_time_s = 0.3
    meter, sim = build_sim_system(cfg, realtime=False)
    meter.start(poll=False)
    assert sim.get_wavelength() == 1064.0
    assert sim.get_avg_time() == pytest.approx(0.3)
    meter.shutdown()


def test_limits_come_from_the_head(system):
    meter, sim, cfg, _ = system
    assert meter.wavelength_limits() == (400.0, 1100.0)       # Si photodiode
    _swap(meter, cfg, "thermal")
    assert meter.wavelength_limits() == (190.0, 25000.0)      # thermal head
    cfg.limits.avg_time_max_s = 0.5                           # config narrower than the console
    assert meter.avg_time_limits()[1] == 0.5


def test_head_swap_to_pyro_changes_quantity_and_unit(system):
    meter, sim, cfg, events = system
    _swap(meter, cfg, "pyro")
    s = meter.status()
    assert s.head == "pyro" and s.quantity == "energy" and s.unit == "J"
    assert not s.auto_range_available and not s.zero_supported
    assert math.isnan(s.avg_time_s)
    meter.poll_once()
    s = meter.status()
    # 0.2 mJ pulses on the head, within the 2 % pulse-to-pulse scatter
    assert s.value == pytest.approx(cfg.sim.pulse_energy_J, rel=0.1)
    assert any("head changed" in m for _, m in events)


def test_pulse_rate_is_reported_for_pyro(system):
    meter, sim, cfg, _ = system
    _swap(meter, cfg, "pyro")
    meter.check_head()                        # the rate is read at the head check
    assert meter.status().rep_rate_Hz == cfg.sim.rep_rate_Hz


def test_no_head_refuses_acquire_and_reports_it(system):
    meter, sim, cfg, _ = system
    _swap(meter, cfg, "none")
    meter.poll_once()
    s = meter.status()
    assert s.quantity == "none" and s.flag == "no_sensor" and math.isnan(s.value)
    with pytest.raises(ValueError, match="head"):
        meter.acquire()
    with pytest.raises(ValueError):
        meter.zero()


def test_head_swap_aborts_a_running_acquisition(system):
    """A scan waiting for acquisition n must get n back -- empty and flagged --
    never the previous sample (gotcha #28)."""
    meter, sim, cfg, _ = system
    n = meter.acquire()
    _swap(meter, cfg, "thermal")
    s = meter.status()
    assert not s.acquiring and s.sample["acq_id"] == n
    assert math.isnan(s.sample["value"]) and "head changed" in s.sample["flag"]


def test_poll_checks_the_head_periodically(system):
    meter, sim, cfg, _ = system
    clock = {"t": 100.0}
    meter._clock = lambda: clock["t"]
    meter._last_head_check = 100.0
    cfg.sim.head = "thermal"
    meter.poll_once()
    assert meter.status().head == "photodiode"      # not due yet
    clock["t"] += cfg.hardware.head_check_s + 0.1
    meter.poll_once()
    assert meter.status().head == "thermal"


# ---- readings and settings -------------------------------------------------------

def test_live_reading_and_wavelength_matter(system):
    meter, sim, cfg, _ = system
    meter.set_wavelength(cfg.sim.laser_nm)
    meter.poll_once()
    right = meter.status().value
    assert right == pytest.approx(cfg.sim.incident_W, rel=0.03)
    meter.set_wavelength(1064.0)                     # wrong wavelength -> wrong power
    meter.poll_once()
    assert meter.status().value > right * 1.1


def test_thermal_head_lags_the_light(system):
    """A thermopile starts cold and needs ~5 time constants to read true."""
    meter, sim, cfg, _ = system
    clock = {"t": 0.0}
    sim._clock = lambda: clock["t"]
    _swap(meter, cfg, "thermal")
    meter.set_wavelength(cfg.sim.laser_nm)
    clock["t"] = 0.2
    meter.poll_once()
    early = meter.status().value
    clock["t"] = 8.0
    meter.poll_once()
    late = meter.status().value
    assert early < 0.5 * late
    assert late == pytest.approx(cfg.sim.incident_W, rel=0.1)   # + the un-zeroed offset


def test_clamp_emits_warning(system):
    meter, _, cfg, events = system
    meter.set_wavelength(5000.0)
    assert cfg.sensor.wavelength_nm == 1100.0
    assert events[-1][0] == "warn"
    meter.set_avg_time(30.0)                         # 30 s averaging -> the 1 s envelope
    assert cfg.sensor.avg_time_s == 1.0
    assert events[-1][0] == "warn"


def test_nan_refused(system):
    meter, _, _, _ = system
    with pytest.raises(ValueError):
        meter.set_wavelength(float("nan"))
    with pytest.raises(ValueError):
        meter.set_range(float("inf"))


def test_averaging_time_is_pushed_and_reduces_noise(system):
    meter, sim, cfg, _ = system

    def spread(avg):
        meter.set_avg_time(avg)
        vals = []
        for _ in range(60):
            meter.poll_once()
            vals.append(meter.status().value)
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))

    noisy = spread(0.001)
    quiet = spread(1.0)
    assert sim.get_avg_time() == 1.0
    assert meter.status().avg_time_set_s == 1.0
    assert quiet < noisy / 5                           # sqrt(1000) ~ 30 in theory


def test_set_range_switches_auto_off_and_snaps_up(system):
    meter, sim, cfg, _ = system
    meter.set_range(5.2e-4)
    s = meter.status()
    assert s.auto_range is False and sim.get_auto_range() is False
    assert s.range_set == 5.2e-4                     # what we asked (settle echoes this)
    assert s.range == pytest.approx(1e-3)            # what the console picked (next range up)


def test_energy_range_on_pyro(system):
    meter, sim, cfg, _ = system
    _swap(meter, cfg, "pyro")
    meter.set_range(4e-4)
    s = meter.status()
    assert cfg.sensor.range_J == 4e-4 and s.range_set == 4e-4
    assert s.range == pytest.approx(1.5e-3)
    with pytest.raises(ValueError, match="auto range"):
        meter.set_auto_range(True)
    with pytest.raises(ValueError):
        meter.set_avg_time(0.1)


def test_auto_off_keeps_the_range_auto_chose(system):
    meter, sim, cfg, _ = system
    meter.poll_once()
    chosen = meter.status().range
    meter.set_auto_range(False)
    assert cfg.sensor.range_W == chosen
    assert meter.status().range == chosen


def test_overrange_is_flagged(system):
    meter, sim, cfg, _ = system
    cfg.sim.incident_W = 0.1                         # 100 mW into the 1 uW range
    meter.set_range(1e-7)
    meter.poll_once()
    assert meter.status().flag == "overrange"


# ---- acquire ---------------------------------------------------------------------

def test_acquire_latches_mean_of_fresh_readings(system):
    meter, sim, cfg, _ = system
    cfg.acquisition.readings = 4
    n = meter.acquire()
    s = meter.status()
    assert s.acq_id == n and s.acquiring            # id and flag move together
    for _ in range(4):
        assert meter.status().acquiring
        meter.poll_once()
    s = meter.status()
    assert not s.acquiring
    assert s.sample["acq_id"] == n and s.sample["n"] == 4
    assert s.sample["value"] > 0 and s.sample["std"] >= 0
    assert s.sample["unit"] == "W" and s.sample["quantity"] == "power"


def test_reading_started_before_the_trigger_is_not_used(system):
    """The whole point of acquire: a reading that began before the trigger
    describes the previous scan point and must not be averaged in."""
    meter, sim, cfg, _ = system
    cfg.acquisition.readings = 1
    clock = {"t": 0.0}
    meter._clock = lambda: clock["t"]
    meter._last_head_check = 0.0
    real_measure = sim.measure_power

    def trigger_arrives_mid_reading():
        clock["t"] = 1.0                 # the reading started at t=0
        meter.acquire()
        return real_measure()

    sim.measure_power = trigger_arrives_mid_reading
    meter.poll_once()
    assert meter.status().acquiring, "a reading that began before the trigger was accepted"
    sim.measure_power = real_measure
    meter.poll_once()                    # starts at t=1.0, after the trigger: counts
    assert not meter.status().acquiring


def test_settle_time_skips_early_readings(system):
    meter, sim, cfg, _ = system
    cfg.acquisition.readings = 1
    meter.set_settle(5.0)
    clock = {"t": 0.0}
    meter._clock = lambda: clock["t"]
    meter._last_head_check = 0.0
    meter.acquire()
    clock["t"] = 2.0
    meter.poll_once()
    assert meter.status().acquiring      # still settling
    clock["t"] = 5.5
    meter.poll_once()
    assert not meter.status().acquiring


def test_acquire_refused_when_not_connected():
    meter, _ = build_sim_system(Config(), realtime=False)
    with pytest.raises(ValueError):
        meter.acquire()


# ---- zero --------------------------------------------------------------------------

def test_zero_is_numbered_and_blocks_readings_until_done(system):
    meter, sim, cfg, events = system
    sim.zero_time_s = 10.0
    clock = {"t": 0.0}
    sim._clock = lambda: clock["t"]
    before = meter.status().readings
    n = meter.zero()
    s = meter.status()
    assert s.zeroing and s.zero_id == n and s.zero_error == ""
    with pytest.raises(ValueError):
        meter.acquire()                  # no readings possible while zeroing
    meter.poll_once()
    assert meter.status().readings == before
    clock["t"] = 11.0
    meter.poll_once()
    s = meter.status()
    assert not s.zeroing and s.zero_error == "OK" and s.zero_id == n
    assert "zero adjustment" in events[-1][1]


def test_zero_removes_the_thermal_offset(system):
    meter, sim, cfg, _ = system
    _swap(meter, cfg, "thermal")
    cfg.sim.incident_W = 0.0                         # dark
    meter.poll_once()
    before = meter.status().value
    meter.zero()
    meter.poll_once()                                # zero_time_s = 0: finishes at once
    meter.poll_once()
    assert before > 2e-5 and abs(meter.status().value) < 2e-5


def test_zero_refused_on_pyro(system):
    meter, sim, cfg, _ = system
    _swap(meter, cfg, "pyro")
    with pytest.raises(ValueError, match="zero"):
        meter.zero()


def test_cancel_zero_marks_it(system):
    meter, sim, cfg, _ = system
    sim.zero_time_s = 10.0
    meter.zero()
    meter.cancel_zero()
    s = meter.status()
    assert not s.zeroing and s.zero_error == "cancelled"


# ---- robustness ------------------------------------------------------------------------

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

    for name in ("measure_power", "measure_energy", "get_range", "get_wavelength",
                 "zero_running", "sensor_info", "get_avg_time"):
        setattr(sim, name, boom)
    s = meter.status()
    assert s.connected and not math.isnan(s.wavelength_nm)


def test_apply_config_follows_a_sim_head_change(system):
    """Settings > Simulation > head is a head swap; apply must not push
    power-head settings into the new pyro head."""
    meter, sim, cfg, _ = system
    cfg.sim.head = "pyro"
    meter.apply_config()
    assert meter.status().quantity == "energy"


def test_shutdown_is_idempotent(system):
    meter, _, _, _ = system
    meter.shutdown()
    meter.shutdown()
    assert meter.status().connected is False


# ---- review additions (2026-09-27) ---------------------------------------------------

def test_zero_with_the_light_on_zeroes_the_light_too(system):
    """The console takes as its zero WHATEVER reaches the head when the
    adjustment finishes. The sim must be as unforgiving as the real thing, or
    the GUI's 'cover the head first' warning looks like paranoia."""
    meter, sim, cfg, _ = system
    cfg.sim.incident_W = 1e-3
    meter.poll_once()
    assert meter.status().value > 5e-4
    meter.zero()
    meter.poll_once()                                # zero_time_s = 0: finishes now
    meter.poll_once()
    assert abs(meter.status().value) < 5e-5          # the light became the zero
    cfg.sim.incident_W = 0.0                         # "uncover" -> reads NEGATIVE
    meter.poll_once()
    assert meter.status().value < -5e-4


def test_zero_does_not_change_the_offset_until_it_finishes(system):
    meter, sim, cfg, _ = system
    _swap(meter, cfg, "thermal")
    cfg.sim.incident_W = 0.0
    sim.zero_time_s = 10.0
    meter.zero()
    meter.cancel_zero()                              # cancelled: old zero stays
    meter.poll_once()
    assert meter.status().value > 2e-5


def test_set_wavelength_without_a_head_says_so(system):
    meter, sim, cfg, _ = system
    _swap(meter, cfg, "none")
    with pytest.raises(ValueError, match="no usable sensor head"):
        meter.set_wavelength(700.0)


def test_acquire_timeout_follows_settle_and_readings(system):
    meter, sim, cfg, _ = system
    base = meter.acquire_timeout_s()
    assert base >= cfg.acquisition.timeout_s
    meter.set_settle(50.0)
    meter.set_acquisition(500)                       # 500 x 0.1 s + 50 s settle
    assert meter.acquire_timeout_s() >= 100.0
