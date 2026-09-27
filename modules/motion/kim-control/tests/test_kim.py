"""Brain behaviour (§9): calibration bridge, clamping, lifecycle, both zeros."""

import time

import pytest

from kim.config import Config
from kim.sim_system import build_sim_system


def _settle(brain, axis, timeout=6.0):
    t0 = time.monotonic()
    while brain.status().moving[axis] and time.monotonic() - t0 < timeout:
        time.sleep(0.01)


def _fast(brain):
    for a in range(3):
        brain.set_step_rate(a, 2000)


def test_lifecycle_and_step_move():
    brain, _ = build_sim_system(Config())
    brain.start()
    assert brain.status().connected
    _fast(brain)
    brain.move_to_step(0, 4000)
    _settle(brain, 0)
    assert brain.status().position_steps[0] == 4000
    brain.shutdown()
    assert not brain.status().connected
    brain.shutdown()  # idempotent


def test_calibration_bridge_um_to_steps():
    cfg = Config()
    cfg.calibration.um_per_step_x = 0.02   # 20 nm/step
    cfg.calibration.um_per_step_y = 0.05   # different per axis
    brain, _ = build_sim_system(cfg)
    brain.start()
    _fast(brain)

    # 100 um / 0.02 = 5000 steps
    brain.move_to_um(0, 100.0)
    _settle(brain, 0)
    assert brain.status().position_steps[0] == 5000
    assert abs(brain.status().position_um[0] - 100.0) < 1e-9

    # axis Y uses its own calibration: 100 um / 0.05 = 2000 steps
    brain.move_to_um(1, 100.0)
    _settle(brain, 1)
    assert brain.status().position_steps[1] == 2000
    brain.shutdown()


def test_relative_um_is_incremental():
    cfg = Config()
    cfg.calibration.um_per_step_x = 0.02
    brain, _ = build_sim_system(cfg)
    brain.start()
    _fast(brain)

    brain.move_to_um(0, 20.0)      # 1000 steps
    _settle(brain, 0)
    brain.move_relative_um(0, 10.0)  # +500 steps -> 1500
    _settle(brain, 0)
    assert brain.status().position_steps[0] == 1500
    assert abs(brain.status().position_um[0] - 30.0) < 1e-9
    brain.shutdown()


def test_velocity_um_converts_and_clamps():
    cfg = Config()
    cfg.calibration.um_per_step_x = 0.02
    cfg.limits.max_step_rate = 2000.0
    brain, _ = build_sim_system(cfg)
    brain.start()

    # 20 um/s -> 1000 steps/s
    actual = brain.set_velocity_um(0, 20.0)
    assert abs(brain.status().step_rate[0] - 1000.0) < 1e-9
    assert abs(actual - 20.0) < 1e-9

    # 100 um/s would be 5000 steps/s -> clamps to 2000 steps/s = 40 um/s
    actual = brain.set_velocity_um(0, 100.0)
    assert abs(brain.status().step_rate[0] - 2000.0) < 1e-9
    assert abs(actual - 40.0) < 1e-9
    brain.shutdown()


def test_step_target_clamping_both_ends():
    cfg = Config()
    cfg.limits.min_steps_x, cfg.limits.max_steps_x = 0, 10_000
    events = []
    brain, _ = build_sim_system(cfg)
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    assert brain.move_to_step(0, 99_999) == 10_000   # clamp high
    assert brain.move_to_step(0, -50) == 0            # clamp low
    assert any(level == "warn" for level, _ in events)
    brain.shutdown()


def test_clamp_can_be_disabled():
    cfg = Config()
    cfg.limits.enforce = False
    cfg.limits.max_steps_x = 10_000
    brain, _ = build_sim_system(cfg)
    brain.start()
    assert brain.move_to_step(0, 50_000) == 50_000
    brain.shutdown()


def test_parameter_clamps():
    cfg = Config()
    cfg.limits.max_step_rate = 2000.0
    cfg.limits.max_acceleration = 100000.0
    cfg.limits.min_voltage, cfg.limits.max_voltage = 85.0, 125.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    assert brain.set_step_rate(0, 9999) == 2000.0
    assert brain.set_step_rate(0, 0) == 1.0             # floor at 1
    assert brain.set_acceleration(0, 10**9) == 100000.0
    assert brain.set_voltage(0, 200) == 125.0
    assert brain.set_voltage(0, 10) == 85.0
    brain.shutdown()


def test_calibration_must_be_positive():
    brain, _ = build_sim_system(Config())
    brain.start()
    with pytest.raises(ValueError):
        brain.set_calibration(0, 0.0)
    with pytest.raises(ValueError):
        brain.set_calibration(0, -1.0)
    # a valid one sticks and is reflected in the status
    brain.set_calibration(0, 0.03)
    assert abs(brain.status().um_per_step[0] - 0.03) < 1e-12
    brain.shutdown()


def test_move_steps_incremental_and_clamped():
    cfg = Config()
    cfg.limits.max_steps_x = 1000
    brain, _ = build_sim_system(cfg)
    brain.start()
    _fast(brain)
    brain.move_to_step(0, 800)
    _settle(brain, 0)
    target = brain.move_steps(0, 500)   # 800 + 500 = 1300 -> clamps to 1000
    assert target == 1000
    brain.shutdown()


def test_datum_vs_display_zero():
    brain, _ = build_sim_system(Config())
    brain.start()
    _fast(brain)

    brain.move_to_step(0, 3000)
    _settle(brain, 0)

    # display zero: read-out re-references, absolute counter unchanged
    brain.set_zero(0)
    st = brain.status()
    assert st.rel_steps[0] == 0
    assert st.position_steps[0] == 3000
    assert st.rel_origin[0] == 3000

    # hardware datum: absolute counter itself resets to 0
    brain.zero_counter(0)
    st = brain.status()
    assert st.position_steps[0] == 0
    assert st.rel_origin[0] == 0     # datum also clears the display origin
    brain.shutdown()


def test_apply_config_repushes_params():
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    cfg.motion.rate_x = 750.0
    cfg.motion.voltage_x = 120.0
    brain.apply_config()
    st = brain.status()
    assert abs(st.step_rate[0] - 750.0) < 1e-9
    assert abs(st.voltage[0] - 120.0) < 1e-9
    brain.shutdown()


def test_leash_limits_travel_around_datum():
    cfg = Config()
    cfg.limits.leash_enabled = True
    cfg.limits.leash_xy = 1000
    cfg.limits.leash_z = 500       # Z uses its own parameter
    brain, _ = build_sim_system(cfg)
    brain.start()
    _fast(brain)
    # X and Y bounded to +/-1000 around the datum (0)
    assert brain.move_to_step(0, 5000) == 1000
    assert brain.move_to_step(1, -5000) == -1000
    # Z bounded to +/-500
    assert brain.move_to_step(2, 9999) == 500
    assert brain.move_to_step(2, -9999) == -500
    # incremental moves are leashed too
    assert brain.move_steps(0, 10_000) == 1000
    # status reports the symmetric effective bounds + leash flag
    st = brain.status()
    assert st.leash is True
    assert st.limit_lo[0] == -1000 and st.limit_hi[0] == 1000
    assert st.limit_lo[2] == -500 and st.limit_hi[2] == 500
    brain.shutdown()


def test_set_leash_toggles_and_resizes():
    brain, _ = build_sim_system(Config())
    brain.start()
    assert brain.status().leash is False           # off by default
    # arming with the absolute limits still in place
    absolute_hi = brain.cfg.limits.max_steps_x
    state = brain.set_leash(enabled=True, leash_xy=2000, leash_z=300)
    assert state == {"enabled": True, "leash_xy": 2000, "leash_z": 300}
    st = brain.status()
    assert st.leash and st.limit_hi[0] == 2000 and st.limit_hi[2] == 300
    # toggling off keeps the ranges but restores the absolute clamp
    brain.set_leash(enabled=False)
    st = brain.status()
    assert not st.leash
    assert st.limit_hi[0] == absolute_hi
    brain.shutdown()


def test_default_limits_symmetric_allow_negative():
    brain, _ = build_sim_system(Config())
    brain.start()
    _fast(brain)
    st = brain.status()
    assert st.limit_lo[0] == -1_250_000 and st.limit_hi[0] == 1_250_000
    # a negative absolute target is allowed by default now (datum is arbitrary)
    assert brain.move_to_step(0, -5000) == -5000
    _settle(brain, 0)
    assert brain.status().position_steps[0] == -5000
    # incremental below zero works too
    assert brain.move_steps(0, -1000) == -6000
    brain.shutdown()


def test_changing_rate_midmove_does_not_jump():
    # regression: set_speed (rate change) during a move must not teleport the
    # simulated position -- the indicator was "changing a lot" because of it.
    brain, _ = build_sim_system(Config())
    brain.start()
    brain.set_step_rate(0, 1000)
    brain.move_to_step(0, 1_000_000)     # far target -> still moving
    time.sleep(0.2)
    p1 = brain.status().position_steps[0]
    brain.set_step_rate(0, 2000)         # speed up mid-move
    p2 = brain.status().position_steps[0]
    assert p2 >= p1                       # never runs backwards
    assert (p2 - p1) < 60                 # continuous, not a ~2x jump (~+200)
    brain.shutdown()


def test_speed_preset_applies_to_all_axes():
    cfg = Config()
    cfg.motion.fast_rate, cfg.motion.slow_rate = 1500.0, 300.0
    cfg.motion.fast_accel, cfg.motion.slow_accel = 20000.0, 5000.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    state = brain.set_speed(False)
    assert state["fast"] is False and state["rate"] == 300.0
    st = brain.status()
    assert st.speed_fast is False
    assert all(abs(st.step_rate[a] - 300.0) < 1e-6 for a in range(3))
    assert all(abs(st.acceleration[a] - 5000.0) < 1e-6 for a in range(3))
    brain.set_speed(True)
    st = brain.status()
    assert st.speed_fast and all(abs(st.step_rate[a] - 1500.0) < 1e-6 for a in range(3))
    brain.shutdown()


def test_step_size_preset_uses_voltage_extremes():
    cfg = Config()
    cfg.limits.min_voltage, cfg.limits.max_voltage = 85.0, 125.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    state = brain.set_step_size(True)          # large steps -> max voltage
    assert state["large"] and state["voltage"] == 125.0
    st = brain.status()
    assert st.step_large and all(abs(st.voltage[a] - 125.0) < 1e-6 for a in range(3))
    brain.set_step_size(False)                 # small steps -> min voltage
    st = brain.status()
    assert not st.step_large and all(abs(st.voltage[a] - 85.0) < 1e-6 for a in range(3))
    brain.shutdown()


def test_position_list_store_goto(tmp_path):
    brain, _ = build_sim_system(Config())
    brain.start()
    _fast(brain)
    brain.move_to_step(0, 300)
    brain.move_to_step(1, 400)
    _settle(brain, 0)
    _settle(brain, 1)

    brain.store_position(5, "spot")
    p = brain.get_positions()[5]
    assert p["used"] and p["name"] == "spot"
    assert int(p["x"]) == 300 and int(p["y"]) == 400

    brain.move_to_step(0, 0)
    _settle(brain, 0)
    brain.goto_position(5)
    _settle(brain, 0)
    assert brain.status().position_steps[0] == 300
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
