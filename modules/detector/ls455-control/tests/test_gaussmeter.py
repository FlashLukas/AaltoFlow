"""The Gaussmeter brain against the simulator, driven by hand with poll_once()
(no thread), so every test is deterministic and fast."""

import math

import pytest

from ls455.backends.base import GaussmeterBackend, PROBE_RANGES_mT, from_mT, to_mT
from ls455.backends.sim import SimulatedLS455
from ls455.config import Config
from ls455.sim_system import build_sim_system


class Clock:
    """A hand-driven clock shared by the brain and the simulator."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def system():
    cfg = Config()
    clock = Clock()
    meter, sim = build_sim_system(cfg, realtime=False, seed=3, zero_time_s=0.0, clock=clock)
    meter._clock = clock
    events = []
    meter._on_event = lambda lvl, msg: events.append((lvl, msg))
    meter.start(poll=False)
    yield meter, sim, cfg, events, clock
    meter.shutdown()


def _polls(meter, clock, n, dt=0.05):
    for _ in range(n):
        clock.t += dt
        meter.poll_once()


# ---- units ---------------------------------------------------------------------

def test_unit_conversion_to_mT():
    assert to_mT(10.0, "G") == pytest.approx(1.0)
    assert to_mT(1.0, "T") == pytest.approx(1000.0)
    assert to_mT(10.0, "Oe") == pytest.approx(1.0)               # B = mu0 H in air
    assert to_mT(1e4 / (4 * math.pi), "A/m") == pytest.approx(1.0)   # 1e4/(4 pi) A/m = 1 mT
    for u in ("G", "T", "Oe", "A/m"):
        assert from_mT(to_mT(3.21, u), u) == pytest.approx(3.21)


def test_sim_satisfies_the_backend_protocol():
    assert isinstance(SimulatedLS455(), GaussmeterBackend)


# ---- start ------------------------------------------------------------------------

def test_start_adopts_the_meters_own_settings(system):
    meter, sim, cfg, _, _ = system
    assert cfg.meter.mode == "dc" and cfg.meter.dc_digits == 4
    assert meter.status().ranges_mT == PROBE_RANGES_mT["HSE"]
    assert meter.status().probe == "HSE"


class NoWriteBackend:
    """Wraps a backend and FAILS on any call that would change the meter. Used
    to prove that start() only reads (Lukas's rule, 2026-09-27)."""

    WRITES = ("set_mode", "set_auto_range", "set_range", "set_display_unit",
              "set_relative", "start_zero", "clear_zero")

    def __init__(self, inner):
        self._inner = inner
        self.armed = True

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in self.WRITES and self.armed:
            def refuse(*a, **k):
                raise AssertionError(f"start-up changed the meter: {name}{a}")
            return refuse
        return attr


# a deliberately NON-default meter: every setting differs from Config() and
# from the sim's own defaults, so adoption is actually tested
ODD_STATE = dict(mode="dc", dc_digits=5, auto_range=False, range_mT=3500.0,
                 unit="T", relative=True, rel_setpoint_mT=40.0)


def test_start_writes_nothing_to_the_meter():
    cfg = Config()                                   # its values must NOT reach the meter
    sim = SimulatedLS455(realtime=False, seed=2, **ODD_STATE)
    from ls455.gaussmeter import Gaussmeter
    meter = Gaussmeter(NoWriteBackend(sim), cfg)
    meter.start(poll=False)                          # raises if anything is written
    try:
        assert sim.get_mode() == ("dc", 5, "wide")
        assert sim.get_auto_range() is False and sim.get_display_unit() == "T"
        assert sim.get_relative() == (True, 40.0)
    finally:
        meter.shutdown()


def test_status_after_start_is_the_meters_preexisting_state():
    cfg = Config()
    meter, sim = build_sim_system(cfg, realtime=False, seed=2, **ODD_STATE)
    meter.start(poll=False)
    meter.poll_once()
    try:
        s = meter.status()
        assert (s.mode, s.dc_digits) == ("dc", 5)
        assert s.auto_range is False and s.range_mT == 3500.0 and s.range_set_mT == 3500.0
        assert s.display_unit == "T"
        assert s.relative is True and s.rel_setpoint_mT == 40.0
        assert s.field_rel_mT == pytest.approx(s.field_mT - 40.0)
        assert s.settle_s == pytest.approx(7.0)      # 5 digits: 7 x 1 s
        # and cfg now holds the ADOPTED values, so Save config stores the truth
        assert cfg.meter.dc_digits == 5 and cfg.meter.display_unit == "T"
    finally:
        meter.shutdown()


def test_rms_state_is_adopted():
    meter, sim = build_sim_system(Config(), realtime=False, mode="rms", rms_band="narrow")
    meter.start(poll=False)
    try:
        s = meter.status()
        assert (s.mode, s.rms_band) == ("rms", "narrow")
        assert "RMS" in s.quantity
    finally:
        meter.shutdown()


def test_range_limits_come_from_the_probe():
    for probe in ("HST", "HSE", "UHS"):
        meter, _ = build_sim_system(Config(), realtime=False, probe=probe)
        meter.start(poll=False)
        assert meter.range_limits() == (min(PROBE_RANGES_mT[probe]), max(PROBE_RANGES_mT[probe]))
        meter.shutdown()


# ---- readings ---------------------------------------------------------------------

def test_live_reading_is_the_field_in_mT(system):
    meter, sim, _, _, clock = system
    _polls(meter, clock, 5)
    s = meter.status()
    assert s.field_mT == pytest.approx(sim.field_mT + sim.offset_mT, abs=0.2)
    assert s.measured_field_mT == s.field_mT          # the field-source key
    assert s.range_mT == 350.0                        # auto: smallest range above 42 mT


def test_rms_mode_reads_the_ac_part_and_blanks_the_field_source(system):
    meter, sim, _, _, clock = system
    meter.set_mode("rms")
    _polls(meter, clock, 3)
    s = meter.status()
    assert s.field_mT == pytest.approx(sim.ac_rms_mT, rel=0.05)
    assert math.isnan(s.measured_field_mT)            # a VNA must not file an RMS value as B


def test_bad_mode_is_refused(system):
    meter = system[0]
    with pytest.raises(ValueError):
        meter.set_mode("ac")
    with pytest.raises(ValueError):
        meter.set_rms_band("medium")


def test_digits_clamp_with_warning(system):
    meter, sim, cfg, events, _ = system
    meter.set_dc_digits(9)
    assert cfg.meter.dc_digits == 5 and sim.get_mode()[1] == 5
    assert events[-1][0] == "warn"


def test_nan_refused(system):
    meter = system[0]
    with pytest.raises(ValueError):
        meter.set_range(float("nan"))
    with pytest.raises(ValueError):
        meter.set_relative(True, float("inf"))


def test_set_range_switches_auto_off_and_snaps_up(system):
    meter, sim, _, _, _ = system
    meter.set_range(100.0)
    s = meter.status()
    assert s.auto_range is False and sim.get_auto_range() is False
    assert s.range_set_mT == 100.0                    # what we asked (settle echoes this)
    assert s.range_mT == 350.0                        # the probe's next range up


def test_range_clamped_to_the_probe(system):
    meter, _, _, events, _ = system
    meter.set_range(1e6)
    assert meter.status().range_mT == 3500.0
    assert events[-1][0] == "warn"


def test_auto_off_keeps_the_range_auto_chose(system):
    meter, _, cfg, _, clock = system
    _polls(meter, clock, 1)
    chosen = meter.status().range_mT
    meter.set_auto_range(False)
    assert cfg.meter.range_mT == chosen == meter.status().range_mT


def test_overload_is_flagged(system):
    meter, sim, _, _, clock = system
    meter.set_range(3.5)                              # 3.5 mT range, 42 mT field
    _polls(meter, clock, 1)
    assert meter.status().flag == "overload"


def test_noise_grows_with_range_and_shrinks_with_digits():
    def spread(range_mT, digits):
        cfg = Config()
        clock = Clock()
        meter, sim = build_sim_system(cfg, realtime=False, seed=1, field_mT=0.2,
                                      offset_mT=0.0, clock=clock)
        meter._clock = clock
        meter.start(poll=False)
        meter.set_dc_digits(digits)
        meter.set_range(range_mT)
        vals = []
        for _ in range(200):
            clock.t += 1.0                            # long gaps: filter fully settled
            meter.poll_once()
            vals.append(meter.status().field_mT)
        meter.shutdown()
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))

    assert spread(3500.0, 4) > 10 * spread(3.5, 4)
    assert spread(35.0, 3) > 5 * spread(35.0, 5)


def test_relative_mode(system):
    meter, sim, cfg, _, clock = system
    meter.set_relative(True, 40.0)
    _polls(meter, clock, 3)
    s = meter.status()
    assert s.relative and s.field_rel_mT == pytest.approx(s.field_mT - 40.0)
    meter.relative_here()
    assert cfg.meter.rel_setpoint_mT == pytest.approx(s.field_mT)
    meter.set_relative(True, 1e9)                     # clamped to +-350 kG
    assert cfg.meter.rel_setpoint_mT == cfg.limits.rel_setpoint_max_mT


def test_display_unit_changes_panel_not_wire(system):
    meter, sim, _, _, clock = system
    _polls(meter, clock, 2)
    before = meter.status().field_mT
    meter.set_display_unit("T")
    _polls(meter, clock, 2)
    assert sim.get_display_unit() == "T"
    assert meter.status().field_mT == pytest.approx(before, abs=0.2)
    with pytest.raises(ValueError):
        meter.set_display_unit("furlong")


# ---- acquire ------------------------------------------------------------------------

def test_acquire_latches_mean_and_sd_of_fresh_readings(system):
    meter, sim, cfg, _, clock = system
    cfg.acquisition.readings = 4
    n = meter.acquire()
    s = meter.status()
    assert s.acq_id == n and s.acquiring              # id and flag move together
    _polls(meter, clock, 20)
    s = meter.status()
    assert not s.acquiring
    assert s.sample["acq_id"] == n and s.sample["n"] == 4
    assert s.sample["field_mT"] == pytest.approx(sim.field_mT, abs=0.3)
    assert s.sample["std_mT"] >= 0


def test_acquire_waits_for_the_filter_after_a_field_step(system):
    """At 5 digits the manual's filter time constant is 1 s, so the reading
    needs ~7 s to follow a step to 0.1 %. An acquire right after the step must
    NOT average the lagging readings."""
    meter, sim, cfg, _, clock = system
    meter.set_dc_digits(5)
    cfg.acquisition.readings = 3
    _polls(meter, clock, 100, dt=0.1)                 # settled at 42 mT
    sim.field_mT = 80.0                               # the magnet steps
    meter.acquire()
    assert meter.status().settle_s == pytest.approx(7 * 1.0)
    _polls(meter, clock, 60, dt=0.1)
    assert meter.status().acquiring                   # 6 s: still waiting for the filter
    _polls(meter, clock, 30, dt=0.1)
    assert not meter.status().acquiring
    smp = meter.status().sample
    assert smp["field_mT"] == pytest.approx(80.0, abs=0.2)

    # without the settling wait the same experiment reads visibly low
    cfg.acquisition.settle_time_constants = 0.0
    sim.field_mT = 42.0
    meter.acquire()
    _polls(meter, clock, 3, dt=0.1)
    assert meter.status().sample["field_mT"] > 50.0


def test_reading_started_before_the_trigger_is_not_used(system):
    meter, sim, cfg, _, clock = system
    cfg.acquisition.readings = 1
    cfg.acquisition.settle_time_constants = 0.0
    real_read = sim.read_field

    def trigger_arrives_mid_reading():
        clock.t += 1.0                    # the reading began before the trigger
        meter.acquire()
        return real_read()

    sim.read_field = trigger_arrives_mid_reading
    meter.poll_once()
    assert meter.status().acquiring, "a reading that began before the trigger was accepted"
    sim.read_field = real_read
    _polls(meter, clock, 1)
    assert not meter.status().acquiring


def test_acquire_refused_when_not_connected():
    meter, _ = build_sim_system(Config(), realtime=False)
    with pytest.raises(ValueError):
        meter.acquire()


# ---- zero ---------------------------------------------------------------------------

def test_zero_blocks_readings_until_done(system):
    meter, sim, _, events, clock = system
    sim.zero_time_s = 10.0
    before = meter.status().readings
    meter.zero()
    assert meter.status().zeroing
    assert events[-1][0] == "warn"                    # the chamber warning
    with pytest.raises(ValueError):
        meter.acquire()
    meter.poll_once()
    assert meter.status().readings == before
    clock.t = 11.0
    meter.poll_once()
    assert not meter.status().zeroing
    assert sim.offset_mT == 0.0
    meter.clear_zero()
    assert sim.offset_mT > 0


def test_zero_refused_during_acquisition(system):
    meter = system[0]
    meter.acquire()
    with pytest.raises(ValueError):
        meter.zero()


# ---- robustness ----------------------------------------------------------------------

def test_hw_error_is_reported_not_hidden(system):
    meter, sim, _, events, _ = system

    def broken():
        raise OSError("GPIB timeout")

    sim.read_field = broken
    meter.poll_once()
    assert "GPIB timeout" in meter.status().hw_error
    assert events[-1][0] == "error"


def test_status_never_touches_hardware(system):
    meter, sim, _, _, _ = system

    def boom(*a, **k):
        raise AssertionError("status() called the backend")

    for name in ("read_field", "get_range", "get_mode", "zero_running", "get_auto_range"):
        setattr(sim, name, boom)
    s = meter.status()
    assert s.connected and s.ranges_mT


def test_apply_config_sanitises(system):
    meter, sim, cfg, _, _ = system
    cfg.meter.mode = "nonsense"
    cfg.meter.dc_digits = 17
    cfg.meter.display_unit = "furlong"
    cfg.meter.range_mT = -5
    meter.apply_config()
    assert cfg.meter.mode == "dc" and cfg.meter.dc_digits == 5
    assert cfg.meter.display_unit == "G"
    assert cfg.meter.range_mT == min(PROBE_RANGES_mT["HSE"])


def test_shutdown_is_idempotent(system):
    meter = system[0]
    meter.shutdown()
    meter.shutdown()
    assert meter.status().connected is False


# ---- review additions (2026-09-27) ------------------------------------------------

def test_settle_follows_the_manuals_time_constants(system):
    meter, _, cfg, _, _ = system
    for digits, tau in ((3, 0.01), (4, 0.1), (5, 1.0)):
        meter.set_dc_digits(digits)
        assert meter.settle_s() == pytest.approx(cfg.acquisition.settle_time_constants * tau)


def test_acquire_timeout_covers_settling_and_readings(system):
    meter, _, cfg, _, _ = system
    meter.set_dc_digits(5)
    meter.set_acquisition(1000)
    # 1000 readings at 5 digits cannot finish within the bare 30 s margin
    assert meter.acquire_timeout_s() > cfg.acquisition.timeout_s + 7 + 100


def test_overloaded_acquisition_finishes_with_nan_and_a_flag(system):
    """A range too small for the field must not make the scan hang until its
    timeout, and the clipped full-scale value must not be filed as the field."""
    meter, sim, cfg, _, clock = system
    meter.set_range(3.5)                              # 42 mT on a 3.5 mT range
    cfg.acquisition.readings = 3
    cfg.acquisition.settle_time_constants = 0.0
    n = meter.acquire()
    _polls(meter, clock, 5)
    smp = meter.status().sample
    assert smp["acq_id"] == n and not meter.status().acquiring
    assert math.isnan(smp["field_mT"]) and smp["n"] == 0
    assert "overload" in smp["flag"]


def test_measured_field_is_nan_when_the_reading_is_not_a_measurement(system):
    meter, sim, _, _, clock = system
    _polls(meter, clock, 2)
    assert math.isfinite(meter.status().measured_field_mT)
    meter.set_range(3.5)                              # overload
    _polls(meter, clock, 1)
    assert math.isnan(meter.status().measured_field_mT)
    with pytest.raises(ValueError):
        meter.relative_here()                         # a clipped value is no setpoint
    meter.set_auto_range(True)
    _polls(meter, clock, 1)
    assert math.isfinite(meter.status().measured_field_mT)

    def broken():
        raise OSError("GPIB timeout")

    sim.read_field = broken
    meter.poll_once()
    assert math.isnan(meter.status().measured_field_mT)   # stale value hidden


def test_rms_mode_never_publishes_a_field_source_value(system):
    meter, _, _, _, clock = system
    meter.set_mode("rms")
    _polls(meter, clock, 2)
    s = meter.status()
    assert math.isfinite(s.field_mT) and math.isnan(s.measured_field_mT)


def test_peak_mode_left_on_the_meter_is_adopted_not_switched():
    """Before 2026-09-27 a meter in PEAK mode was switched to DC at start. Now
    it is adopted: nothing is written, and every reading says it is a peak."""
    cfg = Config()
    cfg.acquisition.settle_time_constants = 0.0      # real clock here: no settling wait
    sim = SimulatedLS455(realtime=False, seed=1, mode="peak", peak_mode="pulse",
                         peak_display="negative")
    from ls455.gaussmeter import Gaussmeter
    meter = Gaussmeter(NoWriteBackend(sim), cfg)
    events = []
    meter._on_event = lambda lvl, msg: events.append((lvl, msg))
    meter.start(poll=False)
    try:
        assert sim.get_mode()[0] == "peak"             # untouched
        for _ in range(3):
            meter.poll_once()
        s = meter.status()
        assert s.mode == "peak" and (s.peak_mode, s.peak_display) == ("pulse", "negative")
        assert "negative peak" in s.quantity
        # the negative peak: DC field minus the ripple amplitude
        assert s.field_mT == pytest.approx(sim.field_mT + sim.offset_mT
                                           - sim.ac_rms_mT * math.sqrt(2), abs=0.3)
        assert math.isnan(s.measured_field_mT)       # never filed as a DC field
        assert any(lvl == "warn" and "PEAK" in msg for lvl, msg in events)
        n = meter.acquire()
        for _ in range(cfg.acquisition.readings + 40):
            meter.poll_once()
        assert meter.status().sample["acq_id"] == n
        assert "peak" in meter.status().sample["quantity"]
    finally:
        meter.shutdown()


# ---- the probe ----------------------------------------------------------------

def test_probe_is_read_at_start_and_logged(system):
    meter, sim, _, events, _ = system
    s = meter.status()
    assert s.probe == "HSE" and s.probe_serial == "SIM-HSE"
    assert s.probe_type_code == 40 and s.probe_sensitivity_mV_per_kG == 8.0
    assert s.probe_geometry == "axial"
    assert s.probe_desc.startswith("HSE axial")
    assert any("probe: HSE axial" in msg for _, msg in events)


def test_reread_probe_follows_a_swapped_probe(system):
    meter, sim, _, events, _ = system
    assert meter.range_limits() == (0.35, 3500.0)
    sim.swap_probe("HST")
    meter.reread_probe()
    s = meter.status()
    assert s.probe == "HST" and s.ranges_mT == PROBE_RANGES_mT["HST"]
    assert meter.range_limits() == (3.5, 35000.0)
    assert any(lvl == "warn" and "PROBE CHANGED" in msg for lvl, msg in events)


def test_a_probe_plugged_back_in_is_reread_automatically(system):
    meter, sim, _, events, clock = system
    sim.probe_present = False
    _polls(meter, clock, 1)
    assert meter.status().flag == "no probe"
    sim.swap_probe("UHS")
    sim.probe_present = True
    _polls(meter, clock, 1)
    assert meter.status().probe == "UHS"
    assert meter.status().ranges_mT == PROBE_RANGES_mT["UHS"]


# ---- the front panel stays in charge while the service runs --------------------

def test_front_panel_change_is_adopted_by_the_periodic_reread(system):
    """Someone switches the meter to RMS/narrow and tesla by hand: after the
    next re-read the module reports exactly that, and wrote nothing."""
    from ls455.gaussmeter import FRONT_PANEL_SYNC_S
    meter, sim, cfg, events, clock = system
    sim._mode, sim._band, sim._unit = "rms", "narrow", "T"      # hand on the front panel
    guard = NoWriteBackend(sim)
    meter.backend = guard                                       # any write now fails
    _polls(meter, clock, 1, dt=FRONT_PANEL_SYNC_S + 0.1)
    s = meter.status()
    assert (s.mode, s.rms_band, s.display_unit) == ("rms", "narrow", "T")
    assert "RMS" in s.quantity and math.isnan(s.measured_field_mT)
    assert cfg.meter.mode == "rms"
    assert any(lvl == "warn" and "front panel changed" in msg for lvl, msg in events)
    # nothing changes -> no second warning
    n = len(events)
    _polls(meter, clock, 1, dt=FRONT_PANEL_SYNC_S + 0.1)
    assert len(events) == n


def test_no_reread_before_the_period(system):
    meter, sim, cfg, events, clock = system
    sim._unit = "T"
    _polls(meter, clock, 3, dt=0.05)
    assert meter.status().display_unit == "G"


def test_acquire_rereads_first_and_reports_the_new_quantity(system):
    meter, sim, cfg, events, clock = system
    sim._mode = "peak"                                           # changed by hand just now
    acq = meter.acquire()                                        # well inside the sync period
    _polls(meter, clock, 60, dt=0.05)
    s = meter.status()
    assert s.acq_id == acq and not s.acquiring
    assert s.sample["mode"] == "peak" and "peak" in s.sample["quantity"]
    # adopted BEFORE the first counted reading, so not flagged as mixed
    assert "settings changed" not in s.sample["flag"]


def test_change_during_an_acquisition_is_flagged(system):
    meter, sim, cfg, events, clock = system
    cfg.acquisition.readings = 5
    meter.acquire()
    _polls(meter, clock, 16, dt=0.05)                           # settle + 2-3 readings
    assert meter.status().acquiring
    sim._unit = "T"
    meter._sync_now = True                                       # as the periodic timer would
    _polls(meter, clock, 20, dt=0.05)
    assert "settings changed" in meter.status().sample["flag"]


def test_reread_does_not_undo_a_setter_in_flight(system):
    """A setter writes cfg first and the meter a moment later. A re-read that
    lands in between must not put the meter's OLD value back into cfg."""
    meter, sim, cfg, events, clock = system
    cfg.meter.mode = "rms"                  # set_mode has stored it, not yet pushed
    meter._sync_front_panel()
    assert cfg.meter.mode == "rms"
    meter.set_mode("rms")
    assert sim.get_mode()[0] == "rms"


def test_describe_revision_follows_a_probe_swap_on_auto_range(system):
    from ls455.net.describe import build_manifest
    meter, sim, cfg, events, clock = system
    assert cfg.meter.auto_range
    rev = build_manifest(meter)["revision"]
    sim.swap_probe("HST")
    meter.reread_probe()
    assert build_manifest(meter)["revision"] != rev
