"""The brain against the simulator: moves, wrap, clamps, the move_id guard,
homing, stop, offsets, errors, lifecycle."""

import time

import pytest

from elliptec.config import Config
from elliptec.sim_system import build_sim_system


def make(**cfg_edits):
    cfg = Config()
    cfg.sim.max_speed_deg_s = 900.0      # fast, so the tests are quick
    for k, v in cfg_edits.items():
        group, name = k.split("__")
        setattr(getattr(cfg, group), name, v)
    brain, bus = build_sim_system(cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    return cfg, brain, bus, events


def wait_idle(brain, axis=0, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if not brain.status().moving[axis]:
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def rig():
    cfg, brain, bus, events = make()
    yield cfg, brain, bus, events
    brain.shutdown()


def test_start_state(rig):
    cfg, brain, _bus, _ev = rig
    st = brain.status()
    assert st.connected and st.n_axes == 1 and st.addresses == ["0"]
    assert abs(st.angle_deg[0] - cfg.sim.start_deg) < 0.01
    # at start the target IS the current angle: nothing has been commanded
    assert abs(st.target_deg[0] - st.angle_deg[0]) < 1e-9
    assert st.moving == [False] and st.homed == [False]


def test_absolute_move_and_move_id_guard(rig):
    _cfg, brain, _bus, _ev = rig
    r = brain.move_abs(0, 123.0)
    st = brain.status()
    # the very first snapshot after the command already shows the NEW target
    # and "moving": the stale-status trap (gotcha #2) cannot fire
    assert st.target_deg[0] == 123.0 and st.moving[0] and st.move_id[0] == r["move_id"]
    assert wait_idle(brain)
    assert abs(brain.status().angle_deg[0] - 123.0) < 0.01


def test_angle_is_quantised_to_encoder_pulses(rig):
    cfg, brain, _bus, _ev = rig
    brain.move_abs(0, 10.0001)
    assert wait_idle(brain)
    a = brain.status().device_deg[0]
    pulses = a / 360.0 * cfg.sim.pulses_per_rev
    assert abs(pulses - round(pulses)) < 1e-6


def test_wrap_and_360(rig):
    _cfg, brain, _bus, _ev = rig
    assert brain.move_abs(0, 370.0)["target"] == pytest.approx(10.0)
    assert brain.move_abs(0, -30.0)["target"] == pytest.approx(330.0)
    # 360 is kept as commanded so an adopt check matches; the mount goes to 0
    assert brain.move_abs(0, 360.0)["target"] == 360.0
    assert wait_idle(brain)
    a = brain.status().angle_deg[0]
    assert a < 0.01 or a > 359.99


def test_relative_move_crosses_zero(rig):
    _cfg, brain, _bus, _ev = rig
    brain.move_abs(0, 20.0)
    assert wait_idle(brain)
    r = brain.move_rel(0, -30.0)
    assert r["target"] == pytest.approx(350.0)
    assert wait_idle(brain)
    assert abs(brain.status().angle_deg[0] - 350.0) < 0.01


def test_angle_window_clamps_and_warns():
    _cfg, brain, _bus, events = make(limits__min_angle_deg=20.0, limits__max_angle_deg=100.0)
    try:
        assert brain.move_abs(0, 150.0)["target"] == 100.0
        assert any(lv == "warn" and "clamped" in m for lv, m in events)
        assert wait_idle(brain)
        # with a window, a relative move becomes an absolute one, clamped too
        assert brain.move_rel(0, 50.0)["target"] == 100.0
        assert wait_idle(brain)
        assert brain.move_rel(0, -300.0)["target"] == 20.0
    finally:
        brain.shutdown()


def test_relative_step_clamped(rig):
    cfg, brain, _bus, events = rig
    cfg.limits.max_relative_deg = 90.0
    brain.move_abs(0, 0.0)
    assert wait_idle(brain)
    assert brain.move_rel(0, 200.0)["target"] == pytest.approx(90.0)
    assert any("step" in m and "clamped" in m for _lv, m in events)


def test_velocity_clamped(rig):
    cfg, brain, bus, events = rig
    assert brain.set_velocity(0, 5) == cfg.limits.min_velocity_pct
    assert brain.set_velocity(0, 250) == 100
    assert brain.set_velocity(0, 55) == 55
    time.sleep(0.15)
    assert bus.read_velocity("0") == 55
    assert brain.status().velocity_pct == [55]
    assert sum(1 for lv, _m in events if lv == "warn") >= 2


def test_bad_values_refused(rig):
    _cfg, brain, _bus, _ev = rig
    with pytest.raises(ValueError):
        brain.move_abs(0, float("nan"))
    with pytest.raises(ValueError):
        brain.move_abs(3, 10.0)      # no such axis


def test_home_sets_homed(rig):
    _cfg, brain, _bus, _ev = rig
    brain.home(0)
    assert brain.status().moving[0]
    assert wait_idle(brain)
    st = brain.status()
    assert st.homed[0]
    assert st.device_deg[0] < 0.01 or st.device_deg[0] > 359.99


def test_home_ccw_also_ends_on_the_mark(rig):
    _cfg, brain, _bus, _ev = rig
    brain.home(0, "ccw")
    assert wait_idle(brain)
    assert brain.status().homed[0]
    assert brain.status().device_deg[0] < 0.01


def test_stop_halts_mid_move(rig):
    cfg, brain, _bus, _ev = rig
    brain.move_abs(0, 0.0)
    assert wait_idle(brain)
    cfg.sim.max_speed_deg_s = 90.0
    brain.move_abs(0, 300.0)
    time.sleep(0.3)
    brain.stop(0)
    assert wait_idle(brain, timeout=1.0)
    a = brain.status().angle_deg[0]
    assert 5.0 < a < 200.0, a


def test_stop_drops_queued_moves_and_does_not_hang_moving(rig):
    _cfg, brain, _bus, _ev = rig
    for k in range(5):
        brain.move_abs(0, 10.0 * k)
    brain.stop(0)
    assert wait_idle(brain, timeout=1.0)


def test_zero_and_offset(rig):
    _cfg, brain, _bus, _ev = rig
    brain.move_abs(0, 40.0)
    assert wait_idle(brain)
    brain.set_zero(0)
    time.sleep(0.1)
    st = brain.status()
    assert abs(st.offset_deg[0] - 40.0) < 0.01
    assert st.angle_deg[0] < 0.01 or st.angle_deg[0] > 359.99
    # user 10 deg is now device 50 deg
    brain.move_abs(0, 10.0)
    assert wait_idle(brain)
    assert abs(brain.status().device_deg[0] - 50.0) < 0.01
    brain.clear_zero(0)
    time.sleep(0.1)
    assert brain.status().offset_deg[0] == 0.0
    assert brain.cfg.offsets.offsets_deg == "0"


def test_error_code_is_reported(rig):
    _cfg, brain, bus, events = rig
    bus.inject_error("0", 2)
    time.sleep(0.15)
    st = brain.status()
    assert st.error_code[0] == 2 and "mechanical" in st.error[0]
    assert any(lv == "warn" and "mechanical" in m for lv, m in events)


def test_several_addresses_move_independently():
    _cfg, brain, _bus, _ev = make(axes__addresses="0,1", axes__names="HWP,POL")
    try:
        st = brain.status()
        assert st.addresses == ["0", "1"] and st.names == ["HWP", "POL"]
        brain.move_abs(0, 100.0)
        brain.move_abs(1, 200.0)
        assert wait_idle(brain, 0) and wait_idle(brain, 1)
        st = brain.status()
        assert abs(st.angle_deg[0] - 100.0) < 0.01
        assert abs(st.angle_deg[1] - 200.0) < 0.01
    finally:
        brain.shutdown()


def test_status_never_touches_hardware(rig):
    _cfg, brain, bus, _ev = rig
    brain.shutdown()                 # worker gone: nothing else calls poll

    def boom(*_a, **_k):
        raise AssertionError("status() must not call the backend")

    bus.poll = boom
    brain.status()                   # served from the snapshot


def test_worker_survives_a_failing_poll(rig):
    _cfg, brain, bus, _ev = rig

    def broken(_addr):
        raise RuntimeError("cable pulled")

    bus.poll = broken
    time.sleep(0.2)
    assert "poll failed" in brain.status().error[0]
    assert brain._worker.is_alive()


def test_shutdown_is_idempotent_and_stops_motion(rig):
    cfg, brain, bus, _ev = rig
    cfg.sim.max_speed_deg_s = 50.0
    brain.move_abs(0, 300.0)
    time.sleep(0.15)
    brain.shutdown()
    brain.shutdown()
    assert not brain.status().connected
    assert not bus._mounts["0"].moving


def test_apply_config_pushes_speed_and_warns_on_new_addresses(rig):
    cfg, brain, bus, events = rig
    cfg.motion.velocity_pct = 70
    cfg.axes.addresses = "0,1"
    brain.apply_config()
    time.sleep(0.15)
    assert bus.read_velocity("0") == 70
    assert any("restart" in m for _lv, m in events)


# --------------------------------------------------------------------------- #
# review additions (2026-09-27)
# --------------------------------------------------------------------------- #
def test_second_move_waits_for_the_first(rig):
    """A move sent into a running one is never passed to the bus: the real
    mount would refuse it as busy, or end the first move early with a reply
    that looks like the end of the second (a scan would read the wrong angle)."""
    cfg, brain, bus, _ev = rig
    cfg.sim.max_speed_deg_s = 300.0
    calls = []
    orig = bus.start_move_abs

    def spy(address, deg):
        calls.append(bus._mount(address).moving)   # was the mount turning?
        orig(address, deg)
    bus.start_move_abs = spy
    brain.move_abs(0, 150.0)
    r2 = brain.move_abs(0, 60.0)
    assert brain.status().moving[0]
    time.sleep(0.1)
    assert brain.status().moving[0] and brain.status().target_deg[0] == 60.0
    assert wait_idle(brain)
    assert calls == [False, False]            # both sent, each to an idle mount
    st = brain.status()
    assert st.move_id[0] == r2["move_id"] and abs(st.angle_deg[0] - 60.0) < 0.01


def test_stop_discards_a_held_move(rig):
    cfg, brain, bus, _ev = rig
    cfg.sim.max_speed_deg_s = 100.0
    brain.move_abs(0, 300.0)
    time.sleep(0.15)                          # first move is now running
    brain.move_abs(0, 100.0)                  # held behind it
    time.sleep(0.1)
    brain.stop(0)
    assert wait_idle(brain, timeout=3.0)
    time.sleep(0.3)
    a = brain.status().angle_deg[0]
    assert not brain.status().moving[0]
    assert abs(a - 100.0) > 5.0               # the held move never ran


def test_window_across_the_home_mark_is_never_left():
    """Window 10..200 deg with offset 300 deg = device 310..140 via the home
    mark.  An absolute 'ma' from user 20 to user 160 would turn backwards
    through device 200 (user 260), out of the window; the brain must step
    along the user frame instead."""
    cfg, brain, bus, _ev = make(limits__min_angle_deg=10.0, limits__max_angle_deg=200.0,
                                offsets__offsets_deg="300")
    try:
        brain.move_abs(0, 20.0)
        assert wait_idle(brain)
        assert abs(brain.status().angle_deg[0] - 20.0) < 0.01
        cfg.sim.max_speed_deg_s = 300.0
        brain.move_abs(0, 160.0)
        seen = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3.0:
            st = brain.status()
            seen.append(st.angle_deg[0])
            if not st.moving[0]:
                break
            time.sleep(0.005)
        assert abs(brain.status().angle_deg[0] - 160.0) < 0.01
        assert all(10.0 - 0.01 <= a <= 200.0 + 0.01 for a in seen), \
            [a for a in seen if not 9.99 <= a <= 200.01][:5]
    finally:
        brain.shutdown()
