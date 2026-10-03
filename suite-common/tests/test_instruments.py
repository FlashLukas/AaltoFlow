"""suite_common/instruments.py with a fake pyvisa and a fake pyserial: what
is listed, what is asked *IDN?, what is NEVER opened (a serial port nobody
asked for, an address a running service holds), and which module fits."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from suite_common import instruments as I
from suite_common.modules import ModuleSpec


class FakeInst:
    def __init__(self, rm, address):
        self.rm, self.address, self.timeout = rm, address, None

    def query(self, text):
        self.rm.queried.append((self.address, text))
        answer = self.rm.answers.get(self.address)
        if answer is None:
            raise RuntimeError("VI_ERROR_TMO (-1073807339): Timeout expired")
        return answer + "\n"

    def close(self):
        pass


class FakeRM:
    def __init__(self, resources, answers):
        self.resources, self.answers = resources, answers
        self.opened, self.queried = [], []

    def list_resources(self, query="?*"):
        return tuple(self.resources)

    def open_resource(self, address, open_timeout=None):
        self.opened.append(address)
        return FakeInst(self, address)


def _port(device, description="", manufacturer=None, vid=None, pid=None, serial_number=None):
    return SimpleNamespace(device=device, description=description, manufacturer=manufacturer,
                           vid=vid, pid=pid, serial_number=serial_number)


@pytest.fixture
def lab():
    rm = FakeRM(["GPIB0::28::INSTR", "GPIB0::6::INSTR", "GPIB0::9::INSTR",
                 "USB0::0x1313::0x8078::P0012345::INSTR", "ASRL5::INSTR",
                 "TCPIP0::10.0.0.7::inst0::INSTR", "PRLGX-ASRL::COM5::INTFC"],
                {"GPIB0::28::INSTR": "Rohde&Schwarz,SMB100A,1406.6000k03/123,3.1",
                 "USB0::0x1313::0x8078::P0012345::INSTR": "Thorlabs,PM100D,P0012345,2.8",
                 "TCPIP0::10.0.0.7::inst0::INSTR": "Keysight,N5222A,MY123,A.1"})
    ports = [_port("COM5", "USB Serial Port (COM5)", "FTDI", 0x0403, 0x6001, "A1B2C3"),
             _port("COM7", "Silicon Labs CP210x (COM7)", "Silicon Labs", 0x10C4, 0xEA60)]
    held = {"GPIB0::6": "kepco"}          # the kepco service holds GPIB0::6
    return rm, ports, held


def test_visa_instruments_are_asked_who_they_are(lab):
    rm, _, held = lab
    rows = {f.address: f for f in I.scan_visa(rm, held=held)}
    assert rows["GPIB0::28::INSTR"].identity.startswith("Rohde&Schwarz,SMB100A")
    assert rows["USB0::0x1313::0x8078::P0012345::INSTR"].bus == "usb"
    assert rows["GPIB0::9::INSTR"].error == "no answer to *IDN? (timeout)"
    assert "PRLGX-ASRL::COM5::INTFC" not in rows           # an interface, not an instrument


def test_never_opened_a_held_address_or_a_serial_port(lab):
    rm, _, held = lab
    rows = {f.address: f for f in I.scan_visa(rm, held=held)}
    assert rows["GPIB0::6::INSTR"].held_by == "kepco"
    assert "GPIB0::6::INSTR" not in rm.opened
    assert "ASRL5::INSTR" not in rm.opened and not rows["ASRL5::INSTR"].asked
    assert all("ASRL" not in a for a in rm.opened)


def test_ask_can_be_switched_off(lab):
    rm, _, held = lab
    I.scan_visa(rm, ask=False, held=held)
    assert rm.opened == []


def test_serial_ports_are_described_not_contacted(lab):
    _, ports, held = lab
    rows = I.scan_serial(ports, held={"COM7": "superk"})
    com5 = rows[0]
    assert com5.identity == "USB Serial Port (COM5)" and not com5.asked
    assert "FTDI" in com5.detail and "0403:6001" in com5.detail and "A1B2C3" in com5.detail
    assert rows[1].held_by == "superk"


def test_scan_merges_visas_name_for_a_com_port(lab, monkeypatch):
    rm, ports, held = lab
    monkeypatch.setattr(I, "held_by_address", lambda: held)
    rows, notes = I.scan(rm=rm, ports=ports)
    assert notes == []
    com5 = [f for f in rows if f.key == "COM5"]
    assert len(com5) == 1 and com5[0].aliases == ["ASRL5::INSTR"]
    assert [f.bus for f in rows] == sorted((f.bus for f in rows),
                                           key=["gpib", "usb", "tcpip", "serial"].index)


def test_a_missing_package_is_a_note_not_a_crash(monkeypatch):
    def no_visa():
        raise I.MissingPackage("pyvisa is not installed")
    monkeypatch.setattr(I, "resource_manager", no_visa)
    monkeypatch.setattr(I, "_comports", lambda: [])
    monkeypatch.setattr(I, "held_by_address", lambda: {})
    rows, notes = I.scan()
    assert rows == [] and notes == ["pyvisa is not installed"]


def test_ask_serial_refuses_a_held_port_before_opening_it():
    f = I.ask_serial("COM5", held={"COM5": "windfreak"})
    assert f.error == "held by windfreak: not opened" and not f.asked


def test_a_typed_ip_is_tried_as_vxi11_then_as_a_socket():
    rm = FakeRM([], {"TCPIP0::10.0.0.9::5025::SOCKET": "Copper Mountain,C1209,1,1"})
    f = I.ask_address("10.0.0.9", rm=rm, held={})
    assert f.address == "TCPIP0::10.0.0.9::5025::SOCKET" and f.identity.startswith("Copper")
    assert rm.opened == ["TCPIP0::10.0.0.9::inst0::INSTR", "TCPIP0::10.0.0.9::5025::SOCKET"]
    assert I.ask_address("COM3", rm=rm, held={}).error.startswith("a serial port")
    held = I.ask_address("10.0.0.9", rm=rm, held={"TCPIP::10.0.0.9": "vna"})
    assert held.held_by == "vna" and len(rm.opened) == 2      # not opened again


def _spec(key, bus, arg="--x", remote=False):
    return ModuleSpec(id=key, key=key, name=key, address_arg=arg, address_bus=bus,
                      remote=remote)


def test_each_address_is_offered_to_the_modules_it_fits():
    mods = [_spec("smb", "visa"), _spec("windfreak", "serial"), _spec("sr7230", "ip"),
            _spec("kim", "", arg=""), _spec("smb_remote", "visa", remote=True)]
    gpib = I.Found("GPIB0::28::INSTR", "gpib")
    com = I.Found("COM5", "serial", aliases=["ASRL5::INSTR"])
    lan = I.Found("TCPIP0::10.0.0.7::inst0::INSTR", "tcpip")
    assert [(m.key, a) for m, a in I.modules_for(gpib, mods)] == [("smb", "GPIB0::28::INSTR")]
    assert dict((m.key, a) for m, a in I.modules_for(com, mods)) == \
        {"smb": "ASRL5::INSTR", "windfreak": I.address_for(com, "serial")}
    assert dict((m.key, a) for m, a in I.modules_for(lan, mods)) == \
        {"smb": "TCPIP0::10.0.0.7::inst0::INSTR", "sr7230": "10.0.0.7"}


def test_a_linux_port_keeps_its_device_path():
    f = I.Found("/dev/ttyUSB0", "serial", aliases=["ASRL/dev/ttyUSB0::INSTR"])
    assert I.address_for(f, "serial") == "/dev/ttyUSB0"
    assert I.address_for(f, "visa") == "ASRL/dev/ttyUSB0::INSTR"
    assert I.Found("ASRL/dev/ttyUSB0::INSTR", "serial").key == f.key


def test_printed_text_is_ascii():
    from pathlib import Path
    assert Path(I.__file__).read_text(encoding="utf-8").isascii()
