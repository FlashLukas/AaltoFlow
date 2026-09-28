"""One physical K-Cube, one service (Lukas's rule, hwlock.py).

The real backend claims the controller's Kinesis SERIAL NUMBER before it
opens it. These tests run the real backend against a fake pylablib (the same
pattern as test_kinesis_backend.py) with the lock folder moved into pytest's
tmp_path, so they never touch the real %LOCALAPPDATA% locks of a running
service.
"""

import sys
import types
from pathlib import Path

import pytest

from ddr25 import hwlock
from ddr25.config import Config


class _Motor:
    """Minimal stand-in for pylablib's KinesisMotor: enough for open/close."""

    opened = 0
    fail = False

    def __init__(self, conn, scale="step", default_channel=1):
        if _Motor.fail:
            raise OSError("no such K-Cube")      # e.g. USB unplugged
        _Motor.opened += 1
        self.conn = conn
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    return tmp_path / "locks"


@pytest.fixture()
def fake_pylablib(monkeypatch):
    pll = types.ModuleType("pylablib")
    devices = types.ModuleType("pylablib.devices")
    devices.Thorlabs = types.SimpleNamespace(KinesisMotor=_Motor)
    pll.devices = devices
    monkeypatch.setitem(sys.modules, "pylablib", pll)
    monkeypatch.setitem(sys.modules, "pylablib.devices", devices)
    _Motor.opened, _Motor.fail = 0, False
    yield


def _backend(serial):
    from ddr25.backends.kinesis import KinesisRotator

    cfg = Config()
    cfg.hardware.serial = serial
    return KinesisRotator(cfg)


def test_copy_is_identical_to_the_master():
    # tools/check_modules.py checks this too; here it fails early in a dev loop.
    master = Path(__file__).resolve().parents[4] / "suite-common" / "src" / "suite_common" / "hwlock.py"
    if not master.is_file():
        pytest.skip("suite-common not next to this module (installed on its own)")
    mine = Path(hwlock.__file__)
    assert mine.read_bytes() == master.read_bytes()


def test_second_open_on_same_serial_is_refused(fake_pylablib):
    a = _backend("28000042")
    a.open()
    b = _backend("28000042")
    with pytest.raises(hwlock.HardwareBusy, match="ddr25"):
        b.open()
    assert _Motor.opened == 1            # the second never reached the driver
    a.close()


def test_same_serial_spelled_differently_conflicts(fake_pylablib):
    a = _backend("28000042")
    a.open()
    with pytest.raises(hwlock.HardwareBusy):
        _backend("  28000042 ").open()   # stray spaces from an .ini edit
    a.close()


def test_close_releases(fake_pylablib):
    a = _backend("28000042")
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["ddr25"]
    a.close()
    assert hwlock.held() == []
    b = _backend("28000042")
    b.open()                              # free again
    b.close()


def test_failing_open_releases(fake_pylablib):
    _Motor.fail = True
    a = _backend("28000042")
    with pytest.raises(OSError):
        a.open()
    assert hwlock.held() == []
    _Motor.fail = False
    b = _backend("28000042")
    b.open()
    b.close()


def test_different_serials_do_not_conflict(fake_pylablib):
    a, b = _backend("28000042"), _backend("28000043")
    a.open()
    b.open()
    assert len(hwlock.held()) == 2
    a.close()
    b.close()


def test_empty_serial_is_refused_without_claiming(fake_pylablib):
    with pytest.raises(RuntimeError, match="serial"):
        _backend("").open()
    assert hwlock.held() == [] and _Motor.opened == 0


def test_busy_brain_sends_nothing_and_shutdown_is_silent(fake_pylablib):
    """A service that finds the K-Cube taken must not send its shutdown stop
    to a controller that belongs to the other service."""
    from ddr25.sim_system import build_real_system

    holder = _backend("28000042")
    holder.open()
    cfg = Config()
    cfg.hardware.serial = "28000042"
    brain, be = build_real_system(cfg)
    with pytest.raises(hwlock.HardwareBusy):
        brain.start()
    assert be._m is None
    brain.shutdown()                      # must not raise, must not talk
    assert not brain.status().connected
    holder.close()


def test_sim_never_claims():
    from ddr25.sim_system import build_sim_system

    brain, _be = build_sim_system(Config())
    brain.start()
    try:
        assert hwlock.held() == []
    finally:
        brain.shutdown()
    assert hwlock.held() == []
