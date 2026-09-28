"""One 2450, one service (Lukas: "the same instrument has to be defined by the
same physical address").

The REAL backend claims its VISA address in open() before the first byte goes
to the instrument (hwlock.py); these tests run it against a fake pyvisa, so no
VISA or 2450 is needed. The conftest points AALTOFLOW_LOCK_DIR at a temp
folder, so nothing here touches the PC's real lock folder.
"""

import sys
import types

import pytest

from k2450 import hwlock
from k2450.backends.scpi_2450 import VisaK2450
from k2450.config import Config
from k2450.sim_system import build_sim_system


class _Inst:
    """Just enough of a 2450 for open(): *IDN?, *LANG?, *CLS."""

    def __init__(self, lang="SCPI"):
        self.lang = lang
        self.writes = []
        self.closed = False
        self.timeout = 0
        self.read_termination = self.write_termination = ""

    def query(self, cmd):
        return {"*IDN?": "KEITHLEY INSTRUMENTS,MODEL 2450,FAKE,1.0",
                "*LANG?": self.lang}[cmd] + "\n"

    def write(self, cmd):
        self.writes.append(cmd)

    def close(self):
        self.closed = True


@pytest.fixture
def visa(monkeypatch):
    """A fake pyvisa. `visa.opened` lists every resource actually opened, so a
    test can prove a refused claim never reached the instrument."""
    state = types.SimpleNamespace(opened=[], lang="SCPI", insts=[])

    class RM:
        def __init__(self, *a):
            pass

        def open_resource(self, name):
            state.opened.append(name)
            inst = _Inst(state.lang)
            state.insts.append(inst)
            return inst

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "pyvisa", types.SimpleNamespace(ResourceManager=RM))
    return state


def test_second_backend_on_the_same_address_is_refused(visa):
    a = VisaK2450("GPIB0::18::INSTR")
    a.open()
    try:
        b = VisaK2450("GPIB0::18::INSTR")
        with pytest.raises(hwlock.HardwareBusy, match="k2450"):
            b.open()
        assert visa.opened == ["GPIB0::18::INSTR"]      # b never opened the 2450
        assert b._inst is None and b._claim is None
    finally:
        a.close()


@pytest.mark.parametrize("first, second", [
    ("GPIB0::18::INSTR", "GPIB::18"),
    ("gpib0::18", "GPIB0::18::INSTR"),
    ("USB0::0x05E6::0x2450::04412345::INSTR", "usb0::0x05e6::0x2450::04412345"),
    ("TCPIP0::192.168.1.50::inst0::INSTR", "TCPIP0::192.168.1.50::5025::SOCKET"),
])
def test_the_same_address_spelled_differently_conflicts(visa, first, second):
    a = VisaK2450(first)
    a.open()
    try:
        with pytest.raises(hwlock.HardwareBusy):
            VisaK2450(second).open()
    finally:
        a.close()


def test_the_visa_canonical_name_is_claimed_for_an_alias(visa, monkeypatch):
    """An NI MAX alias ("SMU1") and the full resource name are the same box:
    the backend asks VISA for the canonical name and claims that."""
    import pyvisa  # the fake
    monkeypatch.setattr(pyvisa.ResourceManager, "resource_info",
                        lambda self, r: types.SimpleNamespace(
                            resource_name="GPIB0::18::INSTR"), raising=False)
    a = VisaK2450("SMU1")
    a.open()
    try:
        with pytest.raises(hwlock.HardwareBusy):
            VisaK2450("GPIB::18").open()
    finally:
        a.close()


def test_close_releases_the_address(visa):
    a = VisaK2450("GPIB0::18::INSTR")
    a.open()
    a.close()
    assert visa.insts[0].writes[-1] == ":OUTP OFF"      # off BEFORE the release
    assert hwlock.held() == []
    b = VisaK2450("GPIB::18")
    b.open()                                            # free again
    b.close()


def test_a_failed_open_releases_the_address(visa):
    """The 2450 answers in the wrong command set: open() fails AFTER the claim
    -- the claim and the VISA session must both be let go."""
    visa.lang = "TSP"
    a = VisaK2450("GPIB0::18::INSTR")
    with pytest.raises(RuntimeError, match="command set"):
        a.open()
    assert a._claim is None and a._inst is None
    assert visa.insts[0].closed is True
    assert hwlock.held() == []
    assert visa.insts[0].writes == []                   # and nothing was written
    visa.lang = "SCPI"
    b = VisaK2450("GPIB0::18::INSTR")
    b.open()
    b.close()


def test_the_brain_start_refused_writes_nothing_and_shutdown_is_harmless(visa):
    """The service path: SourceMeter.start() on a busy address raises
    HardwareBusy; the shutdown that may follow must not send :OUTP OFF to a
    2450 another service is driving."""
    from k2450.smu import SourceMeter
    holder = VisaK2450("GPIB0::18::INSTR")
    holder.open()
    try:
        smu = SourceMeter(VisaK2450("GPIB::18"), Config())
        with pytest.raises(hwlock.HardwareBusy):
            smu.start(poll=False)
        smu.shutdown()
        assert len(visa.insts) == 1                     # only the holder's session
        assert visa.insts[0].writes == ["*CLS"]         # no stray :OUTP OFF
    finally:
        holder.close()


def test_the_simulator_claims_nothing():
    smu, _ = build_sim_system(Config(), realtime=False, seed=0)
    smu.start(poll=False)
    try:
        assert hwlock.held() == []
        smu2, _ = build_sim_system(Config(), realtime=False, seed=1)
        smu2.start(poll=False)                          # two sims: no conflict
        smu2.shutdown()
    finally:
        smu.shutdown()
    assert hwlock.held() == []
