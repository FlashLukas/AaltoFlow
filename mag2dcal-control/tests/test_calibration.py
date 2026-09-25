"""The measured calibration: the data model, the sweep, and what it buys.

Three things are checked here:
  * the lookup tables interpolate and clamp, and the two legs stay apart;
  * a sweep on the simulator recovers the simulator's own gain and hysteresis --
    if it did not, the calibration would be measuring the controller instead of
    the magnet;
  * the calibrated JUMP alone lands within a couple of millitesla, i.e. the
    calibration really is doing the coarse work and the PI really is only
    trimming.
"""

import json

import pytest

from mag2dcal.backends.sim import FakeClock
from mag2dcal.calibration import (Calibration, AxisCalibration, build_calibration,
                                  leg_name, newest_calibration, sweep_plan, UP, DOWN)
from mag2dcal.config import Config
from mag2dcal.controller import Refused
from mag2dcal.net.protocol import calibration_from_dict, calibration_to_dict
from mag2dcal.sim_system import build_sim_system


# ------------------------------------------------------------------ the model

def _toy_axis(h=0.4, gain=20.0, v=5.0, n=11):
    """A synthetic axis: B = gain*V +- h, exactly, no noise."""
    up = [(-v + 2 * v * i / (n - 1), gain * (-v + 2 * v * i / (n - 1)) + h)
          for i in range(n)]
    down = [(V, B - 2 * h) for V, B in up]
    return AxisCalibration(up=up, down=down)


def test_leg_name_maps_directions_and_names():
    assert leg_name(1) == UP and leg_name(+0.5) == UP and leg_name(0) == UP
    assert leg_name(-1) == DOWN and leg_name("down") == DOWN
    assert leg_name("UP") == UP


def test_the_two_legs_answer_differently_and_by_the_hysteresis():
    a = _toy_axis(h=0.4)
    assert a.field_for_volts(1.0, UP) == pytest.approx(20.4)
    assert a.field_for_volts(1.0, DOWN) == pytest.approx(19.6)
    # The inverse lookup is what the seek actually uses: to reach 20 mT you need
    # LESS voltage coming up than coming down.
    assert a.volts_for_field(20.0, UP) < a.volts_for_field(20.0, DOWN)
    assert a.volts_for_field(20.0, UP) == pytest.approx(0.98)
    assert a.volts_for_field(20.0, DOWN) == pytest.approx(1.02)


def test_interpolation_is_linear_between_points_and_clamped_outside():
    a = _toy_axis(h=0.0, n=3)                 # points at -5, 0, +5 V
    assert a.field_for_volts(2.5, UP) == pytest.approx(50.0)
    assert a.field_for_volts(99.0, UP) == pytest.approx(100.0)    # clamped
    assert a.field_for_volts(-99.0, UP) == pytest.approx(-100.0)  # clamped
    assert a.volts_for_field(1e6, UP) == pytest.approx(5.0)


def test_one_measured_leg_is_better_than_none():
    a = AxisCalibration(up=_toy_axis().up, down=[])
    assert a.field_for_volts(1.0, DOWN) == a.field_for_volts(1.0, UP)


def test_duplicate_points_do_not_break_the_inverse_lookup():
    """Noise on a flat piece of curve can produce two samples with the same
    field; bisect would divide by zero on them."""
    a = AxisCalibration(up=[(0.0, 0.0), (1.0, 20.0), (2.0, 20.0), (3.0, 60.0)],
                        down=[])
    assert a.volts_for_field(20.0, UP) == pytest.approx(1.0)
    assert a.volts_for_field(40.0, UP) == pytest.approx(2.0)


def test_field_envelope_is_the_weakest_half_range_of_either_axis():
    """A vector setpoint needs BOTH axes, so |B| at any angle is limited by the
    weakest of the four half-ranges."""
    cal = Calibration(axes=[_toy_axis(gain=20.0), _toy_axis(gain=10.0)])
    assert cal.field_max_mT() == pytest.approx(49.6, abs=0.1)     # 10 * 5 - h


def test_calibration_round_trips_through_dict_json_and_file(tmp_path):
    cal = Calibration(axes=[_toy_axis(), _toy_axis(gain=19.4)], note="unit test")
    d = cal.to_dict()
    assert json.loads(json.dumps(d)) == d          # JSON-safe, no NaN
    back = Calibration.from_dict(d)
    assert back.to_dict() == d

    path = tmp_path / "cal.json"
    cal.save(path)
    assert Calibration.load(path).to_dict() == d
    assert newest_calibration(str(tmp_path)) == path

    # and over the wire, which is the same shape on purpose
    assert calibration_from_dict(calibration_to_dict(cal)).to_dict() == d
    assert calibration_to_dict(None) is None
    assert calibration_to_dict(Calibration()) is None            # empty -> None
    assert calibration_from_dict(None) is None


def test_empty_calibration_is_recognised_as_empty():
    assert Calibration().is_empty
    assert not Calibration(axes=[_toy_axis(), AxisCalibration()]).is_empty


# ------------------------------------------------------------------ the sweep

def test_sweep_plan_shape_and_over_travel():
    plan = sweep_plan(5, 4.0, limit_V=10.0)
    assert len(plan) == 2 * (2 * 5 + 3)
    axis0 = [s for s in plan if s.axis == 0]
    # primed one leg step (2 V) past +v_max
    assert axis0[0].volts == pytest.approx(6.0) and not axis0[0].record
    down = [s for s in axis0 if s.leg == DOWN]
    up = [s for s in axis0 if s.leg == UP]
    assert [s.volts for s in down] == pytest.approx([4, 2, 0, -2, -4])
    assert [s.volts for s in up] == pytest.approx([-4, -2, 0, 2, 4])
    assert all(s.record for s in down + up)
    # the turn between the legs goes PAST -v_max, so the up leg's first point is
    # reached with the drive already rising (the right branch)
    turn = axis0[1 + 5]
    assert turn.volts == pytest.approx(-6.0) and not turn.record
    assert axis0[-1].volts == 0.0                                # parked


def test_sweep_plan_respects_the_amplifier_limit():
    plan = sweep_plan(5, 10.0, limit_V=10.0)
    assert max(abs(s.volts) for s in plan) == pytest.approx(10.0)


def test_build_calibration_sorts_each_leg_by_voltage():
    pts = [(0, DOWN, 2.0, 40.0), (0, DOWN, -2.0, -40.0), (0, UP, 1.0, 20.0),
           (1, UP, -1.0, -19.4)]
    cal = build_calibration(pts)
    assert [v for v, _ in cal.axes[0].down] == [-2.0, 2.0]
    assert cal.created and cal.axes[1].up == [(-1.0, -19.4)]


# --------------------------------------------------- the sweep on the magnet

@pytest.fixture
def rig(tmp_path):
    cfg = Config()
    cfg.calibration.directory = str(tmp_path)
    clock = FakeClock()
    ctrl, sim = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=5)
    ctrl.start(run_thread=False)
    return cfg, clock, ctrl, sim


def _run_sweep(ctrl, clock, limit_s=900.0, **kw):
    ctrl.calibrate(**kw)
    dt = 1.0 / ctrl.cfg.control.loop_hz
    t0 = clock()
    while clock() - t0 < limit_s:
        clock.advance(dt)
        ctrl.tick()
        if ctrl.status().state != "CALIBRATE":
            return clock() - t0
    raise AssertionError("the calibration never finished")


def test_a_sweep_recovers_the_simulators_gain_and_hysteresis(rig, tmp_path):
    cfg, clock, ctrl, sim = rig
    _run_sweep(ctrl, clock, n_per_leg=11, dwell_s=0.5, v_max=5.0)
    cal = ctrl.get_calibration()
    assert ctrl.is_calibrated and cal.n_points == 2 * 2 * 11

    for axis, gain in ((0, cfg.sim.gain_x_mT_per_V), (1, cfg.sim.gain_y_mT_per_V)):
        a = cal.axes[axis]
        # the slope is the simulator's gain, to about a percent
        slope = (a.field_for_volts(4.0, UP) - a.field_for_volts(-4.0, UP)) / 8.0
        assert slope == pytest.approx(gain, rel=0.02), axis
        # and the two legs are 2h apart, which is the whole reason for measuring
        # them separately
        gap = a.field_for_volts(0.0, UP) - a.field_for_volts(0.0, DOWN)
        assert gap == pytest.approx(2 * cfg.sim.hysteresis_mT, abs=0.2), axis

    # the coils are parked and the loop has the magnet back
    assert sim.ao == [0.0, 0.0]
    assert ctrl.status().state in ("SEEK", "HOLD", "STABLE")
    assert ctrl.status().setpoint_field_mT == 0.0
    # auto-saved, and the service would pick it up again at the next start
    saved = list(tmp_path.glob("*.json"))
    assert len(saved) == 1 and Calibration.load(saved[0]).n_points == cal.n_points


def test_the_limits_follow_the_calibration(rig):
    cfg, clock, ctrl, sim = rig
    assert ctrl.field_envelope_mT() == cfg.limits.field_max_mT     # 180, uncalibrated
    _run_sweep(ctrl, clock, n_per_leg=7, dwell_s=0.5, v_max=3.0)
    envelope = ctrl.field_envelope_mT()
    # 3 V at ~19.4-20 mT/V, so about 58 mT -- and far below the configured 180
    assert 50.0 < envelope < 65.0, envelope
    ctrl.set_field(150.0)                # outside the measured range now
    assert ctrl.status().setpoint_field_mT == pytest.approx(envelope)


def test_the_jump_alone_lands_within_a_few_mT(rig):
    """The point of the calibration: almost all of the move happens open-loop.

    The trim is only allowed to start once the jump has finished and the coil has
    settled, so the field at that moment is what the calibration ALONE achieved.
    """
    cfg, clock, ctrl, sim = rig
    _run_sweep(ctrl, clock, n_per_leg=21, dwell_s=0.5, v_max=5.0)
    dt = 1.0 / cfg.control.loop_hz
    ctrl.set_field(60.0, 0.0)
    for _ in range(3000):
        clock.advance(dt)
        ctrl.tick()
        if ctrl._seek[0].phase in ("trim", "frozen"):
            break
    bx, _ = sim.true_field()
    # The jump aims field_step_mT short on purpose, so "landed well" means close
    # to the UNDERSHOT target, not to the target.
    assert bx == pytest.approx(60.0 - cfg.control.field_step_mT, abs=1.0), bx


def test_calibrate_is_refused_when_it_would_be_unsafe(rig):
    cfg, clock, ctrl, sim = rig
    ctrl.set_output(False)
    for _ in range(400):
        clock.advance(0.02)
        ctrl.tick()
    with pytest.raises(Refused):
        ctrl.calibrate()                       # not energized
    ctrl.set_output(True)
    clock.advance(0.02)
    ctrl.tick()
    ctrl.calibrate(n_per_leg=5, dwell_s=0.1, v_max=2.0)
    with pytest.raises(Refused):
        ctrl.calibrate()                       # already running
    with pytest.raises(Refused):
        ctrl.set_field(10.0)                   # no setpoints during a sweep
    with pytest.raises(ValueError):
        ctrl._cal_job = None
        ctrl.calibrate(n_per_leg=1)


def test_zero_aborts_a_sweep_and_leaves_the_coils_safe(rig):
    cfg, clock, ctrl, sim = rig
    ctrl.calibrate(n_per_leg=21, dwell_s=0.5, v_max=5.0)
    for _ in range(200):                       # part-way into the first leg
        clock.advance(0.02)
        ctrl.tick()
    assert ctrl.status().state == "CALIBRATE"
    assert 0.0 < ctrl.status().calibration_progress < 1.0
    assert abs(sim.ao[0]) > 1.0                # the magnet really is driven
    ctrl.zero()
    for _ in range(600):
        clock.advance(0.02)
        ctrl.tick()
    s = ctrl.status()
    assert s.state in ("SEEK", "HOLD", "STABLE") and s.setpoint_field_mT == 0.0
    assert abs(sim.true_field()[0]) < 1.0
    assert not ctrl.is_calibrated              # a part-done sweep is not a curve


def test_a_fault_abandons_a_sweep(rig):
    cfg, clock, ctrl, sim = rig
    ctrl.calibrate(n_per_leg=21, dwell_s=0.5, v_max=5.0)
    for _ in range(100):
        clock.advance(0.02)
        ctrl.tick()
    sim.p.water_ok = False
    for _ in range(600):
        clock.advance(0.02)
        ctrl.tick()
    s = ctrl.status()
    assert s.state == "FAULT" and s.output_V == [0.0, 0.0] and not s.energized
    assert s.calibration_progress == 0.0


def test_setting_a_calibration_re_clamps_a_standing_setpoint(rig):
    cfg, clock, ctrl, sim = rig
    ctrl.set_field(150.0, 0.0)
    assert ctrl.status().setpoint_field_mT == 150.0
    ctrl.set_calibration(Calibration(axes=[_toy_axis(gain=20.0, v=2.0),
                                           _toy_axis(gain=20.0, v=2.0)]))
    assert ctrl.status().setpoint_field_mT == pytest.approx(ctrl.field_envelope_mT())
    ctrl.set_calibration(None)
    assert not ctrl.is_calibrated
    assert ctrl.field_envelope_mT() == cfg.limits.field_max_mT
