"""scripts/probe.py (Mission Control's "Instruments on this PC"): it LISTS the
analysers' serial numbers through a fake sa_api.dll and calls NOTHING else --
every other function of the fake raises. Serials are made up."""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from pathlib import Path

from signalhound import hwlock, probe as P

ROOT = Path(__file__).resolve().parents[1]


class _Fn:
    def __init__(self, fn):
        self._fn, self.restype, self.argtypes = fn, None, None

    def __call__(self, *a):
        return self._fn(*a)


class ListOnlyDll:
    """saGetSerialNumberList works; touching anything else is a test failure."""

    def __init__(self, serials):
        self.serials, self.calls = serials, []

        def listing(arr, count):
            self.calls.append("saGetSerialNumberList")
            for i, s in enumerate(self.serials):
                arr[i] = s
            count._obj.value = len(self.serials)      # through ctypes.byref()
            return 0
        self.saGetSerialNumberList = _Fn(listing)

    def __getattr__(self, name):
        if name.startswith("sa"):
            def refuse(*a):
                raise AssertionError(f"the probe called {name}")
            return _Fn(refuse)
        raise AttributeError(name)


def test_lists_serials_and_calls_nothing_else():
    dll = ListOnlyDll([17000001, 17000002])
    out = P.probe(dll=dll)
    assert [(d["address"], d["lock"]) for d in out["devices"]] == [
        ("17000001", "SIGNALHOUND::17000001"), ("17000002", "SIGNALHOUND::17000002")]
    assert dll.calls == ["saGetSerialNumberList"] and out["note"] == ""


def test_a_held_analyser_is_reported():
    lock = hwlock.claim("SIGNALHOUND::17000003", "signalhound", wait_s=0.0)
    try:
        out = P.probe(dll=ListOnlyDll([]))
    finally:
        lock.release()
    assert [(d["address"], d["detail"]) for d in out["devices"]] == [("17000003", P.HELD)]


def test_a_missing_dll_is_a_note(monkeypatch):
    def fail(path):
        raise OSError("not found")
    monkeypatch.setattr(ctypes, "CDLL", fail)
    out = P.probe()
    assert out["devices"] == [] and "sa_api.dll" in out["note"]


def test_the_script_prints_one_ascii_json_line():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "probe.py")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    line = [ln for ln in r.stdout.splitlines() if ln.strip()][-1]
    assert isinstance(json.loads(line)["devices"], list) and line.isascii()
