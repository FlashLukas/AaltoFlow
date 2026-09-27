"""One physical analyser, one service (hwlock, Lukas's rule 2026-09-27).

The real backend claims the GSP-818's VISA address before the first byte goes
to it. These tests run the REAL open() path offline: a fake `pyvisa` module is
put in sys.modules, whose ResourceManager hands out tests/fake_gsp.FakeGsp
instruments. The lock files go to a temp folder (AALTOFLOW_LOCK_DIR), so a
service running on this PC is never disturbed."""

import sys
import types

import pytest

from fake_gsp import FakeGsp
from gsp818 import hwlock
from gsp818.backends.gsp import GspAnalyzer
from gsp818.config import Config
from gsp818.hwlock import HardwareBusy
from gsp818.sim_system import build_sim_system

USB = "USB0::0x2184::0x1234::GSP000000::INSTR"


class FakeRM:
    """Stands in for pyvisa.ResourceManager: every open is recorded, so a test
    can prove a refused backend never even opened a session."""

    def __init__(self, bus):
        self.bus = bus

    def list_resources(self, pattern="?*::INSTR"):
        return list(self.bus["resources"])

    def open_resource(self, name):
        self.bus["opened"].append(name)
        inst = FakeGsp()
        if self.bus.get("idn_fails"):
            def bad(cmd, _q=inst.query):
                if cmd == "*IDN?":
                    raise TimeoutError("VI_ERROR_TMO")
                return _q(cmd)
            inst.query = bad
        self.bus["instruments"].append(inst)
        return inst

    def close(self):
        pass


@pytest.fixture
def bus(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    state = {"resources": [USB], "opened": [], "instruments": []}
    fake = types.ModuleType("pyvisa")
    fake.ResourceManager = lambda *a: FakeRM(state)
    monkeypatch.setitem(sys.modules, "pyvisa", fake)
    return state


def _real(resource=""):
    cfg = Config()
    cfg.hardware.resource = resource
    return GspAnalyzer(cfg, sleep=lambda s: None)


def test_second_backend_on_same_address_is_refused(bus):
    a = _real(USB)
    a.open()
    b = _real(USB)
    with pytest.raises(HardwareBusy) as err:
        b.open()
    assert "gsp818" in str(err.value)
    assert bus["opened"] == [USB]          # the refused one never opened a session
    b.close()                              # sends nothing, raises nothing
    a.close()


@pytest.mark.parametrize("first, second", [
    (USB, "usb0::0x2184::0x1234::GSP000000::INSTR"),
    (USB, "USB::0x2184::0x1234::GSP000000"),
    ("TCPIP0::192.168.1.168::inst0::INSTR", "192.168.1.168"),
    ("TCPIP0::192.168.1.168::inst0::INSTR", "TCPIP::192.168.1.168::5025::SOCKET"),
])
def test_same_address_written_differently_conflicts(bus, first, second):
    a = _real(first)
    a.open()
    with pytest.raises(HardwareBusy):
        _real(second).open()
    a.close()


def test_close_releases(bus):
    a = _real(USB)
    a.open()
    assert len(hwlock.held()) == 1 and hwlock.held()[0]["module"] == "gsp818"
    a.close()
    assert hwlock.held() == []
    b = _real(USB)
    b.open()                               # the address is free again
    b.close()


def test_failed_open_releases(bus):
    bus["idn_fails"] = True
    a = _real(USB)
    with pytest.raises(TimeoutError):
        a.open()
    assert hwlock.held() == []
    assert bus["instruments"][0].writes == []   # start-up wrote nothing, even on failure
    assert bus["instruments"][0].closed
    bus["idn_fails"] = False
    b = _real(USB)
    b.open()
    b.close()


def test_auto_discovery_claims_and_skips_a_busy_analyser(bus):
    a = _real("")                          # "" = find the first GSP-818 on USB
    a.open()
    assert hwlock.held()[0]["normalized"] == hwlock.normalize(USB)
    n_opened = len(bus["opened"])
    with pytest.raises(HardwareBusy) as err:
        _real("").open()
    assert "gsp818" in str(err.value)
    assert len(bus["opened"]) == n_opened   # not even *IDN? went to the owned one
    a.close()
    assert hwlock.held() == []


def test_simulator_claims_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    sa, _ = build_sim_system(Config(), realtime=False)
    sa.start(run=False)
    assert hwlock.held() == []
    sa.shutdown()
    assert hwlock.held() == []


def test_hwlock_copy_is_the_master():
    """tools/check_modules.py compares the copies too; this catches an edit
    to the module copy while working inside this folder."""
    from pathlib import Path
    master = Path(__file__).resolve().parents[4] / "suite-common" / "src" / "suite_common" / "hwlock.py"
    if not master.is_file():
        pytest.skip("suite-common not next to this module (installed on its own)")
    mine = Path(hwlock.__file__)
    assert mine.read_bytes() == master.read_bytes()
