"""scripts/probe.py (Mission Control's "Instruments on this PC"): it LISTS the
meters through a fake TLPMX DLL with vi = 0 and opens none -- TLPMX_init (and
every other function) raises. Serials are made up."""

from __future__ import annotations

import ctypes as C
import json
import subprocess
import sys
from pathlib import Path

from pm16 import hwlock, probe as P
from pm16.backends import tlpmx

ROOT = Path(__file__).resolve().parents[1]
MODULE = "pm16"


class ListOnlyTLPMX:
    """findRsrc / getRsrcName / getRsrcInfo with a session handle of 0; any
    other call (TLPMX_init opens the meter) fails the test."""

    def __init__(self, meters):
        self.meters, self.calls = meters, []

    def TLPMX_findRsrc(self, vi, count):
        assert vi == 0
        self.calls.append("findRsrc")
        count._obj.value = len(self.meters)
        return 0

    def TLPMX_getRsrcName(self, vi, i, buf):
        assert vi == 0
        self.calls.append("getRsrcName")
        buf.value = self.meters[i][0].encode()
        return 0

    def TLPMX_getRsrcInfo(self, vi, i, model, serial, manuf, avail):
        assert vi == 0
        self.calls.append("getRsrcInfo")
        res, mod, sn, free = self.meters[i]
        model.value, serial.value, manuf.value = mod.encode(), sn.encode(), b"Thorlabs"
        avail._obj.value = 1 if free else 0
        return 0

    def __getattr__(self, name):
        def refuse(*a):
            raise AssertionError(f"the probe called {name}")
        return refuse


METERS = [("USB0::0x1313::0x807B::100000001::INSTR", "PM160", "100000001", True),
          ("USB0::0x1313::0x807D::P5000001::INSTR", "PM400", "P5000001", False),
          ("USB0::0x1313::0x8078::P0000001::INSTR", "PM100D", "P0000001", True)]


def _fake(monkeypatch, meters=METERS):
    dll = ListOnlyTLPMX(meters)
    monkeypatch.setattr(tlpmx, "load_dll", lambda path="": dll)
    return dll


def test_lists_only_its_own_meters_without_a_session(monkeypatch):
    dll = _fake(monkeypatch)
    out = P.probe()
    mine = [m for m in METERS if P._mine({"model": m[1], "resource": m[0]})]
    assert len(mine) == 1
    assert [d["address"] for d in out["devices"]] == [mine[0][0]]
    assert "2 other Thorlabs meter(s)" in out["note"]
    assert set(dll.calls) == {"findRsrc", "getRsrcName", "getRsrcInfo"}


def test_a_held_meter_is_reported(monkeypatch):
    _fake(monkeypatch, [])
    res = "USB0::0x1313::0x8000::H0000001::INSTR"
    lock = hwlock.claim(res, MODULE, wait_s=0.0)
    try:
        out = P.probe()
    finally:
        lock.release()
    assert [(d["address"], d["detail"]) for d in out["devices"]] == [(res, P.HELD)]


def test_missing_tlpmx_is_a_note(monkeypatch):
    def missing(path=""):
        raise tlpmx.TLPMXError("TLPMX library not found")
    monkeypatch.setattr(tlpmx, "load_dll", missing)
    out = P.probe()
    assert out["devices"] == [] and "TLPMX unavailable" in out["note"]


def test_the_script_prints_one_ascii_json_line():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "probe.py")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    line = [ln for ln in r.stdout.splitlines() if ln.strip()][-1]
    assert isinstance(json.loads(line)["devices"], list) and line.isascii()
