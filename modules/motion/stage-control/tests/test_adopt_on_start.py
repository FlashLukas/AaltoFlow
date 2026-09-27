"""Adopt-on-start (Lukas's rule, 2026-09-27): starting changes nothing.

The service must READ the BSC203 at start and show what it finds, never push
the .ini's motion parameters, never move and never home (unless
``motion.home_on_start`` is explicitly on).  Two kinds of proof:

  * a RECORDING backend (the simulator with every state-changing call
    trapped) -- start() must make zero such calls;
  * the real Kinesis adapter's open() run against a FAKE pylablib -- the
    file that will talk to the hardware must not call a setter either.

And the positive half: the status and the config after start show the
controller's pre-existing state, not the .ini defaults.
"""

from __future__ import annotations

import sys
import types

import pytest

from stage.backends.sim import SimStage
from stage.config import Config
from stage.net.protocol import config_to_dict
from stage.stage import Stage

# The calls that CHANGE a BSC203.  Everything else on the backend is a read.
WRITES = ("move_to", "home", "stop", "set_velocity", "set_acceleration")

# A plausible "someone left it like this" state, deliberately unlike the
# config defaults (all at 0 mm, 2 mm/s, 2 mm/s^2, not homed).
PRE_POS = [7.5, 12.25, 3.0]
PRE_VEL = [1.3, 0.7, 0.4]
PRE_ACC = [4.0, 1.5, 0.8]
PRE_HOMED = [True, True, False]


class RecordingSim(SimStage):
    """The simulator, with every state-changing call recorded."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.writes: list[tuple] = []
        for name in WRITES:
            real = getattr(super(), name)

            def trap(*args, _name=name, _real=real):
                self.writes.append((_name, args))
                return _real(*args)

            setattr(self, name, trap)


def _preset_brain(cfg: Config | None = None):
    cfg = cfg or Config()
    backend = RecordingSim(cfg)
    backend.preset(position=PRE_POS, velocity=PRE_VEL,
                   acceleration=PRE_ACC, homed=PRE_HOMED)
    return Stage(backend, cfg), backend


def test_start_issues_no_state_changing_writes():
    brain, backend = _preset_brain()
    brain.start()
    try:
        assert backend.writes == [], f"start() wrote to the stage: {backend.writes}"
    finally:
        backend.writes.clear()
        brain.shutdown()  # shutdown stops the axes -- that is allowed, not tested here


def test_status_after_start_reflects_preexisting_state():
    brain, _ = _preset_brain()
    brain.start()
    try:
        st = brain.status()
        assert st.position == pytest.approx(PRE_POS)
        assert st.velocity == pytest.approx(PRE_VEL)
        assert st.acceleration == pytest.approx(PRE_ACC)
        assert st.homed == PRE_HOMED
        assert st.moving == [False, False, False]
        # The config (Settings dialog, get_config over the wire) shows the
        # controller's values too, so pressing OK in Settings re-sends the
        # SAME numbers instead of the .ini defaults.
        m = config_to_dict(brain.cfg)["motion"]
        assert [m["vel_x"], m["vel_y"], m["vel_z"]] == pytest.approx(PRE_VEL)
        assert [m["acc_x"], m["acc_y"], m["acc_z"]] == pytest.approx(PRE_ACC)
    finally:
        brain.shutdown()


def test_value_above_ceiling_is_reported_not_corrected():
    cfg = Config()
    cfg.limits.max_velocity = 1.0          # the controller holds 1.3 on X
    brain, backend = _preset_brain(cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    try:
        assert backend.writes == []
        assert brain.status().velocity[0] == pytest.approx(1.3)
        assert any(lvl == "warn" and "above limits.max_velocity" in msg
                   for lvl, msg in events)
        # ...but the ceiling still applies to anything set afterwards.
        assert brain.set_velocity(0, 3.0) == pytest.approx(1.0)
    finally:
        brain.shutdown()


def test_home_on_start_is_off_by_default():
    assert Config().motion.home_on_start is False


def test_service_start_adopts_and_does_not_write():
    """The same through the real service object (what run_service.py starts)."""
    from stage.net.service import StageService

    brain, backend = _preset_brain()
    svc = StageService(brain, host="127.0.0.1", cmd_port=45659, pub_port=45660)
    svc.start()
    try:
        assert backend.writes == []
        assert brain.status().position == pytest.approx(PRE_POS)
    finally:
        svc.stop()


# --------------------------------------------------------------------------- #
# the REAL adapter against a fake pylablib
# --------------------------------------------------------------------------- #
class FakeKinesisMotor:
    """Answers the reads KinesisStage uses; records every other call."""

    instances: list["FakeKinesisMotor"] = []

    def __init__(self, conn, scale=None):
        self.conn = conn
        self.calls: list[str] = []
        FakeKinesisMotor.instances.append(self)

    def __getattr__(self, name):
        # Anything not defined below is recorded as a (possibly writing) call.
        def rec(*a, **k):
            self.calls.append(name)
        return rec

    # reads
    def get_position(self):
        return 5.0

    def get_velocity_parameters(self):
        return (0.0, 3.3, 1.1)   # (min_velocity, acceleration, max_velocity)

    def is_moving(self):
        return False

    def is_homed(self):
        return True

    def get_device_info(self):     # idn() -- a read
        return types.SimpleNamespace(serial_no=str(self.conn[0]))

    def close(self):
        pass


@pytest.fixture
def fake_pylablib(monkeypatch):
    FakeKinesisMotor.instances = []
    thorlabs = types.SimpleNamespace(KinesisMotor=FakeKinesisMotor)
    devices = types.ModuleType("pylablib.devices")
    devices.Thorlabs = thorlabs
    root = types.ModuleType("pylablib")
    root.devices = devices
    monkeypatch.setitem(sys.modules, "pylablib", root)
    monkeypatch.setitem(sys.modules, "pylablib.devices", devices)
    return FakeKinesisMotor


def test_kinesis_open_and_brain_start_only_read(fake_pylablib):
    from stage.backends.kinesis import KinesisStage

    cfg = Config()
    brain = Stage(KinesisStage(cfg), cfg)
    brain.start()
    motors = fake_pylablib.instances
    assert len(motors) == 3
    for m in motors:
        assert m.calls == [], f"open/start called {m.calls} on {m.conn}"
    st = brain.status()
    assert st.velocity == pytest.approx([1.1] * 3)
    assert st.acceleration == pytest.approx([3.3] * 3)
    assert cfg.motion.vel_x == pytest.approx(1.1)
