"""One physical meter, one service (Lukas: "the same instrument has to be
defined by the same physical address").

The real backend claims its VISA resource in open() through hwlock.py, before a
single byte goes to the meter. These tests use the fake VISA instrument from
test_ls455_backend.py, so they run offline; the lock folder is a temp folder
(conftest.py sets AALTOFLOW_LOCK_DIR)."""

import os
import subprocess
import sys

import pytest

from ls455 import hwlock
from ls455.backends.ls455 import LakeShore455
from ls455.config import Config
from ls455.sim_system import build_sim_system

from test_ls455_backend import ANSWERS, FakeInstrument, FakeRM

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))


def _meter(resource, answers=ANSWERS):
    inst = FakeInstrument(answers)
    rm = FakeRM(inst)
    return LakeShore455(resource, command_gap_s=0.0, zero_time_s=0.0,
                        resource_manager=rm), inst, rm


def test_copy_is_identical_to_the_master():
    master = os.path.join(ROOT, "..", "..", "..", "suite-common", "src",
                          "suite_common", "hwlock.py")
    if not os.path.isfile(master):
        pytest.skip("suite-common not next to this module (installed on its own)")
    with open(master, "rb") as a, open(hwlock.__file__, "rb") as b:
        assert a.read() == b.read()


def test_second_open_on_same_address_is_refused():
    a, _, _ = _meter("GPIB0::12::INSTR")
    b, inst_b, rm_b = _meter("GPIB0::12::INSTR")
    a.open()
    try:
        with pytest.raises(hwlock.HardwareBusy, match="ls455"):
            b.open()
        # refused BEFORE the second one reached the instrument
        assert rm_b.opened is None and inst_b.writes == []
    finally:
        a.close()


@pytest.mark.parametrize("first,second", [
    ("GPIB0::12::INSTR", "GPIB::12"),
    ("gpib0::12::instr", "GPIB0::12"),
    ("ASRL5::INSTR", "COM5"),
    ("com5", "ASRL5::INSTR"),
])
def test_other_spellings_of_one_address_conflict(first, second):
    a, _, _ = _meter(first)
    b, _, _ = _meter(second)
    a.open()
    try:
        with pytest.raises(hwlock.HardwareBusy, match="ls455"):
            b.open()
    finally:
        a.close()


def test_different_addresses_do_not_conflict():
    a, _, _ = _meter("GPIB0::12::INSTR")
    b, _, _ = _meter("GPIB0::13::INSTR")
    a.open(); b.open()
    assert len(hwlock.held()) == 2
    a.close(); b.close()
    assert hwlock.held() == []


def test_close_releases_the_address():
    a, inst, _ = _meter("GPIB0::12::INSTR")
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["ls455"]
    a.close()
    assert inst.closed and hwlock.held() == []
    b, _, _ = _meter("GPIB::12")
    b.open()                                # would raise if the claim leaked
    b.close()
    a.close()                               # a second close is harmless


def test_a_failing_open_releases_the_address():
    bad = dict(ANSWERS)
    del bad["UNIT?"]                        # the first query open() makes -> KeyError
    a, inst, _ = _meter("GPIB0::12::INSTR", bad)
    with pytest.raises(KeyError):
        a.open()
    assert inst.closed, "the port must be closed again"
    assert hwlock.held() == []
    b, _, _ = _meter("GPIB0::12::INSTR")
    b.open()
    b.close()


def test_a_failing_brain_start_releases_the_address():
    """The brain adopts the meter's state after open(); if that fails half way
    the backend is closed, so the claim does not outlive the failed start."""
    from ls455.gaussmeter import Gaussmeter
    bad = dict(ANSWERS)
    del bad["AUTO?"]                        # asked by the brain, not by open()
    backend, _, _ = _meter("GPIB0::12::INSTR", bad)
    with pytest.raises(KeyError):
        Gaussmeter(backend, Config()).start(poll=False)
    assert hwlock.held() == []


def test_the_simulator_claims_nothing():
    meter, _ = build_sim_system(Config())
    meter.start(poll=False)
    try:
        assert hwlock.held() == []
    finally:
        meter.shutdown()


def test_service_refuses_busy_meter_with_one_clean_line(_private_lock_dir):
    """run_service.py --real on a claimed address: exit code != 0, one ASCII
    line on stderr naming the address and the holder, no traceback. The
    claim fails before pyvisa is even imported, so this runs offline."""
    held = hwlock.claim("GPIB0::12::INSTR", "ls455")
    try:
        env = dict(os.environ, AALTOFLOW_LOCK_DIR=str(_private_lock_dir),
                   PYTHONIOENCODING="cp1252")   # a pipe on Windows (gotcha #14)
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "run_service.py"), "--real",
             "--resource", "GPIB::12", "--cmd-port", "17390", "--pub-port", "17391"],
            capture_output=True, text=True, env=env, timeout=60)
    finally:
        held.release()
    assert p.returncode != 0
    assert "Traceback" not in p.stderr
    err = [ln for ln in p.stderr.splitlines() if ln.strip()]
    assert len(err) == 1, p.stderr
    assert "GPIB0::12" in err[0] and "ls455" in err[0] and f"pid {os.getpid()}" in err[0]
    assert err[0].isascii()
