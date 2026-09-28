"""One physical instrument, one service (hwlock).

Lukas's rule: "the same instrument has to be defined by the same physical
address". The real CS260 backend claims its GPIB address in open() before the
first byte goes out, so a second service on the same address is refused with
a message naming the holder. pyvisa is replaced by a fake module, so this runs
offline; the lock files go to a temp folder (AALTOFLOW_LOCK_DIR), never to the
real lock folder (LOCALAPPDATA/AaltoFlow/locks) where a running service may hold one.
"""

import sys
import types

import pytest

from cs260 import hwlock
from cs260.backends.cornerstone import CornerstoneGPIB
from cs260.config import Config
from cs260.sim_system import build_sim_system
from test_real_backend import FakeInst


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path))
    return tmp_path


class _FakeRM:
    """Stands in for pyvisa.ResourceManager; `fail` makes open_resource or the
    first query blow up, to test that a failed open gives the address back."""
    fail = None            # None | "open" | "query"
    opened = []

    def open_resource(self, resource):
        if _FakeRM.fail == "open":
            raise OSError("VI_ERROR_RSRC_NFOUND (fake)")
        inst = FakeInst()
        if _FakeRM.fail == "query":
            def boom(cmd):
                raise TimeoutError("VI_ERROR_TMO (fake)")
            inst.query = boom
        _FakeRM.opened.append(resource)
        return inst

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_pyvisa(monkeypatch):
    mod = types.ModuleType("pyvisa")
    mod.ResourceManager = _FakeRM
    monkeypatch.setitem(sys.modules, "pyvisa", mod)
    _FakeRM.fail = None
    _FakeRM.opened = []
    yield


def test_second_backend_on_same_address_is_refused():
    a = CornerstoneGPIB("GPIB0::4::INSTR")
    a.open()
    b = CornerstoneGPIB("GPIB0::4::INSTR")
    with pytest.raises(hwlock.HardwareBusy, match="cs260"):
        b.open()
    # the refused backend never reached the bus
    assert _FakeRM.opened == ["GPIB0::4::INSTR"]
    a.close()


@pytest.mark.parametrize("other", ["GPIB::4", "gpib0::4::INSTR", "GPIB0::4"])
def test_same_address_written_differently_conflicts(other):
    a = CornerstoneGPIB("GPIB0::4::INSTR")
    a.open()
    try:
        with pytest.raises(hwlock.HardwareBusy, match=r"GPIB0::4 is already in use by cs260"):
            CornerstoneGPIB(other).open()
    finally:
        a.close()


def test_other_address_is_independent():
    a = CornerstoneGPIB("GPIB0::4::INSTR")
    b = CornerstoneGPIB("GPIB0::5::INSTR")
    a.open(); b.open()
    assert {h["normalized"] for h in hwlock.held()} == {"GPIB0::4", "GPIB0::5"}
    a.close(); b.close()
    assert hwlock.held() == []


def test_close_releases():
    a = CornerstoneGPIB("GPIB0::4::INSTR")
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["cs260"]
    a.close()
    assert hwlock.held() == []
    b = CornerstoneGPIB("GPIB0::4::INSTR")
    b.open()                              # a new open succeeds
    b.close()
    a.close()                             # closing twice is harmless


@pytest.mark.parametrize("where", ["open", "query"])
def test_failing_open_releases(where):
    _FakeRM.fail = where
    a = CornerstoneGPIB("GPIB0::4::INSTR", timeout_ms=10)
    with pytest.raises(Exception):
        a.open()
    assert hwlock.held() == []
    _FakeRM.fail = None
    b = CornerstoneGPIB("GPIB0::4::INSTR")
    b.open()
    b.close()


def test_brain_start_failure_leaves_nothing_claimed_and_sends_nothing():
    """The service path: Monochromator.start() on a busy address raises
    HardwareBusy, and the shutdown that may follow must not close the shutter
    of a box another service owns (it never opened it)."""
    from cs260.monochromator import Monochromator
    holder = hwlock.claim("GPIB0::4::INSTR", "other")
    try:
        mono = Monochromator(CornerstoneGPIB("GPIB::4"), Config())
        with pytest.raises(hwlock.HardwareBusy, match="other"):
            mono.start(poll=False)
        mono.shutdown()                   # must not raise, must not touch the bus
        assert _FakeRM.opened == []
    finally:
        holder.release()


def test_sim_backend_claims_nothing():
    mono, _ = build_sim_system(Config())
    mono.start(poll=False)
    try:
        assert hwlock.held() == []
    finally:
        mono.shutdown()
    assert hwlock.held() == []
