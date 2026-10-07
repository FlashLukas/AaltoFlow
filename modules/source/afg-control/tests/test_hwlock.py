"""One physical instrument, one service (Lukas's rule, hwlock.py).

The real backend must claim its VISA address before it sends a byte, refuse a
second claim of the SAME address, and give it back on close() and on a failed
open(). The simulator claims nothing. All offline: a fake `pyvisa`
(fake_visa.py) and a private lock folder (conftest's autouse fixture sets
AALTOFLOW_LOCK_DIR).
"""

import sys

import pytest

from afg import hwlock
from afg.backends.tek_afg import TekAFG
from afg.hwlock import HardwareBusy

from fake_visa import fake_visa  # noqa: F401  (pytest fixture)

RES = "USB0::0x0699::0x0353::C000001::INSTR"


def test_second_backend_on_the_same_address_is_refused(fake_visa):
    first = TekAFG(RES)
    first.open()
    with pytest.raises(HardwareBusy) as err:
        TekAFG(RES).open()
    assert "afg" in str(err.value)
    # refused BEFORE a session was opened: only the first backend made one
    assert len(fake_visa) == 1
    first.close()


def test_different_addresses_do_not_conflict(fake_visa):
    a = TekAFG(RES)
    b = TekAFG("USB0::0x0699::0x0353::C000002::INSTR")
    a.open()
    b.open()
    assert len(hwlock.held()) == 2
    a.close()
    b.close()


def test_close_releases_the_address(fake_visa):
    first = TekAFG(RES)
    first.open()
    assert [h["module"] for h in hwlock.held()] == ["afg"]
    first.close()
    assert hwlock.held() == []
    again = TekAFG(RES)
    again.open()              # would raise HardwareBusy if close() had kept it
    again.close()


def test_a_failing_open_releases_the_address(fake_visa, monkeypatch):
    """The instrument does not answer *IDN?: open() fails, the address is
    free afterwards and nothing but *CLS was written (no "outputs off" to a
    box we never identified)."""
    from fake_visa import FakeAFGInstrument
    monkeypatch.setattr(FakeAFGInstrument, "read_raw",
                        lambda self: (_ for _ in ()).throw(TimeoutError("no answer")))
    import afg.backends.tek_afg as B
    monkeypatch.setattr(B, "_sleep", lambda s: None)   # open() retries (4 tries)
    b = TekAFG(RES)
    with pytest.raises(B.NoReply, match="4 tries") as err:
        b.open()
    assert isinstance(err.value.__cause__, TimeoutError)
    assert hwlock.held() == []
    assert len(fake_visa) == 4
    assert all(i.closed and i.writes == ["*CLS"] for i in fake_visa)
    b.close()                 # after a failed open: sends nothing, no error
    assert fake_visa[-1].writes == ["*CLS"]


def test_a_missing_pyvisa_releases_the_address(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyvisa", None)   # "import pyvisa" -> ImportError
    with pytest.raises(ImportError):
        TekAFG(RES).open()
    assert hwlock.held() == []


def test_the_simulator_claims_nothing():
    from afg.config import Config
    from afg.sim_system import build_sim_system
    gen, _ = build_sim_system(Config())
    gen.start()
    try:
        assert hwlock.held() == []
    finally:
        gen.shutdown()
    assert hwlock.held() == []
