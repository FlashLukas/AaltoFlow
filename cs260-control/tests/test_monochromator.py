"""The brain against the simulator, on a FAKE CLOCK: time only moves when a
test says so, and the worker is replaced by explicit poll_once() calls. So the
timing (a move takes |delta|/slew seconds) is tested exactly, without sleeps."""

import math

import pytest

from cs260.config import Config
from cs260.sim_system import build_sim_system


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(**tweaks):
    cfg = Config()
    for path, value in tweaks.items():
        group, field = path.split("__")
        setattr(getattr(cfg, group), field, value)
    clock = Clock()
    mono, sim = build_sim_system(cfg, clock=clock)
    events = []
    mono._on_event = lambda lvl, msg: events.append((lvl, msg))
    mono.start(poll=False)
    return cfg, mono, sim, clock, events


def run(mono, clock, seconds, dt=0.05):
    """Advance the fake clock, polling like the worker would."""
    for _ in range(int(round(seconds / dt))):
        clock.t += dt
        mono.poll_once()


def settle(mono, clock, limit_s=60.0):
    run(mono, clock, 0.05)
    t0 = clock.t
    while mono.status().moving:
        assert clock.t - t0 < limit_s, "move never finished"
        run(mono, clock, 0.05)
    return mono.status()


def test_start_adopts_without_moving():
    cfg, mono, sim, clock, events = make()
    s = mono.status()
    assert s.connected and s.simulated
    assert s.target_nm == pytest.approx(cfg.sim.start_nm, abs=0.01)
    assert s.wavelength_nm == pytest.approx(s.target_nm)
    assert s.moving is False
    assert "nothing changed" in events[0][1]


def test_moving_is_set_together_with_the_target():
    """The adopt-then-flag rule: the very first status after the command must
    already show the new target AND moving -- never the new target next to the
    previous point's moving=False."""
    cfg, mono, sim, clock, _ = make()
    mono.set_wavelength(800.0)
    s = mono.status()
    assert s.target_nm == 800.0 and s.moving is True


def test_a_move_takes_the_slew_time():
    cfg, mono, sim, clock, _ = make()
    start = mono.status().wavelength_nm
    mono.set_wavelength(start + 205.0)          # 205 nm at 205 nm/s ~ 1 s + overhead
    run(mono, clock, 0.6)
    s = mono.status()
    assert s.moving and start < s.wavelength_nm < start + 205.0
    s = settle(mono, clock)
    assert s.wavelength_nm == pytest.approx(start + 205.0, abs=0.01)
    assert s.moving is False
    assert clock.t - 1000.0 == pytest.approx(1.0 + cfg.sim.move_overhead_s, abs=0.2)


def test_clamp_to_the_grating_range_warns():
    cfg, mono, sim, clock, events = make()
    lo, hi = mono.live_limits()
    assert (lo, hi) == (0.0, cfg.gratings.g1_max_nm)
    got = mono.set_wavelength(5000.0)
    assert got == hi
    assert any(lvl == "warn" and "clamped" in m for lvl, m in events)
    assert settle(mono, clock).wavelength_nm == pytest.approx(hi, abs=0.02)


def test_absolute_limits_fence_the_grating_range():
    cfg, mono, sim, clock, _ = make(limits__wavelength_min_nm=400.0)
    assert mono.live_limits()[0] == 400.0
    assert mono.set_wavelength(250.0) == 400.0


def test_non_finite_wavelength_refused():
    cfg, mono, sim, clock, _ = make()
    with pytest.raises(ValueError):
        mono.set_wavelength(float("nan"))


def test_grating_change_sequence_restores_wavelength_and_shutter():
    cfg, mono, sim, clock, _ = make()
    mono.set_wavelength(900.0)
    settle(mono, clock)
    assert mono.status().shutter_open            # sim starts open
    mono.set_grating(2)
    s = mono.status()
    assert s.moving and s.grating_target == 2 and s.target_nm == 900.0
    assert (s.wl_min_nm, s.wl_max_nm) == (0.0, cfg.gratings.g2_max_nm)
    run(mono, clock, 1.0)
    assert mono.status().shutter_open is False, "shutter must be closed during the swap"
    s = settle(mono, clock)
    assert s.grating == 2 and s.grating_lines == 600
    assert s.wavelength_nm == pytest.approx(900.0, abs=0.03)
    assert s.shutter_open is True
    assert s.moving is False


def test_grating_change_clamps_the_restored_wavelength():
    cfg, mono, sim, clock, _ = make(gratings__g2_min_nm=1000.0)
    mono.set_wavelength(700.0); settle(mono, clock)
    mono.set_grating(2)
    assert mono.status().target_nm == 1000.0
    assert settle(mono, clock).wavelength_nm == pytest.approx(1000.0, abs=0.03)


def test_shutter_command_cancels_the_reopen():
    cfg, mono, sim, clock, _ = make()
    mono.set_grating(2)
    run(mono, clock, 0.5)
    mono.set_shutter(False)
    s = settle(mono, clock)
    assert s.grating == 2 and s.shutter_open is False


def test_bad_grating_refused():
    cfg, mono, sim, clock, _ = make()
    with pytest.raises(ValueError):
        mono.set_grating(3)                    # only 2 fitted by default


def test_absent_accessories_are_refused():
    cfg, mono, sim, clock, _ = make()
    with pytest.raises(ValueError):
        mono.set_filter(2)
    with pytest.raises(ValueError):
        mono.set_port(2)


def test_filter_and_port_when_fitted():
    cfg, mono, sim, clock, _ = make(accessories__filter_wheel=True,
                                    accessories__dual_port=True)
    mono.set_filter(3)
    s = mono.status()
    assert s.moving and s.filter_target == 3
    s = settle(mono, clock)
    assert s.filter == 3 and s.filter_label == "LP715"
    mono.set_port(2)
    assert settle(mono, clock).port == 2
    with pytest.raises(ValueError):
        mono.set_filter(7)


def test_order_sorting_moves_the_filter_after_the_wavelength():
    cfg, mono, sim, clock, events = make(accessories__filter_wheel=True,
                                         accessories__auto_filter=True)
    mono.set_wavelength(900.0)
    s = settle(mono, clock)
    assert s.wavelength_nm == pytest.approx(900.0, abs=0.01)
    assert s.filter == 3                       # band 3: 750-1100 nm
    assert any("order sorting" in m for _, m in events)


def test_abort_stops_and_adopts_where_it_stopped():
    cfg, mono, sim, clock, _ = make()
    start = mono.status().wavelength_nm
    mono.set_wavelength(1300.0)
    run(mono, clock, 1.0)
    mono.abort()
    s = settle(mono, clock)
    assert start < s.wavelength_nm < 1300.0
    assert s.target_nm == pytest.approx(s.wavelength_nm)
    run(mono, clock, 3.0)
    assert mono.status().wavelength_nm == pytest.approx(s.wavelength_nm)


def test_step_adopts_the_landing_point():
    cfg, mono, sim, clock, _ = make()
    start = mono.status().wavelength_nm
    mono.step(100)
    assert math.isnan(mono.status().target_nm)
    s = settle(mono, clock)
    assert s.wavelength_nm == pytest.approx(start + 1.0, abs=0.02)   # 0.01 nm/step
    assert s.target_nm == pytest.approx(s.wavelength_nm)


def test_instrument_error_unadopts_the_target():
    """A move the instrument refuses must not look like an arrival: the target
    is un-adopted so a scan waiting on it times out instead of measuring at the
    wrong wavelength."""
    cfg, mono, sim, clock, events = make(gratings__g1_max_nm=2000.0)
    mono.set_wavelength(1900.0)                # beyond the sim's 1600 nm mechanical max
    run(mono, clock, 0.3)
    s = mono.status()
    assert s.moving is False
    assert math.isnan(s.target_nm)
    assert s.error_code == 3 and "not allowed" in s.error_text
    assert any(lvl == "error" for lvl, _ in events)


def test_status_never_touches_the_instrument():
    cfg, mono, sim, clock, _ = make()
    calls = []
    orig = sim.read_state
    sim.read_state = lambda: calls.append(1) or orig()
    for _ in range(20):
        mono.status()
    assert calls == []


def test_read_failure_shows_as_hw_error_then_recovers():
    cfg, mono, sim, clock, events = make()
    orig = sim.read_state

    def boom():
        raise OSError("GPIB timeout")
    sim.read_state = boom
    mono.poll_once()
    assert "GPIB timeout" in mono.status().hw_error
    sim.read_state = orig
    mono.poll_once()
    assert mono.status().hw_error == ""
    assert any("recovered" in m for _, m in events)


def test_shutdown_closes_the_shutter_and_is_idempotent():
    cfg, mono, sim, clock, _ = make()
    assert sim._shutter is True
    mono.shutdown()
    assert sim._shutter is False
    assert mono.status().connected is False
    mono.shutdown()                            # second call is harmless
    with pytest.raises(RuntimeError):
        mono.set_wavelength(500.0)


class NoWritesAtStart:
    """Wraps the sim and FAILS on any call that would change the instrument
    while `armed` -- i.e. during start()."""

    def __init__(self, sim):
        self._sim = sim
        self.armed = True

    def __getattr__(self, name):
        attr = getattr(self._sim, name)
        if name in ("goto", "set_grating", "set_filter", "set_port", "step",
                    "set_shutter", "abort", "calibrate"):
            def guarded(*a, **k):
                assert not self.armed, f"start() called {name}{a}: it must only read"
                return attr(*a, **k)
            return guarded
        return attr


def test_start_writes_nothing_and_adopts_the_existing_state():
    """Lukas's rule (2026-09-27): the service READS the instrument at start
    and changes nothing. The sim is left in a NON-default state (grating 2,
    750 nm, shutter closed, filter 3, lateral port) and the brain must show
    exactly that -- with no command reaching the backend."""
    from cs260.monochromator import Monochromator
    from cs260.backends.sim import SimulatedCS260
    cfg = Config()
    cfg.accessories.filter_wheel = True
    cfg.accessories.dual_port = True
    cfg.sim.start_grating = 2
    cfg.sim.start_nm = 750.0
    cfg.sim.start_shutter_open = False
    cfg.sim.start_filter = 3
    cfg.sim.start_port = 2
    clock = Clock()
    guard = NoWritesAtStart(SimulatedCS260(cfg, clock=clock))
    mono = Monochromator(guard, cfg, clock=clock)
    mono.start(poll=False)
    s = mono.status()
    assert s.grating == 2 and s.grating_target == 2
    assert s.wavelength_nm == pytest.approx(750.0, abs=0.05)
    assert s.target_nm == pytest.approx(s.wavelength_nm)
    assert s.shutter_open is False
    assert s.filter == 3 and s.filter_target == 3
    assert s.port == 2 and s.port_target == 2
    assert s.moving is False
    # the envelope (and so describe) follows the ADOPTED grating, not grating 1
    assert (s.wl_min_nm, s.wl_max_nm) == mono.limits_for(2)
    # a few idle polls later nothing has been commanded either
    run(mono, clock, 1.0)
    assert mono.status().grating == 2 and mono.status().shutter_open is False
    guard.armed = False
    mono.shutdown()


def test_start_leaves_an_open_shutter_open():
    cfg, mono, sim, clock, _ = make()
    assert mono.status().shutter_open is True
    cfg, mono, sim, clock, _ = make(sim__start_shutter_open=False)
    assert mono.status().shutter_open is False


def test_apply_config_never_moves_and_rejects_bad_bands():
    cfg, mono, sim, clock, events = make(accessories__filter_wheel=True)
    mono.set_wavelength(1300.0); settle(mono, clock)
    cfg.gratings.g1_max_nm = 1000.0
    cfg.accessories.auto_filter = True
    cfg.accessories.filter_bands = "garbage"
    mono.apply_config()
    assert cfg.accessories.auto_filter is False
    assert any("outside the new range" in m for _, m in events)
    assert mono.status().moving is False


def test_bandpass_follows_grating_and_slit():
    cfg, mono, sim, clock, _ = make()
    assert mono.status().bandpass_nm == pytest.approx(6.4 * 0.6)
    mono.set_grating(2); settle(mono, clock)
    assert mono.status().bandpass_nm == pytest.approx(12.8 * 0.6)


def test_worker_thread_runs_in_real_time():
    """The real worker: a fast sim move finishes on its own."""
    import time
    cfg = Config()
    cfg.sim.slew_nm_per_s_at_1200 = 20000.0
    cfg.motion.poll_s = 0.02
    mono, sim = build_sim_system(cfg)
    mono.start()
    try:
        mono.set_wavelength(600.0)
        t_end = time.monotonic() + 5.0
        while mono.status().moving and time.monotonic() < t_end:
            time.sleep(0.02)
        s = mono.status()
        assert not s.moving and s.wavelength_nm == pytest.approx(600.0, abs=0.01)
    finally:
        mono.shutdown()


def test_shutter_and_abort_never_wait_for_the_bus():
    """On the real box a query during a slew may block the GPIB lock for
    seconds. Shutter and abort must still be ACCEPTED at once (the client
    would time out otherwise) and be sent at the next poll."""
    import threading
    import time
    cfg, mono, sim, clock, _ = make()
    mono.set_wavelength(1300.0)
    run(mono, clock, 0.5)
    held = threading.Event()
    release = threading.Event()

    def hog():
        with mono._hw:
            held.set()
            release.wait(5.0)
    t = threading.Thread(target=hog)
    t.start()
    held.wait(2.0)
    try:
        t0 = time.monotonic()
        mono.set_shutter(False)
        mono.abort()
        assert time.monotonic() - t0 < 0.5
    finally:
        release.set()
        t.join()
    assert sim._shutter is True                 # not sent yet: filed for the worker
    s = settle(mono, clock)
    assert sim._shutter is False and s.shutter_open is False
    assert s.wavelength_nm < 1300.0 and s.target_nm == pytest.approx(s.wavelength_nm)


def test_abort_filed_before_shutdown_is_still_sent():
    cfg, mono, sim, clock, _ = make()
    mono.set_wavelength(1300.0)
    run(mono, clock, 0.5)
    mono.abort()
    mono.shutdown()
    assert sim._move is None
