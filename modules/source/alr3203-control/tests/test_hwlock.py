"""One physical address, one service (hwlock): the real backend claims the
supply's COM port BEFORE it sends a byte, so a second service on the same
supply is refused with HardwareBusy. All offline: pyserial is replaced by a
fake ALR3203 (fake_elc.py), and the lock folder is a temporary one."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from alr3203 import hwlock
from alr3203.backends.elc_serial import SerialALR3203
from alr3203.backends.sim import NoRelay
from alr3203.config import Config
from alr3203.hwlock import HardwareBusy
from alr3203.sim_system import build_sim_system
from alr3203.supply import BipolarSupply

from fake_elc import FakeALR, make_serial_module


@pytest.fixture
def ports(monkeypatch, tmp_path):
    """A fake pyserial with ALR3203s on COM5 and COM6. Locks in tmp."""
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    supplies = {"COM5": FakeALR(), "COM6": FakeALR()}
    opened = []
    monkeypatch.setitem(sys.modules, "serial", make_serial_module(supplies, opened))
    return supplies, opened


def test_second_backend_on_the_same_port_is_refused(ports):
    supplies, opened = ports
    a = SerialALR3203("COM5")
    a.open()
    b = SerialALR3203("COM5")
    with pytest.raises(HardwareBusy, match="alr3203") as e:
        b.open()
    assert "COM5" in str(e.value)
    assert len(opened) == 1                      # the refused one never opened the port
    a.close(output_off=False)


@pytest.mark.parametrize("other", ["com5", "\\\\.\\COM5", "ASRL5::INSTR"])
def test_the_same_port_written_differently_still_conflicts(ports, other):
    a = SerialALR3203("COM5")
    a.open()
    with pytest.raises(HardwareBusy, match="alr3203"):
        SerialALR3203(other).open()
    a.close(output_off=False)


def test_another_port_does_not_conflict(ports):
    a = SerialALR3203("COM5")
    a.open()
    b = SerialALR3203("COM6")
    b.open()
    a.close(output_off=False)
    b.close(output_off=False)


def test_close_releases_the_port(ports):
    a = SerialALR3203("COM5")
    a.open()
    assert [h["normalized"] for h in hwlock.held()] == ["COM5"]
    assert hwlock.held()[0]["module"] == "alr3203"
    a.close(output_off=False)
    assert hwlock.held() == []
    b = SerialALR3203("COM5")
    b.open()                                     # free again
    b.close(output_off=False)


def test_a_silent_supply_releases_the_port_and_writes_nothing(ports):
    supplies, opened = ports
    supplies["COM5"].silent = True               # unplugged / PC control disabled
    a = SerialALR3203("COM5", timeout_s=0.01)
    with pytest.raises(RuntimeError):
        a.open()
    assert hwlock.held() == []
    assert opened[0].closed
    assert supplies["COM5"].log == ["0 IDN RD"]  # and no OUT WR 0 on the way out


def test_no_port_configured_is_refused_without_claiming(ports):
    with pytest.raises(RuntimeError, match="no COM port"):
        SerialALR3203("").open()
    assert hwlock.held() == []


def test_brain_that_cannot_read_the_state_releases_without_writing(ports):
    supplies, opened = ports
    dev = supplies["COM5"]
    real = dev.answer
    dev.answer = lambda line: "0 ERR" if "OUT RD" in line else real(line)
    supply = BipolarSupply(SerialALR3203("COM5"), Config(), relay=NoRelay())
    with pytest.raises(RuntimeError, match="could not read"):
        supply.start(poll=False)
    assert hwlock.held() == []
    assert not any(" WR " in line for line in dev.log)
    assert opened[0].closed


def test_busy_brain_start_sends_nothing(ports):
    supplies, opened = ports
    holder = SerialALR3203("COM5")
    holder.open()
    n = len(supplies["COM5"].log)
    supply = BipolarSupply(SerialALR3203("COM5"), Config(), relay=NoRelay())
    with pytest.raises(HardwareBusy):
        supply.start(poll=False)
    supply.shutdown()                            # e.g. a GUI closing after the error
    assert len(supplies["COM5"].log) == n        # the holder's supply untouched
    holder.close(output_off=False)


def test_the_simulator_claims_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    supply, _ = build_sim_system(Config())
    supply.start(poll=False)
    supply.step()
    assert hwlock.held() == []
    supply.shutdown()


def test_hwlock_is_the_master_copy():
    """Modules carry a copy of suite-common's hwlock.py; tools/check_modules.py
    also compares them."""
    here = Path(hwlock.__file__)
    master = here.parents[5] / "suite-common" / "src" / "suite_common" / "hwlock.py"
    if not master.is_file():
        pytest.skip("installed without suite-common next to it")
    assert here.read_bytes() == master.read_bytes()


def test_service_exits_cleanly_when_the_supply_is_busy(ports, tmp_path):
    """run_service.py --real against a claimed port: one line on stderr naming
    port + holder, exit code 4, no traceback. The port is held by THIS
    process; the child finds it busy through the lock file before it would
    import pyserial."""
    holder = SerialALR3203("COM5")
    holder.open()
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_service.py"
    cfg = tmp_path / "alr3203.ini"
    c = Config()
    c.polarity.driver = "none"
    c.save(str(cfg))
    r = subprocess.run([sys.executable, str(script), "--real", "--com", "com5",
                        "--config", str(cfg),
                        "--cmd-port", "17018", "--pub-port", "17019"],
                       capture_output=True, text=True, timeout=60, env=dict(os.environ))
    holder.close(output_off=False)
    assert r.returncode == 4, r.stderr
    assert "Traceback" not in r.stderr
    err = r.stderr.strip().splitlines()
    assert len(err) == 1
    assert "COM5" in err[0] and "alr3203" in err[0] and "pid" in err[0]
