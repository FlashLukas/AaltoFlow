"""One SMB100A, one service: the real backend claims its GPIB address.

Lukas's rule: "the same instrument has to be defined by the same physical
address". VisaSMB100A.open() claims the VISA resource (smb/hwlock.py) before the
first byte, releases it in close() and in every failure path of open(). The
simulator claims nothing. All offline: pyvisa is a fake, the lock folder is a
temp dir (conftest.py sets AALTOFLOW_LOCK_DIR).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from smb import hwlock
from smb.backends.visa_scpi import VisaSMB100A


class _Inst:
    def __init__(self, fail_on=None):
        self.writes = []
        self.timeout = None
        self.write_termination = self.read_termination = None
        self._fail_on = fail_on

    def write(self, cmd):
        if cmd == self._fail_on:
            raise OSError("VI_ERROR_TMO (fake)")
        self.writes.append(cmd)

    def query(self, cmd):
        return {"UNIT:ANGL?": "DEG"}.get(cmd, "0") + "\n"

    def close(self):
        pass


@pytest.fixture
def fake_pyvisa(monkeypatch):
    """A pyvisa whose open_resource hands out _Inst objects (and records them)."""
    state = {"opened": [], "fail_open": False, "fail_on": None}

    class RM:
        def open_resource(self, name):
            if state["fail_open"]:
                raise OSError("VI_ERROR_RSRC_NFOUND (fake)")
            inst = _Inst(state["fail_on"])
            state["opened"].append((name, inst))
            return inst

        def close(self):
            pass

    mod = types.ModuleType("pyvisa")
    mod.ResourceManager = RM
    monkeypatch.setitem(sys.modules, "pyvisa", mod)
    return state


def _backend(resource="GPIB0::28::INSTR"):
    return VisaSMB100A(resource, settle_s=0)


def test_second_backend_on_same_address_is_refused(fake_pyvisa):
    a = _backend()
    a.open()
    b = _backend()
    with pytest.raises(hwlock.HardwareBusy, match="smb"):
        b.open()
    # the refused one never talked to the instrument: only A's session exists
    assert len(fake_pyvisa["opened"]) == 1
    a.close()


@pytest.mark.parametrize("other", ["GPIB::28", "gpib0::28::INSTR", "GPIB0::28"])
def test_same_address_written_differently_conflicts(fake_pyvisa, other):
    a = _backend("GPIB0::28::INSTR")
    a.open()
    with pytest.raises(hwlock.HardwareBusy):
        _backend(other).open()
    a.close()


def test_other_address_does_not_conflict(fake_pyvisa):
    a, b = _backend("GPIB0::28::INSTR"), _backend("GPIB0::29::INSTR")
    a.open()
    b.open()
    assert len(hwlock.held()) == 2
    a.close()
    b.close()


def test_close_releases(fake_pyvisa):
    a = _backend()
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["smb"]
    a.close()
    assert hwlock.held() == []
    b = _backend()
    b.open()                                   # a new open succeeds
    b.close()


@pytest.mark.parametrize("how", ["open_resource", "first_write"])
def test_failing_open_releases(fake_pyvisa, how):
    if how == "open_resource":
        fake_pyvisa["fail_open"] = True
    else:
        fake_pyvisa["fail_on"] = "*CLS"
    a = _backend()
    with pytest.raises(OSError):
        a.open()
    assert hwlock.held() == []
    fake_pyvisa["fail_open"] = False
    fake_pyvisa["fail_on"] = None
    b = _backend()
    b.open()
    b.close()


def test_busy_start_sends_no_rf_off(fake_pyvisa):
    """A generator that loses the claim must not switch off somebody else's RF."""
    from smb.config import Config
    from smb.generator import Generator
    a = _backend()
    a.open()
    g = Generator(_backend(), Config())
    with pytest.raises(hwlock.HardwareBusy):
        g.start()
    g.shutdown()                               # the crash path of the service
    owner_inst = fake_pyvisa["opened"][0][1]
    assert "OUTP:STAT OFF" not in owner_inst.writes
    assert len(fake_pyvisa["opened"]) == 1
    a.close()


def test_sim_claims_nothing():
    from smb.config import Config
    from smb.sim_system import build_sim_system
    gen, _ = build_sim_system(Config())
    gen.start()
    assert hwlock.held() == []
    gen.shutdown()
    assert hwlock.held() == []


def test_the_module_copy_of_hwlock_is_the_master():
    master = (Path(__file__).resolve().parents[4] / "suite-common" / "src"
              / "suite_common" / "hwlock.py")
    if not master.exists():
        pytest.skip("suite-common not next to this module (installed on its own)")
    assert Path(hwlock.__file__).read_bytes() == master.read_bytes()
