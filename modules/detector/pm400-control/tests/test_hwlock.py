"""One physical console, one service (suite hwlock rule, 2026-09-27).

The real TLPMX backend claims the console's VISA resource BEFORE TLPMX_init
and releases it in close(). These tests run the real backend against a fake
TLPMX_64.dll, so they need no hardware and no Thorlabs software. The lock
files go to a temporary folder (AALTOFLOW_LOCK_DIR), never to the real one,
so a service Lukas has running is not disturbed.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from pm400 import hwlock
from pm400.backends import tlpmx
from pm400.config import Config
from pm400.sim_system import build_sim_system

RES = "USB0::0x1313::0x807D::P5000000::INSTR"


def _obj(arg):
    return getattr(arg, "_obj", arg)


class FakeDLL:
    """Just enough of TLPMX_64.dll to open and close a session. `fail` names
    a function that returns an error status, to exercise the failure paths."""

    def __init__(self, fail: str = ""):
        self.calls: list[str] = []
        self.fail = fail

    def __getattr__(self, name):
        if not name.startswith("TLPMX_"):
            raise AttributeError(name)

        def fn(*args):
            self.calls.append(name)
            if name == self.fail:
                return -1                       # any negative status is an error
            if name == "TLPMX_init":
                _obj(args[3]).value = 1
            return 0
        return fn


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path))
    # A refused claim normally retries for 2 s (Windows frees a killed
    # process's lock a moment late); no need to wait for that in a test.
    monkeypatch.setattr(tlpmx, "claim", lambda a, m: hwlock.claim(a, m, wait_s=0.0))
    return tmp_path


def _backend(monkeypatch, resource=RES, fake=None):
    fake = fake or FakeDLL()
    monkeypatch.setattr(tlpmx, "load_dll", lambda path="": fake)
    return tlpmx.TLPMXConsole(resource=resource), fake


def test_second_open_of_the_same_console_is_refused_naming_pm400(monkeypatch):
    a, _ = _backend(monkeypatch)
    a.open()
    b, fake_b = _backend(monkeypatch)
    with pytest.raises(hwlock.HardwareBusy, match="pm400"):
        b.open()
    # The refused backend never spoke to the console.
    assert "TLPMX_init" not in fake_b.calls
    a.close()


def test_the_same_console_spelled_differently_still_conflicts(monkeypatch):
    a, _ = _backend(monkeypatch)
    a.open()
    b, _ = _backend(monkeypatch, resource="usb::0x1313::0x807d::p5000000")
    with pytest.raises(hwlock.HardwareBusy):
        b.open()
    a.close()


def test_close_releases_the_console(monkeypatch):
    a, _ = _backend(monkeypatch)
    a.open()
    assert len(hwlock.held()) == 1
    a.close()
    assert hwlock.held() == []
    b, _ = _backend(monkeypatch)
    b.open()                                    # free again
    b.close()


@pytest.mark.parametrize("fail", ["TLPMX_init", "TLPMX_setTimeoutValue"])
def test_a_failed_open_releases_the_console(monkeypatch, fail):
    a, fake = _backend(monkeypatch, fake=FakeDLL(fail=fail))
    with pytest.raises(tlpmx.TLPMXError):
        a.open()
    assert hwlock.held() == []
    if fail == "TLPMX_setTimeoutValue":
        # The session WAS opened, so it must be closed again too.
        assert "TLPMX_close" in fake.calls
    b, _ = _backend(monkeypatch)
    b.open()
    b.close()


def test_auto_discovery_skips_a_console_another_service_holds(monkeypatch):
    other = "USB0::0x1313::0x807D::P5000001::INSTR"
    monkeypatch.setattr(tlpmx, "list_resources", lambda path="": [
        {"resource": RES, "model": "PM400"}, {"resource": other, "model": "PM400"}])
    held = hwlock.claim(RES, "somebody")
    a, _ = _backend(monkeypatch, resource="")
    a.open()
    assert a.resource == other                  # took the free one, not the held one
    a.close()
    held.release()


def test_auto_discovery_with_only_a_held_console_says_who_holds_it(monkeypatch):
    monkeypatch.setattr(tlpmx, "list_resources", lambda path="": [
        {"resource": RES, "model": "PM400"}])
    held = hwlock.claim(RES, "pm16")
    a, fake = _backend(monkeypatch, resource="")
    with pytest.raises(hwlock.HardwareBusy, match="pm16"):
        a.open()
    assert "TLPMX_init" not in fake.calls
    held.release()


def test_the_simulator_claims_nothing():
    meter, _ = build_sim_system(Config())
    meter.start(poll=False)
    meter.poll_once()
    assert hwlock.held() == []
    meter.shutdown()


def test_run_service_ends_with_one_line_when_the_console_is_busy(monkeypatch, capsys):
    """The service's start-up path: HardwareBusy -> one ASCII line on stderr,
    a non-zero exit, no traceback, and not a single call to the console."""
    fake = FakeDLL()
    monkeypatch.setattr(tlpmx, "load_dll", lambda path="": fake)
    held = hwlock.claim(RES, "pm16")
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "run_service.py")
    spec = importlib.util.spec_from_file_location("pm400_run_service", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "argv", ["run_service.py", "--real", "--resource", RES,
                                      "--cmd-port", "15917", "--pub-port", "15918"])
    code = mod.main()
    err = capsys.readouterr().err
    assert code != 0
    assert "already in use by pm16" in err and "Traceback" not in err
    assert err.isascii() and len(err.strip().splitlines()) == 1
    assert fake.calls == []                     # never opened, so nothing sent
    held.release()
