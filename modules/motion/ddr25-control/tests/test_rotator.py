"""The brain against the simulator: lifecycle, homing rule, clamps, wrap
policies, the honest moving flag, stop, the display zero, stored angles, the
stream, and a failing backend."""

import math
import time

import pytest

from ddr25.config import Config
from ddr25.rotator import Rotator
from ddr25.sim_system import build_sim_system


def _fast(cfg=None):
    cfg = cfg or Config()
    # The CONTROLLER's stored profile: start() adopts it, pushes nothing.
    cfg.hardware.sim_start_velocity = 720.0
    cfg.hardware.sim_start_acceleration = 3600.0
    brain, sim = build_sim_system(cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    return brain, sim, events


def _wait_idle(brain, timeout=10.0):
    t0 = time.monotonic()
    while brain.status().moving:
        assert time.monotonic() - t0 < timeout, "stage never settled"
        time.sleep(0.01)


def _homed():
    brain, sim, events = _fast()
    brain.home()
    _wait_idle(brain)
    return brain, sim, events


def test_starts_unhomed_at_counter_zero_and_shuts_down_idempotently():
    brain, _sim, _ev = _fast()
    st = brain.status()
    assert st.connected and not st.homed and not st.moving
    assert abs(st.raw_deg) < 0.01            # a K-Cube counts from 0 at power-up
    brain.shutdown()
    brain.shutdown()                         # second call is a no-op
    assert not brain.status().connected


def test_absolute_move_refused_until_homed():
    brain, _sim, _ev = _fast()
    try:
        with pytest.raises(RuntimeError, match="not homed"):
            brain.move_to(10.0)
        brain.home()
        assert brain.status().homing and brain.status().moving
        with pytest.raises(RuntimeError, match="homing"):
            brain.move_to(10.0)              # not while homing either
        _wait_idle(brain)
        st = brain.status()
        assert st.homed and not st.homing and abs(st.raw_deg) < 0.01
        assert st.home_id == 1
        brain.move_to(10.0)                  # now fine
    finally:
        brain.shutdown()


def test_require_home_can_be_switched_off():
    cfg = Config()
    cfg.motion.require_home = False
    brain, _sim, _ev = _fast(cfg)
    try:
        brain.move_to(20.0)
        _wait_idle(brain)
        assert brain.status().angle_deg == pytest.approx(20.0, abs=0.01)
    finally:
        brain.shutdown()


def test_move_adopts_target_and_is_moving_in_the_same_frame():
    """gotcha #28: the very first status after the command must not say
    'target adopted, not moving' -- a scan would read the old angle."""
    brain, _sim, _ev = _homed()
    try:
        brain.set_velocity(10.0)
        brain.move_to(90.0)
        st = brain.status()
        assert st.target_deg == 90.0 and st.moving
        brain.stop(immediate=True)
    finally:
        brain.shutdown()


def test_zero_length_move_still_settles():
    brain, _sim, _ev = _homed()
    try:
        brain.move_to(0.0)                   # already there
        _wait_idle(brain, timeout=2.0)
        assert brain.status().target_deg == 0.0
    finally:
        brain.shutdown()


def test_literal_clamp_warns():
    brain, _sim, events = _homed()
    try:
        assert brain.move_to(5000.0) == brain.cfg.limits.max_deg
        assert any(lvl == "warn" and "clamped" in m for lvl, m in events)
        brain.stop(immediate=True)
    finally:
        brain.shutdown()


def test_shortest_turns_the_short_way_and_echoes_the_command():
    brain, _sim, _ev = _homed()
    try:
        brain.set_wrap("shortest")
        brain.move_to(350.0)
        _wait_idle(brain)
        st = brain.status()
        assert st.raw_deg == pytest.approx(-10.0, abs=0.01)
        assert st.angle_deg == pytest.approx(350.0, abs=0.01)
        # 370 is accepted, echoed AS COMMANDED (scan-core compares it) and
        # lands on 10 by the short way
        assert brain.move_to(370.0) == 370.0
        assert brain.status().target_deg == 370.0
        _wait_idle(brain)
        assert brain.status().raw_deg == pytest.approx(10.0, abs=0.01)
    finally:
        brain.shutdown()


def test_positive_policy_always_turns_forward():
    brain, _sim, _ev = _homed()
    try:
        brain.set_wrap("positive")
        brain.move_to(350.0)
        _wait_idle(brain)
        brain.move_to(10.0)
        _wait_idle(brain)
        assert brain.status().raw_deg == pytest.approx(370.0, abs=0.01)
        with pytest.raises(ValueError):
            brain.set_wrap("sideways")
    finally:
        brain.shutdown()


def test_relative_move_allowed_before_homing_and_adds_up():
    brain, _sim, _ev = _fast()
    try:
        brain.move_by(15.0)
        brain.move_by(15.0)                  # issued mid-move: stacks on the target
        _wait_idle(brain)
        assert brain.status().raw_deg == pytest.approx(30.0, abs=0.01)
        # parked on a target: the next step starts from the TARGET exactly,
        # not from the servo's dithering readout
        t = brain.status().target_deg
        assert brain.move_by(5.0) == t + 5.0
    finally:
        brain.shutdown()


def test_stop_forgets_the_target():
    brain, _sim, events = _homed()
    try:
        brain.set_velocity(20.0)
        brain.move_to(300.0)
        time.sleep(0.2)
        brain.stop()
        st = brain.status()
        assert st.target_deg is None          # a waiting scan must not "arrive"
        _wait_idle(brain)
        assert brain.status().angle_deg < 300.0
        assert any("STOP" in m for _l, m in events)
    finally:
        brain.shutdown()


def test_velocity_and_acceleration_clamped_and_read_back():
    brain, sim, events = _fast()
    try:
        v = brain.set_velocity(1e6)
        assert v == brain.cfg.limits.max_velocity
        assert sim.read_velocity_params()[0] == v
        assert brain.status().velocity == v
        assert brain.set_velocity(0.0) > 0         # zero would never arrive
        a = brain.set_acceleration(-5)
        assert a > 0
        assert sum(1 for lvl, _m in events if lvl == "warn") >= 3
    finally:
        brain.shutdown()


def test_zero_here_and_clear():
    brain, _sim, _ev = _homed()
    try:
        brain.move_to(40.0)
        _wait_idle(brain)
        z = brain.set_zero()
        assert z == pytest.approx(40.0, abs=0.01)
        time.sleep(0.1)
        assert brain.status().angle_deg == pytest.approx(0.0, abs=0.01)
        brain.move_to(10.0)                  # 10 deg from the NEW zero
        _wait_idle(brain)
        assert brain.status().raw_deg == pytest.approx(50.0, abs=0.02)
        brain.clear_zero()
        time.sleep(0.1)
        assert brain.status().angle_deg == pytest.approx(50.0, abs=0.02)
    finally:
        brain.shutdown()


def test_stored_angles_store_goto_clear():
    brain, _sim, _ev = _homed()
    try:
        brain.move_to(33.0)
        _wait_idle(brain)
        s = brain.store_angle(1, "s-pol")
        assert s["raw"] == 33.0 and s["used"]
        brain.move_to(200.0)
        _wait_idle(brain)
        assert brain.goto_angle(1) == 33.0
        _wait_idle(brain)
        assert brain.status().angle_deg == pytest.approx(33.0, abs=0.01)
        brain.clear_angle(1)
        with pytest.raises(ValueError):
            brain.goto_angle(1)
    finally:
        brain.shutdown()


def test_stream_records_the_angle():
    brain, _sim, _ev = _homed()
    try:
        sid = brain.stream_start(50)
        brain.move_to(90.0)
        time.sleep(0.4)
        chunk = brain.stream_stop()
        assert chunk["id"] == sid
        assert len(chunk["t"]) >= 5 and len(chunk["values"]["angle"]) == len(chunk["t"])
        assert chunk["values"]["angle"][-1] > chunk["values"]["angle"][0]
        assert "now" in chunk and chunk["delay_s"]["angle"] == 0.0
    finally:
        brain.shutdown()


class _Broken:
    """A backend whose reads fail after open -- a pulled USB cable."""

    def __init__(self):
        self.fail = False

    def open(self): pass
    def close(self): pass
    def idn(self): return "broken"
    def home(self): pass
    def is_homed(self): return True
    def move_to(self, p): pass
    def stop(self, immediate=False): pass
    def set_velocity(self, v): pass
    def set_acceleration(self, a): pass
    def read_velocity_params(self): return (10.0, 10.0)

    def is_moving(self):
        return False

    def read_position(self):
        if self.fail:
            raise OSError("device not responding")
        return 1.0


def test_hardware_failure_is_reported_not_raised():
    be = _Broken()
    brain = Rotator(be, Config())
    events = []
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    brain.start()
    try:
        be.fail = True
        time.sleep(0.2)
        st = brain.status()                  # must not raise
        assert "not responding" in st.hw_error
        assert math.isnan(st.angle_deg)
        assert any(lvl == "error" for lvl, _m in events)
        with pytest.raises(RuntimeError):
            brain.move_to(5.0)               # position unknown -> refused
    finally:
        brain.shutdown()


def test_shutdown_stops_a_moving_stage():
    brain, sim, _ev = _homed()
    brain.set_velocity(10.0)
    brain.move_to(300.0)
    time.sleep(0.2)
    brain.shutdown()
    time.sleep(0.5)
    assert not sim.is_moving()


def test_relative_full_turn_in_modulo_mode_is_numbered():
    """In a modulo mode `move_by 360` keeps the SAME target number, so a frame
    from before the command ("target 10, not moving") would look like the
    arrival. The move number is what tells the two apart."""
    brain, _sim, _ev = _homed()
    try:
        brain.set_wrap("shortest")
        brain.move_to(10.0)
        _wait_idle(brain)
        before = brain.status()
        assert before.target_deg == pytest.approx(10.0) and not before.moving
        brain.move_by(360.0)
        after = brain.status()
        assert after.target_deg == pytest.approx(before.target_deg)   # same number...
        assert after.move_id == before.move_id + 1                     # ...new move
        assert after.moving
        _wait_idle(brain)
        assert brain.status().raw_deg == pytest.approx(before.raw_deg + 360.0, abs=0.01)
    finally:
        brain.shutdown()


class _LaggyHomeBackend:
    """A controller that keeps its OLD 'homed' bit through a re-home and only
    reports the motion 80 ms after the command (USB status lag) -- the case
    where 'not moving and homed' on the first poll would be a lie."""

    def __init__(self):
        self.t_home = None
        self.pos = 0.0

    def open(self): pass
    def close(self): pass
    def idn(self): return "laggy stub"

    v, a = 10.0, 10.0

    def home(self): self.t_home = time.monotonic()
    def is_homed(self): return True

    def is_moving(self):
        if self.t_home is None:
            return False
        dt = time.monotonic() - self.t_home
        return 0.08 <= dt < 0.5

    def move_to(self, p): self.pos = p
    def stop(self, immediate=False): pass
    def read_position(self): return self.pos
    def set_velocity(self, v): self.v = v
    def set_acceleration(self, a): self.a = a
    def read_velocity_params(self): return (self.v, self.a)


def test_rehome_with_stale_homed_bit_waits_for_the_motion():
    cfg = Config()
    cfg.hardware.poll_hz = 100.0
    brain = Rotator(_LaggyHomeBackend(), cfg)
    brain.start()
    try:
        brain.home()
        time.sleep(0.3)                      # the controller is still homing
        assert brain.status().homing and brain.status().moving
        t0 = time.monotonic()
        while brain.status().homing:
            assert time.monotonic() - t0 < 3.0
            time.sleep(0.01)
        assert brain.status().homed
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# adopt-on-start (Lukas, 2026-09-27): starting the software changes nothing
# --------------------------------------------------------------------------- #
def _preexisting_cfg():
    """A controller found in a NON-default state: homed in an earlier session,
    parked at 212.5 deg, with its own stored profile (not the config's)."""
    cfg = Config()
    cfg.hardware.sim_start_deg = 212.5
    cfg.hardware.sim_start_homed = True
    cfg.hardware.sim_start_velocity = 55.0
    cfg.hardware.sim_start_acceleration = 140.0
    return cfg


def test_start_issues_no_state_changing_writes():
    brain, sim = build_sim_system(_preexisting_cfg())
    brain.start()
    try:
        time.sleep(0.1)                      # a few polls
        assert sim.writes == []              # no profile push, no home, no move
    finally:
        brain.shutdown()
    assert sim.writes == [("stop", False)]   # shutdown behaviour unchanged


def test_status_after_start_reflects_the_preexisting_state():
    cfg = _preexisting_cfg()
    assert cfg.motion.velocity != 55.0       # the config default differs...
    brain, _sim = build_sim_system(cfg)
    brain.start()
    try:
        st = brain.status()
        assert st.homed and not st.moving and not st.homing
        assert st.raw_deg == pytest.approx(212.5, abs=0.01)
        assert st.angle_deg == pytest.approx(212.5, abs=0.01)
        assert (st.velocity, st.acceleration) == (55.0, 140.0)   # ...the controller wins
        assert (cfg.motion.velocity, cfg.motion.acceleration) == (55.0, 140.0)
        brain.move_to(213.0)                 # homed state adopted: no re-home needed
        _wait_idle(brain)
    finally:
        brain.shutdown()


def test_set_config_writes_only_what_changed():
    brain, sim = build_sim_system(_preexisting_cfg())
    brain.start()
    try:
        brain.cfg.frame.zero_deg = 10.0      # an unrelated group
        brain.apply_config()
        assert sim.writes == []              # the adopted profile is not re-sent
        brain.cfg.motion.velocity = 80.0     # the user changes one value
        brain.apply_config()
        assert sim.writes == [("set_velocity", 80.0)]
        assert brain.status().velocity == 80.0
    finally:
        brain.shutdown()
