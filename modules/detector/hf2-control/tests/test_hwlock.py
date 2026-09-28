"""One physical HF2LI, one service (Lukas's rule, 2026-09-27).

"The same instrument has to be defined by the same physical address." For the
HF2LI that address is its device id (devNNNN = its serial). The real backend
claims it in open() BEFORE contacting LabOne, releases it in close(), and a
failed open must not leave it claimed. The simulator claims nothing.

Runs offline: `zhinst.core` is a fake (as in test_adopt.py) and conftest.py
points AALTOFLOW_LOCK_DIR at a temp folder.
"""

import os
import subprocess
import sys
import types

import pytest

from hf2 import hwlock
from hf2.config import Config
from hf2.hwlock import HardwareBusy
from hf2.lockin import LockIn
from hf2.sim_system import build_sim_system

_HERE = os.path.dirname(os.path.abspath(__file__))


class _FakeDAQ:
    """Enough of ziDAQServer for open()/close(). Records every instance."""

    instances = []
    fail_on_get = False

    def __init__(self, host, port, api_level):
        self.closed = False
        _FakeDAQ.instances.append(self)

    def connectDevice(self, dev, iface):
        pass

    def getString(self, path):
        if _FakeDAQ.fail_on_get:
            raise RuntimeError(f"no node {path}")   # e.g. a wrong device id
        return "HF2LI"

    def disconnect(self):
        self.closed = True


@pytest.fixture
def fake_zhinst(monkeypatch):
    _FakeDAQ.instances = []
    _FakeDAQ.fail_on_get = False
    core = types.ModuleType("zhinst.core")
    core.ziDAQServer = _FakeDAQ
    pkg = types.ModuleType("zhinst")
    pkg.core = core
    monkeypatch.setitem(sys.modules, "zhinst", pkg)
    monkeypatch.setitem(sys.modules, "zhinst.core", core)
    from hf2.backends.zhinst_hf2 import ZhinstHF2
    return ZhinstHF2


def test_second_open_of_same_device_is_refused(fake_zhinst):
    a = fake_zhinst("dev1234")
    a.open()
    try:
        b = fake_zhinst("dev1234")
        with pytest.raises(HardwareBusy, match="hf2"):
            b.open()
        # refused BEFORE the data server was contacted: only a's connection exists
        assert len(_FakeDAQ.instances) == 1
    finally:
        a.close()


def test_same_device_written_differently_conflicts(fake_zhinst):
    # LabOne shows "dev1234"; someone may type "DEV1234" -- the same box.
    a = fake_zhinst("dev1234")
    a.open()
    try:
        with pytest.raises(HardwareBusy):
            fake_zhinst(" DEV1234 ").open()
    finally:
        a.close()


def test_other_device_on_same_server_is_allowed(fake_zhinst):
    # One ziServer can serve two lock-ins; the claim is on the device, not the host.
    a, b = fake_zhinst("dev1234"), fake_zhinst("dev5678")
    a.open()
    b.open()
    a.close()
    b.close()


def test_close_releases(fake_zhinst):
    a = fake_zhinst("dev1234")
    a.open()
    a.close()
    assert hwlock.held() == []
    b = fake_zhinst("dev1234")
    b.open()                       # succeeds: the claim went with close()
    b.close()


def test_failed_open_releases(fake_zhinst):
    _FakeDAQ.fail_on_get = True
    a = fake_zhinst("dev1234")
    with pytest.raises(RuntimeError):
        a.open()
    assert _FakeDAQ.instances[0].closed          # connection dropped too
    assert hwlock.held() == []
    _FakeDAQ.fail_on_get = False
    b = fake_zhinst("dev1234")
    b.open()
    b.close()


def test_failed_start_after_open_releases(fake_zhinst):
    # open() works but reading the channels fails (read_channel raises):
    # LockIn.start must close the backend again, releasing the claim.
    backend = fake_zhinst("dev1234")

    def boom(demod):
        raise RuntimeError("read failed")
    backend.read_channel = boom
    li = LockIn(backend, Config())
    with pytest.raises(RuntimeError, match="read failed"):
        li.start(poll=False)
    assert hwlock.held() == []


def test_sim_claims_nothing():
    li, _ = build_sim_system(Config())
    li.start(poll=False)
    try:
        assert hwlock.held() == []
    finally:
        li.shutdown()


def test_service_busy_is_one_clean_line(tmp_path, monkeypatch):
    # Hold the device from THIS process, then start run_service.py --real on it
    # in a child: it must exit non-zero with one ASCII line naming the holder,
    # no traceback. The child never needs zhinst: the claim fails first.
    lockdir = os.environ["AALTOFLOW_LOCK_DIR"]
    held = hwlock.claim("dev4321", "hf2")
    try:
        env = dict(os.environ, AALTOFLOW_LOCK_DIR=lockdir, PYTHONUNBUFFERED="1")
        r = subprocess.run(
            [sys.executable, os.path.join(_HERE, "..", "scripts", "run_service.py"),
             "--real", "--device", "dev4321", "--cmd-port", "15969", "--pub-port", "15970"],
            capture_output=True, text=True, timeout=60, env=env)
    finally:
        held.release()
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1 and "DEV4321" in lines[0] and "hf2" in lines[0]
    assert f"pid {os.getpid()}" in lines[0]
    lines[0].encode("ascii")
