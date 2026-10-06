"""The SourceMeter brain against the simulated 2450: physics, clamps, the
output boxes, safety, settling and the scan-safe acquisition."""

import math
import time

import pytest

from k2450.backends.base import snap_range
from k2450.config import Config
from k2450.sim_system import build_sim_system


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(clock=None, **cfg_changes):
    """A started brain with NO poll thread: tests drive poll_once() by hand.
    cfg_changes: group__field=value."""
    cfg = Config()
    cfg.measure.nplc = 1.0
    cfg.sim.noise_ppm = 0.0            # deterministic unless a test wants noise
    for k, v in cfg_changes.items():
        grp, field = k.split("__")
        setattr(getattr(cfg, grp), field, v)
    smu, sim = build_sim_system(cfg, realtime=False, seed=0)
    if clock is not None:
        smu._clock = clock
    events = []
    smu._on_event = lambda lvl, msg: events.append((lvl, msg))
    smu.events = events
    smu.start(poll=False)
    return smu, sim


def acquire_now(smu, clock=None):
    """Trigger, then feed readings until the sample is latched."""
    n = smu.acquire()
    for _ in range(2000):
        if clock is not None:
            clock.t += 0.05
        else:
            time.sleep(0.001)          # let the (real-clock) settle time pass
        smu.poll_once()
        st = smu.status()
        if st.acq_id == n and not st.acquiring:
            return st.sample
    raise AssertionError("acquisition never finished")


# ---- safety ---------------------------------------------------------------

def test_output_off_at_start():
    smu, sim = make()
    assert smu.status().output is False
    assert sim.get_output() is False
    smu.shutdown()


def test_shutdown_turns_output_off():
    smu, sim = make()
    smu.set_output(True)
    assert sim.get_output() is True
    smu.shutdown()
    assert sim.get_output() is False
    assert smu.status().connected is False
    smu.shutdown()                          # twice is harmless


def test_shutdown_keep_outputs_writes_nothing():
    # a restart for a code update: disconnect, but not a single write
    smu, sim = make()
    smu.set_output(True)
    n = len(sim.writes)
    smu.shutdown(keep_outputs=True)
    assert sim.writes[n:] == []
    assert sim.get_output() is True and sim._open is False
    assert smu.status().connected is False


def test_compliance_is_written_before_the_level_and_before_output_on():
    smu, sim = make()
    calls = []

    def spy(name):
        orig = getattr(sim, name)

        def wrapped(*a):
            calls.append(name)
            return orig(*a)
        setattr(sim, name, wrapped)
    for name in ("set_limit", "set_level", "set_output"):
        spy(name)
    smu.set_output(True)
    assert calls[:3] == ["set_limit", "set_level", "set_output"]
    smu.shutdown()


def test_function_change_switches_output_off_first():
    smu, sim = make()
    smu.set_output(True)
    smu.set_source_function("current")
    assert sim.get_output() is False
    assert smu.status().output is False
    assert smu.status().source_function == "current"
    assert any("OFF before" in m for _, m in smu.events)
    smu.shutdown()


def test_bad_function_refused():
    smu, _ = make()
    with pytest.raises(ValueError):
        smu.set_source_function("resistance")
    smu.shutdown()


def test_nan_refused():
    smu, _ = make()
    with pytest.raises(ValueError):
        smu.set_voltage(float("nan"))
    smu.shutdown()


# ---- clamps and the output boxes -------------------------------------------

def test_voltage_clamped_to_envelope():
    smu, _ = make(limits__voltage_max_V=10.0)
    smu.set_voltage(50.0)
    assert smu.status().source_voltage_set_V == 10.0
    assert any(lvl == "warn" and "clamped" in m for lvl, m in smu.events)
    smu.set_voltage(-50.0)
    assert smu.status().source_voltage_set_V == -10.0
    smu.shutdown()


def test_high_current_limit_confines_voltage_to_21V():
    smu, _ = make()
    smu.set_current_limit(0.5)            # above the 105 mA box
    smu.set_voltage(100.0)
    assert smu.status().source_voltage_set_V == 21.0
    assert smu.status().level_max == 21.0
    smu.shutdown()


def test_high_voltage_confines_current_limit_to_105mA():
    smu, _ = make()
    smu.set_current_limit(1e-3)
    smu.set_voltage(100.0)
    smu.set_current_limit(1.0)
    assert smu.status().current_limit_A == pytest.approx(0.105)
    smu.shutdown()


def test_current_source_boxes():
    smu, _ = make()
    smu.set_source_function("current")
    smu.set_voltage_limit(100.0)           # above 21 V ...
    smu.set_current(0.5)                   # ... so |I| <= 105 mA
    assert smu.status().source_current_set_A == pytest.approx(0.105)
    smu.shutdown()


def test_fixed_source_range_caps_level():
    smu, _ = make()
    smu.set_voltage(10.0)
    smu.set_source_range(1.5)              # snaps UP to the 2 V range
    st = smu.status()
    assert st.source_auto_range is False
    assert smu.cfg.source.range_V == 2.0
    assert st.source_voltage_set_V == pytest.approx(2.1)   # 105 % of 2 V
    smu.shutdown()


def test_snap_range():
    assert snap_range("voltage", 1.5) == 2.0
    assert snap_range("voltage", 2.0) == 2.0
    assert snap_range("current", 3e-6) == 1e-5
    assert snap_range("current", 5.0) == 1.0


def test_nplc_clamped():
    smu, _ = make()
    smu.set_nplc(100)
    assert smu.status().nplc == smu.cfg.limits.nplc_max
    smu.shutdown()


def test_inactive_function_level_is_stored_not_sent():
    smu, sim = make()
    smu.set_current(1e-3)                  # sourcing voltage: current is just stored
    assert smu.status().source_current_set_A == 1e-3
    assert sim._level["current"] == 0.0
    assert any("stored" in m for _, m in smu.events)
    smu.shutdown()


def test_apply_config_reclamps():
    smu, _ = make()
    smu.set_voltage(20.0)
    smu.cfg.limits.voltage_max_V = 5.0
    smu.apply_config()
    assert smu.status().source_voltage_set_V == 5.0
    smu.shutdown()


# ---- physics of the simulator -------------------------------------------------

def test_ohms_law_and_leads_2wire_vs_4wire():
    smu, _ = make()
    smu.set_current_limit(0.01)
    smu.set_voltage(1.0)
    smu.set_output(True)
    s2 = acquire_now(smu)
    # 2-wire: 1000 ohm sample + 0.5 ohm leads
    assert s2["resistance_ohm"] == pytest.approx(1000.5, rel=1e-6)
    smu.set_four_wire(True)
    s4 = acquire_now(smu)
    assert s4["resistance_ohm"] == pytest.approx(1000.0, rel=1e-6)
    assert s4["four_wire"] is True
    smu.shutdown()


def test_compliance_in_voltage_mode():
    smu, _ = make()
    smu.set_current_limit(1e-3)
    smu.set_voltage(5.0)                   # 5 mA wanted, 1 mA allowed
    smu.set_output(True)
    s = acquire_now(smu)
    assert s["tripped"] is True
    assert s["current_A"] == pytest.approx(1e-3)
    assert s["voltage_V"] == pytest.approx(1.0005, rel=1e-4)   # readback, not setpoint
    assert "compliance" in s["flag"]
    assert smu.status().tripped is True
    smu.shutdown()


def test_compliance_in_current_mode():
    smu, _ = make()
    smu.set_source_function("current")
    smu.set_voltage_limit(2.0)
    smu.set_current(0.01)                  # 10 V needed on 1 kohm, 2 V allowed
    smu.set_output(True)
    s = acquire_now(smu)
    assert s["tripped"] is True
    assert s["voltage_V"] == pytest.approx(2.0)
    assert s["current_A"] == pytest.approx(2.0 / 1000.5, rel=1e-4)
    smu.shutdown()


def test_diode_is_rectifying():
    smu, _ = make(sim__load="diode")
    smu.set_current_limit(0.1)
    smu.set_output(True)
    smu.set_voltage(0.8)
    fwd = acquire_now(smu)["current_A"]
    smu.set_voltage(-0.8)
    rev = acquire_now(smu)["current_A"]
    assert fwd > 1e-3
    assert abs(rev) < 1e-8
    smu.shutdown()


def test_fixed_measure_range_overflows():
    smu, _ = make()
    smu.set_current_limit(0.01)
    smu.set_voltage(1.0)                   # 1 mA
    smu.set_measure_range(1e-5)            # 10 uA range
    smu.set_output(True)
    s = acquire_now(smu)
    assert math.isnan(s["current_A"])
    assert "overflow" in s["flag"]
    smu.shutdown()


def test_noise_falls_with_nplc():
    def spread(nplc):
        smu, _ = make(sim__noise_ppm=1000.0)
        smu.set_nplc(nplc)
        smu.set_current_limit(0.01)
        smu.set_voltage(1.0)
        smu.set_acquisition(200)
        smu.set_output(True)
        s = acquire_now(smu)
        smu.shutdown()
        return s["current_std_A"]
    assert spread(0.1) > 5 * spread(10.0)


# ---- settling and the acquisition ------------------------------------------------

def test_new_level_is_unsettled_until_settle_time():
    clk = FakeClock()
    smu, _ = make(clock=clk)
    smu.set_output(True)
    clk.t += 1.0
    assert smu.status().settled is True
    smu.set_voltage(0.5)
    st = smu.status()
    assert st.source_voltage_set_V == 0.5 and st.settled is False
    clk.t += smu.cfg.source.settle_s + 1e-3
    assert smu.status().settled is True
    smu.shutdown()


def test_acquire_refused_with_output_off():
    smu, _ = make()
    with pytest.raises(ValueError, match="OFF"):
        smu.acquire()
    smu.shutdown()


def test_acquire_ignores_readings_before_trigger_and_during_settling():
    clk = FakeClock()
    smu, _ = make(clock=clk)
    smu.set_current_limit(0.01)
    smu.set_output(True)
    smu.set_voltage(1.0)
    n = smu.acquire()
    smu.poll_once()                        # still settling: must not count
    assert smu.status().acquiring is True
    assert smu.status().acq_progress == 0.0
    for _ in range(100):                   # time passes, readings arrive
        clk.t += 0.05
        smu.poll_once()
        if not smu.status().acquiring:
            break
    s = smu.status().sample
    assert s["acq_id"] == n
    assert s["n"] == smu.cfg.acquisition.readings
    assert s["current_A"] == pytest.approx(1.0 / 1000.5)
    smu.shutdown()


def test_ids_increase_and_sample_matches_id():
    smu, _ = make()
    smu.set_output(True)
    a = acquire_now(smu)
    b = acquire_now(smu)
    assert b["acq_id"] == a["acq_id"] + 1
    smu.shutdown()


def test_output_off_aborts_and_latches_an_aborted_sample():
    """A scan waiting for this id must not read the PREVIOUS sample (gotcha #28)."""
    smu, _ = make()
    smu.set_output(True)
    acquire_now(smu)
    n = smu.acquire()
    smu.output_off()
    st = smu.status()
    assert st.acquiring is False
    assert st.sample["acq_id"] == n and st.sample["aborted"] is True
    assert math.isnan(st.sample["current_A"])
    assert math.isnan(st.voltage_V)          # no output, no live reading
    smu.shutdown()


def test_status_never_touches_hardware():
    smu, sim = make()
    smu.set_output(True)

    def boom(*a):
        raise AssertionError("status() read the hardware")
    for name in ("measure", "get_source_range", "get_measure_range",
                 "get_output", "idn"):
        setattr(sim, name, boom)
    smu.status()


def test_hardware_error_is_reported_not_fatal():
    smu, sim = make()
    smu.set_output(True)

    def broken():
        raise OSError("VI_ERROR_TMO")
    sim.measure = broken
    smu.poll_once()
    assert "VI_ERROR_TMO" in smu.status().hw_error
    assert any(lvl == "error" for lvl, _ in smu.events)
    smu.shutdown()


def test_poll_thread_runs_in_real_time():
    cfg = Config()
    cfg.measure.nplc = 0.1
    smu, _ = build_sim_system(cfg, seed=0)
    smu.start()
    try:
        smu.set_output(True)
        t0 = time.monotonic()
        while smu.status().readings < 3 and time.monotonic() - t0 < 5:
            time.sleep(0.01)
        assert smu.status().readings >= 3
    finally:
        smu.shutdown()


# ---- review fixes (2026-09-27) ----------------------------------------------------

def test_set_config_cannot_change_function_with_output_on():
    """A set_config / loaded .ini is not a back door around 'output OFF before a
    function change'."""
    smu, sim = make()
    smu.set_output(True)
    smu.cfg.source.function = "current"      # what apply_config_dict does
    smu.apply_config()
    assert smu.status().output is False
    assert sim.get_output() is False
    assert any(lvl == "warn" and "source function" in msg for lvl, msg in smu.events)
    smu.shutdown()


def test_user_envelope_never_wider_than_the_instrument():
    smu, _ = make()
    smu.cfg.limits.voltage_max_V = 500.0
    smu.cfg.limits.current_max_A = 3.0
    smu.cfg.limits.nplc_max = 100.0
    smu.apply_config()
    lim = smu.cfg.limits
    assert lim.voltage_max_V == 210.0 and lim.current_max_A == 1.05
    assert lim.nplc_max == 10.0
    smu.set_current_limit(1e-3)
    smu.set_voltage(400.0)
    assert smu.status().source_voltage_set_V == 210.0
    smu.shutdown()


def test_limit_and_sense_changes_restart_settling():
    """In compliance the limit IS the operating point; 4-wire moves the sense
    point. Neither may be read as settled at once."""
    clk = FakeClock()
    smu, _ = make(clock=clk)
    smu.set_output(True)
    clk.t += 1.0
    assert smu.status().settled
    smu.set_current_limit(2e-3)
    assert smu.status().settled is False
    clk.t += 1.0
    smu.set_four_wire(True)
    assert smu.status().settled is False
    clk.t += 1.0
    smu.set_source_range(20.0)
    assert smu.status().settled is False
    smu.shutdown()
