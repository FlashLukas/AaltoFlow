"""One generator, one service: the GPIB address claim in visa_8648.open().

Lukas's rule: the same instrument is defined by the same physical address, and
two services must never drive it at once. The real backend claims its VISA
resource (hwlock.py) BEFORE it sends anything, and gives it back in close() and
on every failed open. These tests run against a fake pyvisa; the lock files go
to a temp folder (conftest.py sets AALTOFLOW_LOCK_DIR).
"""

import sys
import types

import pytest

from hp8648 import hwlock
from hp8648.hwlock import HardwareBusy
from hp8648.backends.visa_8648 import Visa8648

from test_real_backend import FakeInstrument, _busy_state


@pytest.fixture
def fake_visa(monkeypatch):
    """A fake pyvisa that records every instrument it opened (so a test can
    see whether a refused open ever reached the bus)."""
    holder = {"opened": [], "state": _busy_state(), "fail_idn": False}

    class RM:
        def open_resource(self, name):
            inst = FakeInstrument(dict(holder["state"]))
            if holder["fail_idn"]:
                def boom(cmd, _q=inst.query):
                    if cmd == "*IDN?":
                        raise OSError("VI_ERROR_TMO: timeout")
                    return _q(cmd)
                inst.query = boom
            holder["opened"].append(inst)
            return inst

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "pyvisa", types.SimpleNamespace(ResourceManager=RM))
    return holder


def test_second_open_on_same_address_is_refused(fake_visa):
    a = Visa8648("GPIB0::19::INSTR")
    a.open()
    try:
        b = Visa8648("GPIB0::19::INSTR")
        with pytest.raises(HardwareBusy, match="hp8648"):
            b.open()
        # the refused backend never opened a VISA session: nothing was sent
        assert len(fake_visa["opened"]) == 1
        # and its close() (the shutdown path) sends nothing either
        b.close()
        assert fake_visa["opened"][0].writes == ["*CLS"]
    finally:
        a.close()


@pytest.mark.parametrize("first,second", [
    ("GPIB0::19::INSTR", "GPIB::19"),
    ("gpib0::19", "GPIB0::19::INSTR"),
])
def test_same_address_written_differently_conflicts(fake_visa, first, second):
    a = Visa8648(first)
    a.open()
    try:
        with pytest.raises(HardwareBusy, match="GPIB0::19"):
            Visa8648(second).open()
    finally:
        a.close()


def test_different_address_does_not_conflict(fake_visa):
    a, b = Visa8648("GPIB0::19::INSTR"), Visa8648("GPIB0::20::INSTR")
    a.open()
    b.open()
    a.close()
    b.close()


def test_close_releases(fake_visa):
    a = Visa8648("GPIB0::19::INSTR")
    a.open()
    assert [h["normalized"] for h in hwlock.held()] == ["GPIB0::19"]
    assert hwlock.held()[0]["module"] == "hp8648"
    a.close()
    assert hwlock.held() == []
    b = Visa8648("GPIB0::19::INSTR")
    b.open()                              # the address is free again
    b.close()


def test_failing_open_releases_and_sends_no_rf_off(fake_visa):
    fake_visa["fail_idn"] = True
    a = Visa8648("GPIB0::19::INSTR")
    with pytest.raises(OSError):
        a.open()
    assert hwlock.held() == []
    inst = fake_visa["opened"][0]
    assert inst.closed                    # the half-open session was closed
    a.close()                             # shutdown path after a failed open
    assert inst.writes == ["*CLS"]        # no OUTP:STAT OFF to it afterwards
    fake_visa["fail_idn"] = False
    b = Visa8648("GPIB0::19::INSTR")
    b.open()
    b.close()


def test_sim_backend_claims_nothing():
    from hp8648.config import Config
    from hp8648.sim_system import build_sim_system
    src, _ = build_sim_system(Config())
    src.start()
    try:
        assert hwlock.held() == []
    finally:
        src.shutdown()
    assert hwlock.held() == []
