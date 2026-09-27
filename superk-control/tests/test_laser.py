"""The SuperK brain against the simulated laser: class 4 safety, clamps,
crystal switching, lifecycle, and the threading rule (status never touches
hardware)."""

import time

import pytest

from superk.config import Config, N_LINES
from superk.laser import SafetyError
from superk.sim_system import build_sim_system


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def rig():
    cfg = Config()
    cfg.hardware.sim_warmup_s = 0.2
    cfg.hardware.poll_hz = 20.0
    laser, backend = build_sim_system(cfg)
    events = []
    laser._on_event = lambda lvl, msg: events.append((lvl, msg))
    laser.start()
    yield laser, backend, events
    laser.shutdown()


# ---- safety -------------------------------------------------------------------

def test_start_never_emits_and_rf_is_off(rig):
    laser, backend, _ = rig
    s = laser.status()
    assert s.connected
    assert s.emission_on is False and s.emission_set is False
    assert s.rf_on is False
    assert backend.read_emission() is False


def test_start_switches_off_emission_left_on():
    cfg = Config()
    laser, backend = build_sim_system(cfg)
    backend.open()
    backend.warmup_s = 0.0
    backend.set_emission(True)                 # someone used the front panel
    assert backend.read_emission()
    laser.start()
    try:
        assert backend.read_emission() is False
        assert laser.status().emission_set is False
    finally:
        laser.shutdown()


def test_emission_refused_with_open_interlock(rig):
    laser, backend, _ = rig
    backend.open_interlock()
    with pytest.raises(SafetyError):
        laser.set_emission(True)
    backend.close_interlock()                  # closed, but not yet reset
    with pytest.raises(SafetyError, match="needs reset"):
        laser.set_emission(True)
    laser.reset_interlock()
    laser.set_emission(True)                   # now allowed
    assert wait_for(lambda: laser.status().emission_on)


def test_emission_is_starting_then_on(rig):
    laser, _, _ = rig
    laser.set_emission(True)
    assert wait_for(lambda: laser.status().emission_state == "starting", 1.0)
    assert wait_for(lambda: laser.status().emission_state == "on")


def test_interlock_opening_clears_the_request(rig):
    laser, backend, events = rig
    laser.set_emission(True)
    assert wait_for(lambda: laser.status().emission_on)
    backend.open_interlock()
    assert wait_for(lambda: laser.status().emission_state == "interlock")
    assert laser.status().emission_set is False
    assert any("interlock" in m for lvl, m in events if lvl == "warn")
    # closing and resetting the interlock must NOT bring the beam back
    backend.close_interlock()
    laser.reset_interlock()
    time.sleep(0.5)
    assert laser.status().emission_on is False


def test_shutdown_switches_everything_off():
    cfg = Config()
    cfg.hardware.sim_warmup_s = 0.0
    laser, backend = build_sim_system(cfg)
    laser.start()
    laser.set_rf(True)
    laser.set_emission(True)
    assert backend.read_emission() and backend.read_rf()
    laser.shutdown()
    assert backend.read_emission() is False
    assert backend.read_rf() is False
    assert laser.status().connected is False
    laser.shutdown()                           # twice is safe


def test_emission_refused_when_not_connected():
    laser, _ = build_sim_system(Config())
    with pytest.raises(SafetyError, match="not connected"):
        laser.set_emission(True)


# ---- clamps ---------------------------------------------------------------------

def test_power_clamped_to_ceiling_with_warning(rig):
    laser, _, events = rig
    laser.set_power(95.0)
    assert wait_for(lambda: laser.status().power_pct == laser.cfg.limits.power_max_pct)
    assert any("clamped" in m for lvl, m in events if lvl == "warn")


def test_amplitude_clamped(rig):
    laser, _, _ = rig
    laser.cfg.limits.amplitude_max_pct = 70.0
    laser.set_amplitude(2, 150.0)
    assert wait_for(lambda: laser.status().amplitude_pct[1] == 70.0)


def test_wavelength_clamped_to_active_crystal(rig):
    laser, _, events = rig
    lo, hi = laser.wavelength_range()           # VIS-nIR 500..900
    laser.set_wavelength(1, 1500.0)
    assert wait_for(lambda: laser.status().wavelength_nm[0] == hi)
    laser.set_wavelength(1, 300.0)
    assert wait_for(lambda: laser.status().wavelength_nm[0] == lo)


def test_wavelength_quantised_to_picometres(rig):
    laser, _, _ = rig
    laser.set_wavelength(1, 632.81649)
    assert wait_for(lambda: abs(laser.status().wavelength_nm[0] - 632.816) < 1e-9)


def test_bad_line_is_an_error(rig):
    laser, _, _ = rig
    with pytest.raises(ValueError):
        laser.set_wavelength(0, 600)
    with pytest.raises(ValueError):
        laser.set_amplitude(N_LINES + 1, 10)


# ---- crystal switching ----------------------------------------------------------

def test_filter_switch_moves_range_and_clamps_lines(rig):
    laser, _, events = rig
    laser.set_line(1, 700.0, 60.0)
    laser.set_filter("IR")
    assert laser.wavelength_range() == (1100.0, 2000.0)
    assert wait_for(lambda: laser.status().wavelength_nm[0] == 1100.0)
    s = laser.status()
    assert s.filter == "IR" and (s.filter_min_nm, s.filter_max_nm) == (1100.0, 2000.0)
    assert any("outside" in m for lvl, m in events if lvl == "warn")


def test_filter_switch_restores_rf(rig):
    laser, backend, _ = rig
    laser.set_rf(True)
    laser.set_filter("nIR2")
    assert backend.read_rf() is True
    assert backend.read_crystal() == 2


def test_unknown_filter_refused(rig):
    laser, _, _ = rig
    with pytest.raises(ValueError, match="unknown filter"):
        laser.set_filter("UV")


def test_driver_reported_range_wins():
    """On the real laser the RF driver knows its crystal's range: it overrides
    the config (here the sim is told to report a different one)."""
    cfg = Config()
    laser, backend = build_sim_system(cfg)
    backend._report_range = True
    backend._ranges = {1: (510.0, 880.0), 2: (820.0, 1390.0)}
    laser.start()
    try:
        assert laser.wavelength_range() == (510.0, 880.0)
    finally:
        laser.shutdown()


# ---- threads / status -------------------------------------------------------------

def test_status_never_touches_hardware(rig):
    laser, backend, _ = rig

    def boom(*a):
        raise AssertionError("status() read the hardware")
    laser._stop.set()                           # park the worker
    laser._thread.join(2)
    for name in ("read_power", "read_emission", "read_interlock", "read_wavelength"):
        setattr(backend, name, boom)
    for _ in range(50):
        laser.status()


def test_hardware_error_is_reported_not_raised(rig):
    laser, backend, _ = rig

    def fail(*a):
        raise OSError("port gone")
    backend.read_power = fail
    assert wait_for(lambda: "port gone" in laser.status().hw_error)
    assert laser.status().emission_state == "error"


def test_apply_config_reclamps(rig):
    laser, _, _ = rig
    laser.set_power(40.0)
    laser.cfg.limits.power_max_pct = 20.0
    laser.apply_config()
    assert wait_for(lambda: laser.status().power_pct == 20.0)


def test_real_backend_imports_without_sdk(tmp_path):
    """The package must import on a PC without the NKT SDK; open() then fails clearly."""
    from superk.backends.nktp import NktpSuperK, NKTError
    b = NktpSuperK("COM99", dll_path=str(tmp_path / "missing.dll"))
    import os
    old = os.environ.pop("NKTP_SDK_PATH", None)
    try:
        with pytest.raises(NKTError, match="NKTPDLL"):
            b.open()
    finally:
        if old is not None:
            os.environ["NKTP_SDK_PATH"] = old
    b.close()                                   # safe when never opened


# ---- crystal switching: the hardware's rules (review 2026-09-27) ------------------

def test_switch_to_ir_with_rf_on_turns_rf_off_first(rig):
    """The SELECT's RF switch must not move under RF power (SDK manual 6.10).
    The sim raises if it does, so this passing proves the brain's order."""
    laser, backend, _ = rig
    laser.set_rf(True)
    laser.set_filter("IR")
    assert backend.read_crystal() == 4 and backend.read_rf() is True


def test_unreachable_crystal_keeps_old_one_and_leaves_rf_off(rig):
    """A crystal in the other housing with the cable not there: refused, the
    brain keeps the crystal it had, and the RF stays OFF (no line out of a
    crystal nobody meant)."""
    laser, backend, _ = rig
    del backend._ranges[4]                      # "the RF cable is not at SELECT2"
    laser.set_rf(True)
    with pytest.raises(Exception, match="not connected"):
        laser.set_filter("IR")
    assert laser.active_filter() == "VIS-nIR"
    assert backend.read_crystal() == 1
    assert backend.read_rf() is False
    assert wait_for(lambda: laser.status().rf_set is False)


def test_unreachable_startup_crystal_adopts_the_connected_one():
    cfg = Config()
    cfg.startup.filter = "IR"
    laser, backend = build_sim_system(cfg)
    del backend._ranges[4]
    events = []
    laser._on_event = lambda lvl, msg: events.append((lvl, msg))
    laser.start()                               # must not refuse to start
    try:
        assert laser.active_filter() == "VIS-nIR"
        assert any("start-up crystal" in m for lvl, m in events if lvl == "warn")
    finally:
        laser.shutdown()


def test_numeric_filter_is_an_index_and_bad_index_refused(rig):
    laser, _, _ = rig
    laser.set_filter("1")
    assert laser.active_filter() == "nIR2"
    with pytest.raises(ValueError, match="unknown filter"):
        laser.set_filter("7")                   # used to fall back to crystal 0
    assert laser.active_filter() == "nIR2"


def test_apply_config_does_not_blip_the_rf(rig):
    """A config change that does not change the crystal must not switch RF
    off and on (each blip is a dark gap in a running measurement)."""
    laser, backend, _ = rig
    laser.set_rf(True)
    calls = []
    orig = backend.set_rf
    backend.set_rf = lambda on: (calls.append(on), orig(on))
    laser.cfg.limits.power_max_pct = 30.0
    laser.apply_config()
    assert calls == []


def test_limits_themselves_are_sanitised(rig):
    laser, _, events = rig
    laser.cfg.limits.power_max_pct = 150.0
    laser.cfg.limits.amplitude_max_pct = -5.0
    laser.apply_config()
    assert laser.cfg.limits.power_max_pct == 100.0
    assert laser.cfg.limits.amplitude_max_pct == 0.0
    assert any("limits out of range" in m for lvl, m in events if lvl == "warn")


# ---- the real backend's register logic, against a fake register map ----------------

class _FakeBus:
    """Stands in for NKTPDLL: a dict of (address, register) -> value, plus the
    one piece of physics that matters here: the RF driver's read-only
    'connected crystal' follows the SELECT's RF switch."""

    def __init__(self, cable_at_select=1, cable_input=0):
        self.regs = {}
        self.cable_at = cable_at_select        # 0 = first housing, 1 = second
        self.cable_input = cable_input         # which RF input the cable is in
        self.writes = []

    def attach(self, b):
        from superk.backends import nktp
        b._dll = object()                      # "connected"
        b.select_addrs = [17, 18]

        def r(dev, reg):
            if dev == b.rf_addr and reg == nktp.REG_CONNECTED_XTAL:
                sw = self.regs.get((b.select_addrs[self.cable_at], nktp.REG_RF_SWITCH), 0)
                return 2 * self.cable_at + 1 + (self.cable_input ^ sw)
            return self.regs.get((dev, reg), 0)

        def w(dev, reg, v):
            self.writes.append((dev, reg, v))
            self.regs[(dev, reg)] = v
        for n in ("_r8", "_r16", "_rs16", "_r32"):
            setattr(b, n, r)
        for n in ("_w8", "_w16", "_w32"):
            setattr(b, n, w)


def test_real_backend_register_values():
    from superk.backends import nktp
    b = nktp.NktpSuperK("COM99")
    bus = _FakeBus()
    bus.attach(b)
    b.set_emission(True)
    b.set_power(12.34)
    b.set_wavelength(2, 650.1234)
    b.set_amplitude(7, 55.55)
    assert (15, 0x30, 3) in bus.writes                        # emission on = 3
    assert (15, 0x37, 123) in bus.writes                      # 0.1 % units
    assert (16, 0x92, 650123) in bus.writes                   # pm, channel 3
    assert (16, 0xB7, 556) in bus.writes                      # 0.1 %, channel 8
    assert b.read_wavelength(2) == 650.123


def test_real_backend_selects_crystal_with_the_select_rf_switch():
    from superk.backends import nktp
    b = nktp.NktpSuperK("COM99")
    bus = _FakeBus(cable_at_select=1, cable_input=1)          # cable in input 2 of SELECT2
    bus.attach(b)
    b.select_crystal(4)
    assert b.read_crystal() == 4
    assert all(dev == 18 and reg == nktp.REG_RF_SWITCH for dev, reg, _ in bus.writes)
    # never written: the read-only 'connected crystal' register
    assert not any(reg == nktp.REG_CONNECTED_XTAL for _, reg, _ in bus.writes)


def test_real_backend_refuses_switch_under_rf_and_names_the_cable():
    from superk.backends import nktp
    b = nktp.NktpSuperK("COM99")
    bus = _FakeBus(cable_at_select=0)
    bus.attach(b)
    bus.regs[(b.rf_addr, nktp.REG_RF_POWER)] = 1
    with pytest.raises(nktp.NKTError, match="RF power must be off"):
        b.select_crystal(2)
    bus.regs[(b.rf_addr, nktp.REG_RF_POWER)] = 0
    with pytest.raises(nktp.NKTError, match="move the RF cable to SuperK SELECT #2"):
        b.select_crystal(4)                                   # cable is at SELECT #1
