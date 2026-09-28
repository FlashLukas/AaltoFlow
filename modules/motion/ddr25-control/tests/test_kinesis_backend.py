"""The real backend against a FAKE pylablib -- no hardware, no pylablib.

What this can prove offline: the package imports without pylablib, the driver
is opened with the configured serial and the DDR25 scale, and each backend
method maps onto the pylablib call it is meant to. Whether pylablib and the
K-Cube behave as documented is the # VERIFY list, not this test.
"""

import sys
import types
from collections import namedtuple

import pytest

from ddr25.config import Config

_Vel = namedtuple("TVelocityParams", ["min_velocity", "acceleration", "max_velocity"])


class _FakeMotor:
    instances = []

    def __init__(self, conn, scale="step", default_channel=1):
        self.conn, self.scale = conn, scale
        self.calls = []
        self.pos = 0.0
        self.vel = _Vel(0.0, 10.0, 20.0)
        _FakeMotor.instances.append(self)

    def get_status(self):
        return ["homed"]                      # not "enabled" -> a MOVE enables

    def _enable_channel(self, enabled=True):
        self.calls.append(("enable", enabled))

    def home(self, sync=True, force=False):
        self.calls.append(("home", sync, force))

    def is_homed(self):
        return True

    def is_moving(self):
        return False

    def move_to(self, position):
        self.calls.append(("move_to", position))
        self.pos = position

    def stop(self, immediate=False, sync=True):
        self.calls.append(("stop", immediate, sync))

    def get_position(self):
        return self.pos

    def setup_velocity(self, min_velocity=None, acceleration=None, max_velocity=None):
        self.vel = _Vel(self.vel.min_velocity,
                        self.vel.acceleration if acceleration is None else acceleration,
                        self.vel.max_velocity if max_velocity is None else max_velocity)

    def get_velocity_parameters(self):
        return self.vel

    def close(self):
        self.calls.append(("close",))


@pytest.fixture()
def fake_pylablib(monkeypatch):
    pll = types.ModuleType("pylablib")
    devices = types.ModuleType("pylablib.devices")
    thorlabs = types.SimpleNamespace(KinesisMotor=_FakeMotor)
    devices.Thorlabs = thorlabs
    pll.devices = devices
    monkeypatch.setitem(sys.modules, "pylablib", pll)
    monkeypatch.setitem(sys.modules, "pylablib.devices", devices)
    _FakeMotor.instances.clear()
    yield


def test_package_imports_without_pylablib():
    import ddr25.backends.kinesis as k   # must not import pylablib at module level
    assert hasattr(k, "KinesisRotator")


def test_backend_maps_onto_pylablib(fake_pylablib):
    from ddr25.backends.kinesis import KinesisRotator

    cfg = Config()
    cfg.hardware.serial = "28000042"
    be = KinesisRotator(cfg)
    be.open()
    m = _FakeMotor.instances[-1]
    assert m.conn == "28000042" and m.scale == "DDR25"
    assert m.calls == []                              # open() writes nothing
    be.move_to(370.0)
    assert ("enable", True) in m.calls                # enabled by the user's move
    assert m.calls[-1] == ("move_to", 370.0) and be.read_position() == 370.0
    be.home()
    assert m.calls[-1] == ("home", False, True)       # fire-and-forget, forced
    be.stop(immediate=True)
    assert m.calls[-1] == ("stop", True, False)
    be.set_velocity(45.0)
    be.set_acceleration(300.0)
    assert be.read_velocity_params() == (45.0, 300.0)
    be.close()
    assert m.calls[-1] == ("close",)


def test_brain_runs_on_the_real_backend(fake_pylablib):
    from ddr25.sim_system import build_real_system

    brain, _be = build_real_system(Config())
    brain.start()
    try:
        st = brain.status()
        # adopted from the controller (the fake stores 20 deg/s, 10 deg/s^2)
        assert st.connected and st.homed and st.velocity == 20.0 and st.acceleration == 10.0
        brain.move_to(12.5)
        assert _FakeMotor.instances[-1].calls[-1] == ("move_to", 12.5)
    finally:
        brain.shutdown()


def test_homing_counts_as_moving(fake_pylablib):
    """pylablib's own is_moving() ignores the "homing" bit; the backend must
    not, or the brain gives up on a home while the stage is still turning."""
    from ddr25.backends.kinesis import KinesisRotator

    be = KinesisRotator(Config())
    be.open()
    m = _FakeMotor.instances[-1]
    m.get_status = lambda: ["homing", "enabled"]
    assert be.is_moving() and not be.is_homed()
    m.get_status = lambda: ["homed", "enabled"]
    assert not be.is_moving() and be.is_homed()
    be.close()


class _WriteGuardMotor(_FakeMotor):
    """A controller that FAILS the test on any state-changing call while
    `armed`: the adopt-on-start rule says starting the service only reads."""

    armed = True

    def _guard(self, what):
        if _WriteGuardMotor.armed:
            raise AssertionError(f"state-changing call at start: {what}")

    def _enable_channel(self, enabled=True):
        self._guard("enable")
        super()._enable_channel(enabled)

    def home(self, sync=True, force=False):
        self._guard("home")
        super().home(sync, force)

    def move_to(self, position):
        self._guard("move_to")
        super().move_to(position)

    def stop(self, immediate=False, sync=True):
        self._guard("stop")
        super().stop(immediate, sync)

    def setup_velocity(self, *a, **k):
        self._guard("setup_velocity")
        super().setup_velocity(*a, **k)


def test_real_start_issues_no_writes_and_adopts(fake_pylablib, monkeypatch):
    """Start the brain on the real backend against a controller that is
    already homed, sits at 212.5 deg and stores 55 deg/s / 140 deg/s^2:
    nothing may be written, and status must show exactly that state."""
    import pylablib.devices as dev
    from ddr25.sim_system import build_real_system

    class _Preset(_WriteGuardMotor):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.pos = 212.5
            self.vel = _Vel(0.0, 140.0, 55.0)

    monkeypatch.setattr(dev.Thorlabs, "KinesisMotor", _Preset)
    _WriteGuardMotor.armed = True
    cfg = Config()
    brain, _be = build_real_system(cfg)
    brain.start()
    try:
        st = brain.status()
        assert st.connected and st.homed and st.raw_deg == 212.5
        assert (st.velocity, st.acceleration) == (55.0, 140.0)
        assert (cfg.motion.velocity, cfg.motion.acceleration) == (55.0, 140.0)
        assert _FakeMotor.instances[-1].calls == []
    finally:
        _WriteGuardMotor.armed = False          # shutdown's stop is allowed
        brain.shutdown()
