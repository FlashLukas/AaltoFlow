"""scripts/probe.py (Mission Control's "Instruments on this PC"): it LISTS the
Kinesis devices and opens none. pylablib is replaced by a fake whose
KinesisPiezoMotor refuses to be constructed -- constructing it is opening.
Serials are made up."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from kim import hwlock, probe as P

ROOT = Path(__file__).resolve().parents[1]


class _NeverOpen:
    def __init__(self, *a, **k):
        raise AssertionError("the probe opened a controller")


def _fake_pylablib(monkeypatch, listed):
    calls = []

    def list_kinesis_devices():
        calls.append(1)
        return listed
    thorlabs = types.SimpleNamespace(KinesisPiezoMotor=_NeverOpen, KinesisMotor=_NeverOpen,
                                     list_kinesis_devices=list_kinesis_devices)
    devices = types.ModuleType("pylablib.devices")
    devices.Thorlabs = thorlabs
    root = types.ModuleType("pylablib")
    root.devices = devices
    monkeypatch.setitem(sys.modules, "pylablib", root)
    monkeypatch.setitem(sys.modules, "pylablib.devices", devices)
    return calls


@pytest.fixture(autouse=True)
def lock_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))


def test_lists_a_kim101_and_names_other_kinesis_devices(monkeypatch):
    calls = _fake_pylablib(monkeypatch, [("97000001", "Piezo Motor Controller"),
                                         ("70000001", "Brushless Motor Controller")])
    out = P.probe()
    assert calls == [1]
    kim, other = out["devices"]
    assert (kim["address"], kim["identity"]) == ("97000001", "Thorlabs KIM101")
    assert "Piezo Motor Controller" in kim["detail"]
    assert other["address"] == "70000001" and "not a KIM101" in other["detail"]
    assert out["note"] == ""


def test_a_controller_the_service_holds_is_reported_as_held(monkeypatch):
    """Lab PC 2026-10-03: the Kinesis list is EMPTY while kim has the KIM101 open."""
    _fake_pylablib(monkeypatch, [])
    lock = hwlock.claim("97000001", "kim", wait_s=0.0)
    try:
        out = P.probe()
    finally:
        lock.release()
    assert out["devices"] == [{"address": "97000001", "identity": "Thorlabs KIM101",
                               "detail": P.HELD}]


def test_nothing_found_says_why(monkeypatch):
    _fake_pylablib(monkeypatch, [])
    out = P.probe()
    assert out["devices"] == [] and "no Kinesis device listed" in out["note"]


def test_missing_pylablib_is_a_note(monkeypatch):
    monkeypatch.setitem(sys.modules, "pylablib", None)          # import fails
    monkeypatch.setitem(sys.modules, "pylablib.devices", None)
    out = P.probe()
    assert out["devices"] == [] and "uv sync --all-extras" in out["note"]


def test_the_script_prints_one_ascii_json_line():
    """The real script, in this environment: whatever pylablib finds here (on
    a PC without a KIM101: nothing), it prints one valid JSON line, exit 0."""
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "probe.py")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    data = json.loads(lines[-1])
    assert isinstance(data["devices"], list) and isinstance(data["note"], str)
    assert lines[-1].isascii()
