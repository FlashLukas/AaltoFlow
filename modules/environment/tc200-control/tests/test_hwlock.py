"""One physical TC200 (= one COM port) may be driven by one service at a time.

The real backend claims its COM port through hwlock before it sends a byte;
these tests run it on the FAKE serial port from test_serial_backend (offline),
with the lock folder pointed at a temp dir by conftest."""

import pytest

from tc200 import hwlock
from tc200.backends.serial_tc200 import SerialTC200
from tc200.config import Config
from tc200.heater import Heater
from tc200.sim_system import build_sim_system

from test_serial_backend import FakeTC200Port


def _backend(port_name="COM5", factory=None):
    cfg = Config()
    cfg.hardware.port = port_name
    fake = FakeTC200Port()
    return SerialTC200(cfg, serial_factory=factory or (lambda p, b, t: fake))


def test_second_open_on_same_port_is_refused():
    a = _backend("COM5")
    a.open()
    b = _backend("COM5")
    with pytest.raises(hwlock.HardwareBusy) as ei:
        b.open()
    assert "tc200" in str(ei.value) and "COM5" in str(ei.value)
    a.close()


@pytest.mark.parametrize("first,second", [("COM5", "com5"), ("COM5", "ASRL5::INSTR"),
                                          ("com5", r"\\.\COM5")])
def test_same_port_spelled_differently_conflicts(first, second):
    a = _backend(first)
    a.open()
    with pytest.raises(hwlock.HardwareBusy):
        _backend(second).open()
    a.close()


def test_different_ports_do_not_conflict():
    a, b = _backend("COM5"), _backend("COM6")
    a.open()
    b.open()
    a.close()
    b.close()


def test_close_releases_the_claim():
    a = _backend("COM5")
    a.open()
    assert len(hwlock.held()) == 1
    a.close()
    assert hwlock.held() == []
    b = _backend("COM5")
    b.open()                                    # a new open succeeds
    b.close()


def test_port_that_cannot_open_releases_the_claim():
    def broken(p, b, t):
        raise OSError("could not open port 'COM5': FileNotFoundError")
    a = _backend("COM5", factory=broken)
    with pytest.raises(RuntimeError):
        a.open()
    assert hwlock.held() == []
    b = _backend("COM5")
    b.open()
    b.close()


def test_silent_box_releases_the_claim():
    """Port opens but nothing answers (box off): open() fails on the first
    reading and must give the address back."""
    class Silent(FakeTC200Port):
        def write(self, data):
            self.sent.append(data)              # never answers
    a = _backend("COM5", factory=lambda p, b, t: Silent())
    with pytest.raises(RuntimeError):
        a.open()
    assert hwlock.held() == []


def test_failed_heater_start_sends_nothing_and_releases():
    """A start that dies after open() closes the port without any command."""
    class BadStat(FakeTC200Port):
        def write(self, data):
            if data.decode("ascii").rstrip("\r") == "stat?":
                self.sent.append("stat?")
                self._out += b"stat?\r> "         # empty answer -> parse error
                return
            super().write(data)
    fake = BadStat()
    cfg = Config()
    cfg.hardware.port = "COM5"
    heater = Heater(SerialTC200(cfg, serial_factory=lambda p, b, t: fake), cfg)
    with pytest.raises(RuntimeError):
        heater.start(poll=False)
    assert hwlock.held() == []
    assert all(c == "" or c.endswith("?") for c in fake.sent), fake.sent
    heater.shutdown()                           # not connected -> no "ens" sent
    assert "ens" not in fake.sent


def test_busy_heater_start_sends_nothing():
    """The second service's brain never reaches the box, and its shutdown
    does not try to switch off a heater it does not own."""
    holder = hwlock.claim("COM5", "kepco")
    fake = FakeTC200Port()
    cfg = Config()
    cfg.hardware.port = "com5"
    heater = Heater(SerialTC200(cfg, serial_factory=lambda p, b, t: fake), cfg)
    with pytest.raises(hwlock.HardwareBusy, match="kepco"):
        heater.start(poll=False)
    heater.shutdown()
    assert fake.sent == []
    holder.release()


def test_sim_backend_claims_nothing():
    heater, _ = build_sim_system(Config())
    heater.start(poll=False)
    try:
        assert hwlock.held() == []
    finally:
        heater.shutdown()
    assert hwlock.held() == []
