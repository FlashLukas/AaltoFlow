"""Brain behaviour (§9): clamping, loop switching, velocity, ramp, relative."""

import time

from piezo.config import Config
from piezo.sim_system import build_sim_system


def _settle(brain, axis, timeout=6.0):
    t0 = time.monotonic()
    while brain.status().moving[axis] and time.monotonic() - t0 < timeout:
        time.sleep(0.01)


def _fast(cfg):
    """A config whose moves finish quickly (high velocity, off-ramp)."""
    cfg.motion.ramp_mode = "off"
    return cfg


def test_lifecycle_and_move():
    brain, _ = build_sim_system(_fast(Config()))
    brain.start()
    assert brain.status().connected
    brain.move_axis(0, 40.0)
    _settle(brain, 0)
    assert abs(brain.status().position[0] - 40.0) < 0.05
    brain.shutdown()
    assert not brain.status().connected
    # shutdown is idempotent
    brain.shutdown()


def test_travel_clamp_depends_on_loop_mode():
    cfg = _fast(Config())
    cfg.limits.travel_max_ol = 200.0
    cfg.limits.travel_max_cl = 160.0
    brain, _ = build_sim_system(cfg)
    brain.start()

    # open loop -> full 200 um available
    brain.set_closed_loop(0, False)
    assert brain.move_axis(0, 190.0) == 190.0
    _settle(brain, 0)
    # over-travel clamps to the open-loop ceiling
    assert brain.move_axis(0, 250.0) == 200.0

    # closed loop -> travel shrinks; a 190 target now clamps to 160
    brain.set_closed_loop(0, True)
    assert brain.move_axis(0, 190.0) == 160.0
    brain.shutdown()


def test_switching_to_closed_loop_reclamps_standing_target():
    cfg = _fast(Config())
    brain, _ = build_sim_system(cfg)
    brain.start()
    brain.set_closed_loop(0, False)
    brain.move_axis(0, 185.0)     # legal in open loop
    _settle(brain, 0)
    brain.set_closed_loop(0, True)  # 185 > 160 CL travel -> auto re-clamp
    _settle(brain, 0)
    assert abs(brain.status().target[0] - 160.0) < 1e-6
    brain.shutdown()


def test_velocity_clamped_to_ceiling():
    cfg = Config()
    cfg.limits.max_velocity = 500.0
    events = []
    brain, _ = build_sim_system(cfg)
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    assert brain.set_velocity(0, 9999.0) == 500.0
    assert any(level == "warn" for level, _ in events)
    brain.shutdown()


def test_clamp_can_be_disabled():
    cfg = _fast(Config())
    cfg.limits.enforce = False
    brain, _ = build_sim_system(cfg)
    brain.start()
    assert brain.move_axis(0, 500.0) == 500.0
    brain.shutdown()


def test_open_loop_readout_biased_closed_loop_accurate():
    cfg = _fast(Config())
    brain, _ = build_sim_system(cfg)
    brain.start()
    brain.set_closed_loop(0, True)
    brain.move_axis(0, 100.0)
    _settle(brain, 0)
    assert abs(brain.status().position[0] - 100.0) < 0.05   # servo is accurate
    brain.set_closed_loop(0, False)
    brain.move_axis(0, 100.0)
    _settle(brain, 0)
    assert abs(brain.status().position[0] - 100.0) > 0.1     # OL carries bias
    brain.shutdown()


def test_software_ramp_takes_time_and_reports_moving():
    cfg = Config()
    cfg.motion.ramp_mode = "software"
    cfg.motion.ramp_hz = 100.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    brain.set_velocity(0, 100.0)      # 100 um/s -> ~0.5 s to go 50 um
    t0 = time.monotonic()
    brain.move_axis(0, 50.0)
    assert brain.status().moving[0]   # ramp is in progress
    _settle(brain, 0)
    elapsed = time.monotonic() - t0
    assert elapsed > 0.2, elapsed     # it did NOT jump instantly
    assert abs(brain.status().position[0] - 50.0) < 0.5
    brain.shutdown()


def test_hardware_ramp_uses_backend_slew_rate():
    cfg = Config()
    cfg.motion.ramp_mode = "hardware"
    brain, backend = build_sim_system(cfg)
    brain.start()
    brain.set_velocity(0, 250.0)
    assert abs(backend.read_slew_rate(0) - 250.0) < 1e-6   # pushed to hardware
    # a single setpoint write; the (simulated) controller rate-limits it
    brain.move_axis(0, 60.0)
    assert brain.status().moving[0]
    _settle(brain, 0)
    assert abs(brain.status().position[0] - 60.0) < 0.5
    brain.shutdown()


def test_off_mode_disables_hardware_slew():
    cfg = Config()
    cfg.motion.ramp_mode = "off"
    brain, backend = build_sim_system(cfg)
    brain.start()
    brain.set_velocity(0, 300.0)
    assert backend.read_slew_rate(0) == 0.0   # off -> no hardware limiting
    brain.shutdown()


def test_relative_zero_and_move():
    brain, _ = build_sim_system(_fast(Config()))
    brain.start()
    brain.move_axis(0, 80.0)
    _settle(brain, 0)
    brain.set_zero(0)
    st = brain.status()
    assert abs(st.rel_origin[0] - 80.0) < 0.05
    assert abs(st.relative[0]) < 0.05

    brain.move_relative(0, 20.0)   # -> device 100
    _settle(brain, 0)
    st = brain.status()
    assert abs(st.position[0] - 100.0) < 0.05
    assert abs(st.relative[0] - 20.0) < 0.05

    brain.move_relative(0, 0.0)    # back to the zero point
    _settle(brain, 0)
    assert abs(brain.status().position[0] - 80.0) < 0.05

    brain.clear_zero(0)
    assert abs(brain.status().relative[0] - 80.0) < 0.05
    brain.shutdown()


def test_relative_move_is_clamped():
    cfg = _fast(Config())
    cfg.limits.travel_max_cl = 160.0
    brain, _ = build_sim_system(cfg)
    brain.start()
    brain.set_closed_loop(0, True)
    brain.move_axis(0, 150.0)
    _settle(brain, 0)
    brain.set_zero(0)                     # origin at 150
    target = brain.move_relative(0, 50.0)  # would be 200 -> clamps to 160 (CL)
    assert target == 160.0
    brain.shutdown()


def test_stop_freezes_axis():
    cfg = Config()
    cfg.motion.ramp_mode = "software"
    brain, _ = build_sim_system(cfg)
    brain.start()
    brain.set_velocity(0, 30.0)   # slow ramp so we can catch it mid-flight
    brain.move_axis(0, 120.0)
    time.sleep(0.2)
    brain.stop(0)
    assert not brain.status().moving[0]
    frozen = brain.status().position[0]
    assert frozen < 120.0          # stopped before arriving
    time.sleep(0.2)
    assert abs(brain.status().position[0] - frozen) < 1.0  # stays put
    brain.shutdown()


def test_position_list_store_goto(tmp_path):
    brain, _ = build_sim_system(_fast(Config()))
    brain.start()
    brain.move_xy(30.0, 40.0)
    _settle(brain, 0); _settle(brain, 1)
    brain.store_position(5, "spot")
    p = brain.get_positions()[5]
    assert p["used"] and p["name"] == "spot"
    assert abs(p["x"] - 30.0) < 0.1 and abs(p["y"] - 40.0) < 0.1

    brain.move_xy(0.0, 0.0)
    _settle(brain, 0); _settle(brain, 1)
    brain.goto_position(5)
    _settle(brain, 0); _settle(brain, 1)
    assert abs(brain.status().position[0] - 30.0) < 0.2
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
