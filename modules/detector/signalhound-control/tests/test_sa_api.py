"""The real backend against a fake sa_api.dll: call order, status codes,
model check, the TG, and that closing aborts first. No Signal Hound needed."""

import time

import numpy as np
import pytest

from fake_sa_api import FakeSaApi
from signalhound.backends.sa_api import SaApiAnalyzer, SaApiError
from signalhound.config import Config
from signalhound.instruments import SweepSettings
from signalhound.spectrum import SpectrumAnalyzer


def _open(**kw):
    cfg = Config()
    for k in ("model", "serial", "attach_tg"):
        if k in kw:
            setattr(cfg.hardware, k, kw.pop(k))
    dll = FakeSaApi(**kw)
    b = SaApiAnalyzer(cfg, dll=dll)
    b.open()
    return b, dll, cfg


def test_open_reads_model_serial_and_attaches_the_tg():
    b, dll, _ = _open()
    assert b.device_model() == "SA44B" and b.tg_attached()
    assert "SA44B" in b.idn() and "17040001" in b.idn() and "3.0.99" in b.idn()
    # serial first: with no serial configured it names the box to claim (hwlock)
    assert dll.names()[:4] == ["saOpenDevice", "saGetSerialNumber", "saGetDeviceType",
                               "saGetAPIVersion"]
    assert dll.mode == -1                                   # nothing initiated: TG silent


def test_open_by_serial_and_a_wrong_serial_is_an_error():
    b, dll, _ = _open(serial=17040001)
    assert dll.names()[0] == "saOpenDeviceBySerialNumber"
    with pytest.raises(SaApiError, match="fake error -8"):
        _open(serial=99)


def test_a_different_model_than_configured_is_refused_and_closed():
    with pytest.raises(SaApiError, match="SA124B"):
        _open(model="SA44B", device_type=4)


def test_no_tg_is_not_an_error():
    b, dll, _ = _open(tg=False)
    assert b.tg_attached() is False


def test_configure_follows_the_manual_order_and_reports_the_grid():
    b, dll, cfg = _open()
    dll.calls.clear()
    g = b.configure(SweepSettings.from_config(cfg))
    assert dll.names() == ["saAbort", "saConfigCenterSpan", "saConfigAcquisition",
                           "saConfigLevel", "saConfigGainAtten", "saConfigSweepCoupling",
                           "saInitiate", "saQuerySweepInfo"]
    assert dll.mode == 0                                    # SA_SWEEPING
    assert g.points == 4001 and g.start_Hz == pytest.approx(0.9e9)


def test_tg_mode_initiates_a_tg_sweep_with_the_level():
    b, dll, cfg = _open()
    cfg.tracking.on = True
    cfg.tracking.points = 301
    cfg.tracking.level_dBm = -15.0
    dll.calls.clear()
    g = b.configure(SweepSettings.from_config(cfg))
    assert "saConfigTgSweep" in dll.names() and dll.mode == 4          # SA_TG_SWEEP
    assert dll.tg_level == -15.0 and g.points == 301
    # saSetTg is only allowed while the TG is idle (sa_api.h), so it must come
    # after the abort and BEFORE the TG sweep is configured and initiated.
    n = dll.names()
    assert n.index("saAbort") < n.index("saSetTg") < n.index("saConfigTgSweep")         < n.index("saInitiate")


def test_real_backend_is_not_kept_waiting_for_the_sweep_time():
    """saGetSweep TAKES the sweep; idling for the estimated sweep time before
    calling it would double every sweep. With a 100 Hz RBW the estimate is
    minutes -- the brain must still return at once."""
    cfg = Config()
    cfg.acquisition.continuous = False
    cfg.sweep.span_Hz = 1e6
    dll = FakeSaApi(bins=101)
    v = SpectrumAnalyzer(SaApiAnalyzer(cfg, dll=dll), cfg)
    v.start(run=False)
    try:
        v.set_rbw(100.0)
        v.set_span(1e6)
        assert v.status().sweep_time_s > 10.0        # the estimate is long ...
        t0 = time.monotonic()
        v.acquire()
        while v.status().acquiring:
            assert v.step() or True
            assert time.monotonic() - t0 < 5.0       # ... but nobody waits for it
        assert v.get_trace("sample")["points"] == 101
    finally:
        v.shutdown()


def test_tg_mode_refused_without_a_tg():
    b, dll, cfg = _open(tg=False)
    cfg.tracking.on = True
    with pytest.raises(SaApiError, match="no tracking generator"):
        b.configure(SweepSettings.from_config(cfg))


def test_sweep_reads_the_right_array_and_flags_compression():
    b, dll, cfg = _open(bins=11, compression=True)
    b.configure(SweepSettings.from_config(cfg))
    b.start_sweep()
    trace, meta = b.finish_sweep()
    assert trace.shape == (11,) and trace[5] == -20.0 and trace[0] == -90.0   # average: min array
    assert meta["overload"] is True
    cfg.sweep.detector = "peak"
    dll.compression = False
    b.configure(SweepSettings.from_config(cfg))
    b.start_sweep()
    trace, meta = b.finish_sweep()
    assert trace[0] == -85.0 and meta["overload"] is False            # peak: the max array


def test_a_negative_status_raises_with_the_api_text():
    b, dll, cfg = _open()
    dll.fail["saConfigSweepCoupling"] = -91                             # saBandwidthErr
    with pytest.raises(SaApiError, match="saConfigSweepCoupling: fake error -91"):
        b.configure(SweepSettings.from_config(cfg))


def test_close_aborts_before_closing_and_is_safe_twice():
    b, dll, _ = _open()
    dll.calls.clear()
    b.close()
    b.close()
    assert dll.names() == ["saAbort", "saCloseDevice"] and dll.open is False


def test_missing_dll_says_what_to_install():
    cfg = Config()
    cfg.hardware.dll_path = "C:/nowhere/sa_api.dll"
    with pytest.raises(SaApiError, match="Spike"):
        SaApiAnalyzer(cfg).open()


def test_the_brain_drives_the_real_backend():
    cfg = Config()
    cfg.acquisition.continuous = False
    dll = FakeSaApi(bins=101)
    v = SpectrumAnalyzer(SaApiAnalyzer(cfg, dll=dll), cfg)
    v.start(run=False)
    try:
        assert v.simulated is False and v.status().device_model == "SA44B"
        v.acquire()
        while v.status().acquiring:
            v.step()
        t = v.get_trace("sample")
        assert t["points"] == 101 and t["peak_dBm"] == pytest.approx(-20.0)
        assert np.isfinite(t["trace"]).all()
        assert v.status().scene == {}                                  # no scene on hardware
        with pytest.raises(ValueError, match="simulator"):
            v.set_scene("tone_dBm", -10)
    finally:
        v.shutdown()
    assert dll.open is False


# ---- start-up: read, never write (Lukas's rule, 2026-09-27) -----------------------

def test_open_issues_no_state_changing_calls():
    """A fake DLL that RAISES on any configure / initiate / abort / TG call:
    opening must get through it."""
    cfg = Config()
    dll = FakeSaApi(device_type=4, readonly=True)          # an SA124B
    b = SaApiAnalyzer(cfg, dll=dll)
    b.open()
    assert dll.writes() == [] and dll.mode == -1
    assert b.device_model() == "SA124B" and b.tg_attached()


def test_brain_start_adopts_the_analyser_and_writes_nothing():
    """The whole start: service-style (sweep thread running), saved config
    asking for continuous sweeps and the TG on. The analyser is an SA124B
    (non-default model) left with the TG at -12 dBm by a previous program."""
    cfg = Config()
    cfg.acquisition.continuous = True                      # the saved default
    cfg.tracking.on = True
    dll = FakeSaApi(device_type=4, readonly=True)
    dll.tg_level = -12.0                                   # pre-existing TG state
    v = SpectrumAnalyzer(SaApiAnalyzer(cfg, dll=dll), cfg)
    v.start(run=True)
    try:
        time.sleep(0.4)                                    # a few idle passes of the thread
        st = v.status()
        assert dll.writes() == [] and dll.mode == -1 and dll.tg_level == -12.0
        assert st.connected and st.hw_error == ""
        assert st.device_model == "SA124B" and st.tg_attached is True
        assert st.freq_max_Hz == pytest.approx(12.4e9)      # the ADOPTED model's envelope
        assert st.configured is False and st.points == 0 and st.sweeps == 0
        assert st.continuous is False and st.tg_on is False
    finally:
        dll.readonly = False                               # shutdown's abort + close is allowed
        v.shutdown()
    assert dll.names()[-2:] == ["saAbort", "saCloseDevice"]


def test_the_first_deliberate_request_configures():
    cfg = Config()
    cfg.acquisition.continuous = False
    dll = FakeSaApi(bins=101)
    v = SpectrumAnalyzer(SaApiAnalyzer(cfg, dll=dll), cfg)
    v.start(run=False)
    try:
        v.step()
        assert dll.writes() == []
        v.set_span(10e6)                                   # a user setting: now configure
        v.step()
        assert "saInitiate" in dll.writes() and v.status().configured is True
    finally:
        v.shutdown()
