"""One physical SCU, one service (Lukas's rule, 2026-09-27).

The real backend claims "SMARACT-SCU::<device id>" -- the ID the controller
reports itself -- before it queries the channel. These tests drive the REAL
backend (backends/scu.py) with a fake SCU DLL, so they run offline on any PC.
conftest.py points AALTOFLOW_LOCK_DIR at a temp folder for every test, so they
never touch the locks of a service that is really running.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from smaract import hwlock
from smaract.backends import scu as scu_mod
from smaract.config import Config
from smaract.sim_system import build_real_system, build_sim_system

DEV_ID = 4711


class FakeScuDll:
    """Just enough of SCU3DControl.dll for open()/close() and a start().

    `fail` = the name of one SA_ function that returns an error code.
    """

    def __init__(self, device_id=DEV_ID, fail=None):
        self.device_id, self.fail = device_id, fail
        self.calls: list[str] = []

    def __getattr__(self, name):
        if not name.startswith("SA_"):
            raise AttributeError(name)

        def fn(*args):
            self.calls.append(name)
            if name == self.fail:
                return 7                                   # TRANSMIT_ERROR
            out = {"SA_GetNumberOfDevices": 1, "SA_GetDeviceID": self.device_id,
                   "SA_GetSensorPresent_S": 1, "SA_GetClosedLoopMaxFrequency_S": 1000,
                   "SA_GetPosition_S": 0, "SA_GetStatus_S": 0,
                   "SA_GetPhysicalPositionKnown_S": 1}.get(name)
            if out is not None:
                args[-1]._obj.value = out                  # ctypes.byref(...)
            return 0
        # ctypes sets .argtypes/.restype on the function: allow it.
        fn.argtypes = fn.restype = None
        return fn


def _backend(monkeypatch, dll):
    monkeypatch.setattr(scu_mod.ctypes, "CDLL", lambda path: dll)
    return scu_mod.ScuStage(Config())


def _hold_in_other_process(address, module="smaract"):
    """Claim `address` from a child process, as a second service would."""
    src = str(Path(scu_mod.__file__).resolve().parents[2])
    code = ("import sys; sys.path.insert(0, %r); from smaract import hwlock; "
            "l = hwlock.claim(%r, %r); print('held', flush=True); sys.stdin.read()"
            % (src, address, module))
    p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_second_backend_on_same_scu_is_refused(monkeypatch):
    a = _backend(monkeypatch, FakeScuDll())
    a.open()
    dll_b = FakeScuDll()
    b = _backend(monkeypatch, dll_b)
    with pytest.raises(hwlock.HardwareBusy, match="smaract"):
        b.open()
    # The refused one found the box and read its ID, nothing more: no
    # channel query, no move, and its library session was released.
    assert dll_b.calls == ["SA_InitDevices", "SA_GetNumberOfDevices",
                           "SA_GetDeviceID", "SA_ReleaseDevices"]
    assert b._lib is None
    a.close()


def test_other_process_holding_the_scu_is_named(monkeypatch):
    p = _hold_in_other_process(scu_mod.scu_address(DEV_ID))
    try:
        with pytest.raises(hwlock.HardwareBusy, match=r"smaract \(pid"):
            _backend(monkeypatch, FakeScuDll()).open()
    finally:
        p.stdin.close()
        p.wait(timeout=10)


def test_same_scu_spelled_differently_conflicts(monkeypatch):
    a = _backend(monkeypatch, FakeScuDll())
    a.open()
    # Lower case: the same physical box, so the same lock.
    with pytest.raises(hwlock.HardwareBusy, match="smaract"):
        hwlock.claim(f"smaract-scu::{DEV_ID}", "other", wait_s=0.1)
    assert hwlock.normalize(f"smaract-scu::{DEV_ID}") == hwlock.normalize(
        scu_mod.scu_address(DEV_ID))
    a.close()


def test_different_scu_does_not_conflict(monkeypatch):
    # (The DLL is loaded inside open(), so patch it right before each open.)
    a = _backend(monkeypatch, FakeScuDll(device_id=1))
    a.open()
    b = _backend(monkeypatch, FakeScuDll(device_id=2))
    b.open()                                  # two boxes, two services: fine
    assert len(hwlock.held()) == 2
    a.close()
    b.close()


def test_close_releases(monkeypatch):
    a = _backend(monkeypatch, FakeScuDll())
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["smaract"]
    a.close()
    assert hwlock.held() == []
    b = _backend(monkeypatch, FakeScuDll())
    b.open()                                  # free again
    b.close()


@pytest.mark.parametrize("fail", ["SA_GetSensorPresent_S",
                                  "SA_GetClosedLoopMaxFrequency_S"])
def test_failure_after_claim_releases(monkeypatch, fail):
    dll = FakeScuDll(fail=fail)
    a = _backend(monkeypatch, dll)
    with pytest.raises(scu_mod.ScuError):
        a.open()
    assert hwlock.held() == []
    assert dll.calls[-1] == "SA_ReleaseDevices"
    dll.fail = None
    a.open()                                  # retry works: nothing left claimed
    a.close()


def test_wrong_sensor_type_releases(monkeypatch):
    a = _backend(monkeypatch, FakeScuDll())
    a.cfg.hardware.sensor_type = 7            # the fake reports 0
    with pytest.raises(scu_mod.ScuError, match="sensor type"):
        a.open()
    assert hwlock.held() == []


def test_failure_before_claim_releases_session(monkeypatch):
    dll = FakeScuDll(fail="SA_GetNumberOfDevices")
    a = _backend(monkeypatch, dll)
    with pytest.raises(scu_mod.ScuError):
        a.open()
    assert hwlock.held() == []
    assert dll.calls[-1] == "SA_ReleaseDevices"


def test_brain_start_failure_after_open_releases(monkeypatch):
    """If the first reads of start() fail, the brain closes the session so the
    claim does not outlive the failed start -- and it sends no STOP to a
    controller whose start never finished."""
    dll = FakeScuDll(fail="SA_GetPosition_S")
    monkeypatch.setattr(scu_mod.ctypes, "CDLL", lambda path: dll)
    brain, _ = build_real_system(Config())
    with pytest.raises(scu_mod.ScuError):
        brain.start()
    assert hwlock.held() == []
    brain.shutdown()                          # a no-op: we are not connected
    assert "SA_Stop_S" not in dll.calls


def test_busy_brain_never_talks_to_the_scu_afterwards(monkeypatch):
    p = _hold_in_other_process(scu_mod.scu_address(DEV_ID), module="clMag")
    try:
        dll = FakeScuDll()
        monkeypatch.setattr(scu_mod.ctypes, "CDLL", lambda path: dll)
        brain, _ = build_real_system(Config())
        with pytest.raises(hwlock.HardwareBusy, match="clMag"):
            brain.start()
        n = len(dll.calls)
        brain.shutdown()
        assert len(dll.calls) == n            # no stop, no release: not ours
    finally:
        p.stdin.close()
        p.wait(timeout=10)


def test_sim_claims_nothing():
    brain, _ = build_sim_system(Config())
    brain.start()
    try:
        assert hwlock.held() == []
    finally:
        brain.shutdown()
    assert hwlock.held() == []


def test_run_service_busy_is_one_line(tmp_path):
    """The real service with a busy SCU: one ASCII line on stderr naming the
    holder, exit 4, no traceback. A tiny wrapper swaps the DLL for the fake
    and runs scripts/run_service.py on scratch ports."""
    root = Path(scu_mod.__file__).resolve().parents[3]
    wrapper = tmp_path / "wrap.py"
    wrapper.write_text(
        "import sys, runpy\n"
        f"sys.path.insert(0, {str(root / 'src')!r}); sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "from smaract.backends import scu\n"
        "import test_hwlock\n"
        "scu.ctypes.CDLL = lambda path: test_hwlock.FakeScuDll()\n"
        "sys.argv = ['run_service.py', '--real', '--cmd-port', '15941', '--pub-port', '15942']\n"
        f"runpy.run_path({str(root / 'scripts' / 'run_service.py')!r}, run_name='__main__')\n",
        encoding="utf-8")
    p = _hold_in_other_process(scu_mod.scu_address(DEV_ID), module="clMag")
    try:
        r = subprocess.run([sys.executable, str(wrapper)], capture_output=True,
                           text=True, timeout=60)
    finally:
        p.stdin.close()
        p.wait(timeout=10)
    assert r.returncode == 4, r.stderr
    assert "Traceback" not in r.stderr
    line = r.stderr.strip()
    assert "\n" not in line and "clMag" in line and "SMARACT-SCU::4711" in line
    assert line.isascii()
