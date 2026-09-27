"""The real backend's pure parts. The hardware itself is exercised by
scripts/list_devices.py and run_service.py --real, not by the offline suite."""

import ctypes as C

import pytest

from pm400.backends import tlpmx


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


def test_every_new_pm400_call_is_declared():
    for name in ("TLPMX_setAvgTime", "TLPMX_getAvgTime", "TLPMX_setEnergyRange",
                 "TLPMX_getEnergyRange", "TLPMX_measEnergy", "TLPMX_measFreq",
                 "TLPMX_getSensorInfo"):
        assert name in tlpmx._SIGNATURES


def test_sensor_info_decoding():
    pd = tlpmx.decode_sensor("S121C", "123", tlpmx.SENSOR_TYPE_PD_SINGLE,
                             tlpmx.SENS_FLAG_IS_POWER | tlpmx.SENS_FLAG_IS_WAVEL_SET)
    assert pd["kind"] == "photodiode" and not pd["energy"] and pd["zero_supported"]
    th = tlpmx.decode_sensor("S302C", "1", tlpmx.SENSOR_TYPE_THERMO, tlpmx.SENS_FLAG_IS_POWER)
    assert th["kind"] == "thermal" and th["zero_supported"]
    py = tlpmx.decode_sensor("ES111C", "2", tlpmx.SENSOR_TYPE_PYRO,
                             tlpmx.SENS_FLAG_IS_ENERGY | tlpmx.SENS_FLAG_IS_WAVEL_SET)
    assert py["kind"] == "pyro" and py["energy"] and not py["zero_supported"]
    assert tlpmx.decode_sensor("", "", tlpmx.SENSOR_TYPE_NONE, 0)["kind"] == "none"
    assert tlpmx.decode_sensor("4Q", "", tlpmx.SENSOR_TYPE_4Q, 1)["kind"] == "other"


def test_missing_dll_gives_a_helpful_error(tmp_path):
    with pytest.raises(tlpmx.TLPMXError, match="not found|Windows"):
        tlpmx.load_dll(str(tmp_path / "nope.dll"))


def test_calls_before_open_are_refused():
    m = tlpmx.TLPMXConsole()
    with pytest.raises(tlpmx.TLPMXError, match="not open"):
        m.measure_power()
    with pytest.raises(tlpmx.TLPMXError, match="not open"):
        m.measure_energy()
    m.close()                                         # safe without open


def test_error_status_raises_warning_status_returns():
    class FakeDll:
        def TLPMX_errorMessage(self, vi, status, buf):
            buf.value = b"boom"
            return 0

    with pytest.raises(tlpmx.TLPMXError, match="boom"):
        tlpmx._check(FakeDll(), 0, -1073807343)
    assert tlpmx._check(FakeDll(), 0, tlpmx.WARN_OVERFLOW) == tlpmx.WARN_OVERFLOW
