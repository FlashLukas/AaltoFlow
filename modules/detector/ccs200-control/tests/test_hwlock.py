"""One spectrometer, one service: the real backend claims its USB resource
(suite hwlock) before tlccs_init, and gives it back on close or on a failed
open. The simulator claims nothing. Runs offline against the fake DLL; the
lock folder is a per-test temp folder (conftest)."""

import pytest

from fake_tlccs import FakeTlccs
from ccs200 import hwlock
from ccs200.backends.sim import SimulatedSpectrometer
from ccs200.backends.tlccs import TLCCSError, TlccsSpectrometer
from ccs200.config import Config

RES = "USB0::0x1313::0x8089::M00412345::RAW"


def _backend(resource=RES, **kw):
    dll = FakeTlccs(**kw)
    return TlccsSpectrometer(resource=resource, dll=dll), dll


def test_second_open_of_the_same_spectrometer_is_refused():
    a, _ = _backend()
    a.open()
    b, dll_b = _backend()
    with pytest.raises(hwlock.HardwareBusy, match="ccs200"):
        b.open()
    assert "init" not in dll_b.calls          # not one byte went to the device
    a.close()


def test_the_same_unit_spelt_differently_still_conflicts():
    a, _ = _backend("USB0::0x1313::0x8089::M00412345::RAW")
    a.open()
    b, _ = _backend("usb::0x1313::0x8089::m00412345")
    with pytest.raises(hwlock.HardwareBusy):
        b.open()
    a.close()


def test_a_different_unit_is_not_blocked():
    a, _ = _backend("USB0::0x1313::0x8089::M00412345::RAW")
    b, _ = _backend("USB0::0x1313::0x8089::M00499999::RAW")
    a.open()
    b.open()
    a.close()
    b.close()


def test_close_releases_the_claim():
    a, _ = _backend()
    a.open()
    assert len(hwlock.held()) == 1
    a.close()
    assert hwlock.held() == []
    b, _ = _backend()
    b.open()                                  # free again
    b.close()


def test_a_failed_init_releases_the_claim():
    a, _ = _backend(fail_init=True)
    with pytest.raises(TLCCSError):
        a.open()
    assert hwlock.held() == []
    b, _ = _backend()
    b.open()
    b.close()


def test_a_failure_after_init_closes_the_session_and_releases(monkeypatch):
    a, dll = _backend()

    def boom(*args):
        raise TLCCSError("wavelengths unreadable")
    monkeypatch.setattr(a, "_read_wavelengths", boom)
    with pytest.raises(TLCCSError):
        a.open()
    assert dll.calls[-1] == "close"            # the TLCCS session was closed
    assert hwlock.held() == []


def test_the_simulator_claims_nothing():
    s = SimulatedSpectrometer(Config())
    s.open()
    assert hwlock.held() == []
    s.close()


def test_the_module_copy_of_hwlock_is_the_master():
    # tools/check_modules.py checks this too; here it fails fast in the module's
    # own test run if someone edits the copy instead of the master.
    from pathlib import Path
    master = Path(__file__).resolve().parents[4] / "suite-common" / "src" / "suite_common" / "hwlock.py"
    if not master.is_file():
        pytest.skip("module installed on its own, no suite-common next to it")
    copy = Path(hwlock.__file__)
    assert copy.read_bytes() == master.read_bytes()
