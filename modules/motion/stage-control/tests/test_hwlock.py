"""One instrument, one service (Lukas's rule): the BSC203 is claimed by serial.

The real Kinesis adapter is opened against a FAKE pylablib (nothing leaves the
PC), with the lock folder redirected to a temp dir by the autouse fixture in
conftest.py.  What we prove:

  (a) two real backends on one serial -> the second raises HardwareBusy that
      names "stage", and it never constructed a motor handle;
  (b) the same serial written differently (spaces, case) is the same box;
  (c) close() releases, so a new open succeeds;
  (d) a failing open (pylablib raising) releases too;
  (e) the simulator claims nothing;
  plus: the service refuses cleanly and sends no "stop" to motors it does not
  own, and run_service.py exits non-zero with ONE line on stderr.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from stage import hwlock
from stage.backends.sim import SimStage
from stage.config import Config
from stage.hwlock import HardwareBusy
from stage.stage import Stage


class FakeMotor:
    instances: list["FakeMotor"] = []
    fail = False  # set True to make construction fail (device not found)

    def __init__(self, conn, scale=None):
        if FakeMotor.fail:
            raise RuntimeError("fake: no device with that serial")
        self.conn = conn
        self.calls: list[str] = []
        FakeMotor.instances.append(self)

    def __getattr__(self, name):  # record anything we did not model
        def rec(*a, **k):
            self.calls.append(name)
        return rec

    def get_position(self):
        return 0.0

    def get_velocity_parameters(self):
        return (0.0, 2.0, 2.0)

    def is_moving(self):
        return False

    def is_homed(self):
        return True

    def get_device_info(self):
        return types.SimpleNamespace(serial_no=str(self.conn[0]))

    def close(self):
        pass


@pytest.fixture
def fake_pylablib(monkeypatch):
    FakeMotor.instances = []
    FakeMotor.fail = False
    devices = types.ModuleType("pylablib.devices")
    devices.Thorlabs = types.SimpleNamespace(KinesisMotor=FakeMotor)
    root = types.ModuleType("pylablib")
    root.devices = devices
    monkeypatch.setitem(sys.modules, "pylablib", root)
    monkeypatch.setitem(sys.modules, "pylablib.devices", devices)
    return FakeMotor


def _backend(serial: str = "70000001"):
    from stage.backends.kinesis import KinesisStage

    cfg = Config()
    cfg.hardware.serial = serial
    return KinesisStage(cfg)


def _held_addresses() -> list[str]:
    return [h["normalized"] for h in hwlock.held()]


def test_second_open_same_serial_is_refused(fake_pylablib):
    a = _backend()
    a.open()
    assert _held_addresses() == ["70000001"]
    n_before = len(fake_pylablib.instances)
    b = _backend()
    with pytest.raises(HardwareBusy) as ei:
        b.open()
    assert "stage" in str(ei.value)
    assert "70000001" in str(ei.value)
    # the refused backend never touched pylablib
    assert len(fake_pylablib.instances) == n_before
    a.close()


def test_same_serial_written_differently_conflicts(fake_pylablib):
    a = _backend("70000001")
    a.open()
    with pytest.raises(HardwareBusy):
        _backend("  70000001 ").open()
    a.close()


def test_different_serial_does_not_conflict(fake_pylablib):
    a, b = _backend("70000001"), _backend("70000002")
    a.open()
    b.open()
    assert sorted(_held_addresses()) == ["70000001", "70000002"]
    a.close()
    b.close()


def test_close_releases(fake_pylablib):
    a = _backend()
    a.open()
    a.close()
    assert _held_addresses() == []
    b = _backend()
    b.open()  # must not raise
    b.close()


def test_failing_open_releases(fake_pylablib):
    fake_pylablib.fail = True
    a = _backend()
    with pytest.raises(RuntimeError):
        a.open()
    assert _held_addresses() == []
    fake_pylablib.fail = False
    b = _backend()
    b.open()
    b.close()


def test_missing_pylablib_releases(monkeypatch):
    # No pylablib at all (the usual state on a dev PC): the ImportError comes
    # AFTER the claim, so the claim must be dropped again.
    monkeypatch.setitem(sys.modules, "pylablib", None)
    a = _backend()
    with pytest.raises(ImportError):
        a.open()
    assert _held_addresses() == []


def test_empty_serial_is_refused_without_claim(fake_pylablib):
    with pytest.raises(ValueError):
        _backend("").open()
    assert _held_addresses() == []


def test_sim_claims_nothing():
    cfg = Config()
    brain = Stage(SimStage(cfg), cfg)
    brain.start()
    assert hwlock.held() == []
    brain.shutdown()
    assert hwlock.held() == []


def test_service_refuses_and_sends_nothing_to_foreign_motors(fake_pylablib):
    from stage.net.service import StageService

    owner = _backend()
    owner.open()
    owner_motors = list(fake_pylablib.instances)

    cfg = Config()
    from stage.backends.kinesis import KinesisStage
    brain = Stage(KinesisStage(cfg), cfg)
    svc = StageService(brain, host="127.0.0.1", cmd_port=15959, pub_port=15960)
    with pytest.raises(HardwareBusy):
        svc.serve_forever()
    # The refused service never connected, so its shutdown path is a no-op:
    # no stop() reached any motor (the owner's handles saw no calls at all).
    assert brain._connected is False
    brain.shutdown()
    for m in owner_motors:
        assert m.calls == []
    assert len(fake_pylablib.instances) == len(owner_motors)
    owner.close()


def test_run_service_busy_is_one_clean_line(tmp_path, monkeypatch):
    # Hold the default serial (as "clMag", to prove the holder is named) in
    # the SAME lock folder the child service will use.
    lockdir = tmp_path / "hwlocks-child"
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(lockdir))
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    lock = hwlock.claim(Config().hardware.serial, "clMag")
    try:
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_service.py"
        p = subprocess.run(
            [sys.executable, str(script), "--real", "--cmd-port", "15961", "--pub-port", "15962"],
            env=env, capture_output=True, text=True, timeout=60,
        )
    finally:
        lock.release()
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert "Traceback" not in p.stderr
    err = [ln for ln in p.stderr.splitlines() if ln.strip()]
    assert len(err) == 1, p.stderr
    assert "already in use by clMag" in err[0]
    assert err[0].isascii()
