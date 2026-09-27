"""One amplifier, one service: the COM-port claim of the real backend.

Lukas's rule: the same instrument is defined by the same physical address, and
two services must never drive it at once. The real backend claims its COM port
in open() (dsamp/hwlock.py, a copy of suite-common's). These tests run offline
against the fake pyserial of test_real_backend.py; the autouse fixture in
conftest.py points the lock folder at a temp dir.
"""

import sys
import types

import pytest

from dsamp import hwlock
from dsamp.backends import dsi_serial
from dsamp.backends.dsi_serial import DsiSerialAmp
from dsamp.hwlock import HardwareBusy

from test_real_backend import FakeSerial


@pytest.fixture
def fake_serial(monkeypatch):
    mod = types.ModuleType("serial")
    made = []

    def Serial(*a, **kw):
        s = FakeSerial()
        made.append(s)
        return s
    mod.Serial = Serial
    monkeypatch.setitem(sys.modules, "serial", mod)
    monkeypatch.setattr(dsi_serial.time, "sleep", lambda s: None)
    return made


def test_hwlock_is_the_master_copy():
    # check_modules.py compares the bytes too; this catches it in pytest.
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[4]
    master = root / "suite-common" / "src" / "suite_common" / "hwlock.py"
    if not master.is_file():
        pytest.skip("suite-common not next to this module (installed alone)")
    assert pathlib.Path(hwlock.__file__).read_bytes() == master.read_bytes()


def test_second_open_on_same_port_is_refused(fake_serial):
    a = DsiSerialAmp("COM5")
    a.open()
    try:
        b = DsiSerialAmp("COM5")
        with pytest.raises(HardwareBusy, match="dsamp"):
            b.open()
        # the refused backend never opened a port, let alone wrote to it
        assert len(fake_serial) == 1
        b.close()                                  # harmless, sends nothing
        assert len(fake_serial) == 1
    finally:
        a.close()


@pytest.mark.parametrize("other", ["com5", r"\\.\COM5", "ASRL5::INSTR"])
def test_same_port_written_differently_conflicts(fake_serial, other):
    a = DsiSerialAmp("COM5")
    a.open()
    try:
        with pytest.raises(HardwareBusy, match="COM5"):
            DsiSerialAmp(other).open()
    finally:
        a.close()


def test_different_ports_do_not_conflict(fake_serial):
    a, b = DsiSerialAmp("COM5"), DsiSerialAmp("COM6")
    a.open()
    b.open()
    a.close()
    b.close()


def test_close_releases(fake_serial):
    a = DsiSerialAmp("COM5")
    a.open()
    assert [h["normalized"] for h in hwlock.held()] == ["COM5"]
    assert hwlock.held()[0]["module"] == "dsamp"
    a.close()
    assert hwlock.held() == []
    b = DsiSerialAmp("com5")
    b.open()                                       # free again
    b.close()


def test_failing_open_releases(monkeypatch, fake_serial):
    def broken(*a, **kw):
        raise OSError("could not open port 'COM5': FileNotFoundError")
    sys.modules["serial"].Serial = broken
    a = DsiSerialAmp("COM5")
    with pytest.raises(OSError):
        a.open()
    assert hwlock.held() == []                     # the claim went with the failure
    a.close()                                      # and close() sends nothing


def test_busy_brain_start_sends_nothing(fake_serial):
    """A brain whose backend lost the claim must not 'switch off' a device it
    does not own on shutdown -- that device belongs to the other service."""
    from dsamp.amplifier import Amplifier
    from dsamp.config import Config
    holder = DsiSerialAmp("COM5")
    holder.open()
    try:
        amp = Amplifier(DsiSerialAmp("COM5"), Config())
        with pytest.raises(HardwareBusy):
            amp.start()
        amp.shutdown()
        assert len(fake_serial) == 1               # only the holder's port exists
        assert not any("OUTP:STAT OFF" == w for w in fake_serial[0].written)
    finally:
        holder.close()


def test_sim_never_claims():
    from dsamp.config import Config
    from dsamp.sim_system import build_sim_system
    amp, _ = build_sim_system(Config())
    amp.start()
    try:
        assert hwlock.held() == []
    finally:
        amp.shutdown()
    assert hwlock.held() == []
