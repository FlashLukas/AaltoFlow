"""SignalSource against the simulated 8648D: set/read-back, clamps (including
the frequency-dependent ceiling), write order, lifecycle, safety, RPP, threads."""

import time

import pytest

from hp8648 import spec
from hp8648.backends.base import SigGenBackend
from hp8648.config import Config
from hp8648.sim_system import build_sim_system


def _wait(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def rig():
    cfg = Config()
    cfg.hardware.poll_s = 0.02
    cfg.hardware.switch_settle_s = 0.0
    src, sim = build_sim_system(cfg)
    events = []
    src._on_event = lambda lvl, msg: events.append((lvl, msg))
    src.start()
    yield src, sim, events
    src.shutdown()


def test_sim_implements_the_interface():
    _, sim = build_sim_system(Config())
    assert isinstance(sim, SigGenBackend)


#: A box someone left running: RF ON, not the default frequency / level, AM on.
BUSY_BOX = {"rf_on": True, "frequency_Hz": 2.2e9, "power_dBm": 3.0,
            "modulation": {"am": True}}

#: Backend methods that CHANGE the instrument. None may run during start().
_WRITES = ("set_output", "set_power", "set_frequency")


def _forbid_writes(sim):
    """Make every state-changing backend call fail loudly, and log it."""
    log = []
    for name in _WRITES:
        def boom(*a, _n=name):
            log.append(_n)
            raise AssertionError(f"start() wrote to the instrument: {_n}{a}")
        setattr(sim, name, boom)
    return log


def test_start_adopts_the_instruments_state_and_writes_nothing():
    """Lukas, 2026-09-27: read the instrument on startup, change nothing."""
    cfg = Config()
    cfg.hardware.poll_s = 0.02
    src, sim = build_sim_system(cfg, initial=BUSY_BOX)
    writes = _forbid_writes(sim)
    events = []
    src._on_event = lambda lvl, msg: events.append((lvl, msg))
    src.start()
    try:
        time.sleep(0.15)                 # several worker cycles
        assert writes == []
        s = src.status()
        assert s.connected is True
        # read back AND desired both equal what the box was doing
        assert (s.rf_on, s.frequency_Hz, s.power_dBm) == (True, 2.2e9, 3.0)
        assert (s.rf_set, s.frequency_set_Hz, s.power_set_dBm) == (True, 2.2e9, 3.0)
        assert s.modulation_off is False and s.modulation["am"] is True
        # the box is untouched
        assert sim.read_output() is True and sim.read_modulation()["am"] is True
        warns = [m for lvl, m in events if lvl == "warn"]
        assert any("already ON" in m for m in warns)
        assert sum("modulation" in m for m in warns) == 1     # reported once
        assert any("adopted" in m for _, m in events)
    finally:
        # shutdown behaviour is NOT part of the rule: RF off on the way out
        for name in _WRITES:
            delattr(sim, name)
        src.shutdown()
    assert sim.read_output() is False


def test_config_signal_is_not_pushed_at_start():
    cfg = Config()
    cfg.signal.frequency_Hz = 1.5e9
    cfg.signal.power_dBm = -20.0
    src, sim = build_sim_system(cfg, initial=BUSY_BOX)
    src.start()
    try:
        assert src.wait_idle()
        s = src.status()
        assert (s.frequency_Hz, s.power_dBm) == (2.2e9, 3.0)
    finally:
        src.shutdown()


def test_adopted_state_outside_the_envelope_is_left_and_reported():
    """Limits guard what WE command; a box found outside them is reported,
    not moved. The next explicit setpoint is clamped as usual."""
    cfg = Config()
    cfg.limits.power_max_dBm = 0.0
    src, sim = build_sim_system(cfg, initial=BUSY_BOX)       # +3 dBm
    events = []
    src._on_event = lambda lvl, msg: events.append((lvl, msg))
    src.start()
    try:
        assert src.wait_idle()
        assert src.status().power_dBm == 3.0 and sim.read_power() == 3.0
        assert any("outside your envelope" in m for lvl, m in events if lvl == "warn")
        src.set_power(5.0)
        assert src.wait_idle()
        assert src.status().power_dBm == 0.0
    finally:
        src.shutdown()


def test_rpp_already_tripped_at_connect_is_adopted():
    src, sim = build_sim_system(Config(), initial={"rpp": True, "rf_on": False})
    writes = _forbid_writes(sim)
    events = []
    src._on_event = lambda lvl, msg: events.append((lvl, msg))
    src.start()
    try:
        s = src.status()
        assert s.rpp_tripped is True and s.rf_set is False and writes == []
        assert sum("REVERSE POWER" in m for lvl, m in events if lvl == "error") == 1
    finally:
        for name in _WRITES:
            delattr(sim, name)
        src.shutdown()


def test_unreadable_at_connect_writes_nothing_and_adopts_later():
    cfg = Config()
    cfg.hardware.poll_s = 0.02
    src, sim = build_sim_system(cfg, initial=BUSY_BOX)
    writes = _forbid_writes(sim)
    orig = sim.read_frequency
    sim.read_frequency = lambda: (_ for _ in ()).throw(IOError("bus asleep"))
    src.start()
    try:
        assert src.status().hw_error
        sim.read_frequency = orig
        assert _wait(lambda: src.status().frequency_set_Hz == 2.2e9
                     and src.status().rf_set is True and not src.status().hw_error)
        assert writes == []
    finally:
        for name in _WRITES:
            delattr(sim, name)
        src.shutdown()


def test_settings_ok_without_changes_writes_nothing(rig):
    src, sim, _ = rig
    src.set_frequency(1.7e9)
    assert src.wait_idle()
    writes = _forbid_writes(sim)
    try:
        src.apply_config()                 # same config: nothing to do
        time.sleep(0.1)
        assert writes == []
    finally:
        for name in _WRITES:
            delattr(sim, name)


def test_changed_signal_default_is_applied_on_request(rig):
    src, _, _ = rig
    src.cfg.signal.frequency_Hz = 3.3e9
    src.cfg.signal.power_dBm = -11.0
    src.apply_config()
    assert src.wait_idle()
    s = src.status()
    assert (s.frequency_Hz, s.power_dBm) == (3.3e9, -11.0)


def test_set_and_read_back(rig):
    src, _, _ = rig
    src.set_frequency(2.0e9)
    src.set_power(-7.0)
    src.set_rf(True)
    assert src.wait_idle()
    s = src.status()
    assert (s.frequency_Hz, s.power_dBm, s.rf_on) == (2.0e9, -7.0, True)


def test_readback_is_quantized_to_the_instrument_resolution(rig):
    src, _, _ = rig
    src.set_frequency(1_234_567_894.0)
    src.set_power(-12.34)
    assert src.wait_idle()
    s = src.status()
    assert s.frequency_Hz == 1_234_567_890.0          # 10 Hz
    assert s.power_dBm == pytest.approx(-12.3)        # 0.1 dB
    assert s.frequency_set_Hz == 1_234_567_894.0      # the request is kept too


def test_status_never_touches_hardware(rig):
    """status() returns the worker's snapshot; a dead bus must not block it."""
    src, sim, _ = rig
    def boom(*a):
        raise RuntimeError("GPIB timeout")
    sim.read_output = boom
    t0 = time.monotonic()
    for _ in range(100):
        src.status()
    assert time.monotonic() - t0 < 0.1
    assert _wait(lambda: src.status().hw_error != "")


def test_setters_do_not_touch_the_snapshot(rig):
    """gotcha #1: the setter changes the brain, the worker publishes."""
    src, _, _ = rig
    src._stop.set()                 # freeze the worker
    src._thread.join(1.0)
    before = src.status()
    src.set_power(-20.0)
    assert src.status() is before


def test_power_clamped_high_and_low(rig):
    src, _, events = rig
    src.set_power(1000.0)
    assert src.wait_idle()
    assert src.status().power_dBm == src.power_ceiling()
    assert any("clamped" in m for lvl, m in events if lvl == "warn")
    src.set_power(-1000.0)
    assert src.wait_idle()
    assert src.status().power_dBm == -136.0


def test_frequency_clamped(rig):
    src, _, _ = rig
    src.set_frequency(1e15)
    assert src.wait_idle()
    assert src.status().frequency_Hz == 4e9
    src.set_frequency(0.0)
    assert src.wait_idle()
    assert src.status().frequency_Hz == 9e3


def test_ceiling_follows_frequency(rig):
    src, _, _ = rig
    src.set_frequency(2.0e9)
    assert src.power_ceiling() == 13.0
    src.set_frequency(3.0e9)
    assert src.power_ceiling() == 10.0


def test_moving_above_2500_MHz_lowers_a_too_high_level(rig):
    src, sim, events = rig
    src.set_frequency(2.0e9)
    src.set_power(12.0)
    assert src.wait_idle()
    assert src.status().power_dBm == 12.0
    # record every write the sim receives, to check the ORDER
    log = []
    orig_p, orig_f = sim.set_power, sim.set_frequency
    sim.set_power = lambda v: (log.append(("p", v)), orig_p(v))
    sim.set_frequency = lambda v: (log.append(("f", v)), orig_f(v))
    src.set_frequency(3.0e9)
    assert src.wait_idle()
    s = src.status()
    assert s.power_dBm == 10.0 and s.frequency_Hz == 3.0e9
    assert s.level_unspecified is False
    # the level came DOWN before the frequency moved into the lower band
    assert log.index(("p", 10.0)) < log.index(("f", 3.0e9))
    assert any("lowered" in m for _, m in events)


def test_raising_level_is_written_after_the_frequency(rig):
    src, sim, _ = rig
    src.set_frequency(3.0e9)
    src.set_power(0.0)
    assert src.wait_idle()
    log = []
    orig_p, orig_f = sim.set_power, sim.set_frequency
    sim.set_power = lambda v: (log.append("p"), orig_p(v))
    sim.set_frequency = lambda v: (log.append("f"), orig_f(v))
    with src._lock:                 # queue both in one worker cycle
        src._dirty.clear()
    src._stop.set(); src._thread.join(1.0)
    src.set_frequency(2.0e9)
    src.set_power(12.0)
    src._cycle()
    assert log == ["f", "p"]


def test_option_1ea_raises_the_ceiling(rig):
    src, _, _ = rig
    src.set_frequency(500e6)
    assert src.power_ceiling() == 13.0          # envelope power_max_dBm
    src.cfg.limits.power_max_dBm = 25.0
    src.cfg.hardware.option_1ea = True
    assert src.power_ceiling() == 20.0          # 1EA: +20 dBm up to 1000 MHz
    src.set_frequency(2.2e9)
    assert src.power_ceiling() == 15.0          # 1EA: +15 dBm in 2100..2500 MHz
    src.cfg.limits.enforce_spec_ceiling = False
    assert src.power_ceiling() == 25.0


def test_unspecified_level_flag_without_spec_enforcement(rig):
    src, _, _ = rig
    src.cfg.limits.enforce_spec_ceiling = False
    src.set_frequency(3.0e9)
    src.set_power(12.0)
    assert src.wait_idle()
    assert src.status().level_unspecified is True


def test_rf_on_is_written_last_and_rf_off_first(rig):
    src, sim, _ = rig
    log = []
    for name in ("set_output", "set_power", "set_frequency"):
        orig = getattr(sim, name)
        setattr(sim, name, (lambda n, o: lambda v: (log.append(n), o(v)))(name, orig))
    src._stop.set(); src._thread.join(1.0)
    src.set_rf(True); src.set_frequency(1.1e9); src.set_power(-5.0)
    src._cycle()
    assert log[-1] == "set_output"
    log.clear()
    src.set_power(-50.0); src.set_rf(False)
    src._cycle()
    assert log[0] == "set_output"


def test_reverse_power_trip_is_followed_not_fought(rig):
    src, sim, events = rig
    src.set_rf(True)
    assert src.wait_idle()
    assert sim.read_output() is True
    sim.inject_reverse_power()
    assert _wait(lambda: src.status().rpp_tripped)
    s = src.status()
    assert s.rf_on is False and s.rf_set is False       # desired followed the box
    assert any(lvl == "error" and "REVERSE POWER" in m for lvl, m in events)
    time.sleep(0.1)
    assert sim.read_output() is False                   # nobody switched it back on
    # re-arm: an explicit RF on clears the protection
    src.set_rf(True)
    assert _wait(lambda: src.status().rf_on and not src.status().rpp_tripped)
    assert any("re-arms" in m for _, m in events)


def test_shutdown_turns_rf_off_and_is_idempotent():
    src, sim = build_sim_system(Config())
    src.start()
    src.set_rf(True)
    assert src.wait_idle()
    assert sim.read_output() is True
    src.shutdown()
    assert sim.read_output() is False
    assert src.status().connected is False
    assert src.status().rf_on is False
    src.shutdown()                      # a second call must not raise


def test_shutdown_keep_outputs_leaves_rf_as_it_is():
    src, sim = build_sim_system(Config())
    src.start()
    src.set_rf(True)
    assert src.wait_idle()
    calls = []
    sim.set_output = lambda on: calls.append(on)     # spy: any RF switch
    src.shutdown(keep_outputs=True)
    assert calls == [] and sim.read_output() is True
    assert sim._open is False and src.status().connected is False


def test_apply_config_reclamps_to_new_limits(rig):
    src, _, _ = rig
    src.set_frequency(1e9)
    src.set_power(12.0)
    assert src.wait_idle()
    src.cfg.limits.power_max_dBm = 0.0
    src.apply_config()
    assert src.wait_idle()
    assert src.status().power_dBm == 0.0


def test_hardware_error_is_reported_once_and_recovers(rig):
    src, sim, events = rig
    orig = sim.read_frequency
    sim.read_frequency = lambda: (_ for _ in ()).throw(IOError("bus error"))
    assert _wait(lambda: src.status().hw_error)
    time.sleep(0.1)
    assert sum("hardware read failed" in m for _, m in events) == 1
    sim.read_frequency = orig
    assert _wait(lambda: src.status().hw_error == "")


def test_spec_table():
    assert spec.spec_max_dBm(1e6) == 13.0
    assert spec.spec_max_dBm(2.5e9) == 13.0
    assert spec.spec_max_dBm(2.6e9) == 10.0
    assert spec.spec_max_dBm(50e3, option_1ea=True) == 17.0
    assert spec.spec_max_dBm(1.2e9, option_1ea=True) == 19.0
    assert spec.spec_max_dBm(3.9e9, option_1ea=True) == 13.0
    # the GUI draws its ceiling from these edges: they must cover the range
    for opt in (False, True):
        e = spec.band_edges(opt)
        assert e[0] == spec.FREQ_MIN_HZ and e[-1] == spec.FREQ_MAX_HZ
        assert e == sorted(e)
    assert spec.switching_time_s(500e6) < spec.switching_time_s(2e9)


def test_a_failed_write_is_retried(rig):
    """A GPIB write that raises must not be forgotten: the worker puts the
    change back and the next cycle delivers it (reviewer fix)."""
    src, sim, events = rig
    orig = sim.set_frequency
    calls = {"n": 0}

    def flaky(hz):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("VI_ERROR_TMO")
        orig(hz)

    sim.set_frequency = flaky
    src.set_frequency(1.234e9)
    assert _wait(lambda: src.status().frequency_Hz == 1.234e9)
    assert calls["n"] >= 2
    assert src.status().hw_error == ""
    sim.set_frequency = orig


def test_envelope_never_wider_than_the_instrument():
    """An .ini that asks for 6 GHz / -150 dBm must not be advertised: the
    8648D stops at 4 GHz and -136 dBm."""
    cfg = Config()
    cfg.limits.freq_max_Hz = 6e9
    cfg.limits.power_min_dBm = -150.0
    src, _ = build_sim_system(cfg)
    assert src.freq_limits() == (spec.FREQ_MIN_HZ, spec.FREQ_MAX_HZ)
    assert src.power_floor() == spec.POWER_MIN_DBM
    src.set_frequency(5e9)
    src.set_power(-149.0)
    assert src._freq == spec.FREQ_MAX_HZ and src._power == spec.POWER_MIN_DBM
