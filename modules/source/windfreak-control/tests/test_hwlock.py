"""One physical instrument, one service (Lukas's rule, hwlock.py).

The real backend must claim its COM port before it sends a byte, refuse a
second claim of the SAME port however it is spelled, and give the port back
on close() and on a failed open(). The simulator claims nothing. All offline:
a fake `serial` module (from test_backend_serial) and a private lock folder
(conftest's autouse fixture sets AALTOFLOW_LOCK_DIR).
"""

import pytest

from windfreak import hwlock
from windfreak.backends.synthhd import SerialSynthHD
from windfreak.hwlock import HardwareBusy

from test_backend_serial import fake_serial  # noqa: F401  (pytest fixture)


def test_second_backend_on_the_same_port_is_refused(fake_serial):
    first = SerialSynthHD("COM5")
    first.open()
    second = SerialSynthHD("COM5")
    with pytest.raises(HardwareBusy) as err:
        second.open()
    assert "windfreak" in str(err.value) and "COM5" in str(err.value)
    # refused BEFORE a port was opened: only the first backend made one
    assert len(fake_serial) == 1
    first.close()


@pytest.mark.parametrize("other", ["com5", "ASRL5::INSTR", r"\\.\COM5"])  # Win32 device path
def test_the_same_port_spelled_differently_is_the_same_instrument(fake_serial, other):
    first = SerialSynthHD("COM5")
    first.open()
    with pytest.raises(HardwareBusy):
        SerialSynthHD(other).open()
    first.close()


def test_different_ports_do_not_conflict(fake_serial):
    a, b = SerialSynthHD("COM5"), SerialSynthHD("COM6")
    a.open()
    b.open()
    assert {h["normalized"] for h in hwlock.held()} == {"COM5", "COM6"}
    a.close()
    b.close()


def test_close_releases_the_port(fake_serial):
    first = SerialSynthHD("COM5")
    first.open()
    assert [h["module"] for h in hwlock.held()] == ["windfreak"]
    first.close()
    assert hwlock.held() == []
    again = SerialSynthHD("COM5")
    again.open()              # would raise HardwareBusy if close() had kept it
    again.close()


def test_a_failing_open_releases_the_port(fake_serial):
    """The instrument does not answer the id query: open() fails, and the
    port must be free (and closed) afterwards -- with nothing written but
    the query itself (no "RF off" to a box we never identified)."""
    b = SerialSynthHD("COM5")
    orig = b._query

    def dead(cmd):
        raise TimeoutError("no answer")
    b._query = dead
    with pytest.raises(TimeoutError):
        b.open()
    assert hwlock.held() == []
    assert fake_serial[0].closed and fake_serial[0].writes == []
    b._query = orig
    b.open()                  # the next try gets the port
    b.close()


def test_a_missing_pyserial_releases_the_port(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "serial", None)   # "import serial" -> ImportError
    with pytest.raises(ImportError):
        SerialSynthHD("COM5").open()
    assert hwlock.held() == []


def test_the_simulator_claims_nothing():
    from windfreak.config import Config
    from windfreak.sim_system import build_sim_system
    synth, _ = build_sim_system(Config())
    synth.start()
    try:
        assert hwlock.held() == []
    finally:
        synth.shutdown()
    assert hwlock.held() == []
