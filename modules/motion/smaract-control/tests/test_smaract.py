"""Brain behaviour (section 9): lifecycle, referencing, clamps, safety, threads."""

import math
import time

import pytest

from helpers import fast_cfg, make_brain, referenced_brain, wait_idle, wait_until
from smaract.backends.sim import SimScu
from smaract.smaract import Positioner


def test_lifecycle_and_idempotent_shutdown():
    brain, _ = make_brain()
    st = brain.status()
    assert st.connected and not st.referenced and not st.moving
    assert math.isfinite(st.position_mm)
    brain.shutdown()
    assert not brain.status().connected
    brain.shutdown()  # idempotent


def test_nothing_moves_at_start():
    brain, backend = make_brain()
    time.sleep(0.2)
    assert backend.channel_state() == "stopped"
    assert brain.status().speed_mm_s == 0.0
    brain.shutdown()


def test_absolute_move_refused_until_referenced():
    brain, _ = make_brain()
    with pytest.raises(RuntimeError, match="NOT referenced"):
        brain.move_to(10.0)
    with pytest.raises(RuntimeError):
        brain.store_position(0)
    brain.shutdown()


def test_absolute_move_allowed_unreferenced_when_not_required():
    cfg = fast_cfg()
    cfg.motion.require_reference = False
    brain, _ = make_brain(cfg)
    brain.move_to(1.0)
    assert wait_idle(brain)
    assert abs(brain.status().position_mm - 1.0) < 1e-3
    brain.shutdown()


def test_unreferenced_step_is_capped():
    cfg = fast_cfg()
    cfg.motion.max_unreferenced_step_mm = 2.0
    events = []
    brain, _ = make_brain(cfg)
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    p0 = brain.status().position_mm
    target = brain.move_by(50.0)
    assert target == pytest.approx(p0 + 2.0, abs=1e-3)
    assert any(lvl == "warn" and "clamped" in msg for lvl, msg in events)
    assert wait_idle(brain)
    assert brain.status().on_target
    brain.shutdown()


def test_reference_makes_the_scale_absolute():
    brain, backend = referenced_brain(power_on_mm=38.0)
    st = brain.status()
    assert st.referenced and not st.referencing and st.ref_id == 1
    # the counter now reads the TRUE rail position (the simulator knows it)
    assert st.position_mm == pytest.approx(backend._x, abs=2e-4)
    # it stopped on a reference mark: the second one ahead of 38 mm
    assert st.position_mm == pytest.approx(50.02, abs=2e-3)
    brain.shutdown()


def test_move_and_clamp_both_ends():
    cfg = fast_cfg()
    cfg.limits.min_mm, cfg.limits.max_mm = 30.0, 60.0
    events = []
    brain, _ = referenced_brain(cfg, power_on_mm=38.0)
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    assert brain.move_to(99.0) == 60.0
    assert brain.move_to(-5.0) == 30.0
    assert sum(1 for lvl, _ in events if lvl == "warn") >= 2
    assert wait_idle(brain)
    st = brain.status()
    assert st.on_target and st.position_mm == pytest.approx(30.0, abs=1e-3)
    brain.shutdown()


def test_clamp_can_be_disabled_and_end_stop_is_reported():
    """Without the envelope a target past the rail's end stop stalls there;
    the brain must say it stopped short, and not claim to be on target."""
    cfg = fast_cfg()
    cfg.limits.enforce = False
    events = []
    brain, _ = referenced_brain(cfg, power_on_mm=85.0, rail_stop_mm=118.0)
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    assert brain.move_to(130.0) == 130.0
    assert wait_idle(brain)
    st = brain.status()
    assert not st.on_target
    assert st.position_mm == pytest.approx(118.0, abs=1e-3)
    assert any(lvl == "warn" and "away from the target" in msg for lvl, msg in events)
    brain.shutdown()


def test_velocity_clamp_and_frequency_mapping():
    cfg = fast_cfg()
    cfg.limits.max_velocity_mm_s = 5.0
    cfg.motion.velocity_mm_s = 2.0
    cfg.hardware.um_per_step = 0.5
    brain, backend = make_brain(cfg)
    assert brain.set_velocity(100.0) == 5.0
    assert backend.get_max_frequency() == 10000         # 5 mm/s / 0.5 um
    # the floor is the higher of the config floor and the frequency floor
    lo, _ = brain.velocity_range()
    assert lo == pytest.approx(max(cfg.limits.min_velocity_mm_s,
                                   cfg.hardware.min_frequency_hz * 0.5e-3))
    assert brain.set_velocity(0.0) == pytest.approx(lo)
    assert brain.status().velocity_mm_s == pytest.approx(lo)
    brain.shutdown()


def test_hold_time_is_clamped():
    brain, _ = make_brain()
    assert brain.set_hold_time(-5) == 0
    assert brain.set_hold_time(10**7) == 60000
    assert brain.set_hold_time(250) == 250
    brain.shutdown()


def test_hold_reports_holding_then_stopped():
    cfg = fast_cfg()
    cfg.motion.hold_time_ms = 400
    brain, _ = referenced_brain(cfg, power_on_mm=38.0)
    brain.move_to(49.0)
    assert wait_until(lambda: brain.status().channel_state == "holding", 5.0)
    assert not brain.status().moving          # holding is NOT moving
    assert wait_until(lambda: brain.status().channel_state == "stopped", 2.0)
    brain.shutdown()


def test_stop_halts_and_retargets_here():
    brain, _ = referenced_brain(power_on_mm=38.0)
    brain.set_velocity(2.0)
    brain.move_to(100.0)
    time.sleep(0.3)
    brain.stop()
    assert wait_idle(brain, 2.0)
    st = brain.status()
    assert st.position_mm < 60.0
    assert st.target_mm == pytest.approx(st.position_mm, abs=1e-3)
    brain.shutdown()


def test_shutdown_stops_a_running_move():
    brain, backend = referenced_brain(power_on_mm=38.0)
    brain.set_velocity(1.0)
    brain.move_to(100.0)
    time.sleep(0.1)
    brain.shutdown()
    assert backend.channel_state() == "stopped"


def test_zero_here_and_move_from_zero():
    brain, _ = referenced_brain(power_on_mm=38.0)
    brain.move_to(45.0)
    assert wait_idle(brain)
    origin = brain.set_zero()
    assert origin == pytest.approx(45.0, abs=1e-3)
    brain.move_from_zero(2.0)
    assert wait_idle(brain)
    assert wait_until(lambda: abs(brain.status().relative_mm - 2.0) < 1e-3)
    brain.clear_zero()
    assert wait_until(lambda: abs(brain.status().relative_mm - brain.status().position_mm) < 1e-9)
    brain.shutdown()


def test_zero_taken_unreferenced_is_cleared_by_referencing():
    # A zero on the power-on counter scale is meaningless once the scale jumps.
    brain, _ = make_brain(power_on_mm=38.0)
    brain.set_zero()
    brain.find_reference()
    assert wait_until(lambda: brain.status().referenced and not brain.status().referencing, 15.0)
    assert brain.cfg.relative.rel_origin_mm == 0.0
    brain.shutdown()


def test_zero_survives_a_second_reference_search():
    # Re-referencing an axis that was already referenced does NOT change the
    # scale, so the user's zero must stay (it used to be wiped every time).
    brain, _ = referenced_brain(power_on_mm=38.0)
    brain.move_to(45.0)
    assert wait_idle(brain)
    origin = brain.set_zero()
    rid = brain.find_reference()
    assert wait_until(lambda: brain.status().ref_id == rid and not brain.status().referencing, 15.0)
    assert brain.status().referenced
    assert brain.cfg.relative.rel_origin_mm == pytest.approx(origin)
    brain.shutdown()


def test_stored_positions_round_trip(tmp_path):
    brain, _ = referenced_brain(power_on_mm=38.0)
    brain.move_to(44.0)
    assert wait_idle(brain)
    brain.store_position(5, "spot")
    brain.move_to(41.0)
    assert wait_idle(brain)
    brain.goto_position(5)
    assert wait_idle(brain)
    assert brain.status().position_mm == pytest.approx(44.0, abs=2e-3)
    with pytest.raises(ValueError):
        brain.goto_position(6)                       # empty slot
    path = tmp_path / "pos.json"
    brain.save_positions(str(path))
    brain.clear_position(5)
    brain.load_positions(str(path))
    assert brain.get_positions()[5]["name"] == "spot"
    brain.shutdown()


def test_snapshot_never_pairs_new_target_with_stale_idle():
    """gotcha #1/#2: right after move_to, a snapshot shows EITHER the old
    target, OR the new one together with moving=True -- never the new target
    and 'not moving' before the carriage got there."""
    brain, _ = referenced_brain(power_on_mm=38.0)
    brain.set_velocity(5.0)
    for target in (45.0, 41.0, 47.5):
        brain.move_to(target)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.5:
            st = brain.status()
            if st.target_mm == target and not st.moving:
                assert abs(st.position_mm - target) < 2e-3, st
            time.sleep(0.001)
        assert wait_idle(brain)
    brain.shutdown()


class _FlakyScu(SimScu):
    """A simulator whose reads can be made to fail, like a pulled USB cable."""
    fail = False

    def read_position_mm(self):
        if self.fail:
            raise OSError("USB read failed")
        return super().read_position_mm()


def test_status_never_touches_hardware_and_survives_read_errors():
    cfg = fast_cfg()
    backend = _FlakyScu(cfg)
    brain = Positioner(backend, cfg)
    brain.start()
    backend.fail = True
    assert wait_until(lambda: "USB read failed" in brain.status().hw_error, 2.0)
    st = brain.status()                    # must not raise
    assert math.isfinite(st.position_mm)   # the last good reading is kept
    backend.fail = False
    assert wait_until(lambda: brain.status().hw_error == "", 2.0)
    brain.shutdown()


def test_stream_records_the_position():
    brain, _ = make_brain()
    sid = brain.stream_start()
    brain.move_by(0.5)
    time.sleep(0.3)
    chunk = brain.stream_stop()
    assert chunk["id"] == sid
    assert len(chunk["t"]) >= 5
    assert len(chunk["values"]["position"]) == len(chunk["t"])
    assert chunk["delay_s"]["position"] == 0.0
    brain.shutdown()


def test_config_apply_repushes_speed():
    cfg = fast_cfg()
    brain, backend = make_brain(cfg)
    cfg.motion.velocity_mm_s = 3.0
    brain.apply_config()
    assert backend.get_max_frequency() == 3000
    brain.shutdown()
