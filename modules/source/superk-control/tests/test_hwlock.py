"""One physical laser, one service: the COM-port claim of the real backend.

The whole SuperK system (EXTREME + RF driver + SELECT housings) sits behind ONE
USB virtual COM port, so that port is what the real backend claims (hwlock).
These tests run offline: the NKT DLL is replaced by a tiny fake, and the lock
files go to a temporary folder (AALTOFLOW_LOCK_DIR), never the real one.
"""

from __future__ import annotations

import pytest

from superk import hwlock
from superk.backends import nktp
from superk.backends.nktp import NktpSuperK, NKTError
from superk.config import Config
from superk.hwlock import HardwareBusy
from superk.sim_system import build_sim_system


class _FakeDLL:
    """Just enough of NKTPDLL for open()/close(): every call succeeds (0)
    unless told otherwise, and every call is recorded."""

    def __init__(self, open_result=0, find_raises=False):
        self.open_result = open_result
        self.find_raises = find_raises
        self.calls = []

    def openPorts(self, port, auto, live):
        self.calls.append(("openPorts", port))
        return self.open_result

    def closePorts(self, port):
        self.calls.append(("closePorts", port))
        return 0

    def deviceGetAllTypes(self, port, buf, n):
        self.calls.append(("deviceGetAllTypes", port))
        if self.find_raises:
            raise OSError("bus error")
        n._obj.value = 0                       # no modules: keep configured addresses
        return 0

    def __getattr__(self, name):               # registerRead*/registerWrite*
        if name.startswith("register"):
            def call(*a):
                self.calls.append((name,) + a[1:3])
                return 0
            return call
        raise AttributeError(name)


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    return tmp_path / "locks"


def _backend(port, monkeypatch, dll=None):
    b = NktpSuperK(port)
    fake = dll or _FakeDLL()
    monkeypatch.setattr(b, "_load_dll", lambda: fake)
    return b, fake


def test_second_backend_on_the_same_port_is_refused(monkeypatch):
    a, _ = _backend("COM5", monkeypatch)
    b, fake_b = _backend("COM5", monkeypatch)
    a.open()
    try:
        with pytest.raises(HardwareBusy, match="superk") as exc:
            b.open()
        assert "COM5" in str(exc.value)
        assert fake_b.calls == []              # not one call reached the laser
        assert b._lock is None and b._dll is None
    finally:
        a.close()


@pytest.mark.parametrize("first,second", [("COM5", "com5"), ("COM5", "ASRL5::INSTR"),
                                          ("\\\\.\\COM5", "COM5")])
def test_same_port_written_differently_conflicts(monkeypatch, first, second):
    a, _ = _backend(first, monkeypatch)
    b, _ = _backend(second, monkeypatch)
    a.open()
    try:
        with pytest.raises(HardwareBusy, match="superk"):
            b.open()
    finally:
        a.close()


def test_different_ports_do_not_conflict(monkeypatch):
    a, _ = _backend("COM5", monkeypatch)
    b, _ = _backend("COM6", monkeypatch)
    a.open()
    b.open()
    a.close()
    b.close()


def test_close_releases(monkeypatch):
    a, _ = _backend("COM5", monkeypatch)
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["superk"]
    a.close()
    assert hwlock.held() == []
    b, _ = _backend("COM5", monkeypatch)
    b.open()                                   # a new open succeeds
    b.close()


@pytest.mark.parametrize("dll", [_FakeDLL(open_result=7), _FakeDLL(find_raises=True)])
def test_failing_open_releases_and_sends_nothing(monkeypatch, dll):
    a, fake = _backend("COM5", monkeypatch, dll)
    with pytest.raises((NKTError, OSError)):
        a.open()
    assert hwlock.held() == []
    assert a._dll is None
    # no register write (emission/RF off) to a system we never took over
    assert not any(c[0].startswith("registerWrite") for c in fake.calls)
    a.close()                                  # safe, sends nothing either
    assert not any(c[0].startswith("registerWrite") for c in fake.calls)
    b, _ = _backend("COM5", monkeypatch)
    b.open()                                   # the port is free again
    b.close()


def test_missing_dll_releases(monkeypatch, tmp_path):
    monkeypatch.delenv("NKTP_SDK_PATH", raising=False)
    a = NktpSuperK("COM5", dll_path=str(tmp_path / "missing.dll"))
    with pytest.raises(NKTError, match="NKTPDLL"):
        a.open()
    assert hwlock.held() == []


def test_brain_start_on_busy_port_sends_nothing(monkeypatch):
    """The service path: the brain's start fails with HardwareBusy, and the
    shutdown that follows (serve_forever's finally) must not send RF/emission
    OFF to the laser another service owns."""
    from superk.laser import SuperK
    a, _ = _backend("COM5", monkeypatch)
    a.open()
    try:
        b, fake_b = _backend("COM5", monkeypatch)
        laser = SuperK(b, Config())
        with pytest.raises(HardwareBusy):
            laser.start()
        laser.shutdown()
        assert fake_b.calls == []
        assert [h["module"] for h in hwlock.held()] == ["superk"]   # still a's
    finally:
        a.close()


def test_sim_never_claims():
    laser, _ = build_sim_system(Config())
    laser.start()
    try:
        assert hwlock.held() == []
    finally:
        laser.shutdown()
    assert hwlock.held() == []


def test_module_key():
    assert nktp.MODULE_KEY == "superk"
