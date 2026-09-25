"""The real backend's pure parts. The hardware itself is exercised by
scripts/list_devices.py and run_service.py --real, not by the offline suite."""

import ctypes as C

import pytest

from pm16.backends import tlpmx


def test_warning_codes_map_to_flags():
    assert tlpmx.warning_flag(0) == ""
    assert tlpmx.warning_flag(tlpmx.WARN_OVERFLOW) == "overrange"
    assert tlpmx.warning_flag(tlpmx.WARN_UNDERRUN) == "underrun"
    assert tlpmx.warning_flag(tlpmx.WARN_NAN) == "nan"


def test_vi_types_match_visatype_h():
    # ViBoolean is 16 bits in VISA; declaring it as a C bool would corrupt the call
    assert C.sizeof(tlpmx.ViBoolean) == 2
    assert C.sizeof(tlpmx.ViSession) == 4
    assert C.sizeof(tlpmx.ViStatus) == 4


def test_missing_dll_gives_a_helpful_error(tmp_path):
    with pytest.raises(tlpmx.TLPMXError, match="not found|Windows"):
        tlpmx.load_dll(str(tmp_path / "nope.dll"))


def test_calls_before_open_are_refused():
    m = tlpmx.TLPMXPowerMeter()
    with pytest.raises(tlpmx.TLPMXError, match="not open"):
        m.measure_power()
    m.close()                                         # safe without open


def test_error_status_raises_warning_status_returns():
    class FakeDll:
        def TLPMX_errorMessage(self, vi, status, buf):
            buf.value = b"boom"
            return 0

    with pytest.raises(tlpmx.TLPMXError, match="boom"):
        tlpmx._check(FakeDll(), 0, -1073807343)
    assert tlpmx._check(FakeDll(), 0, tlpmx.WARN_OVERFLOW) == tlpmx.WARN_OVERFLOW
