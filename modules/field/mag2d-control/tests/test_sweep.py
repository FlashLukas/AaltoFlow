"""The SWEEPS (ramp_field / ramp_angle): the field magnitude and the field
ANGLE moved continuously at a set pace, for fly scans.

The setpoint walks along a straight line in time (computed from the elapsed
time in the control loop), the continuous PI makes the field follow, and every
Hall reading is recorded with its time so a fly scan can bin by the MEASURED
field. Checked here, on simulated time (FakeClock, as in test_controller.py):
the pace and the exact end, ramp_stop, an ordinary set taking over, clamping,
the stream, the water interlock ending a sweep, the angle convention the
readback uses, describe, and the verbs over the wire.
"""

import math
import time

import pytest

from mag2d.backends.sim import FakeClock
from mag2d.config import Config
from mag2d.controller import Refused
from mag2d.sim_system import build_sim_system

# This module's own test ports (test_net 15950-15957, test_control 18800/1).
WIRE_CMD, WIRE_PUB = 19940, 19941


@pytest.fixture
def rig():
    cfg = Config()
    clock = FakeClock()
    ctrl, sim = build_sim_system(cfg, clock=clock, sleep=clock.sleep, seed=5)
    events = []
    ctrl._on_event = lambda level, msg: events.append((level, msg))
    ctrl.start(run_thread=False)
    ctrl.set_output(True)
    run(ctrl, clock, 1.0)
    return cfg, clock, ctrl, sim, events


def run(ctrl, clock, seconds):
    dt = 1.0 / ctrl.cfg.control.loop_hz
    for _ in range(int(round(seconds / dt))):
        clock.advance(dt)
        ctrl.tick()


def run_until(ctrl, clock, pred, limit_s=30.0):
    dt = 1.0 / ctrl.cfg.control.loop_hz
    t0 = clock()
    while clock() - t0 < limit_s:
        clock.advance(dt)
        ctrl.tick()
        if pred(ctrl.status()):
            return clock() - t0
    return None


def test_field_sweep_walks_at_the_pace_follows_and_ends_on_the_target(rig):
    cfg, clock, ctrl, sim, events = rig
    rid = ctrl.ramp_field(50.0, 10.0)                  # 50 mT at 10 mT/s = 5 s
    assert rid == 1
    st = ctrl.status()
    assert st.ramping and st.ramp_id == 1 and st.ramp_knob == "field"
    assert st.ramp_target == 50.0 and st.ramp_rate == 10.0
    assert not st.field_stable
    worst = 0.0
    for _ in range(25):                                # 2.5 s, half way
        run(ctrl, clock, 0.1)
        s = ctrl.status()
        worst = max(worst, abs(s.measured_field_mT - s.setpoint_field_mT))
        assert not s.field_stable and s.state == "REGULATING"
    s = ctrl.status()
    assert s.setpoint_field_mT == pytest.approx(25.0, abs=0.3)
    assert worst < 3.0                                 # the PI follows the moving setpoint
    assert s.setpoint_angle_deg == 0.0                 # the angle is kept
    run(ctrl, clock, 2.7)
    s = ctrl.status()
    assert not s.ramping and s.ramp_id == 1
    assert s.setpoint_field_mT == 50.0                 # exactly: scan-core's adopt check
    assert run_until(ctrl, clock, lambda st: st.field_stable) is not None
    assert abs(ctrl.status().measured_field_mT - 50.0) < cfg.control.tolerance_mT
    assert any("reached" in m for _, m in events)


def test_angle_sweep_rotates_at_the_pace_and_the_readback_follows(rig):
    cfg, clock, ctrl, sim, events = rig
    ctrl.set_field(60.0, 0.0)
    assert run_until(ctrl, clock, lambda st: st.field_stable) is not None
    rid = ctrl.ramp_angle(90.0, 9.0)                   # 10 s
    assert rid == 1 and ctrl.status().ramp_knob == "angle"
    run(ctrl, clock, 5.0)
    s = ctrl.status()
    assert s.ramping and s.setpoint_angle_deg == pytest.approx(45.0, abs=0.3)
    assert s.setpoint_field_mT == 60.0                 # magnitude kept
    assert abs(s.measured_angle_deg - s.setpoint_angle_deg) < 3.0
    assert s.measured_magnitude_mT == pytest.approx(60.0, abs=2.0)
    run(ctrl, clock, 5.2)
    s = ctrl.status()
    assert not s.ramping and s.setpoint_angle_deg == 90.0
    assert run_until(ctrl, clock, lambda st: st.field_stable) is not None
    assert ctrl.status().measured_angle_deg == pytest.approx(90.0, abs=1.0)


def test_ramp_stop_ends_it_where_it_is(rig):
    cfg, clock, ctrl, sim, events = rig
    ctrl.ramp_field(100.0, 10.0)
    run(ctrl, clock, 2.0)
    assert ctrl.ramp_stop() is True
    s = ctrl.status()
    assert not s.ramping and 18.0 < s.setpoint_field_mT < 22.0
    held = s.setpoint_field_mT
    run(ctrl, clock, 2.0)
    assert ctrl.status().setpoint_field_mT == held      # it stays there
    assert run_until(ctrl, clock, lambda st: st.field_stable) is not None
    assert ctrl.ramp_stop() is False


def test_an_ordinary_set_takes_the_knob_over(rig):
    cfg, clock, ctrl, sim, events = rig
    ctrl.ramp_angle(180.0, 5.0)
    run(ctrl, clock, 1.0)
    ctrl.set_field(10.0)                               # the angle stays where the sweep got
    s = ctrl.status()
    assert not s.ramping and s.setpoint_field_mT == 10.0
    a = s.setpoint_angle_deg
    run(ctrl, clock, 1.0)
    assert ctrl.status().setpoint_angle_deg == a
    assert any("set takes over" in m for _, m in events)
    for setter in (lambda: ctrl.set_angle(5.0), lambda: ctrl.set_vector(3.0, 4.0),
                   lambda: ctrl.set_bx(1.0), lambda: ctrl.set_by(1.0), ctrl.zero,
                   lambda: ctrl.set_output(False)):
        ctrl.ramp_field(-50.0, 5.0)
        run(ctrl, clock, 0.2)
        setter()
        assert not ctrl.status().ramping


def test_a_new_sweep_replaces_a_running_one_from_where_it_got(rig):
    cfg, clock, ctrl, sim, events = rig
    ctrl.ramp_field(100.0, 10.0)
    run(ctrl, clock, 1.0)
    rid = ctrl.ramp_field(0.0, 10.0)
    assert rid == 2
    s = ctrl.status()
    assert s.ramping and 9.0 < s.setpoint_field_mT < 11.0
    run(ctrl, clock, 1.2)
    assert not ctrl.status().ramping and ctrl.status().setpoint_field_mT == 0.0


def test_clamps_and_refusals(rig):
    cfg, clock, ctrl, sim, events = rig
    with pytest.raises(ValueError):
        ctrl.ramp_field(10.0, 0.0)
    with pytest.raises(ValueError):
        ctrl.ramp_field(float("nan"), 1.0)
    with pytest.raises(ValueError):
        ctrl.ramp_angle(10.0, float("inf"))
    ctrl.ramp_field(1e6, 1e6)
    s = ctrl.status()
    assert s.ramp_target == cfg.limits.field_max_mT
    assert s.ramp_rate == cfg.limits.field_rate_max_mT_per_s
    ctrl.ramp_angle(-1e6, 1e-9)
    s = ctrl.status()
    assert s.ramp_target == cfg.limits.angle_min_deg
    assert s.ramp_rate == cfg.limits.angle_rate_min_deg_per_s
    assert sum("clamped" in m for lvl, m in events if lvl == "warn") >= 4
    ctrl.ramp_stop()


def test_the_stream_records_every_reading_with_its_time(rig):
    cfg, clock, ctrl, sim, events = rig
    sid = ctrl.stream_start()
    assert sid == 1
    ctrl.ramp_field(30.0, 15.0)                        # 2 s
    run(ctrl, clock, 3.0)
    c = ctrl.stream_stop()
    t, f = c["t"], c["values"]["field"]
    assert len(t) == len(f) == 150                     # one per loop tick (50 Hz)
    assert set(c["values"]) >= {"field", "angle", "bx", "by", "setpoint_field"}
    assert f[0] < 2.0 and f[-1] == pytest.approx(30.0, abs=1.5)
    sp = c["values"]["setpoint_field"]
    assert sp == sorted(sp) and sp[-1] == 30.0
    assert c["delay_s"]["field"] == 0.0 and "now" in c and not c["overflow"]
    assert t == sorted(t)
    # recording stopped: nothing more
    run(ctrl, clock, 0.5)
    assert ctrl.stream_read()["t"] == []


def test_water_lost_ends_a_sweep_and_refuses_a_new_one(rig):
    cfg, clock, ctrl, sim, events = rig
    ctrl.ramp_field(100.0, 10.0)
    run(ctrl, clock, 1.0)
    sim.p.water_ok = False
    run(ctrl, clock, 0.1)
    s = ctrl.status()
    assert s.state == "FAULT" and not s.ramping and s.ramp_id == 1
    assert s.setpoint_field_mT == 0.0
    run(ctrl, clock, 1.0)
    assert ctrl.status().setpoint_field_mT == 0.0       # the sweep does not walk it up again
    with pytest.raises(Refused):
        ctrl.ramp_field(10.0, 1.0)
    with pytest.raises(Refused):
        ctrl.ramp_angle(10.0, 1.0)


def test_measured_angle_lives_on_the_setpoints_axis(rig):
    """The readback a fly scan bins an angle sweep by must use the setpoint's
    own convention: a negative field measures the direction of -B, the angle
    is unwrapped to the setpoint's turn, and a field below the noise reports
    the setpoint angle rather than a random direction."""
    cfg, clock, ctrl, sim, events = rig
    assert ctrl.status().measured_angle_deg == 0.0       # ~0 mT: the setpoint angle
    ctrl.set_field(-50.0, 30.0)
    run_until(ctrl, clock, lambda st: st.field_stable)
    assert ctrl.status().measured_angle_deg == pytest.approx(30.0, abs=1.0)
    ctrl.set_field(50.0, 350.0)
    run_until(ctrl, clock, lambda st: st.field_stable)
    assert ctrl.status().measured_angle_deg == pytest.approx(350.0, abs=1.0)
    ctrl.set_field(50.0, 170.0)
    run_until(ctrl, clock, lambda st: st.field_stable)
    ctrl.ramp_angle(195.0, 10.0)                         # through atan2's +-180: no jump
    seen = []
    for _ in range(25):
        run(ctrl, clock, 0.1)
        seen.append(ctrl.status().measured_angle_deg)
    assert all(b - a > -1.0 for a, b in zip(seen, seen[1:]))
    assert seen[-1] > 185.0


def test_describe_offers_both_ramps():
    from mag2d.net.describe import build_manifest
    ctrl, _ = build_sim_system(Config(), seed=1)
    p = {d["id"]: d for d in build_manifest(ctrl)["parameters"]}
    lim = ctrl.cfg.limits
    for pid, verb, args, unit, lo, hi, ch in (
            ("field", "ramp_field", {"to": "field_mT", "rate": "rate_mT_per_s"}, "mT/s",
             lim.field_rate_min_mT_per_s, lim.field_rate_max_mT_per_s, "field"),
            ("angle", "ramp_angle", {"to": "angle_deg", "rate": "rate_deg_per_s"}, "deg/s",
             lim.angle_rate_min_deg_per_s, lim.angle_rate_max_deg_per_s, "angle")):
        r = p[pid]["ramp"]
        assert r["kind"] == "software"
        assert r["start"] == {"verb": verb, "args": args}
        assert r["stop"] == {"verb": "ramp_stop"}
        assert r["rate"]["unit"] == unit and r["rate"]["min"] == lo and r["rate"]["max"] == hi
        assert lo <= r["rate"]["default"] <= hi
        assert r["readback"] == {"stream": {"group": "field", "channel": ch}, "measured": True}
        assert r["done"] == {"key": "ramping", "id_key": "ramp_id"}
    assert p["measured_field"]["stream"] == {"group": "field", "channel": "field"}
    assert p["measured_angle"]["stream"] == {"group": "field", "channel": "angle"}
    assert p["ramping"]["read_path"] == ["ramping"]
    # a safety verb is an action, so the Control tab offers it to a viewer
    assert p["ramp_stop"]["kind"] == "action" and "wait" not in p["ramp_stop"]


def test_sweeps_over_the_wire():
    zmq = pytest.importorskip("zmq")
    from mag2d.net.service import Mag2dService
    ctrl, sim = build_sim_system(Config(), seed=6)
    svc = Mag2dService(ctrl, host="127.0.0.1", cmd_port=WIRE_CMD, pub_port=WIRE_PUB,
                       status_hz=20.0)
    svc.start()
    s = zmq.Context.instance().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, 3000)
    s.connect(f"tcp://127.0.0.1:{WIRE_CMD}")

    def ask(**msg):
        s.send_json(msg)
        return s.recv_json()

    def wait(pred, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.03)
        return False

    try:
        assert ask(cmd="set_output", enabled=True)["ok"]
        assert ask(cmd="stream_start")["ok"]
        r = ask(cmd="ramp_angle", angle_deg=5.0, rate_deg_per_s=10.0)
        assert r["ok"] and r["ramp_id"] == 1
        assert wait(lambda: (lambda st: st["ramp_id"] == 1 and not st["ramping"])(
            ask(cmd="status")["status"]))
        c = ask(cmd="stream_stop")["stream"]
        assert len(c["t"]) == len(c["values"]["angle"]) >= 10   # ~50 Hz for 0.5 s
        st = ask(cmd="status")["status"]
        assert st["setpoint_angle_deg"] == 5.0 and math.isfinite(st["measured_angle_deg"])
        r = ask(cmd="ramp_field", field_mT=20.0, rate_mT_per_s=5.0)
        assert r["ok"] and r["ramp_id"] == 2
        assert ask(cmd="ramp_stop") == {"ok": True, "stopped": True}
        assert ask(cmd="ramp_stop") == {"ok": True, "stopped": False}
        assert ask(cmd="ramp_field", field_mT=1.0)["ok"] is False      # no rate
    finally:
        s.close(0)
        svc.stop()
