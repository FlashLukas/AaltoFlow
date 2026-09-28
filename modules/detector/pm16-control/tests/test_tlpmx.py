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


class _FakeTLPMX:
    """A stand-in for TLPMX_64.dll that records every call and FAILS on any
    that would change the meter (Lukas's rule 2026-09-27: start-up only reads).
    The meter it pretends to be was left at 980 nm on manual range 17.4 mW."""

    ALLOWED_WRITES = {"TLPMX_setTimeoutValue"}   # the VISA session's timeout, not the meter

    def __init__(self):
        self.calls = []
        self.state = {"TLPMX_getWavelength": (980.0, 400.0, 1100.0),
                      "TLPMX_getPowerRange": (1.736957e-2, 1.73668e-4, 1.743936),
                      "TLPMX_getAvgTime": (0.06024, 0.06024, 0.06024)}

    def __getattr__(self, name):
        def fn(*args):
            self.calls.append(name)
            if name in self.ALLOWED_WRITES:
                return 0
            if name.startswith(("TLPMX_set", "TLPMX_start", "TLPMX_cancel", "TLPMX_reset")):
                raise AssertionError(f"start-up wrote to the meter: {name}")
            if name == "TLPMX_init":
                assert args[2] == 0, "TLPMX_init must be called with reset OFF"
                args[3]._obj.value = 1
            elif name in self.state:
                args[2]._obj.value = self.state[name][args[1]]    # (vi, attribute, out, channel)
            elif name == "TLPMX_getPowerAutorange":
                args[1]._obj.value = 0                     # manual range
            elif name == "TLPMX_getDarkAdjustState":
                args[1]._obj.value = 0
            elif name in ("TLPMX_getDarkOffset", "TLPMX_measPower"):
                args[1]._obj.value = 1e-6
            return 0
        return fn


def test_real_backend_start_only_reads_and_adopts(monkeypatch):
    from pm16.config import Config
    from pm16.meter import PowerMeter

    fake = _FakeTLPMX()
    monkeypatch.setattr(tlpmx, "load_dll", lambda path="": fake)
    backend = tlpmx.TLPMXPowerMeter(resource="USB0::0x1313::0x807B::000000000::INSTR")
    meter = PowerMeter(backend, Config())          # config says 800 nm, auto range
    meter.start(poll=False)
    meter.poll_once()
    st = meter.status()
    assert st.wavelength_nm == 980.0 and st.auto_range is False
    assert st.range_W == pytest.approx(1.736957e-2)
    assert "TLPMX_measPower" in fake.calls
    meter.shutdown()
