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


@pytest.mark.parametrize("address", ["COM1", "COM7", "COM9", "LPT1", "CON", "NUL", "AUX", "PRN"])
def test_lock_file_never_has_a_windows_device_name(address):
    # Found on a Windows 10 PC: "COM7.lock" opens the serial port COM7, not a
    # file, and every claim of a COM port failed. Windows 11 does not show it,
    # so the name itself is checked here, not only the claim.
    stem = hwlock._filename(hwlock.normalize(address)).split(".")[0]
    assert not hwlock._RESERVED.fullmatch(stem)
    hwlock.claim(address, "test").release()


def test_ordinary_names_are_unchanged():
    # Existing lock names stay as they were, so services of an older version
    # running at the same time still see the same file.
    assert hwlock._filename("GPIB0::6") == "GPIB0_6.lock"
    assert hwlock._filename("COM12") == "COM12.lock"
    assert hwlock._filename("DEV1") == "DEV1.lock"


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


# --------------------------------------------------------------------------- #
# 2026-11-10: ONE lock folder per PC, in %PROGRAMDATA% (Blue Box: %LOCALAPPDATA%
# is redirected for programs started from the Claude app, so services started
# from Claude and from Mission Control looked in different folders).
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(sys.platform != "win32", reason="Windows folder layout")
def test_the_lock_folder_is_programdata_not_localappdata(monkeypatch, tmp_path):
    monkeypatch.delenv("AALTOFLOW_LOCK_DIR", raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    assert hwlock.lock_dir() == tmp_path / "pd" / "AaltoFlow" / "locks"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows permissions")
def test_a_new_lock_folder_lets_every_user_modify_its_files(monkeypatch, tmp_path):
    """Default %PROGRAMDATA% permissions make a file one user creates read-only
    for the others; the lock folder grants 'Users' modify, inherited by the
    lock files. Checked with the real icacls."""
    monkeypatch.delenv("AALTOFLOW_LOCK_DIR", raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    lock = hwlock.claim("COM42", "test", wait_s=0.0)
    try:
        d = hwlock.lock_dir()
        # the folder's security descriptor as SDDL: "BU" = BUILTIN\Users (any
        # language), OICI = inherited by files and folders, 0x1301bf = Modify
        r = subprocess.run(["icacls", str(d), "/save", str(tmp_path / "acl.txt")],
                           capture_output=True, text=True)
        sddl = (tmp_path / "acl.txt").read_bytes().decode("utf-16", errors="replace")
        assert r.returncode == 0
        assert "(A;OICI;0x1301bf;;;BU)" in sddl, sddl
        assert [h["normalized"] for h in hwlock.held()] == ["COM42"]
    finally:
        lock.release()


def test_the_grant_runs_once_and_a_failing_grant_does_not_stop_a_claim(monkeypatch, tmp_path):
    monkeypatch.delenv("AALTOFLOW_LOCK_DIR", raising=False)
    monkeypatch.setattr(hwlock, "lock_dir", lambda: tmp_path / "locks")
    monkeypatch.setattr(hwlock.sys, "platform", "win32")
    calls = []

    def boom(cmd, **kw):
        calls.append(cmd)
        raise OSError("icacls not found")
    monkeypatch.setattr(subprocess, "run", boom)
    a = hwlock.claim("GPIB0::9", "test", wait_s=0.0)        # grant fails: still claimed
    b_err = None
    try:
        hwlock.claim("GPIB0::9", "other", wait_s=0.0)
    except hwlock.HardwareBusy as exc:
        b_err = str(exc)
    a.release()
    c = hwlock.claim("GPIB0::10", "test", wait_s=0.0)        # folder exists: no new grant
    c.release()
    assert b_err and "test" in b_err
    assert len(calls) == 1 and "S-1-5-32-545" in calls[0][3]


def test_tests_never_touch_the_real_lock_folder(lockdir):
    """With AALTOFLOW_LOCK_DIR set (every test) nothing is granted or created
    outside it."""
    assert hwlock.lock_dir() == lockdir
