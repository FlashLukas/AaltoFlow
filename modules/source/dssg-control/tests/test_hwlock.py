"""One physical SG12000L, one service (Lukas's rule: "the same instrument has
to be defined by the same physical address").

The real backend claims its COM port / IP in open() through hwlock.py; these
tests drive it against fake links (no pyserial, no hardware) and point the
lock folder at a temp dir, so they never see a service Lukas has running.
"""

import os
import subprocess
import sys

import pytest

from dssg import hwlock
from dssg.backends import dsi_scpi
from dssg.backends.dsi_scpi import DsiSG12000L
from dssg.config import Config
from dssg.hwlock import HardwareBusy
from dssg.sim_system import build_sim_system

_UNIT = {"*IDN?": "DS INSTRUMENTS,SG12000L,1234,2.1", "PHASE?": "0.00",
         "SYST:ERR?": '0,"No error"'}


class FakeLink:
    def __init__(self, *args, fail_on=None):
        self.sent, self.closed, self.fail_on = [], False, fail_on

    def write(self, line):
        self.sent.append(line)

    def query(self, line):
        self.sent.append(line)
        if line == self.fail_on:
            raise TimeoutError(line)
        return _UNIT[line]

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def lockdir(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(dsi_scpi, "_SerialLink", FakeLink)
    monkeypatch.setattr(dsi_scpi, "_TcpLink", FakeLink)
    return tmp_path


def test_second_open_on_same_com_port_is_refused():
    a = DsiSG12000L(com_port="COM5")
    a.open()
    b = DsiSG12000L(com_port="COM5")
    with pytest.raises(HardwareBusy, match="dssg"):
        b.open()
    assert b._link is None                     # never talked to the unit
    a.close()


@pytest.mark.parametrize("first,second", [("COM5", "com5"), ("COM5", r"\\.\COM5"),
                                          ("COM5", "ASRL5::INSTR")])
def test_same_com_port_written_differently_conflicts(first, second):
    a = DsiSG12000L(com_port=first)
    a.open()
    with pytest.raises(HardwareBusy):
        DsiSG12000L(com_port=second).open()
    a.close()


def test_same_ip_on_another_tcp_port_conflicts():
    # One box is one box, whatever TCP port a second client picks.
    a = DsiSG12000L(transport="tcp", host="10.0.0.23", tcp_port=10001)
    a.open()
    with pytest.raises(HardwareBusy):
        DsiSG12000L(transport="tcp", host="10.0.0.23", tcp_port=5025).open()
    a.close()


def test_different_addresses_coexist():
    a, b = DsiSG12000L(com_port="COM5"), DsiSG12000L(com_port="COM6")
    a.open()
    b.open()
    assert len(hwlock.held()) == 2
    a.close()
    b.close()


def test_close_releases():
    a = DsiSG12000L(com_port="COM5")
    a.open()
    a.close()
    assert hwlock.held() == []
    b = DsiSG12000L(com_port="COM5")
    b.open()                                   # free again
    b.close()


def test_close_without_rf_off_also_releases():
    # The brain's failed-start path: close(rf_off=False).
    a = DsiSG12000L(com_port="COM5")
    a.open()
    a.close(rf_off=False)
    assert hwlock.held() == []


def test_failing_open_releases(monkeypatch):
    # *IDN? times out after the port opened: the link is closed silently and
    # the claim dropped, so a retry (or another service) can have the unit.
    links = []

    def failing(*args):
        links.append(FakeLink(fail_on="*IDN?"))
        return links[-1]
    monkeypatch.setattr(dsi_scpi, "_SerialLink", failing)
    a = DsiSG12000L(com_port="COM5")
    with pytest.raises(TimeoutError):
        a.open()
    assert links[0].closed and "OUTP:STAT OFF" not in links[0].sent
    assert hwlock.held() == []
    monkeypatch.setattr(dsi_scpi, "_SerialLink", FakeLink)
    b = DsiSG12000L(com_port="COM5")
    b.open()
    b.close()


def test_link_construction_failure_releases(monkeypatch):
    def boom(*args):
        raise OSError("could not open port COM5")
    monkeypatch.setattr(dsi_scpi, "_SerialLink", boom)
    with pytest.raises(OSError):
        DsiSG12000L(com_port="COM5").open()
    assert hwlock.held() == []


def test_busy_brain_start_sends_nothing(monkeypatch):
    # The brain's failed start must not "RF off" a unit it never claimed.
    from dssg.synthesizer import Synthesizer
    holder = DsiSG12000L(com_port="COM5")
    holder.open()
    links = []

    def spy(*args):
        links.append(FakeLink())
        return links[-1]
    monkeypatch.setattr(dsi_scpi, "_SerialLink", spy)
    cfg = Config()
    cfg.hardware.transport, cfg.hardware.com_port = "serial", "COM5"
    synth = Synthesizer(DsiSG12000L(com_port="COM5"), cfg)
    with pytest.raises(HardwareBusy):
        synth.start()
    synth.shutdown()                           # the crash path, too
    assert links == []                         # no link was ever made
    assert len(hwlock.held()) == 1             # the holder still has it
    holder.close()


def test_sim_backend_claims_nothing():
    synth, _ = build_sim_system(Config())
    synth.start()
    try:
        assert hwlock.held() == []
    finally:
        synth.shutdown()


def test_run_service_reports_busy_in_one_line(lockdir):
    """The service script: one readable stderr line naming the holder, exit 4,
    no traceback. The address is held by THIS process; the child sees it."""
    held = hwlock.claim("COM97", "dssg")
    script = os.path.join(os.path.dirname(__file__), "..", "scripts", "run_service.py")
    env = dict(os.environ, AALTOFLOW_LOCK_DIR=str(lockdir), PYTHONUNBUFFERED="1")
    try:
        r = subprocess.run([sys.executable, script, "--real", "--com", "COM97",
                            "--cmd-port", "25991", "--pub-port", "25992"],
                           capture_output=True, text=True, timeout=60, env=env)
    finally:
        held.release()
    assert r.returncode == 4, r.stderr
    assert "Traceback" not in r.stderr
    assert "COM97 is already in use by dssg" in r.stderr
