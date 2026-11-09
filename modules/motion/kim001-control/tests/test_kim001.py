"""Brain behaviour (section 9): calibration bridge, clamping, lifecycle, both
zeros -- on the ONE axis a KIM001 has (Z, index 2) -- and the refusal of X / Y."""

import time

import pytest

from kim001.config import Config, NotFitted
from kim001.sim_system import build_sim_system

Z = 2


def _settle(brain, axis=Z, timeout=6.0):
    t0 = time.monotonic()
    while brain.status().moving[axis] and time.monotonic() - t0 < timeout:
        time.sleep(0.01)


def _fast(brain):
    brain.set_step_rate(Z, 2000)


def test_lifecycle_and_step_move():
    brain, _ = build_sim_system(Config())
    brain.start()
    assert brain.status().connected
    _fast(brain)
    brain.move_to_step(Z, 4000)
    _settle(brain)
    assert brain.status().position_steps[Z] == 4000
    brain.shutdown()
    assert not brain.status().connected
    brain.shutdown()  # idempotent


def test_x_and_y_are_refused_with_a_clear_error_and_read_as_not_fitted():
    """THE contract of this module: kim's verbs, but only Z exists. Every
    per-axis verb refuses X / Y saying so, and status shows them as constant,
    at rest, with no travel."""
    brain, backend = build_sim_system(Config())
    brain.start()
    calls = [
        lambda a: brain.move_to_step(a, 10),
        lambda a: brain.move_steps(a, 10),
        lambda a: brain.move_to_um(a, 1.0),
        lambda a: brain.move_relative_um(a, 1.0),
        lambda a: brain.stop(a),
        lambda a: brain.zero_counter(a),
        lambda a: brain.set_zero(a),
        lambda a: brain.clear_zero(a),
        lambda a: brain.set_step_rate(a, 500),
        lambda a: brain.set_velocity_um(a, 5.0),
        lambda a: brain.set_acceleration(a, 1000),
        lambda a: brain.set_voltage(a, 100),
        lambda a: brain.set_calibration(a, 0.03, +1),
    ]
    for axis in (0, 1):
        for call in calls:
            with pytest.raises(NotFitted, match="not fitted on a KIM001"):
                call(axis)
    with pytest.raises(NotFitted):
        brain.move_image_px(10, 0)
    with pytest.raises(NotFitted):
        brain.start_px_calibration()
    assert brain.get_px_calibration() is None
    brain.abort_px_calibration()                 # a safety verb: never raises
    assert backend.writes == []                  # nothing reached the "controller"

    st = brain.status()
    assert st.fitted == [False, False, True]
    assert st.position_steps[:2] == [0, 0] and st.position_um[:2] == [0.0, 0.0]
    assert st.moving[:2] == [False, False]
    assert st.limit_lo[:2] == [0, 0] and st.limit_hi[:2] == [0, 0]
    assert st.target_um[:2] == [0.0, 0.0]
    assert len(st.um_per_step) == len(st.um_per_step_fwd) == len(st.voltage) == 3
    brain.shutdown()


def test_all_axes_verbs_act_on_z_only():
    """stop / datum / zero with no axis = everything there is: the focus."""
    brain, backend = build_sim_system(Config())
    brain.start()
    _fast(brain)
    brain.move_to_step(Z, 300)
    _settle(brain)
    assert brain.set_zero_all() == [0, 0, 300]
    brain.zero_counter_all()
    assert brain.status().position_steps == [0, 0, 0]
    brain.stop_all()
    brain.clear_zero_all()
    brain.shutdown()


def test_calibration_bridge_um_to_steps():
    cfg = Config()
    cfg.calibration.um_per_step_z = 0.02   # 20 nm/step
    brain, _ = build_sim_system(cfg)
    brain.start()
    _fast(brain)
    # 100 um / 0.02 = 5000 steps
    brain.move_to_um(Z, 100.0)
    _settle(brain)
    assert brain.status().position_steps[Z] == 5000
    assert abs(brain.status().position_um[Z] - 100.0) < 1e-9
    brain.shutdown()


def test_relative_um_is_incremental_and_directional():
    cfg = Config()
    cfg.calibration.um_per_step_z = 0.02
    brain, _ = build_sim_system(cfg)
    brain.start()
    _fast(brain)
    brain.move_to_um(Z, 20.0)      # 1000 steps
    _settle(brain)
    brain.move_relative_um(Z, 10.0)  # +500 steps -> 1500
    _settle(brain)
    assert brain.status().position_steps[Z] == 1500
    assert abs(brain.status().position_um[Z] - 30.0) < 1e-9
    # down steps measured 2x bigger: -10 um is only 250 steps
    brain.set_calibration(Z, 0.04, -1)
    brain.move_relative_um(Z, -10.0)
    _settle(brain)
    assert brain.status().position_steps[Z] == 1250
    brain.shutdown()


def test_velocity_um_converts_and_clamps():
    cfg = Config()
    cfg.calibration.um_per_step_z = 0.02
    cfg.limits.max_step_rate = 2000.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    # 20 um/s -> 1000 steps/s
    actual = brain.set_velocity_um(Z, 20.0)
    assert abs(brain.status().step_rate[Z] - 1000.0) < 1e-9
    assert abs(actual - 20.0) < 1e-9
    # 100 um/s would be 5000 steps/s -> clamps to 2000 steps/s = 40 um/s
    actual = brain.set_velocity_um(Z, 100.0)
    assert abs(brain.status().step_rate[Z] - 2000.0) < 1e-9
    assert abs(actual - 40.0) < 1e-9
    brain.shutdown()


def test_step_target_clamping_both_ends():
    cfg = Config()
    cfg.limits.min_steps_z, cfg.limits.max_steps_z = 0, 10_000
    events = []
    brain, _ = build_sim_system(cfg)
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    assert brain.move_to_step(Z, 99_999) == 10_000   # clamp high
    assert brain.move_to_step(Z, -50) == 0            # clamp low
    assert any(level == "warn" for level, _ in events)
    brain.shutdown()


def test_clamp_can_be_disabled_but_x_y_stay_refused():
    cfg = Config()
    cfg.limits.enforce = False
    cfg.limits.max_steps_z = 10_000
    brain, _ = build_sim_system(cfg)
    brain.start()
    assert brain.move_to_step(Z, 50_000) == 50_000
    with pytest.raises(NotFitted):
        brain.move_to_step(0, 5)          # refused before any clamp is asked
    brain.shutdown()


def test_parameter_clamps():
    cfg = Config()
    cfg.limits.max_step_rate = 2000.0
    cfg.limits.max_acceleration = 100000.0
    cfg.limits.min_voltage, cfg.limits.max_voltage = 85.0, 125.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    assert brain.set_step_rate(Z, 9999) == 2000.0
    assert brain.set_step_rate(Z, 0) == 1.0             # floor at 1
    assert brain.set_acceleration(Z, 10**9) == 100000.0
    assert brain.set_voltage(Z, 200) == 125.0
    assert brain.set_voltage(Z, 10) == 85.0
    brain.shutdown()


def test_calibration_must_be_positive():
    brain, _ = build_sim_system(Config())
    brain.start()
    with pytest.raises(ValueError):
        brain.set_calibration(Z, 0.0)
    with pytest.raises(ValueError):
        brain.set_calibration(Z, -1.0)
    # a valid one sticks and is reflected in the status
    brain.set_calibration(Z, 0.03)
    assert abs(brain.status().um_per_step[Z] - 0.03) < 1e-12
    assert brain.status().um_per_step_src[Z] == "config"
    brain.shutdown()


def test_move_steps_incremental_and_clamped():
    cfg = Config()
    cfg.limits.max_steps_z = 1000
    brain, _ = build_sim_system(cfg)
    brain.start()
    _fast(brain)
    brain.move_to_step(Z, 800)
    _settle(brain)
    target = brain.move_steps(Z, 500)   # 800 + 500 = 1300 -> clamps to 1000
    assert target == 1000
    brain.shutdown()


def test_datum_vs_display_zero():
    brain, _ = build_sim_system(Config())
    brain.start()
    _fast(brain)
    brain.move_to_step(Z, 3000)
    _settle(brain)
    # display zero: read-out re-references, absolute counter unchanged
    brain.set_zero(Z)
    st = brain.status()
    assert st.rel_steps[Z] == 0
    assert st.position_steps[Z] == 3000
    assert st.rel_origin[Z] == 3000
    # hardware datum: absolute counter itself resets to 0
    brain.zero_counter(Z)
    st = brain.status()
    assert st.position_steps[Z] == 0
    assert st.rel_origin[Z] == 0     # datum also clears the display origin
    assert st.target_um[Z] == 0.0    # the echo target follows the new frame
    brain.shutdown()


def test_apply_config_repushes_params():
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    cfg.motion.rate_z = 750.0
    cfg.motion.voltage_z = 120.0
    brain.apply_config()
    st = brain.status()
    assert abs(st.step_rate[Z] - 750.0) < 1e-9
    assert abs(st.voltage[Z] - 120.0) < 1e-9
    brain.shutdown()


def test_leash_limits_travel_around_datum():
    cfg = Config()
    cfg.limits.leash_enabled = True
    cfg.limits.leash_z = 500
    brain, _ = build_sim_system(cfg)
    brain.start()
    _fast(brain)
    assert brain.move_to_step(Z, 9999) == 500
    assert brain.move_to_step(Z, -9999) == -500
    # incremental moves are leashed too
    assert brain.move_steps(Z, 10_000) == 500
    # status reports the symmetric effective bounds + leash flag
    st = brain.status()
    assert st.leash is True
    assert st.limit_lo[Z] == -500 and st.limit_hi[Z] == 500
    assert st.leash_half == [0, 0, 500]
    brain.shutdown()


def test_set_leash_toggles_and_resizes():
    brain, _ = build_sim_system(Config())
    brain.start()
    assert brain.status().leash is False           # off by default (as kim)
    absolute_hi = brain.cfg.limits.max_steps_z
    # leash_xy is accepted (kim's wire shape) but limits nothing here
    state = brain.set_leash(enabled=True, leash_xy=2000, leash_z=300)
    assert state == {"enabled": True, "leash_xy": 2000, "leash_z": 300}
    st = brain.status()
    assert st.leash and st.limit_hi[Z] == 300 and st.limit_hi[0] == 0
    # toggling off keeps the range but restores the absolute clamp
    brain.set_leash(enabled=False)
    st = brain.status()
    assert not st.leash
    assert st.limit_hi[Z] == absolute_hi == 650_000
    brain.shutdown()


def test_default_limits_symmetric_allow_negative():
    brain, _ = build_sim_system(Config())
    brain.start()
    _fast(brain)
    st = brain.status()
    assert st.limit_lo[Z] == -650_000 and st.limit_hi[Z] == 650_000
    # a negative absolute target is allowed (the datum is arbitrary)
    assert brain.move_to_step(Z, -500) == -500
    _settle(brain)
    assert brain.status().position_steps[Z] == -500
    assert brain.move_steps(Z, -100) == -600
    brain.shutdown()


def test_changing_rate_midmove_does_not_jump():
    # regression (kim-control): a rate change during a move must not teleport
    # the simulated position.
    brain, _ = build_sim_system(Config())
    brain.start()
    brain.set_step_rate(Z, 1000)
    brain.move_to_step(Z, 600_000)       # far target -> still moving
    time.sleep(0.2)
    p1 = brain.status().position_steps[Z]
    brain.set_step_rate(Z, 2000)         # speed up mid-move
    p2 = brain.status().position_steps[Z]
    assert p2 >= p1                       # never runs backwards
    assert (p2 - p1) < 60                 # continuous, not a ~2x jump (~+200)
    brain.shutdown()


def test_speed_preset_applies_to_the_focus_axis():
    cfg = Config()
    cfg.motion.fast_rate, cfg.motion.slow_rate = 1500.0, 300.0
    cfg.motion.fast_accel, cfg.motion.slow_accel = 20000.0, 5000.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    state = brain.set_speed(False)
    assert state["fast"] is False and state["rate"] == 300.0
    st = brain.status()
    assert st.speed_fast is False
    assert abs(st.step_rate[Z] - 300.0) < 1e-6 and abs(st.acceleration[Z] - 5000.0) < 1e-6
    brain.set_speed(True)
    st = brain.status()
    assert st.speed_fast and abs(st.step_rate[Z] - 1500.0) < 1e-6
    assert st.step_rate[:2] == [0.0, 0.0]          # X / Y untouched (not fitted)
    brain.shutdown()


def test_step_size_preset_uses_voltage_extremes():
    cfg = Config()
    cfg.limits.min_voltage, cfg.limits.max_voltage = 85.0, 125.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    state = brain.set_step_size(True)          # large steps -> max voltage
    assert state["large"] and state["voltage"] == 125.0
    st = brain.status()
    assert st.step_large and abs(st.voltage[Z] - 125.0) < 1e-6
    brain.set_step_size(False)                 # small steps -> min voltage
    st = brain.status()
    assert not st.step_large and abs(st.voltage[Z] - 85.0) < 1e-6
    brain.shutdown()


def test_position_list_store_goto(tmp_path):
    brain, backend = build_sim_system(Config())
    brain.start()
    _fast(brain)
    brain.move_to_step(Z, 300)
    _settle(brain)

    brain.store_position(5, "focus")
    p = brain.get_positions()[5]
    assert p["used"] and p["name"] == "focus"
    assert (int(p["x"]), int(p["y"]), int(p["z"])) == (0, 0, 300)

    brain.move_to_step(Z, 0)
    _settle(brain)
    assert brain.goto_position(5) == [0, 0, 300]
    _settle(brain)
    assert brain.status().position_steps[Z] == 300
    # already there: no zero-length move is sent
    n = backend.writes.count("move_to")
    assert brain.goto_position(5) == [0, 0, 300]
    assert backend.writes.count("move_to") == n
    brain.shutdown()


def test_position_list_save_load(tmp_path):
    brain, _ = build_sim_system(Config())
    brain.store_position(0, "a")
    brain.store_position(1, "b")
    path = tmp_path / "pos.json"
    brain.save_positions(str(path))

    brain2, _ = build_sim_system(Config())
    brain2.load_positions(str(path))
    got = brain2.get_positions()
    assert got[0]["name"] == "a" and got[0]["used"]
    assert got[1]["name"] == "b" and got[1]["used"]
    assert not got[2]["used"]
