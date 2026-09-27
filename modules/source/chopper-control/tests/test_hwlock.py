"""One physical chopper, one service (hwlock.py, Lukas's rule).

The real backend claims its COM port before it sends a byte. These tests use
the fake COM port from test_startup_adopt.py, so they run offline; the lock
folder is a temp dir (conftest.py, AALTOFLOW_LOCK_DIR).
"""

from __future__ import annotations

import sys
import types

import pytest

from chopper import hwlock
from chopper.backends import mc2000b
from chopper.chopper import Chopper
from chopper.config import Config
from chopper.sim_system import build_sim_system

from test_startup_adopt import FakeSerial


@pytest.fixture
def fake_port(monkeypatch):
    FakeSerial.sent = []
    FakeSerial.state = dict(FakeSerial.state)
    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=FakeSerial))
    monkeypatch.setattr(mc2000b.time, "sleep", lambda s: None)
    return FakeSerial


def test_second_open_on_same_port_is_refused_naming_chopper(fake_port):
    a = mc2000b.SerialMC2000B("COM5")
    a.open()
    n_sent = len(fake_port.sent)
    b = mc2000b.SerialMC2000B("COM5")
    with pytest.raises(hwlock.HardwareBusy) as ei:
        b.open()
    assert "chopper" in str(ei.value) and "COM5" in str(ei.value)
    # the refused backend never talked to the instrument
    assert len(fake_port.sent) == n_sent
    assert b._ser is None and b._lock is None
    a.close()


@pytest.mark.parametrize("other", ["com5", "ASRL5::INSTR", r"\\.\COM5"])
def test_same_port_spelled_differently_conflicts(fake_port, other):
    a = mc2000b.SerialMC2000B("COM5")
    a.open()
    with pytest.raises(hwlock.HardwareBusy):
        mc2000b.SerialMC2000B(other).open()
    a.close()


def test_other_port_is_independent(fake_port):
    a = mc2000b.SerialMC2000B("COM5")
    b = mc2000b.SerialMC2000B("COM6")
    a.open()
    b.open()
    assert {h["normalized"] for h in hwlock.held()} == {"COM5", "COM6"}
    a.close()
    b.close()


def test_close_releases(fake_port):
    a = mc2000b.SerialMC2000B("COM5")
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["chopper"]
    a.close()
    assert hwlock.held() == []
    a.close()                                 # twice is harmless
    b = mc2000b.SerialMC2000B("COM5")
    b.open()
    b.close()


def test_failing_open_releases(fake_port, monkeypatch):
    # the port opens but the unit does not answer `id?` -> open() raises
    monkeypatch.setattr(FakeSerial, "read_until", lambda self, term: b"")
    a = mc2000b.SerialMC2000B("COM5")
    with pytest.raises(mc2000b.MC2000BError):
        a.open()
    assert hwlock.held() == []
    assert a._ser is None


def test_failing_serial_constructor_releases(monkeypatch):
    def boom(*a, **k):
        raise OSError("could not open port 'COM5'")
    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=boom))
    with pytest.raises(OSError):
        mc2000b.SerialMC2000B("COM5").open()
    assert hwlock.held() == []


def test_brain_start_failure_after_open_releases(fake_port, monkeypatch):
    # open() works, the first config read fails: the brain must close the
    # backend (and so release the port) and must NOT send any command.
    be = mc2000b.SerialMC2000B("COM5")
    monkeypatch.setattr(be, "get_blade", lambda: (_ for _ in ()).throw(
        mc2000b.MC2000BError("blade?: garbled")))
    ch = Chopper(be, Config(), simulated=False)
    with pytest.raises(mc2000b.MC2000BError):
        ch.start(poll=False)
    assert hwlock.held() == []
    assert [ln for ln in fake_port.sent if not ln.endswith("?")] == []


def test_busy_brain_start_sends_nothing_and_shutdown_is_silent(fake_port):
    holder = mc2000b.SerialMC2000B("COM5")
    holder.open()
    fake_port.sent.clear()
    cfg = Config()
    cfg.hardware.stop_on_exit = True          # would send enable=0 if it thought it was connected
    ch = Chopper(mc2000b.SerialMC2000B("COM5"), cfg, simulated=False)
    with pytest.raises(hwlock.HardwareBusy):
        ch.start(poll=False)
    ch.shutdown()                             # a crash path may still call it
    assert fake_port.sent == []               # nothing reached the other service's unit
    assert [h["module"] for h in hwlock.held()] == ["chopper"]   # holder keeps it
    holder.close()


def test_sim_backend_claims_nothing():
    ch, _ = build_sim_system(Config())
    ch.start(poll=False)
    assert hwlock.held() == []
    ch.shutdown()
    assert hwlock.held() == []


def test_hwlock_copy_is_the_master():
    # tools/check_modules.py checks this too; a cheap local guard.
    from pathlib import Path
    here = Path(hwlock.__file__)
    master = here.parents[5] / "suite-common" / "src" / "suite_common" / "hwlock.py"
    if not master.is_file():
        pytest.skip("installed without the suite tree")
    assert here.read_bytes() == master.read_bytes()
