"""One physical d-Drive, one service (Lukas: "the same instrument has to be
defined by the same physical address").

The real backend claims its COM port in open() before a byte is sent; a second
claim of the same port -- from any module, spelled any way -- is refused with a
message naming the holder.  All offline: pyserial is replaced by a stub module
and the locks go to a temp folder (conftest.py sets AALTOFLOW_LOCK_DIR).
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

from piezo import hwlock
from piezo.backends.ddrive import DDrivePiezo
from piezo.config import Config
from piezo.hwlock import HardwareBusy
from piezo.sim_system import build_sim_system


class _StubPort:
    """Just enough of serial.Serial for open()/close(); records what it saw."""

    def __init__(self, **kw):
        self.kw = kw
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def stub_serial(monkeypatch):
    opened = []

    def _serial(**kw):
        p = _StubPort(**kw)
        opened.append(p)
        return p

    mod = types.ModuleType("serial")
    mod.Serial = _serial
    monkeypatch.setitem(sys.modules, "serial", mod)
    return opened


def _backend(port: str) -> DDrivePiezo:
    cfg = Config()
    cfg.hardware.port = port
    return DDrivePiezo(cfg)


def test_second_backend_on_same_port_is_refused(stub_serial):
    a = _backend("COM5")
    a.open()
    try:
        b = _backend("COM5")
        with pytest.raises(HardwareBusy) as ei:
            b.open()
        assert "piezo" in str(ei.value) and "COM5" in str(ei.value)
        # the refused backend never reached the port
        assert len(stub_serial) == 1 and b._ser is None
    finally:
        a.close()


def test_same_port_spelled_differently_conflicts(stub_serial):
    a = _backend("com5")
    a.open()
    try:
        for other in ("COM5", "ASRL5::INSTR", "\\\\.\\COM5"):
            with pytest.raises(HardwareBusy):
                _backend(other).open()
    finally:
        a.close()


def test_close_releases_the_port(stub_serial):
    a = _backend("COM7")
    a.open()
    a.close()
    assert stub_serial[0].closed
    assert hwlock.held() == []
    b = _backend("COM7")
    b.open()          # must succeed now
    b.close()


def test_failed_open_releases_the_port(monkeypatch):
    mod = types.ModuleType("serial")

    def _boom(**kw):
        raise OSError("could not open port 'COM9'")

    mod.Serial = _boom
    monkeypatch.setitem(sys.modules, "serial", mod)
    with pytest.raises(OSError):
        _backend("COM9").open()
    assert hwlock.held() == []
    # and a later, working open is not blocked by a leftover claim
    good = types.ModuleType("serial")
    good.Serial = lambda **kw: _StubPort(**kw)
    monkeypatch.setitem(sys.modules, "serial", good)
    b = _backend("COM9")
    b.open()
    b.close()


def test_sim_backend_claims_nothing():
    brain, _ = build_sim_system(Config())
    brain.start()
    try:
        assert hwlock.held() == []
    finally:
        brain.shutdown()


def test_busy_brain_start_sends_nothing_and_shutdown_is_silent(stub_serial):
    """The service path: brain.start() fails with HardwareBusy; the cleanup's
    shutdown() must not touch the instrument we never opened."""
    from piezo.piezo import Piezo

    holder = _backend("COM4")
    holder.open()
    try:
        cfg = Config()
        cfg.hardware.port = "COM4"
        brain = Piezo(DDrivePiezo(cfg), cfg)
        with pytest.raises(HardwareBusy):
            brain.start()
        brain.shutdown()            # returns at once: never connected
        assert stub_serial[0].written == []
    finally:
        holder.close()


def test_run_service_reports_busy_in_one_line(tmp_path, monkeypatch):
    """--real against a port another process holds: one ASCII line on stderr
    naming the holder, exit code 3, no traceback."""
    lockdir = tmp_path / "hwlocks"
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(lockdir))
    lock = hwlock.claim("COM3", "otherkey")      # Config() default port is COM3
    try:
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_service.py"
        # A stub pyserial on the path, so the test runs where pyserial is absent.
        stub = tmp_path / "stub"
        stub.mkdir()
        (stub / "serial.py").write_text("def Serial(**kw):\n    raise AssertionError('opened')\n",
                                        encoding="utf-8")
        env = dict(__import__("os").environ, AALTOFLOW_LOCK_DIR=str(lockdir),
                   PYTHONPATH=str(stub))
        r = subprocess.run([sys.executable, str(script), "--real",
                            "--cmd-port", "15694", "--pub-port", "15695"],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 3, r.stderr
        err = r.stderr.strip()
        assert "Traceback" not in err
        assert err.startswith("piezo: cannot start:") and "otherkey" in err
        assert len(err.splitlines()) == 1
        err.encode("ascii")
    finally:
        lock.release()
