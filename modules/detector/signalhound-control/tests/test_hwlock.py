"""One analyser, one service (Lukas's rule): the real backend claims the
analyser by its SERIAL NUMBER in hwlock before it talks to it. Offline, with
the fake sa_api.dll; conftest points AALTOFLOW_LOCK_DIR at a temp folder."""

import pytest

from fake_sa_api import FakeSaApi
from signalhound import hwlock
from signalhound.backends.sa_api import SaApiAnalyzer, SaApiError, lock_address
from signalhound.config import Config
from signalhound.sim_system import build_sim_system

SN = 17040001


def _backend(serial=0, **kw):
    cfg = Config()
    cfg.hardware.serial = serial
    for k in ("model",):
        if k in kw:
            setattr(cfg.hardware, k, kw.pop(k))
    dll = FakeSaApi(serial=SN, **kw)
    return SaApiAnalyzer(cfg, dll=dll), dll


def test_two_backends_on_the_same_serial_conflict():
    a, _ = _backend(serial=SN)
    a.open()
    b, dll_b = _backend(serial=SN)
    with pytest.raises(hwlock.HardwareBusy, match="signalhound"):
        b.open()
    # the claim comes BEFORE the device: the second one never touched it
    assert dll_b.names() == []
    a.close()


def test_auto_discovered_box_is_claimed_and_a_busy_one_is_left_alone():
    a, _ = _backend(serial=SN)          # holds the box by its configured serial
    a.open()
    b, dll_b = _backend(serial=0)       # "the first found" turns out to be the same box
    with pytest.raises(hwlock.HardwareBusy, match="signalhound"):
        b.open()
    # it only asked who the box is, then let go -- no saAbort to a box it does not own
    assert dll_b.names() == ["saOpenDevice", "saGetSerialNumber", "saCloseDevice"]
    a.close()


def test_the_same_serial_written_differently_conflicts():
    assert hwlock.normalize(lock_address(SN)) == hwlock.normalize(f"signalhound::{SN}")
    a, _ = _backend(serial=SN)
    a.open()
    with pytest.raises(hwlock.HardwareBusy):
        hwlock.claim(f"SignalHound::{SN}", "someone-else")
    a.close()


def test_close_releases_the_claim():
    a, _ = _backend(serial=SN)
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["signalhound"]
    a.close()
    assert hwlock.held() == []
    b, _ = _backend(serial=0)
    b.open()                            # auto-discovery claims it now
    assert hwlock.held()[0]["normalized"] == f"SIGNALHOUND::{SN}"
    b.close()
    assert hwlock.held() == []


@pytest.mark.parametrize("serial,kw", [
    (SN, {"fail": {"saOpenDeviceBySerialNumber": -8}}),   # device not found
    (0, {"model": "SA124B"}),                              # wrong model after the claim
    (SN, {"fail": {"saGetDeviceType": -5}}),               # a query fails
])
def test_a_failing_open_releases_the_claim(serial, kw):
    a, _ = _backend(serial=serial, **kw)
    with pytest.raises(SaApiError):
        a.open()
    assert hwlock.held() == []
    b, _ = _backend(serial=SN)
    b.open()
    b.close()


def test_a_missing_dll_releases_the_claim(tmp_path):
    cfg = Config()
    cfg.hardware.serial = SN
    cfg.hardware.dll_path = str(tmp_path / "no_such_sa_api.dll")
    with pytest.raises(SaApiError):
        SaApiAnalyzer(cfg).open()
    assert hwlock.held() == []


def test_the_simulator_claims_nothing():
    sa = build_sim_system(Config())[0]
    sa.start(run=False)
    try:
        assert hwlock.held() == []
    finally:
        sa.shutdown()
    assert hwlock.held() == []
