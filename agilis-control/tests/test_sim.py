"""The simulator's physics: the brain's honesty depends on it keeping the
counter and the true position apart."""

import time

import pytest

from agilis.backends.base import READY, STEPPING, AgilisBackend
from agilis.backends.sim import TRAVEL_UM, SimAgilis
from agilis.config import Config


def _sim():
    s = SimAgilis(Config())
    s.open()
    s.pr_rate = 50000.0
    return s


def _run(s, axis, n):
    s.move_by(axis, n)
    t0 = time.monotonic()
    while s.axis_state(axis) != READY and time.monotonic() - t0 < 3:
        time.sleep(0.005)


def test_it_is_a_backend():
    assert isinstance(SimAgilis(Config()), AgilisBackend)


def test_step_size_grows_with_amplitude_and_has_a_threshold():
    s = _sim()
    sizes = [s.step_size_um(0, +1, a) for a in (1, 4, 10, 16, 30, 50)]
    assert sizes[0] == 0.0 and sizes[1] == 0.0           # below threshold: no motion
    assert all(b > a for a, b in zip(sizes[2:], sizes[3:]))
    assert 0.02 < s.step_size_um(0, +1, 16) < 0.1        # ~50 nm at the default


def test_forward_and_backward_differ_and_counter_is_not_position():
    s = _sim()
    x0 = s.true_um(1)
    _run(s, 1, 2000)
    _run(s, 1, -2000)
    assert s.read_position(1) == 0                       # the counter is back...
    assert abs(s.true_um(1) - x0) > 5.0                  # ...the stage is not


def test_no_motion_below_threshold_but_steps_still_count():
    s = _sim()
    s.set_amplitude(2, +1, 2)
    y0 = s.true_um(2)
    _run(s, 2, 500)
    assert s.read_position(2) == 500
    assert s.true_um(2) == y0


def test_end_stop_and_limit_switch():
    s = _sim()
    s.set_amplitude(1, +1, 50)
    _run(s, 1, 200000)                                   # far past 12 mm
    assert s.true_um(1) == pytest.approx(TRAVEL_UM / 2)
    assert s.limit_status() & 1
    assert not s.limit_status() & 2


def test_controller_refusals():
    s = SimAgilis(Config())
    with pytest.raises(RuntimeError, match="-5"):
        s.move_by(1, 10)                                 # local mode before open
    s.open()
    s.pr_rate = 10.0
    s.move_by(1, 100)
    assert s.axis_state(1) == STEPPING
    with pytest.raises(RuntimeError, match="-6"):
        s.set_amplitude(1, +1, 20)                        # SU only at rest
    with pytest.raises(RuntimeError, match="-6"):
        s.move_by(1, 10)
    s.stop(1)
    with pytest.raises(RuntimeError, match="-4"):
        s.set_amplitude(1, +1, 51)
    with pytest.raises(RuntimeError, match="-2"):
        s.read_position(3)


def test_jog_speeds_and_max_amplitude():
    s = _sim()
    s.jog(1, 3)                                          # 1700 steps/s
    time.sleep(0.2)
    n = s.read_position(1)
    s.jog(1, 0)
    assert 200 < n < 500
    assert s.axis_state(1) == READY
