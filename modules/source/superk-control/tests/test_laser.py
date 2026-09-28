"""The SuperK brain against the simulated laser: class 4 safety, clamps,
crystal switching, lifecycle, and the threading rule (status never touches
hardware)."""

import time

import pytest

from superk import config as C
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


def test_emission_off_on_start_is_an_opt_in():
    """Default: an emitting laser is ADOPTED. The old behaviour (switch it off)
    is still there, but only when asked for."""
    cfg = Config()
    cfg.hardware.emission_off_on_start = True
    laser, backend = build_sim_system(cfg)
    backend.preset(emission=True)              # someone used the front panel
    laser.start()
    try:
        assert backend.read_emission() is False
        assert laser.status().emission_set is False
    finally:
        laser.shutdown()


# ---- adopt on start (Lukas, 2026-09-27: "read the state, change nothing") ------

#: a laser left in a state NOTHING in the config would produce: emitting, RF on,
#: 23.4 %, the nIR2 crystal, line 1 at 1234.5 nm / 42 %, line 4 at 1000 nm / 7 %
LEFT = dict(emission=True, rf=True, power_pct=23.4, crystal=2,
            wavelengths_nm=[1234.5, 900, 950, 1000, 1100, 1200, 1300, 1350],
            amplitudes_pct=[42, 0, 0, 7, 0, 0, 0, 0], watchdog_s=10)

# the only write a start may do: arm the laser's watchdog (safety, see laser.py)
ALLOWED_AT_START = {"set_watchdog"}


def _started(preset, **hw):
    cfg = Config()
    cfg.hardware.poll_hz = 20.0
    for k, v in hw.items():
        setattr(cfg.hardware, k, v)
    laser, backend = build_sim_system(cfg)
    backend.preset(**preset)
    backend.writes.clear()                     # the world before the service
    events = []
    laser._on_event = lambda lvl, msg: events.append((lvl, msg))
    laser.start()
    return laser, backend, events


def test_start_writes_nothing_to_the_laser():
    laser, backend, _ = _started(LEFT)
    try:
        assert backend.writes == []            # watchdog already right -> not even that
    finally:
        laser.shutdown()


def test_start_only_arms_the_watchdog_when_it_differs():
    laser, backend, events = _started({**LEFT, "watchdog_s": 0})
    try:
        assert backend.writes == [("set_watchdog", 10)]
        assert {w[0] for w in backend.writes} <= ALLOWED_AT_START
        assert any("watchdog" in m for _, m in events)
    finally:
        laser.shutdown()


def test_start_with_a_strict_backend_that_refuses_writes():
    """A backend whose every state-changing method raises: start() must still
    succeed (it only reads)."""
    cfg = Config()
    laser, backend = build_sim_system(cfg)
    backend.preset(**LEFT)

    def refuse(name):
        def f(*a, **k):
            raise AssertionError(f"start() wrote {name}{a}")
        return f
    for name in ("set_emission", "set_rf", "set_power", "select_crystal",
                 "set_wavelength", "set_amplitude", "reset_interlock"):
        setattr(backend, name, refuse(name))
    laser.start()
    try:
        assert laser.status().connected
    finally:
        laser._stop.set()
        laser._thread.join(2)
        laser._connected = False               # shutdown would write (and must)
        laser.shutdown()


def test_status_after_start_is_the_laser_as_it_was_left():
    laser, backend, events = _started(LEFT)
    try:
        s = laser.status()
        assert s.emission_on and s.emission_set and s.emission_state == "on"
        assert s.emission_guarded is False     # nobody here switched it on
        assert s.rf_on and s.rf_set
        assert s.power_pct == 23.4 and s.power_set_pct == 23.4
        assert s.filter == "nIR2" and s.crystal == 2
        assert (s.filter_min_nm, s.filter_max_nm) == (800.0, 1400.0)
        assert laser.wavelength_range() == (800.0, 1400.0)
        assert s.wavelength_set_nm[0] == 1234.5 and s.amplitude_set_pct[0] == 42.0
        assert s.wavelength_set_nm[3] == 1000.0 and s.amplitude_set_pct[3] == 7.0
        assert s.wavelength_nm == s.wavelength_set_nm
        assert any("already EMITTING" in m for lvl, m in events if lvl == "warn")
        time.sleep(0.3)                        # and the poll does not "fix" anything
        assert backend.writes == []
        assert backend.read_emission() and backend.read_rf()
    finally:
        laser.shutdown()


def test_adopted_power_above_the_limit_is_announced_not_changed():
    laser, backend, events = _started({**LEFT, "power_pct": 80.0})
    try:
        assert laser.status().power_pct == 80.0
        assert backend.writes == []
        assert any("outside this module's limits" in m for lvl, m in events if lvl == "warn")
        laser.set_power(80.0)                  # an explicit request IS clamped
        assert backend.read_power() == laser.cfg.limits.power_max_pct
    finally:
        laser.shutdown()


def test_unknown_crystal_is_announced_not_switched():
    """The driver reaches a crystal the table does not list: say so, and do
    not touch the RF switch at start."""
    cfg = Config()
    laser, backend = build_sim_system(cfg)
    backend._ranges[3] = (600.0, 1000.0)
    backend.preset(**{**LEFT, "crystal": 3})
    backend.writes.clear()
    events = []
    laser._on_event = lambda lvl, msg: events.append((lvl, msg))
    laser.start()
    try:
        assert backend.writes == [] and backend.read_crystal() == 3
        assert laser.status().crystal == 3
        assert any("not in the filter table" in m for lvl, m in events if lvl == "warn")
    finally:
        laser.shutdown()


def test_unchanged_settings_apply_writes_nothing():
    laser, backend, _ = _started(LEFT)
    try:
        laser.apply_config()
        assert backend.writes == []
    finally:
        laser.shutdown()


def test_a_changed_preset_is_sent_and_only_that_one():
    laser, backend, _ = _started({**LEFT, "emission": False})
    try:
        laser.cfg.startup.power_pct = 12.5
        wl = C.floats(laser.cfg.startup.wavelengths_nm, N_LINES)
        wl[1] = 1111.0                          # line 2 only
        laser.cfg.startup.wavelengths_nm = C.join(wl)
        laser.apply_config()
        assert ("set_power", 12.5) in backend.writes
        assert [w for w in backend.writes if w[0] == "set_wavelength"] == \
            [("set_wavelength", 1, 1111.0)]
        assert not any(w[0] in ("set_amplitude", "select_crystal", "set_rf",
                                "set_emission") for w in backend.writes)
        laser.apply_config()                    # the same again: nothing new
        n = len(backend.writes)
        laser.apply_config()
        assert len(backend.writes) == n
    finally:
        laser.shutdown()


# ---- lost-client guard (Lukas, 2026-09-27) ------------------------------------------

def _guard_rig(timeout=0.3):
    return _started({**LEFT, "emission": False}, client_timeout_s=timeout)


def test_silent_owner_loses_emission():
    laser, backend, events = _guard_rig()
    try:
        laser.set_emission(True, owner="gui-1")
        assert wait_for(lambda: laser.status().emission_guarded)
        assert wait_for(lambda: backend.writes[-1] == ("set_emission", False), 3.0)
        assert laser.status().emission_set is False or wait_for(
            lambda: laser.status().emission_set is False)
        assert any("silent" in m for lvl, m in events if lvl == "warn")
    finally:
        laser.shutdown()


def test_pinging_owner_keeps_emission_and_others_do_not_count():
    laser, backend, _ = _guard_rig()
    try:
        laser.set_emission(True, owner="gui-1")
        end = time.monotonic() + 1.0
        while time.monotonic() < end:           # 3x the timeout, owner pinging
            laser.touch("gui-1")
            laser.touch("someone-else")
            time.sleep(0.05)
        assert laser.status().emission_set is True
        end = time.monotonic() + 1.0            # only the OTHER client talks now
        while time.monotonic() < end and laser.status().emission_set:
            laser.touch("someone-else")
            time.sleep(0.05)
        assert laser.status().emission_set is False
    finally:
        laser.shutdown()


def test_emission_without_owner_is_never_cut():
    """A scan routine (scan-core sends no owner) must survive an hour of
    silence -- here 4x the timeout."""
    laser, backend, _ = _guard_rig()
    try:
        laser.set_emission(True)
        time.sleep(1.2)
        s = laser.status()
        assert s.emission_set is True and s.emission_guarded is False
    finally:
        laser.shutdown()


def test_scan_takes_over_ownership_from_a_gui():
    laser, _, _ = _guard_rig()
    try:
        laser.set_emission(True, owner="gui-1")
        laser.set_emission(True)                # before_scan routine: emission_on
        time.sleep(1.0)                         # the GUI is gone
        assert laser.status().emission_set is True
    finally:
        laser.shutdown()


def test_adopted_emission_has_no_guard():
    laser, _, _ = _started(LEFT, client_timeout_s=0.3)
    try:
        time.sleep(1.0)
        assert laser.status().emission_set is True
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


def test_real_backend_start_is_read_only():
    """The REAL backend + brain start against a fake register map: no register
    is written at start when the watchdog already has the configured value."""
    from superk.backends import nktp
    from superk.laser import SuperK
    b = nktp.NktpSuperK("COM99")
    bus = _FakeBus(cable_at_select=0)
    bus.attach(b)
    b.open = lambda: None                      # no DLL here; attach() connected it
    bus.regs[(b.extreme_addr, nktp.REG_WATCHDOG)] = 10
    bus.regs[(b.extreme_addr, nktp.REG_POWER)] = 234        # 23.4 %
    bus.regs[(b.rf_addr, nktp.REG_RF_POWER)] = 1
    bus.regs[(b.rf_addr, nktp.REG_WL0)] = 700000            # 700 nm
    bus.regs[(b.rf_addr, nktp.REG_AMP0)] = 555              # 55.5 %
    cfg = Config()
    laser = SuperK(b, cfg)
    laser.start()
    try:
        assert bus.writes == []
        s = laser.status()
        assert s.power_pct == 23.4 and s.rf_on and s.crystal == 1
        assert s.wavelength_set_nm[0] == 700.0 and s.amplitude_set_pct[0] == 55.5
    finally:
        laser._stop.set()
        laser._thread.join(2)
    bus.regs[(b.extreme_addr, nktp.REG_WATCHDOG)] = 0      # now it differs
    bus.writes.clear()
    laser2 = SuperK(b, cfg)
    laser2.start()
    laser2._stop.set()
    laser2._thread.join(2)
    assert bus.writes == [(b.extreme_addr, nktp.REG_WATCHDOG, 10)]
