"""One physical instrument, one service: the real backend claims its COM port.

The ELL14K interface board IS its COM port (every mount on the bus sits behind
it), so that port is the address EllSerialBus claims in open(), before the
first byte goes out. These tests use the byte-level fake serial port from
test_serial_backend, and a private lock folder (conftest, AALTOFLOW_LOCK_DIR).
"""

import sys
import types

import pytest

from elliptec import hwlock
from elliptec.backends.ell_serial import EllSerialBus
from elliptec.config import Config
from elliptec.sim_system import build_real_system, build_sim_system
from test_serial_backend import FakeSerial


@pytest.fixture()
def fake_serial(monkeypatch):
    fake = types.ModuleType("serial")
    fake.Serial = FakeSerial
    fake.EIGHTBITS, fake.PARITY_NONE, fake.STOPBITS_ONE = 8, "N", 1
    monkeypatch.setitem(sys.modules, "serial", fake)
    FakeSerial.instances.clear()
    return fake


def _bus(port):
    cfg = Config()
    cfg.hardware.port = port
    return EllSerialBus(cfg)


def test_hwlock_is_the_master_copy():
    # tools/check_modules.py checks this too; failing here says it sooner.
    from pathlib import Path
    here = Path(__file__).resolve()
    master = here.parents[4] / "suite-common" / "src" / "suite_common" / "hwlock.py"
    if not master.is_file():
        pytest.skip("installed without suite-common next to it")
    copy = here.parents[1] / "src" / "elliptec" / "hwlock.py"
    assert copy.read_bytes() == master.read_bytes()


def test_second_open_on_same_port_is_refused_naming_elliptec(fake_serial):
    a, b = _bus("COM7"), _bus("COM7")
    a.open(["0"])
    try:
        n_ports = len(FakeSerial.instances)
        with pytest.raises(hwlock.HardwareBusy) as ei:
            b.open(["0"])
        assert "elliptec" in str(ei.value) and "COM7" in str(ei.value)
        # refused BEFORE the port was opened: no second serial object, no byte sent
        assert len(FakeSerial.instances) == n_ports
    finally:
        a.close()


@pytest.mark.parametrize("first, second", [("com5", "COM5"), ("COM5", "ASRL5::INSTR"),
                                           (r"\\.\COM5", "com05")])
def test_same_port_spelled_differently_conflicts(fake_serial, first, second):
    a, b = _bus(first), _bus(second)
    a.open(["0"])
    try:
        with pytest.raises(hwlock.HardwareBusy):
            b.open(["0"])
    finally:
        a.close()


def test_other_port_does_not_conflict(fake_serial):
    a, b = _bus("COM5"), _bus("COM6")
    a.open(["0"])
    b.open(["1"])
    assert {h["normalized"] for h in hwlock.held()} == {"COM5", "COM6"}
    a.close()
    b.close()


def test_close_releases(fake_serial):
    a = _bus("COM8")
    a.open(["0"])
    assert [h["module"] for h in hwlock.held()] == ["elliptec"]
    a.close()
    assert hwlock.held() == []
    b = _bus("COM8")
    b.open(["0"])            # the next service gets the port
    b.close()
    a.close()                # idempotent


def test_reopen_of_the_same_bus_is_not_refused(fake_serial):
    # open() on an already open bus closes it first (it must not refuse itself)
    a = _bus("COM9")
    a.open(["0"])
    a.open(["0", "1"])
    assert len(hwlock.held()) == 1
    a.close()
    assert hwlock.held() == []


def test_failing_open_releases_and_closes_the_port(fake_serial):
    a = _bus("COM4")
    a.cfg.hardware.read_timeout_s = 0.01
    with pytest.raises(TimeoutError):
        a.open(["5"])        # nobody on address 5: "in" is never answered
    assert not FakeSerial.instances[-1].open      # port closed again
    assert hwlock.held() == []                    # and not left claimed
    b = _bus("COM4")
    b.open(["0"])
    b.close()


def test_failing_serial_constructor_releases(fake_serial, monkeypatch):
    def boom(**kw):
        raise OSError("could not open port 'COM3': PermissionError")
    monkeypatch.setattr(fake_serial, "Serial", boom)
    with pytest.raises(OSError):
        _bus("COM3").open(["0"])
    assert hwlock.held() == []


def test_busy_brain_start_sends_nothing_and_shutdown_is_silent(fake_serial):
    """The service path: a refused claim must not lead to 'safe state' writes
    (stop commands) to a bus another service owns."""
    holder = _bus("COM2")
    holder.open(["0"])
    try:
        n = len(FakeSerial.instances)
        cfg = Config()
        cfg.hardware.port = "com2"
        brain, _backend = build_real_system(cfg)
        with pytest.raises(hwlock.HardwareBusy):
            brain.start()
        brain.shutdown()                          # what the service's finally: does
        assert len(FakeSerial.instances) == n     # never opened a port
        assert FakeSerial.instances[-1].sent.count("0st") == 0
    finally:
        holder.close()


def test_sim_backend_never_claims():
    brain, _backend = build_sim_system(Config())
    brain.start()
    try:
        assert hwlock.held() == []
    finally:
        brain.shutdown()
    assert hwlock.held() == []
