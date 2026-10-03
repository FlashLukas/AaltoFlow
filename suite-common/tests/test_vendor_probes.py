"""The list-only sources of "Instruments on this PC": the OS's USB device list
(usb_devices.py) and the module probes (instruments.run_probe / merge).

Everything here is offline: the PowerShell answer is a captured-style JSON
string, the Linux tree is a fake /sys folder, a probe is a tiny script that
prints a JSON line. All serial numbers are made up.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from suite_common import instruments as I
from suite_common import modules as M
from suite_common import usb_devices as U
from suite_common.modules import ModuleSpec

# What `Get-CimInstance Win32_PnPEntity ... | ConvertTo-Json -Compress` prints,
# shaped after the lab PC's list (2026-10-03) with invented serials.
WINDOWS_JSON = json.dumps([
    {"Name": "APT USB Device", "PNPDeviceID": "USB\\VID_0403&PID_FAF0\\97000001",
     "PNPClass": "USB", "Manufacturer": "Thorlabs", "Status": "OK"},
    {"Name": "APT USB Device", "PNPDeviceID": "FTDIBUS\\VID_0403+PID_FAF0+97000001A\\0000",
     "PNPClass": "USB", "Manufacturer": "Thorlabs", "Status": "OK"},
    {"Name": "PM160", "PNPDeviceID": "USB\\VID_1313&PID_807B\\100000001",
     "PNPClass": "ThorlabsUSBDevice", "Manufacturer": "Thorlabs", "Status": "OK"},
    {"Name": "IDS Imaging Development Systems GmbH / U3-0000XCP-M / 4100000000",
     "PNPDeviceID": "USB\\VID_1409&PID_8000\\4100000000", "PNPClass": "IDSU3VCameras",
     "Manufacturer": "IDS", "Status": "OK"},
    {"Name": "USB-6001", "PNPDeviceID": "USB\\VID_3923&PID_76BF\\01ABCDEF",
     "PNPClass": "DAQDevice", "Manufacturer": "National Instruments", "Status": "OK"},
    {"Name": "USB Composite Device", "PNPDeviceID": "USB\\VID_0403&PID_6010\\SH000001",
     "PNPClass": "USB", "Manufacturer": "(Standard USB Host Controller)", "Status": "OK"},
    {"Name": "USB Serial Converter A", "PNPDeviceID": "USB\\VID_0403&PID_6010&MI_00\\6&1&0&0000",
     "PNPClass": "USB", "Manufacturer": "FTDI", "Status": "OK"},
    {"Name": "USB Serial Converter B", "PNPDeviceID": "USB\\VID_0403&PID_6010&MI_01\\6&1&0&0001",
     "PNPClass": "USB", "Manufacturer": "FTDI", "Status": "OK"},
    {"Name": "USB Serial Converter A", "PNPDeviceID": "FTDIBUS\\VID_0403+PID_6010+SH000001A\\0000",
     "PNPClass": "USB", "Manufacturer": "FTDI", "Status": "OK"},
    {"Name": "USB Serial Converter B", "PNPDeviceID": "FTDIBUS\\VID_0403+PID_6010+SH000001B\\0000",
     "PNPClass": "USB", "Manufacturer": "FTDI", "Status": "OK"},
    {"Name": "USB Serial Port (COM4)", "PNPDeviceID": "USB\\VID_0403&PID_6001\\A0000001",
     "PNPClass": "Ports", "Manufacturer": "FTDI", "Status": "OK"},
    {"Name": "USB Input Device", "PNPDeviceID": "USB\\VID_413C&PID_301A\\5&AC91B4A&0&3",
     "PNPClass": "HIDClass", "Manufacturer": "(Standard system devices)", "Status": "OK"},
    {"Name": "Generic USB Hub", "PNPDeviceID": "USB\\VID_0BDA&PID_5411\\6&2&0&4",
     "PNPClass": "USB", "Manufacturer": "(Generic USB Hub)", "Status": "OK"},
])


# ------------------------------------------------------------ the USB list ---

def test_pnp_ids_give_vid_pid_and_serial():
    assert U.parse_pnp_id("USB\\VID_1313&PID_807B\\100000001") == (0x1313, 0x807B, "100000001", False)
    # Windows' own instance id (has '&') is not a serial
    assert U.parse_pnp_id("USB\\VID_413C&PID_301A\\5&AC91B4A&0&3")[2] == ""
    # an interface of a composite device
    assert U.parse_pnp_id("USB\\VID_0403&PID_6010&MI_01\\6&1&0&0001")[3] is True
    assert U.parse_pnp_id("FTDIBUS\\VID_0403+PID_FAF0+97000001A\\0000")[:3] == \
        (0x0403, 0xFAF0, "97000001A")
    assert U.parse_pnp_id("HID\\VID_1234") is None


def test_windows_list_one_row_per_device():
    devs = U.parse_windows(WINDOWS_JSON)
    ids = sorted((d.usb_id, d.serial) for d in devs)
    assert ids == sorted([
        ("0403:FAF0", "97000001"),      # USB\ and FTDIBUS\ spellings merged
        ("1313:807B", "100000001"),
        ("1409:8000", "4100000000"),
        ("3923:76BF", "01ABCDEF"),
        ("0403:6010", "SH000001"),      # composite parent; interfaces + FTDIBUS A/B folded
        ("0403:6001", "A0000001"),
        ("413C:301A", ""),              # a keyboard: listed (the dialog hides it)
    ])                                  # the hub is gone


def test_windows_json_for_a_single_device_is_an_object():
    one = json.dumps({"Name": "PM160", "PNPDeviceID": "USB\\VID_1313&PID_807B\\100000001",
                      "PNPClass": "ThorlabsUSBDevice"})
    assert [d.usb_id for d in U.parse_windows(one)] == ["1313:807B"]
    assert U.parse_windows("") == []


def test_an_ftdibus_entry_alone_keeps_a_kinesis_serial():
    only = json.dumps([{"Name": "APT USB Device",
                        "PNPDeviceID": "FTDIBUS\\VID_0403+PID_FAF0+97000002A\\0000"}])
    assert [d.serial for d in U.parse_windows(only)] == ["97000002"]


def test_linux_sysfs_tree(tmp_path):
    def dev(name, **files):
        d = tmp_path / name
        d.mkdir()
        for k, v in files.items():
            (d / k).write_text(v + "\n")
    dev("1-2", idVendor="1313", idProduct="807b", serial="100000001", product="PM160",
        manufacturer="Thorlabs", bDeviceClass="00")
    if sys.platform != "win32":                                # ':' is not a Windows name
        dev("1-2:1.0", idVendor="1313", idProduct="807b")     # an interface
    dev("usb1", idVendor="1d6b", idProduct="0002")             # a root hub
    dev("1-3", idVendor="05e3", idProduct="0608", bDeviceClass="09")   # a hub
    devs = U.parse_sysfs(tmp_path)
    assert [(d.usb_id, d.serial, d.name, d.instance) for d in devs] == \
        [("1313:807B", "100000001", "PM160", "1-2")]


def test_known_ids_name_the_module_and_ambiguous_ftdi_names_none():
    assert U.known(0x0403, 0xFAF0)[2] == "kim"
    assert U.known(0x1313, 0x807B)[2] == "pm16"
    assert U.known(0x3923, 0x76BF)[2] == "usb6001"
    assert U.known(0x1409, 0x8000)[2] == "camera"
    assert U.known(0x0403, 0x6010)[2] == "signalhound"
    # a generic FTDI cable could be anything: no module is guessed
    assert U.known(0x0403, 0x6001)[2] == "" and "could be" in U.known(0x0403, 0x6001)[1]
    assert U.known(0x0403, 0x1234) == U.KNOWN_USB[0x0403]       # maker fallback
    assert U.known(0x413C, 0x301A) is None


def test_list_usb_never_raises(monkeypatch):
    monkeypatch.setattr(U, "_windows", lambda: (_ for _ in ()).throw(OSError("no powershell")))
    monkeypatch.setattr(U.sys, "platform", "win32")
    devs, notes = U.list_usb()
    assert devs == [] and "no powershell" in notes[0]


def test_usb_rows_mark_unknown_devices():
    rows = {f.usb_id: f for f in I.usb_rows(U.parse_windows(WINDOWS_JSON), held={})}
    kim = rows["0403:FAF0"]
    assert (kim.bus, kim.address, kim.suggest, kim.known) == ("usb-device", "97000001", "kim", True)
    assert rows["413C:301A"].known is False
    assert "not a known instrument" in rows["413C:301A"].identity
    assert rows["0403:6001"].suggest == ""


# ------------------------------------------------------------ probe output ---

def test_probe_output_becomes_rows():
    text = "a vendor banner\n" + json.dumps({"devices": [
        {"address": "97000001", "identity": "Thorlabs KIM101", "detail": "Kinesis"},
        {"address": "4100000000", "identity": "IDS U3", "lock": "CAMERA::4100000000"},
        {"address": "USB0::0x1313::0x807B::100000001::INSTR", "identity": "PM160"},
        {"identity": "no address"}], "note": "a note"})
    rows, notes = I.parse_probe(text, "kim")
    assert [(f.address, f.bus) for f in rows] == [
        ("97000001", "device"), ("4100000000", "device"),
        ("USB0::0x1313::0x807B::100000001::INSTR", "usb")]
    assert rows[1].key == "CAMERA::4100000000"              # held under its lock spelling
    assert all(f.found_by == ["kim"] and f.source == "kim probe" for f in rows)
    assert notes == ["kim probe: a device without an address was skipped", "kim: a note"]
    assert I.parse_probe("not json", "x")[1][0].startswith("x probe: not the expected JSON")
    assert I.parse_probe("", "x") == ([], ["x probe printed nothing"])


def _probe_module(tmp_path, body: str, key="kim") -> ModuleSpec:
    d = tmp_path / f"{key}-control"
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "probe.py").write_text(body, encoding="utf-8")
    return ModuleSpec(id=key, key=key, name=key, dir=d, probe="scripts/probe.py",
                      address_arg="--serial", address_bus="device")


def test_run_probe_runs_the_script_in_the_given_python(tmp_path):
    spec = _probe_module(tmp_path, "import json\nprint(json.dumps({'devices': "
                                   "[{'address': '97000001', 'identity': 'KIM101'}]}))\n")
    rows, notes = I.run_probe(spec, python=sys.executable)
    assert [f.address for f in rows] == ["97000001"] and notes == []


def test_a_failing_or_slow_probe_is_a_note(tmp_path):
    bad = _probe_module(tmp_path, "raise SystemExit('the SDK exploded')\n", key="bad")
    rows, notes = I.run_probe(bad, python=sys.executable)
    assert rows == [] and "exit 1" in notes[0] and "the SDK exploded" in notes[0]
    slow = _probe_module(tmp_path, "import time\ntime.sleep(5)\n", key="slow")
    rows, notes = I.run_probe(slow, python=sys.executable, timeout_s=0.5)
    assert rows == [] and "no answer within" in notes[0]
    none = _probe_module(tmp_path, "", key="nopy")
    assert "no environment yet" in I.run_probe(none, python=None)[1][0]


def test_probe_modules_skips_remote_and_marks_held(tmp_path):
    a = _probe_module(tmp_path, "", key="kim")
    b = _probe_module(tmp_path, "", key="other")
    b.remote = True

    def runner(spec, py):
        return I.parse_probe(json.dumps({"devices": [{"address": "97000001"}]}), spec.key)
    rows, notes = I.probe_modules([a, b], held={"97000001": "kim"}, runner=runner)
    assert len(rows) == 1 and rows[0].held_by == "kim"


# ------------------------------------------------------------------ merging ---

def test_vendor_scan_merges_probe_usb_and_explains_a_held_device():
    """The lab PC finding: pylablib's list was EMPTY while kim held the KIM101.
    The USB list still shows it, hwlock says kim holds it, and the row says
    why the vendor list is silent."""
    def runner(spec, py):
        return [], []                                      # Kinesis list empty: open
    spec = ModuleSpec(id="kim", key="kim", name="kim", dir=Path("."), probe="p.py",
                      address_arg="--serial", address_bus="device")
    import suite_common.instruments as mod
    old = mod.held_by_address
    mod.held_by_address = lambda: {"97000001": "kim"}
    try:
        rows, notes = I.scan_vendor([spec], runner=runner,
                                    usb_list=lambda: (U.parse_windows(WINDOWS_JSON), []))
    finally:
        mod.held_by_address = old
    kim = next(f for f in rows if f.usb_id == "0403:FAF0")
    assert kim.held_by == "kim" and "held by the running kim service" in kim.detail
    assert "does not show a device while a service has it open" in kim.detail


def test_a_probe_row_and_its_usb_row_are_one_row():
    probe, _ = I.parse_probe(json.dumps({"devices": [
        {"address": "97000001", "identity": "Thorlabs KIM101", "detail": "Kinesis list"}]}), "kim")
    usb = I.usb_rows(U.parse_windows(WINDOWS_JSON), held={})
    rows = I.merge(probe, usb)
    kims = [f for f in rows if "97000001" in (f.address, f.serial_no)]
    assert len(kims) == 1
    k = kims[0]
    assert k.source == "kim probe + USB list" and k.identity == "Thorlabs KIM101"
    assert k.usb_id == "0403:FAF0" and k.found_by == ["kim"]


def test_a_tlpmx_probe_row_merges_with_the_usb_row_by_serial():
    probe, _ = I.parse_probe(json.dumps({"devices": [
        {"address": "USB0::0x1313::0x807B::100000001::INSTR", "identity": "PM160"}]}), "pm16")
    rows = I.merge(probe, I.usb_rows(U.parse_windows(WINDOWS_JSON), held={}))
    pm = [f for f in rows if f.usb_id == "1313:807B" or "807B" in f.address]
    assert len(pm) == 1 and pm[0].address.startswith("USB0::")


def test_a_com_port_and_its_usb_row_are_one_row():
    com = I.Found("COM4", "serial", identity="USB Serial Port (COM4)",
                  detail="FTDI, USB 0403:6001, serial A0000001", source="COM port",
                  usb_id="0403:6001", serial_no="A0000001")
    rows = I.merge([com], I.usb_rows(U.parse_windows(WINDOWS_JSON), held={}))
    assert len([f for f in rows if f.usb_id == "0403:6001"]) == 1
    assert rows[0].address == "COM4" and "USB list" in rows[0].source


# ----------------------------------------------- "Use for module..." offers ---

def _spec(key, bus, arg="--x"):
    return ModuleSpec(id=key, key=key, name=key, address_arg=arg, address_bus=bus)


def test_a_device_bus_module_gets_only_its_own_devices():
    mods = [_spec("kim", "device", "--serial"), _spec("usb6001", "device", "--device"),
            _spec("smb", "visa", "--visa"), _spec("pm16", "visa", "--resource")]
    probe, _ = I.parse_probe(json.dumps({"devices": [{"address": "97000001"}]}), "kim")
    assert [(m.key, a) for m, a in I.modules_for(probe[0], mods)] == [("kim", "97000001")]
    usb = {f.usb_id: f for f in I.usb_rows(U.parse_windows(WINDOWS_JSON), held={})}
    # a USB-list row: the module KNOWN_USB names, by its serial
    assert [(m.key, a) for m, a in I.modules_for(usb["0403:FAF0"], mods)] == [("kim", "97000001")]
    # a VISA module named by KNOWN_USB gets a USB resource string from the ids
    assert [(m.key, a) for m, a in I.modules_for(usb["1313:807B"], mods)] == \
        [("pm16", "USB0::0x1313::0x807B::100000001::INSTR")]
    # an unknown device or an ambiguous FTDI cable is offered to nobody
    assert I.modules_for(usb["413C:301A"], mods) == []
    assert I.modules_for(usb["0403:6001"], mods) == []
    # a VISA row is NOT offered to a device-bus module unless its probe found it
    gpib = I.Found("GPIB0::28::INSTR", "gpib")
    assert {m.key for m, _ in I.modules_for(gpib, mods)} == {"smb", "pm16"}


def test_device_address_is_passed_after_real(tmp_path):
    d = tmp_path / "usb6001-control"
    (d / "scripts").mkdir(parents=True)
    for f in ("run_service.py", "probe.py"):
        (d / "scripts" / f).write_text("")
    (d / "module.toml").write_text(
        '[module]\nkey = "usb6001"\nname = "DAQ"\n[ports]\ncmd = 5625\n'
        '[hardware]\naddress_arg = "--device"\nbus = "device"\nprobe = "scripts/probe.py"\n')
    spec = M.parse_manifest(d / "module.toml")
    assert (spec.address_bus, spec.probe) == ("device", "scripts/probe.py")
    M.set_address("usb6001", "Dev2", tmp_path)
    M.set_real("usb6001", True, tmp_path)
    spec = M.discover(tmp_path).get("usb6001")
    assert M.service_args(spec)[-3:] == ["--real", "--device", "Dev2"]


def test_a_declared_probe_must_exist(tmp_path):
    d = tmp_path / "x-control"
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "run_service.py").write_text("")
    (d / "module.toml").write_text('[module]\nkey = "x"\nname = "X"\n[ports]\ncmd = 5625\n'
                                   '[hardware]\nprobe = "scripts/probe.py"\n')
    with pytest.raises(M.ManifestError, match="probe script"):
        M.parse_manifest(d / "module.toml")


def test_new_files_are_ascii():
    assert Path(U.__file__).read_text(encoding="utf-8").isascii()
