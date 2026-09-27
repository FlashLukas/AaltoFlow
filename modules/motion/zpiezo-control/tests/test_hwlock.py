"""One physical KCube, one service (hwlock).

Lukas's rule: the same instrument is defined by the same physical address.
For a KCube piezo that address is its Kinesis serial number.  These tests run
the REAL backend (KCubeZ) against a fake ``pylablib.devices.Thorlabs`` put into
sys.modules, so nothing touches USB, and point the lock folder at tmp_path so a
service running on this PC is never disturbed.
"""

from __future__ import annotations

import sys
import types

import pytest

from zpiezo import hwlock
from zpiezo.backends.kcube import KCubeZ
from zpiezo.config import Config
from zpiezo.sim_system import build_sim_system
from zpiezo.zpiezo import ZPiezo


class FakeController:
    """Stands in for pylablib's KinesisPiezoController."""

    fail_open = False
    opened: list = []

    def __init__(self, serial):
        if FakeController.fail_open:
            raise RuntimeError("USB open failed (fake)")
        self.serial = serial
        self.v = 12.3
        self.writes: list = []
        FakeController.opened.append(serial)

    def get_output_voltage(self):
        return self.v

    def set_output_voltage(self, v):
        self.writes.append(v)
        self.v = v

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_pylablib(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    thorlabs = types.SimpleNamespace(
        KinesisPiezoController=FakeController,
        list_kinesis_devices=lambda: [("97000001", "KIM101"), ("29500123", "KPZ101")],
    )
    devices = types.ModuleType("pylablib.devices")
    devices.Thorlabs = thorlabs
    root = types.ModuleType("pylablib")
    root.devices = devices
    monkeypatch.setitem(sys.modules, "pylablib", root)
    monkeypatch.setitem(sys.modules, "pylablib.devices", devices)
    FakeController.fail_open = False
    FakeController.opened = []
    yield


def test_second_open_on_same_serial_is_refused():
    a = KCubeZ("29500123")
    a.open()
    b = KCubeZ("29500123")
    with pytest.raises(hwlock.HardwareBusy) as ei:
        b.open()
    assert "zpiezo" in str(ei.value)
    # The refused backend never reached the controller.
    assert FakeController.opened == ["29500123"]
    a.close()


def test_same_serial_written_differently_conflicts():
    # A serial is plain text: surrounding blanks (from an .ini) must not make
    # it look like a different box.
    a = KCubeZ("29500123")
    a.open()
    with pytest.raises(hwlock.HardwareBusy):
        KCubeZ("  29500123 ").open()
    a.close()


def test_autodiscovered_serial_is_claimed():
    # Empty serial -> the first KPZ101 found; that serial is what gets claimed,
    # so a second service with the explicit serial is refused.
    a = KCubeZ("")
    a.open()
    assert a.serial == "29500123"
    with pytest.raises(hwlock.HardwareBusy):
        KCubeZ("29500123").open()
    a.close()


def test_close_releases():
    a = KCubeZ("29500123")
    a.open()
    a.close()
    assert hwlock.held() == []
    b = KCubeZ("29500123")
    b.open()                      # no HardwareBusy
    b.close()


def test_failed_open_releases():
    FakeController.fail_open = True
    a = KCubeZ("29500123")
    with pytest.raises(RuntimeError):
        a.open()
    assert hwlock.held() == []
    FakeController.fail_open = False
    b = KCubeZ("29500123")
    b.open()
    b.close()


def test_sim_backend_claims_nothing():
    brain, _ = build_sim_system(Config())
    brain.start()
    assert hwlock.held() == []
    brain.shutdown()


def test_busy_brain_does_not_park_someone_elses_kcube():
    # The holder's KCube sits at 12.3 V.  A second brain whose open() is
    # refused must not send the shutdown "park at v_min" to it.
    holder = KCubeZ("29500123")
    holder.open()
    loser = ZPiezo(KCubeZ("29500123"), Config())
    with pytest.raises(hwlock.HardwareBusy):
        loser.start()
    loser.shutdown()
    assert holder._dev.writes == []
    assert holder._dev.v == pytest.approx(12.3)
    holder.close()
