"""scripts/probe.py (Mission Control's "Instruments on this PC"): it LISTS the
IDS cameras through a fake ids_peak and opens none -- OpenDevice raises.
Serials are made up."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from camera import hwlock, probe as P

ROOT = Path(__file__).resolve().parents[1]


class _Descr:
    def __init__(self, serial, model, name):
        self._s, self._m, self._n = serial, model, name

    def SerialNumber(self):
        return self._s

    def ModelName(self):
        return self._m

    def DisplayName(self):
        return self._n

    def OpenDevice(self, *a):
        raise AssertionError("the probe opened a camera")


def _fake_ids(monkeypatch, descrs):
    calls = []

    class DeviceManager:
        @staticmethod
        def Instance():
            return types.SimpleNamespace(Update=lambda: calls.append("update"),
                                         Devices=lambda: list(descrs))
    peak = types.SimpleNamespace(
        Library=types.SimpleNamespace(Initialize=lambda: calls.append("init"),
                                      Close=lambda: calls.append("close")),
        DeviceManager=DeviceManager)
    pkg = types.ModuleType("ids_peak")
    pkg.ids_peak = peak
    monkeypatch.setitem(sys.modules, "ids_peak", pkg)
    monkeypatch.setitem(sys.modules, "ids_peak.ids_peak", peak)
    return calls


@pytest.fixture(autouse=True)
def lock_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))


def test_lists_cameras_by_serial_and_closes_the_library(monkeypatch):
    calls = _fake_ids(monkeypatch, [_Descr("4100000001", "U3-0000XCP-M", "cam1")])
    out = P.probe()
    assert out["devices"] == [{"address": "4100000001", "identity": "IDS U3-0000XCP-M",
                               "detail": "IDS peak, serial 4100000001, cam1",
                               "lock": "CAMERA::4100000001"}]
    assert calls == ["init", "update", "close"] and out["note"] == ""


def test_a_held_camera_is_reported_even_when_ids_does_not_list_it(monkeypatch):
    _fake_ids(monkeypatch, [])
    lock = hwlock.claim("CAMERA::4100000002", "camera", wait_s=0.0)
    try:
        out = P.probe()
    finally:
        lock.release()
    assert [(d["address"], d["detail"]) for d in out["devices"]] == [("4100000002", P.HELD)]


def test_a_listed_camera_the_service_holds_says_held(monkeypatch):
    """Lab PC 2026-10-03: IDS peak still lists a held camera; the probe must
    say it is held (not only the CAMERA:: lock for the merge to find)."""
    _fake_ids(monkeypatch, [_Descr("4100000003", "U3-0000XCP-M", "cam1")])
    lock = hwlock.claim("CAMERA::4100000003", "camera", wait_s=0.0)
    try:
        out = P.probe()
    finally:
        lock.release()
    assert len(out["devices"]) == 1
    assert "held by the running camera service" in out["devices"][0]["detail"]


def test_missing_ids_peak_is_a_note(monkeypatch):
    monkeypatch.setitem(sys.modules, "ids_peak", None)
    out = P.probe()
    assert out["devices"] == [] and "IDS peak" in out["note"]


def test_the_script_prints_one_ascii_json_line():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "probe.py")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    line = [ln for ln in r.stdout.splitlines() if ln.strip()][-1]
    data = json.loads(line)
    assert isinstance(data["devices"], list) and line.isascii()


# --------------------------------------------------------------------------- #
# Thorlabs (2026-11-09, the Blue Box's Zelux): listed through a fake TSI SDK
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def no_real_thorlabs(monkeypatch):
    """A PC with the real SDK unpacked (the Blue Box) must not leak a real
    camera into these tests: Thorlabs is off unless a test fakes it."""
    from camera.backends import thorlabs as T
    monkeypatch.setattr(T, "sdk_available", lambda sdk_dir="": False)


def _fake_thorlabs(monkeypatch, serials, fail=None):
    from camera.backends import thorlabs as T
    calls = []

    class TLCameraSDK:
        def __init__(self):
            calls.append("sdk")

        def discover_available_cameras(self):
            calls.append("discover")
            if fail:
                raise RuntimeError(fail)
            return list(serials)

        def open_camera(self, serial):
            raise AssertionError("the probe opened a Thorlabs camera")

        def dispose(self):
            calls.append("dispose")

    monkeypatch.setattr(T, "sdk_available", lambda sdk_dir="": True)
    monkeypatch.setattr(T, "import_tl_camera",
                        lambda *a, **k: types.SimpleNamespace(TLCameraSDK=TLCameraSDK))
    return calls


def test_thorlabs_cameras_are_listed_without_opening_them(monkeypatch):
    monkeypatch.setitem(sys.modules, "ids_peak", None)          # a Thorlabs-only PC
    calls = _fake_thorlabs(monkeypatch, ["00000001"])
    out = P.probe()
    assert [(d["address"], d["identity"], d["lock"]) for d in out["devices"]] == [
        ("00000001", "Thorlabs camera", "CAMERA::00000001")]
    assert "thorlabs" in out["devices"][0]["detail"]
    # a missing IDS runtime is normal there: no IDS note
    assert out["note"] == ""
    assert calls == ["sdk", "discover", "dispose"]


def test_ids_and_thorlabs_side_by_side_and_a_held_one(monkeypatch):
    _fake_ids(monkeypatch, [_Descr("4100000001", "U3-0000XCP-M", "cam1")])
    _fake_thorlabs(monkeypatch, ["00000002"])
    lock = hwlock.claim("CAMERA::00000002", "camera", wait_s=0.0)
    try:
        out = P.probe()
    finally:
        lock.release()
    rows = {d["address"]: d for d in out["devices"]}
    assert set(rows) == {"4100000001", "00000002"}
    assert "held by the running camera service" in rows["00000002"]["detail"]


def test_a_thorlabs_failure_is_a_note_and_the_sdk_is_still_disposed(monkeypatch):
    _fake_ids(monkeypatch, [_Descr("4100000001", "U3-0000XCP-M", "cam1")])
    calls = _fake_thorlabs(monkeypatch, [], fail="boom")
    out = P.probe()
    assert [d["address"] for d in out["devices"]] == ["4100000001"]
    assert "Thorlabs SDK could not list cameras" in out["note"] and "boom" in out["note"]
    assert calls[-1] == "dispose"


def test_without_the_thorlabs_sdk_the_ids_list_is_unchanged(monkeypatch):
    calls = _fake_ids(monkeypatch, [_Descr("4100000001", "U3-0000XCP-M", "cam1")])
    out = P.probe()
    assert [d["address"] for d in out["devices"]] == ["4100000001"]
    assert out["note"] == "" and calls == ["init", "update", "close"]
