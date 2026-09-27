"""The real backend against a fake DLL: the call sequence and buffers, not the
hardware (which is marked # VERIFY in backends/tlccs.py)."""

import numpy as np
import pytest

from fake_tlccs import FakeTlccs
from ccs200.backends.tlccs import TLCCSError, TlccsSpectrometer
from ccs200.config import Config
from ccs200.spectrometer import Spectrometer


def _open(**kw):
    dll = FakeTlccs(**kw)
    b = TlccsSpectrometer(resource="USB0::0x1313::0x8089::M00000000::RAW", dll=dll)
    b.open()
    return b, dll


def test_open_reads_identity_and_calibration():
    b, dll = _open()
    assert "CCS200" in b.idn() and not b.simulated
    wl = b.wavelengths()
    assert wl.shape == (3648,) and wl[0] == 200.0
    assert "getWavelengthData(0)" in dll.calls           # the factory calibration
    b.close()
    assert dll.calls[-1] == "close"


def test_scan_waits_for_the_transfer_bit_and_caches_integration_time():
    b, dll = _open(polls_until_ready=3)
    b.start_scan(0.02)
    polls = 0
    while not b.scan_ready():
        polls += 1
    assert polls == 3
    y = b.read_scan()
    assert y.shape == (3648,) and y[0] == pytest.approx(0.5)
    b.start_scan(0.02)                                   # same time: not re-sent
    assert dll.calls.count("setIntegrationTime") == 1


def test_an_abandoned_scan_is_drained_before_the_next():
    b, dll = _open(polls_until_ready=0)
    b.start_scan(0.01)
    b.abort_scan()
    b.start_scan(0.01)
    assert dll.calls.count("getScanData") == 1           # the stale one, thrown away
    assert b.scan_ready()


def test_an_abandoned_scan_still_exposing_is_waited_out():
    """The old exposure must FINISH (and be thrown away) before the next start:
    started early, its data-ready bit would answer the new scan with light
    collected before the change that abandoned it."""
    b, dll = _open(polls_until_ready=3)
    b.start_scan(0.01)
    b.abort_scan()
    waits = 0
    while b.busy():
        waits += 1
        assert dll.calls.count("startScan") == 1         # nothing new started meanwhile
    assert waits == 3
    assert dll.calls.count("getScanData") == 1           # the stale one, discarded
    b.start_scan(0.01)
    assert dll.calls.count("startScan") == 2
    assert not b.scan_ready()                            # the NEW scan is still exposing


def test_the_brain_waits_for_an_abandoned_scan():
    cfg = Config()
    cfg.scan.continuous = False
    dll = FakeTlccs(polls_until_ready=2)
    spec = Spectrometer(TlccsSpectrometer(resource="USB0::x", dll=dll), cfg)
    spec.start(run=False)
    n1 = spec.acquire()
    spec.backend.start_scan(0.01)                        # a scan in flight ...
    spec.backend.abort_scan()                            # ... abandoned by a change
    dll.level = 0.4                                      # the world after the change
    while spec.status().acquiring:
        spec.step()
    t = spec.get_trace("sample")
    assert t["acq_id"] == n1 and np.allclose(t["spectrum"], 0.4)
    assert dll.calls.count("getScanData") == 2           # stale discarded + the real one
    spec.shutdown()


def test_errors_carry_the_library_text():
    dll = FakeTlccs(fail_init=True)
    b = TlccsSpectrometer(resource="USB0::x", dll=dll)
    with pytest.raises(TLCCSError, match="fake error"):
        b.open()


def test_the_brain_runs_on_the_real_backend():
    cfg = Config()
    cfg.scan.continuous = False
    dll = FakeTlccs(polls_until_ready=0)
    spec = Spectrometer(TlccsSpectrometer(resource="USB0::x", dll=dll), cfg)
    spec.start(run=False)
    assert not spec.status().simulated and spec.status().pixels == 3648
    n = spec.acquire()
    while spec.status().acquiring:
        spec.step()
    t = spec.get_trace("sample")
    assert t["acq_id"] == n and np.allclose(t["spectrum"], 0.25)
    with pytest.raises(ValueError, match="simulator"):
        spec.set_light(False)
    spec.shutdown()
