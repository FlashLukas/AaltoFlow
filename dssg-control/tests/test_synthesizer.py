"""The brain against the simulated SG12000L: clamping to BOTH envelopes,
read-back through the poll thread, lifecycle and RF safety."""

import time

import pytest

from dssg.config import Config
from dssg.sim_system import build_sim_system


def wait_for(synth, pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s = synth.status()
        if pred(s):
            return s
        time.sleep(0.01)
    raise AssertionError(f"timed out; last status {synth.status()}")


@pytest.fixture
def synth():
    cfg = Config()
    cfg.hardware.poll_hz = 20.0
    s, backend = build_sim_system(cfg)
    events = []
    s._on_event = lambda lvl, msg: events.append((lvl, msg))
    s.events, s.sim = events, backend
    s.start()
    yield s
    s.shutdown()


def test_start_leaves_rf_off_and_connected(synth):
    s = synth.status()
    assert s.connected is True
    assert s.rf_on is False
    assert synth.sim.read_output() is False
    assert "SG12000L" in s.idn


def test_rf_forced_off_even_if_the_box_was_left_on():
    cfg = Config()
    s, backend = build_sim_system(cfg)
    backend._output = True                  # someone left it on from the front panel
    s.start()
    try:
        assert backend.read_output() is False
    finally:
        s.shutdown()


def test_set_and_read_back(synth):
    synth.set_frequency(2.0e9)
    synth.set_power(-7.0)
    synth.set_phase(90.0)
    synth.set_reference("internal")
    synth.set_rf(True)
    s = wait_for(synth, lambda s: s.rf_on and s.frequency_Hz == 2.0e9
                 and s.reference == "internal")
    assert s.power_dBm == -7.0
    assert s.phase_deg == 90.0


def test_power_readback_is_quantised_to_the_attenuator_step(synth):
    synth.set_power(-7.3)
    s = wait_for(synth, lambda s: s.power_dBm != synth.cfg.signal.power_dBm)
    assert s.power_dBm == -7.5              # 0.5 dB step attenuator


def test_power_clamped_to_config_ceiling(synth):
    synth.set_power(1000.0)
    assert synth._power == synth.cfg.limits.power_max_dBm
    assert any("clamped" in m for lvl, m in synth.events if lvl == "warn")


def test_power_floor_is_the_units_own_minimum(synth):
    """cfg says -40 dBm, the unit says -21.5: the narrower one wins."""
    synth.set_power(-1000.0)
    lim = synth.limits()
    assert lim["power_min_dBm"] == synth.cfg.sim.power_min_dBm
    assert synth._power == lim["power_min_dBm"]


def test_frequency_clamped_to_the_units_range(synth):
    synth.set_frequency(40e9)
    assert synth._freq == synth.cfg.sim.freq_max_Hz        # 12 GHz, not cfg's 13
    synth.set_frequency(1.0)
    assert synth._freq == synth.cfg.limits.freq_min_Hz


def test_phase_clamped(synth):
    synth.set_phase(1000.0)
    assert synth._phase == synth.cfg.limits.phase_max_deg


def test_bad_reference_refused(synth):
    with pytest.raises(ValueError):
        synth.set_reference("gps")


def test_phase_refused_on_a_unit_without_it():
    cfg = Config()
    cfg.sim.has_phase = False
    s, _ = build_sim_system(cfg)
    s.start()
    try:
        assert s.has_phase() is False
        with pytest.raises(ValueError):
            s.set_phase(10.0)
        assert s.status().has_phase is False
    finally:
        s.shutdown()


def test_status_never_touches_hardware(synth):
    """status() returns the snapshot: with the backend broken it still answers."""
    synth._stop.set()                       # stop the poller so only status() runs
    time.sleep(0.1)

    def boom(*a, **k):
        raise AssertionError("status() called the hardware")
    synth.sim.read_frequency = boom
    for _ in range(5):
        synth.status()


def test_readback_failure_is_reported_not_fatal(synth):
    def broken():
        raise IOError("USB unplugged")
    synth.sim.read_power = broken
    s = wait_for(synth, lambda s: s.hw_error != "")
    assert "USB unplugged" in s.hw_error
    assert s.connected is True
    assert any(lvl == "error" for lvl, _ in synth.events)


def test_setters_do_not_write_the_snapshot(synth):
    """gotcha #1: the snapshot is rebuilt by the poller, never edited in place."""
    synth._stop.set()
    time.sleep(0.1)                         # freeze the poller
    before = synth.status()
    synth.set_frequency(3e9)
    assert synth.status() is before
    assert before.frequency_Hz != 3e9


def test_shutdown_turns_rf_off_and_is_idempotent():
    cfg = Config()
    s, backend = build_sim_system(cfg)
    s.start()
    s.set_rf(True)
    assert backend.read_output() is True
    s.shutdown()
    assert backend.read_output() is False
    assert s.status().connected is False
    s.shutdown()                            # a second call must not raise


def test_apply_config_reclamps_to_new_limits(synth):
    synth.set_power(4.0)
    synth.cfg.limits.power_max_dBm = 0.0
    synth.apply_config()
    assert synth._power == 0.0
    s = wait_for(synth, lambda s: s.power_dBm == 0.0 and s.power_max_dBm == 0.0)
    assert s.power_max_dBm == 0.0


def test_failed_start_closes_the_port_again():
    """open() succeeded, then a query failed: the brain must close the backend
    (RF off) instead of leaving the COM port held by a dying process."""
    cfg = Config()
    s, backend = build_sim_system(cfg)

    def broken():
        raise TimeoutError("no reply to FREQ:CW")
    backend.set_frequency = lambda hz: broken()
    with pytest.raises(TimeoutError):
        s.start()
    assert backend._open is False
    assert backend.read_output() is False
    assert s.status().connected is False
