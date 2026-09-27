"""The brain against the simulator: moves, clamps, the um estimate, amplitude
validity, the jog dead-man, safety on start/stop, and the threading rules."""

import time

import pytest

from agilis.backends.base import JOGGING, READY
from agilis.config import Config
from agilis.sim_system import build_sim_system

from conftest import settle


def test_step_and_um_moves(fast_brain):
    brain, _ = fast_brain
    brain.move_to_step(0, 2000)
    st = settle(brain, 0)
    assert st.position_steps[0] == 2000
    assert st.position_um[0] == pytest.approx(100.0)        # 2000 x 50 nm
    brain.move_to_um(1, -25.0)                              # -500 steps
    st = settle(brain, 1)
    assert st.position_steps[1] == -500
    assert st.target_um[1] == -25.0                         # kept as commanded
    brain.move_relative_um(1, 5.0)
    st = settle(brain, 1)
    assert st.position_steps[1] == -400


def test_estimate_books_forward_and_backward_separately(fast_brain):
    """1000 out and 1000 back is counter 0, but not position 0 when the two
    directions step differently -- the whole reason the brain keeps tallies."""
    brain, _ = fast_brain
    brain.set_calibration(0, 0.05, +1)
    brain.set_calibration(0, 0.04, -1)
    brain.move_steps(0, 1000)
    settle(brain, 0)
    brain.move_steps(0, -1000)
    st = settle(brain, 0)
    assert st.position_steps[0] == 0
    assert st.position_um[0] == pytest.approx(1000 * 0.05 - 1000 * 0.04)
    # a relative um move uses the step size of ITS direction
    brain.move_relative_um(0, -4.0)
    st = settle(brain, 0)
    assert st.position_steps[0] == -100


def test_absolute_um_move_goes_from_the_estimate(fast_brain):
    brain, _ = fast_brain
    brain.set_calibration(0, 0.05, +1)
    brain.set_calibration(0, 0.04, -1)
    brain.move_to_um(0, 50.0)                 # +1000 fwd steps
    settle(brain, 0)
    brain.move_to_um(0, 10.0)                 # -40 um at 40 nm = -1000 steps
    st = settle(brain, 0)
    assert st.position_steps[0] == 0
    assert st.position_um[0] == pytest.approx(10.0)


def test_clamp_to_limits_and_leash_with_warning(fast_brain):
    brain, _ = fast_brain
    events = []
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    assert brain.move_to_step(0, 10**9) == brain.cfg.limits.max_steps_x
    brain.stop(0)
    assert any(lvl == "warn" and "clamped" in msg for lvl, msg in events)
    brain.set_leash(enabled=True, leash_steps=300)
    assert brain.move_to_step(1, -5000) == -300
    st = settle(brain, 1)
    assert st.leash and st.limit_lo == [-300, -300] and st.position_steps[1] == -300


def test_amplitude_is_clamped_pushed_and_invalidates_the_calibration(fast_brain):
    brain, sim = fast_brain
    events = []
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    assert brain.status().cal_valid == [True, True]
    assert brain.set_amplitude(0, 99) == 50                  # clamped
    assert sim.read_amplitude(1, +1) == 50 and sim.read_amplitude(1, -1) == 50
    time.sleep(0.08)
    st = brain.status()
    assert st.amplitude_fwd[0] == 50 and st.cal_valid[0] is False
    assert any("approximate" in msg for _, msg in events)
    brain.set_calibration(0, 0.2)                            # measured at 50 now
    time.sleep(0.08)
    st = brain.status()
    assert st.cal_valid[0] is True and st.cal_amp_fwd[0] == 50 and st.cal_amp_bwd[0] == 50
    # one direction only
    brain.set_amplitude(1, 7, -1)
    assert sim.read_amplitude(2, -1) == 7 and sim.read_amplitude(2, +1) == 16


def test_amplitude_refused_while_moving():
    cfg = Config()
    brain, sim = build_sim_system(cfg)
    sim.pr_rate = 50.0                       # slow: still moving when we ask
    brain.start()
    try:
        brain.move_steps(0, 1000)
        with pytest.raises(RuntimeError, match="at rest"):
            brain.set_amplitude(0, 30)
    finally:
        brain.shutdown()


def test_bad_calibration_is_refused(fast_brain):
    brain, _ = fast_brain
    with pytest.raises(ValueError):
        brain.set_calibration(0, 0.0)


def test_amplitude_preset(fast_brain):
    brain, sim = fast_brain
    brain.set_step_size(True)
    time.sleep(0.08)
    assert brain.status().step_large is True
    assert sim.read_amplitude(1, +1) == 50 and sim.read_amplitude(2, -1) == 50
    brain.set_step_size(False)
    time.sleep(0.08)
    assert brain.status().step_large is False
    assert sim.read_amplitude(1, +1) == 16


def test_jog_deadman_releases_itself(fast_brain):
    brain, sim = fast_brain
    brain.cfg.motion.jog_timeout_s = 0.25
    assert brain.jog(0, 4) == 4
    time.sleep(0.12)
    assert sim.axis_state(1) == JOGGING
    brain.jog(0, 4)                          # keep-alive, still the same jog
    time.sleep(0.2)
    assert sim.axis_state(1) == JOGGING
    time.sleep(0.4)
    assert sim.axis_state(1) == READY
    assert brain.status().jogging[0] is False


def test_jog_stops_at_the_leash(fast_brain):
    brain, sim = fast_brain
    brain.cfg.motion.jog_timeout_s = 10.0
    brain.set_leash(enabled=True, leash_steps=200)
    brain.jog(1, 3)                          # 1700 steps/s: 200 steps in ~0.12 s
    time.sleep(0.6)
    assert sim.axis_state(2) == READY
    assert 200 <= brain.status().position_steps[1] < 260
    # jogging further out is refused; back in is allowed
    assert brain.jog(1, 3) == 0
    assert brain.jog(1, -1) == -1
    brain.stop(1)


def test_moving_is_true_right_after_a_move_even_between_polls(fast_brain):
    """gotcha #2: a scan must never see the previous point's moving=False
    together with the new target. The target and the axis state are read
    under one lock, so a snapshot with the new target_um has the new state."""
    brain, sim = fast_brain
    sim.pr_rate = 400.0
    brain.move_to_um(0, 20.0)                # 400 steps, ~1 s
    time.sleep(0.1)
    st = brain.status()
    assert st.target_um[0] == 20.0 and st.moving[0] is True
    st = settle(brain, 0)
    assert st.moving[0] is False and st.position_um[0] == pytest.approx(20.0)


def test_a_new_move_replaces_one_in_progress(fast_brain):
    brain, sim = fast_brain
    sim.pr_rate = 400.0
    brain.move_steps(0, 5000)
    time.sleep(0.1)
    brain.move_to_step(0, 10)
    st = settle(brain, 0)
    assert st.position_steps[0] == 10


def test_datum_and_display_zero(fast_brain):
    brain, _ = fast_brain
    brain.move_steps(0, 700)
    settle(brain, 0)
    assert brain.set_zero(0) == 700
    st = settle(brain, 0)
    assert st.rel_steps[0] == 0
    brain.zero_counter(0)
    st = settle(brain, 0)
    assert st.position_steps[0] == 0 and st.position_um[0] == 0.0 and st.rel_origin[0] == 0


def test_datum_refused_while_moving():
    brain, sim = build_sim_system(Config())
    sim.pr_rate = 50.0
    brain.start()
    try:
        brain.move_steps(1, 1000)
        with pytest.raises(RuntimeError, match="moving"):
            brain.zero_counter(1)
    finally:
        brain.shutdown()


def test_status_never_touches_the_hardware(fast_brain):
    """gotcha #1: status() returns the poll thread's snapshot."""
    brain, sim = fast_brain
    calls = []
    for name in ("read_position", "axis_state", "limit_status"):
        orig = getattr(sim, name)
        setattr(sim, name, lambda *a, _o=orig, _n=name: (calls.append(_n), _o(*a))[1])
    brain._poll_stop.set()
    brain._poll_thread.join(1.0)
    calls.clear()
    for _ in range(50):
        brain.status()
    assert calls == []


def test_safe_start_stops_a_leftover_jog_and_pushes_amplitudes():
    cfg = Config()
    cfg.motion.amp_fwd_y = 33
    brain, sim = build_sim_system(cfg)
    sim._remote = True
    sim.jog(1, 3)                            # a previous session left X jogging
    sim._remote = False
    brain.start()
    try:
        assert sim.axis_state(1) == READY
        assert sim.read_amplitude(2, +1) == 33
    finally:
        brain.shutdown()


def test_shutdown_stops_motion_and_is_idempotent():
    brain, sim = build_sim_system(Config())
    sim.pr_rate = 50.0
    brain.start()
    brain.move_steps(0, 10000)
    brain.jog(1, 1)
    brain.shutdown()
    assert sim.axis_state(1) == READY and sim.axis_state(2) == READY
    assert brain.status().connected is False
    brain.shutdown()                         # second call is harmless


def test_counter_nonzero_at_start_is_booked():
    brain, sim = build_sim_system(Config())
    sim._count = [400, -200]                 # controller used by hand before
    brain.start()
    try:
        st = brain.status()
        assert st.position_steps == [400, -200]
        assert st.position_um == pytest.approx([20.0, -10.0])
    finally:
        brain.shutdown()


def test_hardware_read_failure_is_reported_not_fatal(fast_brain):
    brain, sim = fast_brain

    def broken(*_a):
        raise TimeoutError("AG-UC2 did not answer")
    orig = sim.read_position
    sim.read_position = broken
    time.sleep(0.15)
    assert "did not answer" in brain.status().hw_error
    sim.read_position = orig
    time.sleep(0.15)
    assert brain.status().hw_error == ""


def test_stream_records_both_axes(fast_brain):
    brain, sim = fast_brain
    sim.pr_rate = 1000.0
    brain.stream_start(rate_hz=100)
    brain.move_steps(0, 400)
    time.sleep(0.6)
    chunk = brain.stream_stop()
    x = chunk["values"]["x"]
    assert list(chunk["values"]) == ["x", "y"]
    assert len(chunk["t"]) >= 30
    assert x[-1] == pytest.approx(20.0)
    assert sum(1 for v in x if 2.0 < v < 18.0) >= 5          # it moved THROUGH
    assert all(b >= a for a, b in zip(x, x[1:]))


def test_max_amplitude_jog_marks_the_estimate_approximate(fast_brain):
    """Jog speeds 2 and 3 step at amplitude 50 whatever SU says (manual, JA/SU).
    Those steps must not be passed off as calibrated micrometres: estimate_ok
    goes False and stays False (even when the jog is over) until the datum."""
    brain, _ = fast_brain
    st = brain.status()
    assert st.cal_valid[0] and st.estimate_ok[0]
    # speed 4 runs at the SU amplitude = the calibrated one: still fine
    brain.jog(0, 4)
    time.sleep(0.15)
    brain.jog(0, 0)
    st = settle(brain, 0)
    assert st.estimate_ok[0] and st.uncal_steps[0] == 0
    # speed 3 = max amplitude: the booked steps are flagged
    brain.jog(0, -3)
    time.sleep(0.15)
    brain.stop(0)
    st = settle(brain, 0)
    assert st.cal_valid[0]                     # the amplitude setting is unchanged...
    assert not st.estimate_ok[0]               # ...but the estimate is not trusted
    assert st.uncal_steps[0] > 0
    assert st.estimate_ok[1]                   # the other axis is untouched
    brain.zero_counter(0)
    st = settle(brain, 0)
    assert st.estimate_ok[0] and st.uncal_steps[0] == 0


def test_steps_at_a_changed_amplitude_stay_flagged_after_changing_back(fast_brain):
    brain, _ = fast_brain
    brain.set_amplitude(0, 30)                 # away from the calibrated 16
    brain.move_steps(0, 200)
    settle(brain, 0)
    brain.set_amplitude(0, 16)                 # back: cal_valid again
    st = settle(brain, 0)
    assert st.cal_valid[0]
    assert not st.estimate_ok[0] and st.uncal_steps[0] == 200


def test_failed_start_closes_the_controller():
    """If anything after open() fails, the brain stops, closes and re-raises,
    so no half-started controller keeps the port or runs a jog."""
    cfg = Config()
    brain, sim = build_sim_system(cfg)
    closed = []
    sim.close = lambda: closed.append(True)

    def boom(*_a):
        raise RuntimeError("controller error -6: not allowed in current state")
    sim.set_amplitude = boom
    with pytest.raises(RuntimeError, match="-6"):
        brain.start()
    assert closed and not brain.status().connected
    brain.shutdown()                            # idempotent, nothing to do


def test_set_config_amplitude_is_clamped_in_the_config_too(fast_brain):
    from agilis.net.protocol import apply_config_dict
    brain, sim = fast_brain
    apply_config_dict(brain.cfg, {"motion": {"amp_fwd_x": 80}})
    brain.apply_config()
    assert brain.cfg.motion.amp_fwd_x == 50
    assert sim.read_amplitude(brain._hw(0), +1) == 50
