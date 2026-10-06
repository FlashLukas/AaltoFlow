"""One physical instrument, one service (Lukas's rule, hwlock.py).

The real backend claims its VISA address before it sends a byte, refuses a
second claim of the same address, and gives it back on close() and on a
failed open(). The simulator claims nothing. Offline: a fake `pyvisa`
(fake_visa.py) and a private lock folder (conftest's autouse fixture).
"""

import sys

import pytest

from scope import hwlock
from scope.backends.siglent import SiglentSDS
from scope.hwlock import HardwareBusy

from fake_visa import fake_visa, FakeSDS  # noqa: F401  (pytest fixture)

RES = "USB0::0xF4EC::0xEE3A::SDS00000000001::INSTR"


def test_second_open_of_the_same_scope_is_refused(fake_visa):
    first = SiglentSDS(RES)
    first.open()
    with pytest.raises(HardwareBusy) as err:
        SiglentSDS(RES).open()
    assert "scope" in str(err.value)
    assert len(fake_visa) == 1               # refused before a session was opened
    first.close()


def test_a_different_scope_is_not_blocked(fake_visa):
    a, b = SiglentSDS(RES), SiglentSDS("GPIB0::18::INSTR")
    a.open()
    b.open()
    assert len(hwlock.held()) == 2
    a.close()
    b.close()


def test_close_releases_the_claim(fake_visa):
    a = SiglentSDS(RES)
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["scope"]
    a.close()
    assert hwlock.held() == []
    again = SiglentSDS(RES)
    again.open()              # would raise HardwareBusy if close() had kept it
    again.close()


def test_a_failed_open_releases_the_claim(fake_visa, monkeypatch):
    monkeypatch.setattr(FakeSDS, "query",
                        lambda self, cmd: (_ for _ in ()).throw(TimeoutError("no answer")))
    b = SiglentSDS(RES)
    with pytest.raises(TimeoutError):
        b.open()
    assert hwlock.held() == []
    assert fake_visa[0].closed and fake_visa[0].writes == []


def test_a_missing_pyvisa_releases_the_claim(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyvisa", None)
    with pytest.raises(ImportError):
        SiglentSDS(RES).open()
    assert hwlock.held() == []


def test_the_simulator_claims_nothing():
    from scope.config import Config
    from scope.sim_system import build_sim_system
    scope, _ = build_sim_system(Config())
    scope.start()
    try:
        assert hwlock.held() == []
    finally:
        scope.shutdown()
