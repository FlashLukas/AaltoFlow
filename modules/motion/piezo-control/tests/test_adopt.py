"""Adopt-on-start rule (Lukas, 2026-09-27): read the controller, change nothing.

Two halves are tested:
  * starting the brain issues NO state-changing call (no setpoint, loop-mode
    or slew-rate write) -- checked on the simulator through a guard wrapper,
    and on the REAL d-Drive driver through a fake serial port that fails on
    any line that is not a bare query;
  * the status after start shows the state the controller was ALREADY in
    (the simulator is preset to a non-default state for that).
"""

import re
import sys
import time
import types

import pytest

from piezo.config import Config
from piezo.piezo import Piezo
from piezo.sim_system import build_sim_system


class _NoWriteGuard:
    """Wrap a backend; any state-changing call raises while ``armed``."""

    WRITES = ("set_setpoint", "set_closed_loop", "set_slew_rate")

    def __init__(self, inner):
        self._inner = inner
        self.armed = True
        self.writes = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in self.WRITES:
            def guarded(*a, **k):
                self.writes.append((name, a))
                if self.armed:
                    raise AssertionError(f"state-changing call at start: {name}{a}")
                return attr(*a, **k)
            return guarded
        return attr


def _guarded_brain(cfg, presets=None):
    _, sim = build_sim_system(cfg)
    for axis, kw in (presets or {}).items():
        sim.preset(axis, **kw)
    guard = _NoWriteGuard(sim)
    return Piezo(guard, cfg), guard, sim


def test_start_writes_nothing_and_adopts_preexisting_state():
    cfg = Config()                      # config says: both CL, software ramp
    events = []
    brain, guard, sim = _guarded_brain(
        cfg, {0: dict(position=123.4, closed=False), 1: dict(position=42.0, closed=True)}
    )
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    brain.start()
    try:
        assert guard.writes == []
        st = brain.status()
        assert st.closed_loop == [False, True]          # X was left in open loop
        assert abs(st.target[0] - 123.4) < 1e-9         # the SETPOINT, not the OL read-out
        assert abs(st.target[1] - 42.0) < 1e-9
        assert abs(st.position[1] - 42.0) < 1e-9
        assert st.travel_max == [cfg.limits.travel_max_ol, cfg.limits.travel_max_cl]
        assert st.moving == [False, False]              # nothing was commanded
        # the config follows the controller, so a later apply_config keeps it
        assert cfg.motion.closed_loop_x is False
        # after start the brain may write again (explicit user action)
        guard.armed = False
        brain.move_axis(0, 100.0)       # software ramp: written by the ramp thread
        t0 = time.monotonic()
        while not guard.writes and time.monotonic() - t0 < 2.0:
            time.sleep(0.01)
        assert guard.writes and guard.writes[0][0] == "set_setpoint"
    finally:
        brain.shutdown()


def test_controller_slew_rate_is_adopted_as_hardware_ramp():
    cfg = Config()                      # config: software ramp, 50 um/s
    brain, guard, _ = _guarded_brain(cfg, {0: dict(slew=300.0), 1: dict(slew=120.0)})
    brain.start()
    try:
        assert guard.writes == []
        st = brain.status()
        assert st.ramp_mode == "hardware"
        assert st.velocity == [300.0, 120.0]
        assert cfg.motion.vel_x == 300.0
    finally:
        brain.shutdown()


def test_hardware_config_without_controller_slew_falls_back_to_software():
    cfg = Config()
    cfg.motion.ramp_mode = "hardware"
    brain, guard, _ = _guarded_brain(cfg, {0: dict(slew=0.0), 1: dict(slew=0.0)})
    brain.start()
    try:
        assert guard.writes == []
        assert brain.status().ramp_mode == "software"   # consistent with slew 0
    finally:
        brain.shutdown()


def test_out_of_travel_setpoint_is_reported_not_corrected():
    cfg = Config()
    events = []
    brain, guard, _ = _guarded_brain(cfg, {0: dict(position=175.0, closed=True)})
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    brain.start()
    try:
        assert guard.writes == []                       # no re-clamp move at start
        assert abs(brain.status().target[0] - 175.0) < 1e-9
        assert any(lvl == "warn" and "outside" in msg for lvl, msg in events)
    finally:
        brain.shutdown()


def test_set_config_of_other_group_keeps_adopted_loop_mode():
    cfg = Config()
    brain, guard, _ = _guarded_brain(cfg, {0: dict(position=10.0, closed=False)})
    brain.start()
    try:
        guard.armed = False
        cfg.ui.theme = "light"                          # an unrelated edit ...
        brain.apply_config()                            # ... re-applies the config
        assert brain.status().closed_loop[0] is False   # X stays in open loop
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------- #
# The REAL driver against a fake serial port
# --------------------------------------------------------------------------- #
_QUERY = re.compile(r"^(mess|cl|set|sr),\d+$")      # a bare verb,<ch> = a query


class _FakeDDrive:
    """Answers the d-Drive queries; records every line written."""

    def __init__(self):
        self.lines = []
        self.state = {("cl", 0): "0", ("cl", 1): "1",
                      ("set", 0): "87.5", ("set", 1): "12.25",
                      ("mess", 0): "88.1", ("mess", 1): "12.25",
                      ("sr", 0): "0", ("sr", 1): "0"}
        self._reply = b""

    def write(self, data):
        line = data.decode("ascii").strip()
        self.lines.append(line)
        parts = line.split(",")
        if len(parts) == 2:
            self._reply = f"{parts[0]},{parts[1]},{self.state[(parts[0], int(parts[1]))]}\r".encode()
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def read_until(self, term):
        r, self._reply = self._reply, b""
        return r

    def close(self):
        pass


@pytest.fixture
def fake_serial(monkeypatch):
    dev = _FakeDDrive()
    mod = types.ModuleType("serial")
    mod.Serial = lambda **kw: dev
    monkeypatch.setitem(sys.modules, "serial", mod)
    return dev


def test_ddrive_start_sends_only_queries_and_adopts(fake_serial):
    from piezo.sim_system import build_real_system

    cfg = Config()
    brain, _ = build_real_system(cfg)
    brain.start()
    try:
        bad = [ln for ln in fake_serial.lines if not _QUERY.match(ln)]
        assert bad == [], f"state-changing lines at start: {bad}"
        st = brain.status()
        assert st.closed_loop == [False, True]
        assert st.target == [87.5, 12.25]
        assert abs(st.position[0] - 88.1) < 1e-9
        assert st.ramp_mode == "software"
        # an explicit move afterwards IS written, with a value
        brain.set_ramp_mode("off")
        brain.move_axis(1, 20.0)
        assert "set,1,20.0000" in fake_serial.lines
    finally:
        brain.shutdown()
