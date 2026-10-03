"""scripts/probe.py (Mission Control's "Instruments on this PC"): it LISTS the
NI-DAQmx devices through a fake nidaqmx and creates no task -- Task() raises.
Serials are made up."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

from usb6001 import hwlock, probe as P

ROOT = Path(__file__).resolve().parents[1]


def _fake_nidaqmx(monkeypatch, devices):
    def no_task(*a, **k):
        raise AssertionError("the probe created a DAQmx task")
    system = types.ModuleType("nidaqmx.system")
    system.System = types.SimpleNamespace(
        local=lambda: types.SimpleNamespace(devices=list(devices)))
    system.Device = lambda name: (_ for _ in ()).throw(AssertionError("not needed"))
    pkg = types.ModuleType("nidaqmx")
    pkg.system = system
    pkg.Task = no_task
    monkeypatch.setitem(sys.modules, "nidaqmx", pkg)
    monkeypatch.setitem(sys.modules, "nidaqmx.system", system)


def _dev(name, product, serial):
    return types.SimpleNamespace(name=name, product_type=product, serial_num=serial)


def test_lists_cards_by_daqmx_name(monkeypatch):
    _fake_nidaqmx(monkeypatch, [_dev("Dev1", "USB-6001", 0x01ABCDEF),
                                _dev("Dev2", "USB-6259 (BNC)", 0x00123456)])
    out = P.probe()
    assert out["devices"] == [
        {"address": "Dev1", "identity": "NI USB-6001", "detail": "DAQmx name Dev1, serial 01ABCDEF"},
        {"address": "Dev2", "identity": "NI USB-6259 (BNC)",
         "detail": "DAQmx name Dev2, serial 00123456, not a USB-6001"}]
    assert out["note"] == ""


def test_a_held_card_is_reported(monkeypatch):
    _fake_nidaqmx(monkeypatch, [])
    lock = hwlock.claim("Dev3", "usb6001", wait_s=0.0)
    try:
        out = P.probe()
    finally:
        lock.release()
    assert [(d["address"], d["detail"]) for d in out["devices"]] == [("Dev3", P.HELD)]


def test_missing_nidaqmx_is_a_note(monkeypatch):
    monkeypatch.setitem(sys.modules, "nidaqmx", None)
    out = P.probe()
    assert out["devices"] == [] and "uv sync --all-extras" in out["note"]


def test_the_script_prints_one_ascii_json_line():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "probe.py")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    line = [ln for ln in r.stdout.splitlines() if ln.strip()][-1]
    assert isinstance(json.loads(line)["devices"], list) and line.isascii()
