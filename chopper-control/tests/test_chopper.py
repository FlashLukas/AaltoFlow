"""The Chopper brain against the simulated MC2000B, on a MANUAL clock.

The simulator and the brain share one clock that the test advances by hand,
and the poll thread is not started (`start(poll=False)`), so every lock
decision is deterministic: no sleeps, no flakiness under load.
"""

import math

import pytest

from chopper.config import Config
from chopper.sim_system import build_sim_system


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(**sim):
    cfg = Config()
    for k, v in sim.items():
        setattr(cfg.sim, k, v)
    cfg.sim.jitter_rel = 0.0            # exact numbers for the assertions
    clock = Clock()
    ch, be = build_sim_system(cfg, clock=clock, seed=1)
    events = []
    ch._on_event = lambda lvl, msg: events.append((lvl, msg))
    ch.events = events
    return ch, be, clock


def run_for(ch, clock, seconds, dt=0.1):
    """Advance the shared clock and poll, like the poll thread would."""
    n = int(round(seconds / dt))
    for _ in range(n):
        clock.t += dt
        ch.poll_once()


@pytest.fixture
def running():
    ch, be, clock = make()
    ch.start(poll=False)
    yield ch, be, clock
    ch.shutdown()


# ---- start-up: adopt, never command --------------------------------------------

def test_start_adopts_a_running_chopper():
    ch, be, clock = make(enabled=True, frequency_Hz=150.0)
    ch.start(poll=False)
    s = ch.status()
    assert s.enabled is True and s.setpoint_frequency_Hz == 150.0
    assert s.blade == "MC1F10HP" and s.ref_mode == "int-inner"
    assert be.get_enable() is True            # still running: nothing was commanded
    ch.shutdown()


def test_start_adopts_a_stopped_chopper_and_does_not_start_it():
    ch, be, clock = make(enabled=False)
    ch.start(poll=False)
    assert ch.status().enabled is False
    assert be.get_enable() is False
    ch.shutdown()


def test_shutdown_leaves_the_wheel_running_by_default(running):
    ch, be, clock = running
    ch.shutdown()
    assert be.get_enable() is True
    assert ch.status().connected is False


def test_stop_on_exit_puts_it_in_standby():
    ch, be, clock = make()
    ch.cfg.hardware.stop_on_exit = True
    ch.start(poll=False)
    ch.shutdown()
    assert be.get_enable() is False


def test_unowned_blade_at_start_is_warned_about():
    ch, be, clock = make(blade="MC1F30", ref_mode="internal", output_mode="actual")
    ch.start(poll=False)
    assert any(l == "warn" and "MC1F30" in m for l, m in ch.events)
    assert "MC1F30" in ch.blade_options()     # shown, not hidden
    ch.shutdown()


# ---- the lock ----------------------------------------------------------------------

def test_a_new_frequency_clears_the_lock_at_once_and_it_relocks(running):
    ch, be, clock = running
    run_for(ch, clock, 2.0)
    assert ch.status().locked is True
    ch.set_frequency(400.0)
    s = ch.status()
    # same critical section: the new setpoint is NEVER shown next to the old lock
    assert s.setpoint_frequency_Hz == 400.0 and s.locked is False
    run_for(ch, clock, 1.0)
    assert ch.status().locked is False        # still spinning up (tau 0.6 s)
    run_for(ch, clock, 8.0)
    s = ch.status()
    assert s.locked is True
    assert abs(s.frequency_Hz - 400.0) < 0.5


def test_lock_needs_the_hold_time(running):
    ch, be, clock = running
    ch.cfg.settle.hold_s = 3.0
    ch.set_frequency(160.0)
    run_for(ch, clock, 3.0)                   # inside the band after ~2.6 s
    assert ch.status().locked is False
    run_for(ch, clock, 3.0)
    assert ch.status().locked is True


def test_a_poll_straddling_a_command_does_not_judge_it(running):
    """Readings taken before a command must not declare the new point locked."""
    ch, be, clock = running
    run_for(ch, clock, 2.0)
    assert ch.status().locked is True
    # simulate: the poll has read the wheel, then a command lands before it stores
    gen0 = ch.status().lock_gen
    orig = be.get_enable

    def enable_and_command():
        v = orig()
        # a command lands while the poll holds its readings
        with ch._lock:
            ch._freq_sp = 800.0
            ch._restart_lock()
        return v
    be.get_enable = enable_and_command
    ch.poll_once()
    be.get_enable = orig
    s = ch.status()
    assert s.lock_gen == gen0 + 1
    assert s.locked is False


def test_standby_is_never_locked(running):
    ch, be, clock = running
    run_for(ch, clock, 2.0)
    ch.set_enable(False)
    assert ch.status().locked is False
    run_for(ch, clock, 20.0)                  # 8 coast-down time constants
    s = ch.status()
    assert s.locked is False
    assert s.frequency_Hz < 1.0               # the wheel coasted down


def test_blind_lock_with_ref_out_on_target(running):
    ch, be, clock = running
    ch.set_enable(False)
    ch.set_output_mode("target")
    ch.set_enable(True)
    s = ch.status()
    assert s.lock_source == "timer"
    run_for(ch, clock, ch.cfg.settle.blind_lock_s - 0.5)
    assert ch.status().locked is False
    run_for(ch, clock, 1.0)
    s = ch.status()
    assert s.locked is True
    assert math.isnan(s.frequency_Hz)         # the wheel is NOT measured, and says so


def test_ref_out_on_the_other_ring_is_scaled(running):
    """10/100 blade locked on the inner ring, REF OUT on the outer ring: the
    outer ring chops 10x faster; the brain reports the INNER frequency."""
    ch, be, clock = running
    ch.set_enable(False)
    ch.set_output_mode("outer")
    ch.set_enable(True)
    run_for(ch, clock, 6.0)
    s = ch.status()
    assert abs(s.refout_frequency_Hz - 1500.0) < 1.0
    assert abs(s.frequency_Hz - 150.0) < 0.1
    assert s.locked is True


# ---- clamps and refusals ---------------------------------------------------------

def test_frequency_clamped_to_the_blade_ring_and_warned(running):
    ch, be, clock = running
    assert ch.set_frequency(5000.0) == 1000.0             # inner ring of the 10/100
    assert any(l == "warn" and "clamped" in m for l, m in ch.events)
    assert ch.set_frequency(1.0) == 20.0


def test_frequency_clamped_to_the_safety_envelope(running):
    ch, be, clock = running
    ch.cfg.limits.freq_max_Hz = 300.0
    assert ch.freq_limits() == (20.0, 300.0)
    assert ch.set_frequency(900.0) == 300.0


def test_apply_config_moves_the_setpoint_inside_a_tighter_envelope(running):
    ch, be, clock = running
    ch.set_frequency(800.0)
    ch.cfg.limits.freq_max_Hz = 500.0
    ch.apply_config()
    assert ch.status().setpoint_frequency_Hz == 500.0


def test_nan_is_refused(running):
    ch, be, clock = running
    with pytest.raises(ValueError):
        ch.set_frequency(float("nan"))


def test_blade_and_modes_only_in_standby(running):
    ch, be, clock = running
    for call in (lambda: ch.set_blade("MC1F60"), lambda: ch.set_ref_mode("int-outer"),
                 lambda: ch.set_output_mode("target"), lambda: ch.set_harmonics(2, 1)):
        with pytest.raises(ValueError, match="standby"):
            call()


def test_unowned_blade_refused(running):
    ch, be, clock = running
    ch.set_enable(False)
    with pytest.raises(ValueError, match="owned"):
        ch.set_blade("MC1F100")


def test_blade_change_keeps_the_meaning_of_the_modes(running):
    ch, be, clock = running
    ch.set_frequency(50.0)
    ch.set_enable(False)
    ch.set_blade("MC1F60")
    s = ch.status()
    # "int-inner" -> "internal", REF OUT on a sensor stays on a sensor
    assert (s.blade, s.ref_mode, s.output_mode) == ("MC1F60", "internal", "actual")
    assert ch.freq_limits() == (120.0, 6000.0)
    # 50 Hz is below the 60-slot blade's minimum: moved inside, loudly
    assert s.setpoint_frequency_Hz == 120.0
    assert any(l == "warn" and "clamped" in m for l, m in ch.events)
    ch.set_blade("MC1F10HP")
    s = ch.status()
    assert (s.ref_mode, s.output_mode) == ("int-inner", "inner")


def test_unknown_mode_names_refused(running):
    ch, be, clock = running
    ch.set_enable(False)
    with pytest.raises(ValueError):
        ch.set_ref_mode("internal")          # the 10/100 blade calls it int-inner / int-outer


# ---- external reference --------------------------------------------------------------

def test_external_reference_follows_input_times_n_over_d():
    ch, be, clock = make(external_input_Hz=100.0)
    ch.start(poll=False)
    ch.set_enable(False)
    ch.set_ref_mode("ext-inner")
    ch.set_harmonics(3, 2)
    with pytest.raises(ValueError, match="external"):
        ch.set_frequency(200.0)
    ch.set_enable(True)
    run_for(ch, clock, 8.0)
    s = ch.status()
    assert s.external is True
    assert s.target_frequency_Hz == pytest.approx(150.0)
    assert abs(s.frequency_Hz - 150.0) < 0.5 and s.locked
    ch.shutdown()


def test_harmonics_clamped_to_1_15(running):
    ch, be, clock = running
    ch.set_enable(False)
    ch.set_harmonics(40, 0)
    s = ch.status()
    assert (s.nharmonic, s.dharmonic) == (15, 1)


# ---- hardware trouble ---------------------------------------------------------------------

def test_a_failing_read_shows_as_hw_error_and_status_never_touches_hardware(running):
    ch, be, clock = running
    run_for(ch, clock, 2.0)

    def boom():
        raise OSError("serial port vanished")
    be.read_refout_frequency = boom
    ch.poll_once()
    s = ch.status()                          # must not raise, must not call the backend
    assert "vanished" in s.hw_error and s.locked is False


def test_a_front_panel_frequency_change_is_adopted(running):
    ch, be, clock = running
    be.set_frequency(250.0)                  # somebody turned the knob
    for _ in range(ch.FULL_READ_EVERY):
        clock.t += 0.1
        ch.poll_once()
    assert ch.status().setpoint_frequency_Hz == 250.0


# ---- races and read-back rounding found in review ---------------------------------------

def _next_poll_is_full(ch):
    ch._npoll = ch.FULL_READ_EVERY - 1


def test_a_full_read_straddling_a_command_does_not_revert_it(running):
    """The poll read freq=150 BEFORE a command set 400: applying that read
    afterwards used to look like a front-panel change and put 150 back."""
    ch, be, clock = running
    run_for(ch, clock, 2.0)
    orig = be.get_phase                       # read in the full read, after freq?
    fired = []

    def phase_then_command():
        v = orig()
        if not fired:
            fired.append(ch.set_frequency(400.0))
        return v
    be.get_phase = phase_then_command
    _next_poll_is_full(ch)
    ch.poll_once()
    be.get_phase = orig
    assert fired == [400.0]
    assert ch.status().setpoint_frequency_Hz == 400.0


def test_a_start_straddled_by_a_poll_keeps_its_lock_generation():
    """`start` returns lock_gen = g and a scan waits for lock_gen == g AND
    locked. A poll that read 'standby' before the start must not bump the
    generation again, or that wait could never finish."""
    ch, be, clock = make(enabled=False)
    ch.start(poll=False)
    orig = be.get_enable
    gens = []

    def read_then_start():
        v = orig()                            # reads False (standby)
        if not gens:
            gens.append(ch.set_enable(True))
        return v
    be.get_enable = read_then_start
    ch.poll_once()
    be.get_enable = orig
    s = ch.status()
    assert s.enabled is True and s.lock_gen == gens[0]
    run_for(ch, clock, 6.0)
    s = ch.status()
    assert s.locked is True and s.lock_gen == gens[0]
    ch.shutdown()


def test_integer_read_back_does_not_change_an_exact_setpoint(running):
    """On the 0.1 Hz blade we send 150.4 for a request of 150.37; the unit
    may answer freq? with whole Hz. Neither may replace the setpoint a scan
    is waiting to see, and the lock is judged on the grid value sent."""
    ch, be, clock = running
    ch.cfg.settle.tolerance_Hz = 0.01         # tighter than the rounding
    ch.cfg.settle.tolerance_rel = 0.0
    assert ch.set_frequency(150.37) == 150.37
    assert abs(be._freq - 150.4) < 1e-9       # the grid value went to the unit
    be.get_frequency = lambda: float(round(be._freq))
    run_for(ch, clock, 8.0)
    s = ch.status()
    assert s.setpoint_frequency_Hz == 150.37
    assert abs(s.target_frequency_Hz - 150.4) < 1e-9
    assert s.locked is True
