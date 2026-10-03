"""scripts/probe.py (Mission Control's "Instruments on this PC"): it LISTS the
lock-ins through a fake zhinst.core.ziDiscovery and never connects to a data
server -- ziDAQServer raises. Device ids are made up."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

from hf2 import hwlock, probe as P

ROOT = Path(__file__).resolve().parents[1]


def _fake_zhinst(monkeypatch, found):
    class Discovery:
        def findAll(self):
            return list(found)

        def get(self, dev):
            return found[dev]

    def no_server(*a, **k):
        raise AssertionError("the probe connected to a data server")
    core = types.ModuleType("zhinst.core")
    core.ziDiscovery = Discovery
    core.ziDAQServer = no_server
    pkg = types.ModuleType("zhinst")
    pkg.core = core
    monkeypatch.setitem(sys.modules, "zhinst", pkg)
    monkeypatch.setitem(sys.modules, "zhinst.core", core)


def test_lists_device_ids(monkeypatch):
    _fake_zhinst(monkeypatch, {
        "dev100": {"devicetype": "HF2LI", "interfaces": ["USB"],
                   "serveraddress": "127.0.0.1", "serverport": 8005},
        "dev200": {"devicetype": "MFLI"}})
    out = P.probe()
    assert out["devices"] == [
        {"address": "dev100", "identity": "Zurich Instruments HF2LI",
         "detail": "device id dev100, via USB, data server 127.0.0.1:8005"},
        {"address": "dev200", "identity": "Zurich Instruments MFLI",
         "detail": "device id dev200, not an HF2"}]


def test_held_and_nothing_listed(monkeypatch):
    _fake_zhinst(monkeypatch, {})
    assert "LabOne discovery lists no device" in P.probe()["note"]
    lock = hwlock.claim("dev300", "hf2", wait_s=0.0)
    try:
        out = P.probe()
    finally:
        lock.release()
    assert [(d["address"], d["detail"]) for d in out["devices"]] == [("dev300", P.HELD)]


def test_missing_zhinst_is_a_note(monkeypatch):
    monkeypatch.setitem(sys.modules, "zhinst", None)
    monkeypatch.setitem(sys.modules, "zhinst.core", None)
    out = P.probe()
    assert out["devices"] == [] and "zhinst-core is not installed" in out["note"]


def test_the_script_prints_one_ascii_json_line():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "probe.py")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    line = [ln for ln in r.stdout.splitlines() if ln.strip()][-1]
    assert isinstance(json.loads(line)["devices"], list) and line.isascii()
