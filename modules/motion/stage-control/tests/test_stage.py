"""Brain behaviour (§9): clamping, lifecycle, events, transform, positions."""

import time

from stage.config import Config
from stage.sim_system import build_sim_system


def _settle(brain, axis, timeout=4.0):
    t0 = time.monotonic()
    while brain.status().moving[axis] and time.monotonic() - t0 < timeout:
        time.sleep(0.01)


def test_lifecycle_and_move():
    brain, _ = build_sim_system(Config())
    brain.start()
    assert brain.status().connected
    brain.set_velocity(0, 5.0)
    brain.move_axis(0, 4.0)
    _settle(brain, 0)
    assert abs(brain.status().position[0] - 4.0) < 1e-6
    brain.shutdown()
    assert not brain.status().connected
    # shutdown is idempotent
    brain.shutdown()


def test_clamping_both_ends_and_events():
    cfg = Config()
    cfg.limits.min_x, cfg.limits.max_x = 0.0, 10.0
    events = []
    brain, _ = build_sim_system(cfg)
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()

    assert brain.move_axis(0, 99.0) == 10.0   # clamp high
    assert brain.move_axis(0, -5.0) == 0.0     # clamp low
    assert any(level == "warn" for level, _ in events)

    # velocity clamps to the ceiling too
    cfg.limits.max_velocity = 2.0
    assert brain.set_velocity(0, 100.0) == 2.0
    brain.shutdown()


def test_clamp_can_be_disabled():
    cfg = Config()
    cfg.limits.enforce = False
    cfg.limits.max_x = 10.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    assert brain.move_axis(0, 50.0) == 50.0
    brain.shutdown()


def test_transform_roundtrip():
    cfg = Config()
    brain, _ = build_sim_system(cfg)
    # rotate 90 deg, scale, plus offsets
    brain.set_offset(0, 1.5)
    brain.set_offset(1, -2.0)
    brain.set_matrix(0.0, -2.0, 0.5, 0.0)
    dev = brain.device_from_logical(3.0, -1.0, 7.0)
    log = brain.logical_from_device(*dev)
    assert all(abs(a - b) < 1e-9 for a, b in zip(log, (3.0, -1.0, 7.0)))


def test_set_matrix_rejects_singular_and_keeps_previous():
    import pytest

    brain, _ = build_sim_system(Config())
    brain.set_matrix(0, -1, 1, 0)          # a valid rotation
    assert brain.status().matrix == [0, -1, 1, 0]

    for bad in [(1, 1, 1, 1), (0, 0, 0, 0), (2, 4, 1, 2)]:  # all singular (det=0)
        with pytest.raises(ValueError):
            brain.set_matrix(*bad)
        # previous valid matrix is untouched
        assert brain.status().matrix == [0, -1, 1, 0]


def test_set_matrix_accepts_small_but_valid_scaling():
    brain, _ = build_sim_system(Config())
    brain.set_matrix(1e-3, 0, 0, 1e-3)     # det = 1e-6 but perfectly invertible
    assert brain.status().matrix == [1e-3, 0, 0, 1e-3]
    # round-trips cleanly
    dev = brain.device_from_logical(5.0, 7.0, 0.0)
    log = brain.logical_from_device(*dev)
    assert abs(log[0] - 5.0) < 1e-6 and abs(log[1] - 7.0) < 1e-6


def test_logical_from_device_never_divides_by_zero():
    # Force a singular matrix straight into the config (bypassing set_matrix)
    # and confirm the read-out is finite rather than raising / inf / nan.
    import math

    cfg = Config()
    cfg.transform.m00, cfg.transform.m01 = 1.0, 1.0
    cfg.transform.m10, cfg.transform.m11 = 1.0, 1.0  # det = 0
    brain, _ = build_sim_system(cfg)
    u, v, w = brain.logical_from_device(2.0, 2.0, 1.0)
    assert all(math.isfinite(val) for val in (u, v, w))
    assert w == 1.0


def test_apply_config_sanitises_singular_matrix():
    cfg = Config()
    cfg.transform.m00 = cfg.transform.m01 = cfg.transform.m10 = cfg.transform.m11 = 1.0
    brain, _ = build_sim_system(cfg)
    brain.start()          # start() sanitises -> identity
    assert brain.status().matrix == [1.0, 0.0, 0.0, 1.0]
    brain.shutdown()


def test_relative_zero_and_move():
    brain, _ = build_sim_system(Config())
    brain.start()
    brain.set_velocity(0, 50)

    # move somewhere, then declare it the relative zero
    brain.move_axis(0, 8.0)
    _settle(brain, 0)
    brain.set_zero(0)
    st = brain.status()
    assert abs(st.rel_origin[0] - 8.0) < 1e-6
    assert abs(st.relative[0]) < 1e-6          # reads zero at the origin
    assert abs(st.position[0] - 8.0) < 1e-6    # device unchanged

    # a relative move of +2 lands at device 10
    brain.move_relative(0, 2.0)
    _settle(brain, 0)
    st = brain.status()
    assert abs(st.position[0] - 10.0) < 1e-6
    assert abs(st.relative[0] - 2.0) < 1e-6

    # relative move to 0 returns to the zero point
    brain.move_relative(0, 0.0)
    _settle(brain, 0)
    assert abs(brain.status().position[0] - 8.0) < 1e-6

    # clearing the zero restores absolute readout
    brain.clear_zero(0)
    assert abs(brain.status().relative[0] - 8.0) < 1e-6
    brain.shutdown()


def test_relative_move_is_clamped():
    cfg = Config()
    cfg.limits.max_x = 10.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    brain.set_velocity(0, 50)
    brain.move_axis(0, 9.0)
    _settle(brain, 0)
    brain.set_zero(0)                    # origin at 9
    target = brain.move_relative(0, 5.0)  # would be device 14 -> clamps to 10
    assert target == 10.0
    brain.shutdown()


def test_position_list_store_goto(tmp_path):
    brain, _ = build_sim_system(Config())
    brain.start()
    brain.set_velocity(0, 20)
    brain.set_velocity(1, 20)
    brain.move_axis(0, 3.0)
    brain.move_axis(1, 4.0)
    _settle(brain, 0)
    _settle(brain, 1)

    brain.store_position(5, "spot")
    p = brain.get_positions()[5]
    assert p["used"] and p["name"] == "spot"
    assert abs(p["x"] - 3.0) < 1e-6 and abs(p["y"] - 4.0) < 1e-6

    # move away, then go back
    brain.move_axis(0, 0.0)
    _settle(brain, 0)
    brain.goto_position(5)
    _settle(brain, 0)
    assert abs(brain.status().position[0] - 3.0) < 1e-6
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
