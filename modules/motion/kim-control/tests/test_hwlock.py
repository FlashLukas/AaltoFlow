"""One KIM101, one service: the real backend claims the controller's serial.

Lukas's rule: "the same instrument has to be defined by the same physical
address". For a KIM101 that address is its Kinesis serial number. Two kim
services (or any other module pointed at the same serial) would both send
moves down one USB link -- so the second one must be refused before it sends
a byte. These tests run offline: pylablib is replaced by a fake module, and
the lock files go into a temp folder (AALTOFLOW_LOCK_DIR).
"""

from __future__ import annotations

import subprocess
import sys
import types
from collections import namedtuple
from pathlib import Path

import pytest

from kim import hwlock
from kim.backends.kinesis_kim import KinesisKim
from kim.config import Config
from kim.sim_system import build_sim_system

DriveParams = namedtuple("DriveParams", "max_voltage velocity acceleration")


class _FakeMotor:
    """Stands in for pylablib's KinesisPiezoMotor: counts opens, can fail."""

    opened: list = []
    fail_on_query = False

    def __init__(self, serial):
        _FakeMotor.opened.append(serial)

    def get_drive_parameters(self, channel):
        if _FakeMotor.fail_on_query:
            raise RuntimeError("unexpected channel in the reply")
        return DriveParams(112, 500, 1000)

    def flush_comm(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_pylablib(monkeypatch, tmp_path):
    """A fake `pylablib.devices.Thorlabs` and a private lock folder per test."""
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    thorlabs = types.SimpleNamespace(
        KinesisPiezoMotor=_FakeMotor,
        list_kinesis_devices=lambda: [("70123456", "BSC203"), ("97654321", "KIM101")],
    )
    devices = types.ModuleType("pylablib.devices")
    devices.Thorlabs = thorlabs
    root = types.ModuleType("pylablib")
    root.devices = devices
    monkeypatch.setitem(sys.modules, "pylablib", root)
    monkeypatch.setitem(sys.modules, "pylablib.devices", devices)
    monkeypatch.setattr(_FakeMotor, "opened", [])
    monkeypatch.setattr(_FakeMotor, "fail_on_query", False)
    # Refusal must be quick in tests (the default waits 2 s for a killed holder).
    real_claim = hwlock.claim
    monkeypatch.setattr(hwlock, "claim",
                        lambda address, module, wait_s=0.0: real_claim(address, module, wait_s))
    yield


def _backend(serial: str) -> KinesisKim:
    cfg = Config()
    cfg.hardware.serial = serial
    return KinesisKim(cfg)


def test_second_backend_on_same_serial_is_refused_naming_kim():
    a = _backend("97000001")
    a.open()
    b = _backend("97000001")
    with pytest.raises(hwlock.HardwareBusy, match="kim"):
        b.open()
    # The refused backend never opened the controller: one open, from `a`.
    assert _FakeMotor.opened == ["97000001"]
    assert b._dev is None
    a.close()


def test_same_serial_written_differently_conflicts():
    # A configured " 97654321 " (stray spaces from an .ini) and the serial the
    # auto-discovery finds are the same physical controller.
    a = _backend(" 97654321 ")
    a.open()
    b = _backend("")                    # no serial: discovers 97654321
    with pytest.raises(hwlock.HardwareBusy, match="kim"):
        b.open()
    a.close()


def test_discovered_serial_is_claimed():
    a = _backend("")
    a.open()
    held = hwlock.held()
    assert [h["normalized"] for h in held] == ["97654321"]
    assert held[0]["module"] == "kim"
    a.close()


def test_close_releases_the_claim():
    a = _backend("97000002")
    a.open()
    a.close()
    assert hwlock.held() == []
    b = _backend("97000002")
    b.open()                            # would raise HardwareBusy if not released
    b.close()


def test_failing_open_releases_the_claim():
    _FakeMotor.fail_on_query = True
    a = _backend("97000003")
    with pytest.raises(RuntimeError):
        a.open()
    assert a._dev is None
    assert hwlock.held() == []
    _FakeMotor.fail_on_query = False
    b = _backend("97000003")
    b.open()
    b.close()


def test_simulator_claims_nothing():
    brain, _backend_sim = build_sim_system(Config())
    brain.start()
    try:
        assert hwlock.held() == []
    finally:
        brain.shutdown()
    assert hwlock.held() == []


def test_brain_start_refused_sends_nothing_on_shutdown():
    """A refused start must not leave the brain 'connected': its shutdown would
    then send stop() to a controller another service owns."""
    from kim.kim import Kim

    a = _backend("97000004")
    a.open()
    b = _backend("97000004")
    brain = Kim(b, b.cfg)
    with pytest.raises(hwlock.HardwareBusy):
        brain.start()
    brain.shutdown()                    # idempotent no-op: not connected
    assert _FakeMotor.opened == ["97000004"]
    a.close()


def test_service_script_exits_with_one_clean_line(tmp_path):
    """run_service.py --real on a held serial: exit 4, one ASCII line, no traceback."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_service.py"
    ini = tmp_path / "kim.ini"
    cfg = Config()
    cfg.hardware.serial = "97000005"
    from kim.config import save_config
    save_config(cfg, ini)

    holder = hwlock.claim("97000005", "kim-test-holder")
    try:
        # The child cannot import pylablib (fake is only in THIS process), so
        # give it a sitecustomize-free shim: a tiny fake package on PYTHONPATH.
        shim = tmp_path / "shim" / "pylablib" / "devices"
        shim.mkdir(parents=True)
        (shim.parent / "__init__.py").write_text("", encoding="utf-8")
        (shim / "__init__.py").write_text(
            "class Thorlabs:\n"
            "    @staticmethod\n"
            "    def KinesisPiezoMotor(serial):\n"
            "        raise AssertionError('must not open a claimed controller')\n",
            encoding="utf-8")
        import os
        env = dict(os.environ, PYTHONPATH=str(tmp_path / "shim"),
                   AALTOFLOW_LOCK_DIR=os.environ["AALTOFLOW_LOCK_DIR"])
        r = subprocess.run(
            [sys.executable, str(script), "--real", "--config", str(ini),
             "--cmd-port", "25967", "--pub-port", "25968"],
            capture_output=True, text=True, timeout=60, env=env)
    finally:
        holder.release()
    assert r.returncode == 4, r.stderr
    err = r.stderr.strip()
    assert "Traceback" not in err
    assert err.startswith("kim: cannot start:")
    assert "kim-test-holder" in err and "97000005" in err
    err.encode("ascii")                 # gotcha #14: printed text is ASCII
