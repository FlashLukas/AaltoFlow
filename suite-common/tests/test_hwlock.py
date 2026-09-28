"""hwlock: one physical instrument, one service -- keyed by the ADDRESS."""

import subprocess
import sys
import textwrap

import pytest

from suite_common import hwlock


@pytest.fixture(autouse=True)
def lockdir(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("a,b", [
    ("GPIB0::6::INSTR", "GPIB0::6"),
    ("gpib::6", "GPIB0::6"),
    ("GPIB1::6::INSTR", "GPIB1::6"),
    ("GPIB0::6::2::INSTR", "GPIB0::6::2"),
    ("COM5", "COM5"),
    ("com05", "COM5"),
    ("ASRL5::INSTR", "COM5"),
    ("\\\\.\\COM12", "COM12"),
    ("TCPIP0::10.0.0.5::inst0::INSTR", "TCPIP::10.0.0.5"),
    ("TCPIP0::10.0.0.5::5025::SOCKET", "TCPIP::10.0.0.5"),
    ("10.0.0.5:50000", "TCPIP::10.0.0.5"),
    ("USB0::0x1313::0x8078::P0012345::INSTR", "USB::0X1313::0X8078::P0012345"),
    ("27000123", "27000123"),
    ("Dev1", "DEV1"),
])
def test_normalize(a, b):
    assert hwlock.normalize(a) == b


def test_same_address_different_spelling_is_refused():
    lock = hwlock.claim("GPIB0::6::INSTR", "clMag")
    with pytest.raises(hwlock.HardwareBusy, match=r"GPIB0::6 is already in use by clMag"):
        hwlock.claim("GPIB::6", "kepco")
    lock.release()
    hwlock.claim("gpib0::6", "kepco").release()   # free again after release


def test_different_addresses_coexist():
    a = hwlock.claim("GPIB0::6", "clMag")
    b = hwlock.claim("GPIB0::19", "hp8648")
    assert {h["module"] for h in hwlock.held()} == {"clMag", "hp8648"}
    a.release(); b.release()
    assert hwlock.held() == []


def test_lock_is_freed_when_the_holder_process_dies(lockdir):
    # A crashed service must not leave the instrument "busy": the OS drops the
    # lock with the process. Hold it in a child, kill the child, claim again.
    src = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(hwlock.__file__.rsplit('suite_common', 1)[0])!r})
        from suite_common import hwlock
        lock = hwlock.claim("COM7", "tc200")
        print(__import__("os").getpid(), flush=True)
        time.sleep(60)
    """)
    env = {**__import__("os").environ, "AALTOFLOW_LOCK_DIR": str(lockdir)}
    child = subprocess.Popen([sys.executable, "-c", src], stdout=subprocess.PIPE, env=env, text=True)
    try:
        pid = int(child.stdout.readline())
        with pytest.raises(hwlock.HardwareBusy, match="tc200"):
            hwlock.claim("COM7", "chopper", wait_s=0)
        # kill the process that HOLDS the lock: on Windows a venv's python.exe
        # is a launcher whose child is the real interpreter (gotcha #7)
        import os, signal
        os.kill(pid, signal.SIGTERM)
    finally:
        child.kill()
        child.wait(10)
    hwlock.claim("COM7", "chopper").release()
