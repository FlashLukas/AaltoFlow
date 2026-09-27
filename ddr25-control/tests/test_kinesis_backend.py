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
        return ["homed"]                      # not "enabled" -> the backend enables

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
    assert ("enable", True) in m.calls
    be.move_to(370.0)
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
        assert st.connected and st.homed and st.velocity == Config().motion.velocity
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
