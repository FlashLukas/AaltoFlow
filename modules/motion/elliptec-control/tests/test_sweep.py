"""The angle SWEEP (ramp_angle): a HARDWARE ramp for fly scans.

The mount turns to the angle at a set speed by itself ("move to at
velocity"): the rate in deg/s becomes the ELL14's velocity percent, the
user's velocity comes back afterwards, and the polled encoder angle is
streamed for the fly scan to bin by. Checked here: the rate, the end, the
restored speed, stop, a set taking over, clamping, the stream (unwrapped at
360), describe, the verbs over the wire (ports 17306/17307, inside this
module's test range 17300..17319) and the real backend's tracking of a move
against the fake serial port.
"""

import time

import pytest

from elliptec.config import Config
from elliptec.sim_system import build_sim_system


def make(**cfg_edits):
    cfg = Config()
    cfg.hardware.poll_hz = 50.0
    for k, v in cfg_edits.items():
        group, name = k.split("__")
        setattr(getattr(cfg, group), name, v)
    brain, bus = build_sim_system(cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    return cfg, brain, bus, events


@pytest.fixture()
def rig():
    cfg, brain, bus, events = make()
    yield cfg, brain, bus, events
    brain.shutdown()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_sweep_turns_at_the_rate_and_puts_the_speed_back(rig):
    cfg, brain, bus, events = rig
    start = brain.status().angle_deg[0]                  # 37 deg, mount at 60 %
    assert brain.status().velocity_pct[0] == 60
    t0 = time.monotonic()
    r = brain.ramp_angle(0, start + 129.0, 129.0)        # 30 %: 129 deg/s, 1 s
    assert r["ramp_id"] == 1 and r["rate"] == pytest.approx(129.0)
    st = brain.status()
    assert st.ramping and st.ramp_id == 1 and st.ramp_axis == 0
    assert st.ramp_target_deg == pytest.approx(start + 129.0)
    assert _wait(lambda: bus._mounts["0"].velocity_pct == 30)       # sweep speed
    assert _wait(lambda: not brain.status().ramping)
    dt = time.monotonic() - t0
    assert 0.8 < dt < 2.0                                # 129 deg at 129 deg/s
    assert brain.status().angle_deg[0] == pytest.approx(start + 129.0, abs=0.01)
    # the user's speed is back, on the mount and in the status
    assert _wait(lambda: bus._mounts["0"].velocity_pct == 60)
    assert brain.status().velocity_pct[0] == 60
    assert any("sweep done" in m for _, m in events)


def test_the_sweep_goes_the_long_way_when_the_target_says_so(rig):
    cfg, brain, bus, events = rig
    brain.move_abs(0, 300.0)
    assert _wait(lambda: not brain.status().moving[0])
    brain.ramp_angle(0, 360.0, 430.0)                    # up through 330, not down
    time.sleep(0.05)
    a = brain.status().angle_deg[0]
    assert a >= 300.0 or a < 1.0
    assert _wait(lambda: not brain.status().ramping)
    a = brain.status().angle_deg[0]
    assert min(a, 360.0 - a) < 0.01                      # 360 = 0


def test_stop_ends_it_where_it_is(rig):
    cfg, brain, bus, events = rig
    brain.ramp_angle(0, 357.0, 129.0)                    # ~2.5 s
    time.sleep(0.5)
    assert brain.ramp_stop() is True
    assert not brain.status().ramping
    assert _wait(lambda: not brain.status().moving[0])
    a = brain.status().angle_deg[0]
    assert 60.0 < a < 140.0                              # somewhere on the way
    assert _wait(lambda: bus._mounts["0"].velocity_pct == 60)
    assert brain.ramp_stop() is False


def test_an_ordinary_command_takes_over(rig):
    cfg, brain, bus, events = rig
    brain.ramp_angle(0, 357.0, 129.0)
    time.sleep(0.3)
    brain.move_abs(0, 10.0)                              # takes the knob over
    assert not brain.status().ramping
    assert _wait(lambda: not brain.status().moving[0])
    assert brain.status().angle_deg[0] == pytest.approx(10.0, abs=0.01)
    assert bus._mounts["0"].velocity_pct == 60           # at the USER's speed
    assert any("stopped by a new command" in m for _, m in events)
    # a stop of the axis ends a sweep as well
    brain.ramp_angle(0, 200.0, 129.0)
    brain.stop(0)
    assert not brain.status().ramping


def test_a_velocity_set_during_a_sweep_applies_after_it(rig):
    cfg, brain, bus, events = rig
    brain.ramp_angle(0, 200.0, 129.0)
    time.sleep(0.2)
    brain.set_velocity(0, 80)
    assert bus._mounts["0"].velocity_pct == 30           # the sweep keeps its pace
    assert _wait(lambda: not brain.status().ramping)
    assert _wait(lambda: bus._mounts["0"].velocity_pct == 80)


def test_rate_and_target_are_clamped_and_warned(rig):
    cfg, brain, bus, events = rig
    lo, hi = brain.ramp_rate_limits()
    assert lo == pytest.approx(129.0) and hi == pytest.approx(430.0)
    r = brain.ramp_angle(0, 100.0, 10.0)                 # below the 30 % floor
    assert r["rate"] == pytest.approx(lo)
    assert any(lvl == "warn" and "clamped" in m for lvl, m in events)
    brain.ramp_stop()
    r = brain.ramp_angle(0, 100.0, 5000.0)
    assert r["rate"] == pytest.approx(hi)
    brain.ramp_stop()
    with pytest.raises(ValueError):
        brain.ramp_angle(0, 100.0, 0.0)
    with pytest.raises(ValueError):
        brain.ramp_angle(0, float("nan"), 200.0)
    with pytest.raises(ValueError):
        brain.ramp_angle(5, 100.0, 200.0)


def test_the_stream_records_the_measured_angle_unwrapped():
    cfg, brain, bus, events = make()
    try:
        brain.move_abs(0, 300.0)
        assert _wait(lambda: not brain.status().moving[0])
        brain.stream_start()
        time.sleep(0.1)
        brain.ramp_angle(0, 360.0, 129.0)                # ~0.47 s
        assert _wait(lambda: not brain.status().ramping)
        time.sleep(0.1)
        c = brain.stream_stop()
        v, t = c["values"]["angle_0"], c["t"]
        assert len(t) == len(v) >= 15                    # polled at 50 Hz
        assert v[0] == pytest.approx(300.0, abs=0.01)
        assert v[-1] == pytest.approx(360.0, abs=0.01)   # NOT 0: unwrapped
        assert all(b >= a - 1e-6 for a, b in zip(v, v[1:]))
        mid = [x for x in v if 310.0 < x < 350.0]
        assert len(mid) >= 5                             # MEASURED on the way
        assert t == sorted(t) and c["delay_s"] == {"angle_0": 0.0}
        assert brain.stream_read()["t"] == []
    finally:
        brain.shutdown()


def test_shutdown_during_a_sweep_puts_the_speed_back():
    cfg, brain, bus, events = make()
    brain.ramp_angle(0, 357.0, 129.0)
    assert _wait(lambda: bus._mounts["0"].velocity_pct == 30)
    brain.shutdown()
    assert bus._mounts["0"].velocity_pct == 60


def test_describe_declares_the_ramp(rig):
    from elliptec.net.describe import build_manifest
    cfg, brain, *_ = rig
    m = {p["id"]: p for p in build_manifest(brain)["parameters"]}
    r = m["angle_0"]["ramp"]
    assert r["kind"] == "hardware"
    assert r["start"] == {"verb": "ramp_angle",
                          "args": {"to": "angle_deg", "rate": "rate_deg_per_s"},
                          "extra": {"axis": 0}}
    assert r["stop"] == {"verb": "ramp_stop"}
    assert r["rate"]["unit"] == "deg/s"
    assert r["rate"]["min"] == pytest.approx(129.0) == r["rate"]["default"]
    assert r["rate"]["max"] == pytest.approx(430.0)
    assert r["readback"] == {"stream": {"group": "angle", "channel": "angle_0"},
                             "measured": True}
    assert m["angle_0"]["stream"] == {"group": "angle", "channel": "angle_0"}


def test_verbs_over_the_wire():
    from elliptec.net.client import ElliptecClient
    from elliptec.net.service import ElliptecService
    cfg = Config()
    cfg.hardware.poll_hz = 50.0
    brain, _ = build_sim_system(cfg)
    svc = ElliptecService(brain, host="127.0.0.1", cmd_port=17306, pub_port=17307)
    svc.start()
    cli = ElliptecClient(host="127.0.0.1", cmd_port=17306, pub_port=17307)
    try:
        cli.start()
        assert cli._rpc(cmd="stream_start")["ok"]
        r = cli.ramp_angle(0, 100.0, 300.0)
        assert r["ramp_id"] == 1

        def done():
            st = cli._rpc(cmd="status")["status"]
            return st["ramp_id"] >= 1 and not st["ramping"]
        assert _wait(done)
        rep = cli._rpc(cmd="stream_stop")
        assert len(rep["stream"]["values"]["angle_0"]) >= 5
        assert cli.ramp_stop() is False
        assert "ramp_stop" in svc.control.safety and "stream_read" in svc.control.read
    finally:
        cli.close()
        svc.stop()


# ---- the REAL backend's tracking, against the fake serial port --------------

def test_real_backend_reads_the_encoder_while_a_tracked_move_runs(monkeypatch):
    import sys
    import types
    from test_serial_backend import FakeSerial, PPR
    from elliptec.backends.ell_serial import EllSerialBus, encode_s32
    fake = types.ModuleType("serial")
    fake.Serial = FakeSerial
    fake.EIGHTBITS, fake.PARITY_NONE, fake.STOPBITS_ONE = 8, "N", 1
    monkeypatch.setitem(sys.modules, "serial", fake)
    FakeSerial.instances.clear()
    cfg = Config()
    cfg.hardware.port = "COM98"
    b = EllSerialBus(cfg)
    b.open(["0"])
    ser = FakeSerial.instances[-1]
    try:
        b.set_tracking("0", True)
        start = ser.mounts["0"].pos                      # 1000 pulses
        b.start_move_rel("0", 90.0)                      # +PPR/4
        target = start + PPR // 4
        # the fake mount answers gp with where it "is": walk it by hand
        ser.mounts["0"].pos = start                      # still at the start
        ser.pending.clear()                              # no completion reply yet
        r = b.poll("0")
        assert r.moving                                  # a gp reply did NOT end it
        assert any(s == "0gp" for s in ser.sent)
        ser.mounts["0"].pos = start + PPR // 8           # half way
        r = b.poll("0")
        assert r.moving and r.device_deg == pytest.approx(45.0 + start / PPR * 360, abs=0.01)
        ser.mounts["0"].pos = target                     # arrived
        r = b.poll("0")
        assert not r.moving                              # the target ends it
        # untracked again: a PO ends a move as before
        b.set_tracking("0", False)
        b.start_move_rel("0", 10.0)
        ser.pending.append([1, f"0PO{encode_s32(target + 100)}"])
        assert _wait(lambda: not b.poll("0").moving, timeout=2.0)
    finally:
        b.close()
