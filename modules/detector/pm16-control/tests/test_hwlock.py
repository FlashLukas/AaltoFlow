"""One physical meter, one service (Lukas's rule, 2026-09-27).

The real backend claims the meter's USB address (which carries its serial
number) before TLPMX_init sends anything. These tests drive the real backend
with a fake TLPMX DLL, so they run offline on any PC. AALTOFLOW_LOCK_DIR points
the lock files at a temp folder, so they never touch a running service's locks.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from pm16 import hwlock
from pm16.backends import tlpmx
from pm16.backends.sim import SimulatedPM16

RES = "USB0::0x1313::0x807B::P00000001::INSTR"


class FakeDll:
    """Just enough of TLPMX_64.dll for open()/close(): discovery lists the
    given resources, init succeeds unless the resource is in `fail_init`."""

    def __init__(self, resources=(RES,), fail_init=(), fail_timeout=False):
        self.resources = list(resources)
        self.fail_init = set(fail_init)
        self.fail_timeout = fail_timeout
        self.inits = []

    def __getattr__(self, name):
        def fn(*args):
            if name == "TLPMX_findRsrc":
                args[1]._obj.value = len(self.resources)
            elif name == "TLPMX_getRsrcName":
                args[2].value = self.resources[args[1]].encode()
            elif name == "TLPMX_init":
                res = args[0].decode()
                self.inits.append(res)
                if res in self.fail_init:
                    return -1073807298              # 0xBFFF003E, I/O error
                args[3]._obj.value = 1
            elif name == "TLPMX_setTimeoutValue" and self.fail_timeout:
                return -1073807298
            elif name == "TLPMX_errorMessage":
                args[2].value = b"fake error"
            return 0
        return fn


@pytest.fixture
def locks(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    return tmp_path / "locks"


def _backend(monkeypatch, dll, resource=RES):
    monkeypatch.setattr(tlpmx, "load_dll", lambda path="": dll)
    return tlpmx.TLPMXPowerMeter(resource=resource)


def _hold_in_other_process(address, module="clMag"):
    """Claim `address` from a child process, as a second service would.
    (Inside ONE process a second claim is refused too, but a real clash is
    always between two processes, so test that.)"""
    src = str(Path(tlpmx.__file__).resolve().parents[2])
    code = ("import sys; sys.path.insert(0, %r); from pm16 import hwlock; "
            "l = hwlock.claim(%r, %r); print('held', flush=True); sys.stdin.read()"
            % (src, address, module))
    p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_second_backend_on_same_meter_is_refused(locks, monkeypatch):
    dll = FakeDll()
    a = _backend(monkeypatch, dll)
    a.open()
    b = tlpmx.TLPMXPowerMeter(resource=RES)
    with pytest.raises(hwlock.HardwareBusy, match="pm16"):
        b.open()
    assert dll.inits == [RES]                        # the second never talked to the meter
    a.close()


def test_other_process_holding_the_meter_is_named(locks, monkeypatch):
    p = _hold_in_other_process(RES, module="pm16")
    try:
        dll = FakeDll()
        b = _backend(monkeypatch, dll)
        with pytest.raises(hwlock.HardwareBusy, match=r"pm16 \(pid"):
            b.open()
        assert dll.inits == []
    finally:
        p.stdin.close()
        p.wait(timeout=10)


def test_same_meter_spelled_differently_conflicts(locks, monkeypatch):
    dll = FakeDll()
    a = _backend(monkeypatch, dll, resource=RES)
    a.open()
    # Board number dropped, lower case, no ::INSTR: still the same box.
    b = tlpmx.TLPMXPowerMeter(resource="usb::0x1313::0x807b::P00000001")
    with pytest.raises(hwlock.HardwareBusy):
        b.open()
    a.close()


def test_close_releases(locks, monkeypatch):
    dll = FakeDll()
    a = _backend(monkeypatch, dll)
    a.open()
    assert len(hwlock.held()) == 1
    a.close()
    assert hwlock.held() == []
    b = tlpmx.TLPMXPowerMeter(resource=RES)
    b.open()                                         # free again
    b.close()


def test_failed_init_releases(locks, monkeypatch):
    dll = FakeDll(fail_init={RES})
    a = _backend(monkeypatch, dll)
    with pytest.raises(tlpmx.TLPMXError):
        a.open()
    assert hwlock.held() == []
    dll.fail_init.clear()
    a.open()
    a.close()


def test_failure_after_init_closes_and_releases(locks, monkeypatch):
    dll = FakeDll(fail_timeout=True)
    a = _backend(monkeypatch, dll)
    with pytest.raises(tlpmx.TLPMXError):
        a.open()
    assert hwlock.held() == []
    assert a._vi.value == 0                          # the half-open session was closed


def test_autodiscovery_claims_the_meter_it_opens(locks, monkeypatch):
    other = "USB0::0x1313::0x807B::111111111::INSTR"
    dll = FakeDll(resources=[RES, other])
    p = _hold_in_other_process(RES)                  # first meter belongs to someone else
    try:
        a = _backend(monkeypatch, dll, resource="")
        a.open()
        assert a.resource == other and dll.inits == [other]
        assert [h["normalized"] for h in hwlock.held()
                if h["module"] == "pm16"] == [hwlock.normalize(other)]
        a.close()
    finally:
        p.stdin.close()
        p.wait(timeout=10)


def test_autodiscovery_all_busy_raises_hardware_busy(locks, monkeypatch):
    p = _hold_in_other_process(RES, module="clMag")
    try:
        a = _backend(monkeypatch, FakeDll(), resource="")
        with pytest.raises(hwlock.HardwareBusy, match="clMag"):
            a.open()
    finally:
        p.stdin.close()
        p.wait(timeout=10)


def test_sim_claims_nothing(locks):
    sim = SimulatedPM16()
    sim.open()
    sim.measure_power()
    assert hwlock.held() == []
    sim.close()


def test_meter_start_failure_after_open_releases(locks, monkeypatch):
    """If the first queries fail, the brain closes the session so the claim
    does not outlive the failed start."""
    from pm16.config import Config
    from pm16.meter import PowerMeter

    a = _backend(monkeypatch, FakeDll())
    monkeypatch.setattr(a, "get_wavelength", lambda: (_ for _ in ()).throw(
        tlpmx.TLPMXError("boom")))
    meter = PowerMeter(a, Config())
    with pytest.raises(tlpmx.TLPMXError):
        meter.start(poll=False)
    assert hwlock.held() == []


def test_run_service_busy_is_one_line(locks, tmp_path):
    """The real service with a busy meter: one ASCII line on stderr naming the
    holder, exit 4, no traceback. Another process claims the meter first; a
    tiny wrapper swaps the DLL for FakeDll and then runs run_service.py."""
    root = Path(tlpmx.__file__).resolve().parents[3]
    wrapper = tmp_path / "wrap.py"
    wrapper.write_text(
        "import sys, runpy\n"
        f"sys.path.insert(0, {str(root / 'src')!r}); sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "from pm16.backends import tlpmx\n"
        "import test_hwlock\n"
        "tlpmx.load_dll = lambda path='': test_hwlock.FakeDll()\n"
        f"sys.argv = ['run_service.py', '--real', '--resource', {RES!r},"
        " '--cmd-port', '15931', '--pub-port', '15932']\n"
        f"runpy.run_path({str(root / 'scripts' / 'run_service.py')!r}, run_name='__main__')\n",
        encoding="utf-8")
    p = _hold_in_other_process(RES, module="clMag")
    try:
        r = subprocess.run([sys.executable, str(wrapper)], capture_output=True,
                           text=True, timeout=60)
    finally:
        p.stdin.close()
        p.wait(timeout=10)
    assert r.returncode == 4
    assert "Traceback" not in r.stderr
    line = r.stderr.strip()
    assert "\n" not in line and "clMag" in line and line.isascii()
