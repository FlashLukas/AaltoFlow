"""Software-paced step rate (motion.step_rate, verb set_step_rate).

The AG-UC2 has no rate setting for a step move (PR), so a slower rate is made
by the brain: the move goes out in PR chunks from the poll thread, each only
when the previous one has finished. These tests count the PR commands, time
the move, and check that every step still lands exactly where it should.
"""

import math
import time

import pytest

from agilis.agilis import STEP_RATE_MAX
from agilis.config import Config
from agilis.sim_system import build_sim_system

CMD, PUB = 17176, 17177


def _started(pr_rate=20000.0, poll_hz=50):
    cfg = Config()
    cfg.hardware.poll_hz = poll_hz
    brain, sim = build_sim_system(cfg)
    sim.pr_rate = pr_rate
    calls = []
    real = sim.move_by

    def counting(hw_axis, delta):          # every PR the brain sends
        calls.append((hw_axis, int(delta)))
        return real(hw_axis, delta)

    sim.move_by = counting
    brain.start()
    return brain, sim, calls


def _wait_idle(brain, axis, timeout=10.0):
    time.sleep(0.08)
    t0 = time.monotonic()
    while brain.status().moving[axis] and time.monotonic() - t0 < timeout:
        time.sleep(0.01)
    return time.monotonic() - t0


def test_rate_zero_is_one_pr_at_the_controllers_rate():
    brain, sim, calls = _started()
    try:
        assert brain.status().step_rate == 0.0
        brain.move_steps(0, 300)
        _wait_idle(brain, 0)
        assert calls == [(1, 300)]
        assert sim.read_position(1) == 300
    finally:
        brain.shutdown()


@pytest.mark.parametrize("delta", [60, -60])
def test_a_paced_move_takes_the_time_the_rate_says_and_lands_exactly(delta):
    brain, sim, calls = _started()
    try:
        assert brain.set_step_rate(100) == 100.0
        t0 = time.monotonic()
        brain.move_steps(0, delta)
        time.sleep(0.15)
        st = brain.status()
        assert st.moving[0] and st.paced[0] and st.step_rate == 100.0
        assert 0 < abs(sim.read_position(1)) < abs(delta)      # part of the way
        _wait_idle(brain, 0)
        took = time.monotonic() - t0
        # 60 steps at 100 steps/s: ~0.6 s (first step at once), not 0.003 s
        assert 0.4 < took < 2.0, took
        assert sim.read_position(1) == delta                    # every step counted
        assert len(calls) > 5                                   # sent in chunks
        assert all(math.copysign(1, d) == math.copysign(1, delta) for _, d in calls)
        assert sum(d for _, d in calls) == delta
        st = brain.status()
        assert not st.paced[0] and st.target_steps[0] == delta and st.hw_error == ""
    finally:
        brain.shutdown()


def test_a_rate_above_the_controller_never_meets_a_busy_axis():
    """The sim refuses PR while an axis steps (like the AG-UC2): a rate far
    above the controller's just runs flat out, with no refused chunk."""
    brain, sim, calls = _started(pr_rate=200.0)
    try:
        brain.set_step_rate(STEP_RATE_MAX)
        brain.move_steps(1, 120)
        errors = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5.0:
            st = brain.status()
            if st.hw_error:
                errors.append(st.hw_error)
            if not st.moving[1] and time.monotonic() - t0 > 0.2:
                break
            time.sleep(0.01)
        assert errors == []
        assert sim.read_position(2) == 120
    finally:
        brain.shutdown()


def test_stop_ends_a_paced_move_and_no_chunk_follows():
    brain, sim, calls = _started()
    try:
        brain.set_step_rate(50)
        brain.move_steps(0, 200)
        time.sleep(0.3)
        brain.stop(0)
        n, pos = len(calls), sim.read_position(1)
        time.sleep(0.3)
        assert len(calls) == n and sim.read_position(1) == pos
        assert 0 < pos < 200
        st = brain.status()
        assert not st.paced[0] and not st.moving[0]
    finally:
        brain.shutdown()


def test_a_new_move_replaces_a_paced_one():
    brain, sim, _ = _started()
    try:
        brain.set_step_rate(50)
        brain.move_to_step(0, 500)
        time.sleep(0.2)
        brain.set_step_rate(0)                  # the next move at full rate
        brain.move_to_step(0, -40)
        _wait_idle(brain, 0)
        assert sim.read_position(1) == -40
        assert not brain.status().paced[0]
    finally:
        brain.shutdown()


def test_datum_and_amplitude_wait_for_a_paced_move():
    """Between two chunks the axis reads READY -- it is still moving."""
    brain, _, _ = _started()
    try:
        brain.set_step_rate(20)
        brain.move_steps(0, 100)
        time.sleep(0.15)
        with pytest.raises(RuntimeError, match="moving"):
            brain.zero_counter(0)
        with pytest.raises(RuntimeError, match="at rest"):
            brain.set_amplitude(0, 30)
        brain.stop(0)
    finally:
        brain.shutdown()


def test_set_step_rate_bounds():
    brain, _, _ = _started()
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    try:
        assert brain.set_step_rate(-5) == 0.0
        assert brain.set_step_rate(float("nan")) == 0.0
        assert brain.set_step_rate(1e9) == STEP_RATE_MAX
        assert any(level == "warn" and "clamped" in msg for level, msg in events)
        assert brain.cfg.motion.step_rate == STEP_RATE_MAX
    finally:
        brain.shutdown()


def test_step_rate_over_the_wire_and_in_describe():
    from agilis.net.client import AgilisClient
    from agilis.net.describe import build_manifest
    from agilis.net.service import AgilisService

    cfg = Config()
    cfg.hardware.poll_hz = 50
    brain, sim = build_sim_system(cfg)
    sim.pr_rate = 20000.0
    svc = AgilisService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = AgilisClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    try:
        assert cli.set_step_rate(250) == 250.0
        t0 = time.monotonic()
        while cli.status().step_rate != 250.0 and time.monotonic() - t0 < 3:
            time.sleep(0.02)
        assert cli.status().step_rate == 250.0
        assert cli.get_config()["motion"]["step_rate"] == 250.0
        p = {d["id"]: d for d in build_manifest(brain)["parameters"]}["step_rate"]
        assert p["kind"] == "control" and p["unit"] == "steps/s"
        assert p["set"] == {"verb": "set_step_rate", "arg": "rate"}
        assert p["min"] == 0.0 and p["max"] == STEP_RATE_MAX
    finally:
        cli.close()
        svc.stop()
        time.sleep(0.1)
