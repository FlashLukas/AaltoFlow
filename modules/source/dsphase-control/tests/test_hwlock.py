"""One physical phase shifter, one service (Lukas's rule).

The real backend claims its COM port in hwlock before it sends a byte. These
tests run the real PS6000L class against the fake `serial` module of
test_backend.py, with the lock folder pointed at tmp_path (conftest), so they
are offline and never touch this PC's real locks.
"""

import sys
import types

import pytest

from dsphase import hwlock
from dsphase.backends.ps6000l import PS6000L
from dsphase.hwlock import HardwareBusy

from test_backend import FakeSerial


@pytest.fixture
def fake_serial(monkeypatch):
    mod = types.ModuleType("serial")
    mod.Serial = FakeSerial
    monkeypatch.setitem(sys.modules, "serial", mod)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return mod


def _ports():
    return [h["normalized"] for h in hwlock.held()]


def test_copy_is_identical_to_the_master():
    """check_modules.py compares these too; this catches it one step earlier."""
    import pathlib
    here = pathlib.Path(hwlock.__file__)
    master = here.parents[5] / "suite-common" / "src" / "suite_common" / "hwlock.py"
    if not master.is_file():
        pytest.skip("installed without suite-common next to it")
    assert here.read_bytes() == master.read_bytes()


def test_second_open_on_same_port_is_refused(fake_serial):
    a = PS6000L("COM5")
    a.open()
    assert _ports() == ["COM5"]
    b = PS6000L("COM5")
    n_serials = FakeSerial.last
    with pytest.raises(HardwareBusy, match="dsphase"):
        b.open()
    # the refused backend never opened the port, so it sent nothing at all
    assert FakeSerial.last is n_serials
    assert b._ser is None
    a.close()


@pytest.mark.parametrize("other", ["com5", "ASRL5::INSTR", r"\\.\COM5", " COM5 "])
def test_same_port_spelled_differently_conflicts(fake_serial, other):
    a = PS6000L("COM5")
    a.open()
    with pytest.raises(HardwareBusy, match="COM5"):
        PS6000L(other).open()
    a.close()


def test_different_ports_do_not_conflict(fake_serial):
    a, b = PS6000L("COM5"), PS6000L("COM6")
    a.open()
    b.open()
    assert sorted(_ports()) == ["COM5", "COM6"]
    a.close()
    b.close()


def test_close_releases(fake_serial):
    a = PS6000L("COM5")
    a.open()
    a.close()
    assert _ports() == []
    b = PS6000L("com5")
    b.open()                                           # no HardwareBusy
    b.close()


def test_failing_open_releases_and_sends_no_safe_state(fake_serial):
    class Mute(FakeSerial):
        def readline(self):
            return b"garbage\n"
    fake_serial.Serial = Mute
    dev = PS6000L("COM5")
    with pytest.raises(RuntimeError, match="PONG"):
        dev.open()
    ser = FakeSerial.last
    assert ser.closed and dev._ser is None
    assert "OUTP:STAT OFF" not in ser.lines           # no command to a box we did not establish
    assert _ports() == []
    dev.close()                                        # harmless after a failed open
    fake_serial.Serial = FakeSerial
    PS6000L("COM5").open()                            # the port is free again


def test_serial_open_error_releases(fake_serial):
    """pyserial itself failing (port missing / held by another program)."""
    def boom(*a, **k):
        raise OSError("could not open port 'COM5'")
    fake_serial.Serial = boom
    with pytest.raises(OSError):
        PS6000L("COM5").open()
    assert _ports() == []


def test_brain_on_a_busy_port_sends_nothing_and_shutdown_is_quiet(fake_serial):
    """The service path: brain.start() raises HardwareBusy; the shutdown that
    may follow must not send RF-off to a unit that belongs to someone else."""
    from dsphase.config import Config
    from dsphase.shifter import PhaseShifter

    holder = hwlock.claim("COM5", "kepco")             # any other module
    try:
        FakeSerial.last = None
        brain = PhaseShifter(PS6000L("COM5"), Config())
        with pytest.raises(HardwareBusy, match="kepco"):
            brain.start()
        brain.shutdown()
        assert FakeSerial.last is None                 # the port was never even opened
        assert brain.status().connected is False
    finally:
        holder.release()


def test_sim_backend_claims_nothing():
    from dsphase.config import Config
    from dsphase.sim_system import build_sim_system
    brain, _ = build_sim_system(Config())
    brain.start()
    try:
        brain.set_phase(45.0)
        assert hwlock.held() == []
    finally:
        brain.shutdown()
    assert hwlock.held() == []
