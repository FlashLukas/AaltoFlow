"""Start-up ADOPTS the controller's state, and the AG-LS25 limit-switch features.

The rule (Lukas, 2026-09-27): every module reads the instrument's state at start
and changes nothing. For the AG-UC2 that means: read the counters (TP) and the
step amplitudes (SU+? / SU-?), never write SU / ZP / ST at start; the one write
is MR, without which TP/SU?/PH are refused. A simulator in a used, non-default
state (counters away from 0, amplitudes 24/20 and 16/30, left in remote mode)
makes sure the brain really READS instead of assuming the power-up values.
"""

import time

import pytest

from agilis.backends.base import READY
from agilis.config import Config
from agilis.sim_system import build_sim_system

from conftest import settle

#: backend calls that change the controller's state
WRITES = ("move_by", "jog", "stop", "zero_counter", "set_amplitude",
          "move_to_limit", "measure_position", "move_absolute")


def _recording(sim):
    """Wrap every state-changing backend call so it is recorded."""
    calls = []
    for name in WRITES + ("enable_remote",):
        orig = getattr(sim, name)
        setattr(sim, name, lambda *a, _o=orig, _n=name: (calls.append(_n), _o(*a))[1])
    return calls


def _used_brain(**hw):
    cfg = Config()
    cfg.hardware.poll_hz = 50
    for k, v in hw.items():
        setattr(cfg.hardware, k, v)
    brain, sim = build_sim_system(cfg)
    sim.preset_used_state()
    sim.pr_rate = 20000.0
    return brain, sim


def test_start_issues_no_state_changing_writes():
    brain, sim = _used_brain()
    brain.cfg.motion.amp_fwd_x = 40          # .ini values that used to be pushed
    brain.cfg.motion.amp_bwd_y = 3
    calls = _recording(sim)
    brain.start()
    try:
        time.sleep(0.15)                     # a few polls as well
        assert [c for c in calls if c in WRITES] == []
        assert calls == ["enable_remote"]    # MR: needed to read TP/SU?/PH
        assert len(brain.startup_writes) == 1 and brain.startup_writes[0].startswith("MR")
    finally:
        brain.shutdown()


def test_status_after_start_reflects_the_controllers_state():
    brain, sim = _used_brain()
    brain.cfg.motion.amp_fwd_x = 40
    brain.start()
    try:
        st = brain.status()
        assert st.position_steps == [1500, -700]           # counters adopted
        assert st.amplitude_fwd == [24, 16]                # amplitudes adopted...
        assert st.amplitude_bwd == [20, 30]
        assert brain.cfg.motion.amp_fwd_x == 24            # ...into the config
        assert sim._amp == [[24, 20], [16, 30]]            # ...and nothing pushed
        # step sizes were measured at 16: the adopted 24/20/30 are not those
        assert st.cal_valid == [False, False]
        assert st.estimate_ok == [False, False]            # net count only
        assert st.uncal_steps == [1500, 700]
    finally:
        brain.shutdown()


def test_ini_amplitude_is_applied_only_when_asked_and_only_if_different():
    brain, sim = _used_brain()
    brain.start()
    try:
        calls = _recording(sim)
        from agilis.net.protocol import apply_config_dict
        apply_config_dict(brain.cfg, {"limits": {"leash_steps": 1000}})
        brain.apply_config()                                # nothing differs
        assert "set_amplitude" not in calls
        apply_config_dict(brain.cfg, {"motion": {"amp_fwd_x": 33}})
        brain.apply_config()                                # the user asked for 33
        assert calls.count("set_amplitude") == 1
        assert sim.read_amplitude(1, +1) == 33
    finally:
        brain.shutdown()


def test_a_move_running_at_start_is_waited_for_not_stopped():
    brain, sim = _used_brain()
    sim.pr_rate = 1000.0
    sim.move_by(2, 300)                                     # ~0.3 s left to run
    calls = _recording(sim)
    brain.start()
    try:
        assert "stop" not in calls
        assert sim.axis_state(2) == READY
        assert brain.status().position_steps[1] == -700 + 300
    finally:
        brain.shutdown()


def test_power_up_controller_in_local_mode_is_adopted():
    cfg = Config()
    brain, sim = build_sim_system(cfg)                      # power-up: local mode
    brain.start()
    try:
        st = brain.status()
        assert st.connected and st.position_steps == [0, 0]
        assert st.amplitude_fwd == [16, 16] and st.estimate_ok == [True, True]
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# AG-LS25 limit switch: MV, MA, PA, the step-size measurement
# --------------------------------------------------------------------------- #
def _fast_ls():
    cfg = Config()
    cfg.hardware.poll_hz = 50
    brain, sim = build_sim_system(cfg)
    sim.pr_rate = 20000.0
    sim.speed_scale = 4000.0          # limit to limit in a fraction of a second
    sim.limit_op_s = 0.3
    brain.start()
    return brain, sim


def _wait_routine(brain, rid, timeout=20.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = brain.status()
        if st.routine_id == rid and not st.routine_running:
            return st
        time.sleep(0.02)
    raise AssertionError("routine did not finish")


def test_move_to_limit_stops_at_the_switch():
    brain, sim = _fast_ls()
    try:
        brain.move_to_limit(0, +1, 3)
        st = settle(brain, 0, timeout=5)
        assert st.limit_switch[0] and not st.limit_switch[1]
        assert 6000 - 2.0 <= sim.true_um(1) <= 6000       # switch closed, before the stop
        # MV3 stepped at amplitude 50, not the calibrated 16: flagged
        assert not st.estimate_ok[0] and st.uncal_steps[0] > 0
    finally:
        brain.shutdown()


def test_measure_step_size_routine_both_directions():
    brain, sim = _fast_ls()
    try:
        rid = brain.measure_step_size(1)
        st = _wait_routine(brain, rid)
        assert st.routine_error == "OK", st.routine_error
        # travel / counted steps = the simulator's true step size (the few um
        # of switch travel and the 4 % per-step scatter average out)
        assert st.um_per_step_fwd[1] == pytest.approx(sim.step_size_um(1, +1, 16), rel=0.02)
        assert st.um_per_step_bwd[1] == pytest.approx(sim.step_size_um(1, -1, 16), rel=0.02)
        assert st.um_per_step_fwd[1] != pytest.approx(st.um_per_step_bwd[1], rel=0.05)
        # ends at the NEGATIVE limit with the datum there, estimate trusted
        assert st.position_steps[1] == 0 and st.measured_um[1] == 0.0
        assert st.limit_switch[1] and sim.true_um(2) < -5990
        assert st.cal_valid[1] and st.estimate_ok[1]
        assert st.cal_amp_fwd[1] == 16 and st.cal_amp_bwd[1] == 16
    finally:
        brain.shutdown()


def test_measure_position_is_a_routine_and_the_poll_keeps_going():
    brain, sim = _fast_ls()
    try:
        x = sim.true_um(1)
        rid = brain.measure_position(0)
        time.sleep(0.1)
        st = brain.status()
        assert st.routine_running and st.usb_busy and st.routine_id == rid
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="busy"):
            brain.move_steps(1, 10)                         # refused at once...
        assert time.monotonic() - t0 < 0.1                  # ...not after 2 min
        st = _wait_routine(brain, rid)
        assert st.routine_error == "OK" and not st.usb_busy
        assert st.measured_um[0] == pytest.approx(x + 6000, abs=12.0)   # 1/1000 of 12 mm
    finally:
        brain.shutdown()


def test_absolute_move_books_its_steps_as_uncalibrated():
    brain, sim = _fast_ls()
    try:
        rid = brain.move_absolute(1, 3000.0)
        st = _wait_routine(brain, rid)
        assert st.routine_error == "OK"
        assert sim.true_um(2) == pytest.approx(3000 - 6000, abs=1.0)
        assert st.measured_um[1] == pytest.approx(3000.0)
        assert not st.estimate_ok[1] and st.uncal_steps[1] > 0
    finally:
        brain.shutdown()


def test_stop_aborts_a_step_size_run():
    brain, sim = _fast_ls()
    sim.speed_scale = 20.0                                  # slow enough to catch
    try:
        rid = brain.measure_step_size(0)
        time.sleep(0.2)
        brain.stop(0)
        st = _wait_routine(brain, rid, timeout=5)
        assert st.routine_error == "aborted"
        assert sim.axis_state(1) == READY
    finally:
        brain.shutdown()


def test_limit_operations_respect_the_leash_and_the_stage_type():
    brain, _ = _fast_ls()
    try:
        brain.set_leash(True, 1000)
        with pytest.raises(RuntimeError, match="leash"):
            brain.measure_step_size(0)
        with pytest.raises(RuntimeError, match="leash"):
            brain.move_to_limit(0, -1)
        brain.set_leash(False)
        brain.cfg.hardware.has_limit_switch = False
        with pytest.raises(RuntimeError, match="limit switch"):
            brain.measure_position(1)
    finally:
        brain.shutdown()


def test_a_leftover_jog_at_start_is_stopped_as_a_safety_interlock():
    """The ONE state change start-up makes on purpose: a JA left running by a
    crashed session (still in remote mode) has no end point and no dead-man,
    so after start_wait_s it is stopped -- and that write is recorded."""
    brain, sim = _used_brain(start_wait_s=0.2)
    sim.jog(1, 1)                                           # X jogging, no owner
    calls = _recording(sim)
    brain.start()
    try:
        assert calls == ["stop", "enable_remote"]           # ST first, then MR
        assert sim.axis_state(1) == READY
        assert len(brain.startup_writes) == 2
        assert brain.startup_writes[0].startswith("1ST")
        assert brain.status().startup_writes == brain.startup_writes
    finally:
        brain.shutdown()


def test_a_jog_that_cannot_be_stopped_fails_the_start_cleanly():
    """Jogging in LOCAL mode = someone holds a push button: ST is refused
    (-5), so start gives up with a clear message instead of fighting them."""
    brain, sim = _used_brain(start_wait_s=0.1)
    sim.jog(2, -1)
    sim._remote = False                                     # buttons active
    with pytest.raises(RuntimeError, match="push button"):
        brain.start()
    assert not brain.status().connected
    assert sim.axis_state(2) != READY                       # left as it was found
