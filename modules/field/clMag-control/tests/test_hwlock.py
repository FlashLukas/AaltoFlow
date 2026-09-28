"""One physical instrument, one service (Lukas's rule).

clMag has no real backend yet, so these tests exercise the claim helper the
real system must use (backends/claims.py) plus the service's HardwareBusy
exit. Offline: every lock file goes to a pytest temp folder via
AALTOFLOW_LOCK_DIR, never to the real %LOCALAPPDATA%\\AaltoFlow\\locks.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from clMag import hwlock
from clMag.backends.claims import HardwareClaims, physical_addresses, daq_device
from clMag.config import Config

FAST = 0.05  # retry window for a busy claim; the default 2 s would slow the suite


@pytest.fixture(autouse=True)
def _lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))


def test_hwlock_is_the_master_copy():
    """The module's copy must stay byte-identical to suite-common's master
    (tools/check_modules.py checks the same; this catches it earlier)."""
    here = os.path.dirname(hwlock.__file__)
    master = os.path.abspath(os.path.join(here, *[".."] * 5, "suite-common", "src",
                                          "suite_common", "hwlock.py"))
    if not os.path.exists(master):
        pytest.skip("suite-common master not present (installed module)")
    with open(master, "rb") as a, open(hwlock.__file__, "rb") as b:
        assert a.read() == b.read()


def test_physical_addresses_are_kepco_and_daq_card():
    cfg = Config()
    addrs = physical_addresses(cfg)
    # Hall on Dev1/ai0 and every AUX line on Dev1 -> ONE DAQ entry, not seven.
    assert [hwlock.normalize(a) for a in addrs] == ["GPIB0::6", "DEV1"]
    # AUX moved to a second card -> that card is claimed too.
    cfg.aux.do_lines = "Dev2/port0/line0"
    assert [hwlock.normalize(a) for a in physical_addresses(cfg)] == ["GPIB0::6", "DEV1", "DEV2"]
    assert daq_device("Dev1/port0/line2") == "Dev1"


def test_second_claim_same_address_is_busy_and_names_clMag():
    first = HardwareClaims.claim(["GPIB0::6::INSTR", "Dev1"], wait_s=FAST)
    try:
        with pytest.raises(hwlock.HardwareBusy) as ei:
            HardwareClaims.claim(["GPIB0::6::INSTR"], wait_s=FAST)
        msg = str(ei.value)
        assert "clMag" in msg and "GPIB0::6" in msg
    finally:
        first.release()


@pytest.mark.parametrize("a, b", [
    ("GPIB0::6::INSTR", "GPIB::6"),
    ("GPIB0::6::INSTR", "gpib0::6"),
    ("Dev1", "dev1"),
])
def test_same_address_written_differently_conflicts(a, b):
    first = HardwareClaims.claim([a], wait_s=FAST)
    try:
        with pytest.raises(hwlock.HardwareBusy):
            HardwareClaims.claim([b], wait_s=FAST)
    finally:
        first.release()


def test_release_frees_the_address():
    c = HardwareClaims.claim(["GPIB0::6::INSTR", "Dev1"], wait_s=FAST)
    assert sorted(c.addresses) == ["DEV1", "GPIB0::6"]
    c.release()
    assert c.addresses == []
    assert hwlock.held() == []
    again = HardwareClaims.claim(["GPIB::6", "dev1"], wait_s=FAST)  # a new open succeeds
    again.release()


def test_failed_claim_releases_what_it_already_took():
    """Kepco free, DAQ busy (e.g. mag2d holds Dev1): the start must fail AND
    leave the Kepco unclaimed, or clMag would block the kepco module forever
    without running."""
    other = hwlock.claim("Dev1", "mag2d")
    try:
        with pytest.raises(hwlock.HardwareBusy) as ei:
            HardwareClaims.claim(["GPIB0::6::INSTR", "Dev1"], wait_s=FAST)
        assert "mag2d" in str(ei.value)
        assert [h["normalized"] for h in hwlock.held()] == ["DEV1"]  # only mag2d's
        HardwareClaims.claim(["GPIB0::6::INSTR"], wait_s=FAST).release()
    finally:
        other.release()


def test_simulator_claims_nothing():
    from clMag.sim_system import build_sim_system
    ctrl, *_ = build_sim_system(Config())
    ctrl.start()
    try:
        assert hwlock.held() == []
    finally:
        ctrl.shutdown()
    assert hwlock.held() == []


def _load_run_service():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "run_service.py")
    spec = importlib.util.spec_from_file_location("clmag_run_service", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_service_exits_cleanly_on_busy_hardware(monkeypatch, capsys):
    """HardwareBusy at start -> one ASCII line on stderr, exit 4, no traceback,
    and no shutdown ("ramp to zero + OUTP OFF") sent to a box we never owned."""
    rs = _load_run_service()
    shut = []

    class FakeCtrl:
        def start(self):
            raise hwlock.HardwareBusy("GPIB0::6 is already in use by kepco (pid 42) -- stop that one first")

        def shutdown(self):
            shut.append(True)

    monkeypatch.setattr(rs, "build_sim_system", lambda cfg: (FakeCtrl(),))
    monkeypatch.setattr(sys, "argv", ["run_service.py", "--cmd-port", "15955", "--pub-port", "15956"])
    code = rs.main()
    err = capsys.readouterr().err
    assert code == rs.EXIT_HARDWARE_BUSY != 0
    assert err.strip().count("\n") == 0
    assert "kepco (pid 42)" in err and "Traceback" not in err
    err.encode("ascii")  # gotcha #14: printed text is ASCII
    assert shut == []
