"""The real backend against a fake ALR3203 that speaks the manual's protocol
(Appendix A): the exact command lines, units (mV / mA), the LOCAL -> REM WR 1
dance, and that start-up only READS. Offline (fake pyserial, fake_elc.py)."""

import sys

import pytest

from alr3203.backends.elc_serial import ElcError, SerialALR3203
from alr3203.backends.sim import NoRelay
from alr3203.config import Config
from alr3203.supply import BipolarSupply

from conftest import FakeClock, run_for
from fake_elc import FakeALR, make_serial_module


@pytest.fixture
def dev(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    alr = FakeALR()
    opened = []
    monkeypatch.setitem(sys.modules, "serial", make_serial_module({"COM5": alr}, opened))
    return alr, opened


def test_open_reads_the_identity_with_the_documented_settings(dev):
    alr, opened = dev
    b = SerialALR3203("COM5")
    b.open()
    assert b.idn() == "ALR3203 VERSION 1.02"
    assert opened[0].settings == (9600, 7, "E", 1)      # 9600 7E1
    assert alr.log == ["0 IDN RD"]
    b.close(output_off=False)


def test_read_state_is_reads_only_and_in_the_right_units(dev):
    alr, _ = dev
    alr.output, alr.volt_mv, alr.curr_ma = True, 32000, 1500
    b = SerialALR3203("COM5")
    b.open()
    st = b.read_state()
    assert st == {"output": True, "voltage_V": 32.0, "current_A": 1.5,
                  "regulation": "current"}
    assert all(" WR " not in line for line in alr.log)
    b.close(output_off=False)


def test_writes_go_out_in_millivolts_and_milliamps(dev):
    alr, _ = dev
    b = SerialALR3203("COM5")
    b.open()
    b.program_voltage(1.25)
    b.program_current(0.4504)
    b.set_output(True)
    assert alr.log[-3:] == ["0 VOLT WR 1250", "0 CURR WR 450", "0 OUT WR 1"]
    assert b.measure_voltage() == pytest.approx(0.9)    # CC: 450 mA x 2 ohm
    assert b.measure_current() == pytest.approx(0.45)
    assert b.read_regulation() == "current"
    b.program_voltage(99.0)                              # clipped to the manual's range
    assert alr.log[-1] == "0 VOLT WR 32200"
    b.close()                                            # output_off: OUT WR 0
    assert alr.output is False


def test_a_local_supply_is_taken_to_remote_once_and_given_back(dev):
    alr, _ = dev
    alr.local = True
    b = SerialALR3203("COM5")
    b.open()
    b.program_current(0.1)
    assert alr.log[-3:] == ["0 CURR WR 100", "0 REM WR 1", "0 CURR WR 100"]
    b.program_current(0.2)
    assert alr.log[-1] == "0 CURR WR 200"               # no second REM
    b.close(output_off=False)
    assert alr.log[-1] == "0 REM WR 0"                   # front panel back


def test_an_error_answer_raises(dev):
    alr, _ = dev
    b = SerialALR3203("COM5")
    b.open()
    with pytest.raises(ElcError, match="ERR"):
        b._write("BOGUS WR 1")
    b.close(output_off=False)


def test_the_brain_on_the_fake_supply_starts_without_a_write_and_ramps(dev):
    """End to end through the real backend: adopt (reads only), then a ramp
    to 1 A in current mode programs CURR in steps and VOLT stays the limit."""
    alr, _ = dev
    clock = FakeClock()
    supply = BipolarSupply(SerialALR3203("COM5"), Config(), relay=NoRelay(), clock=clock)
    supply.start(poll=False)
    supply.step()
    assert all(" WR " not in line for line in alr.log)
    assert supply.status().mode == "current"             # VOLT at 32 V = the limit
    supply.set_current(1.0)
    supply.set_output(True)
    supply.step()
    run_for(supply, clock, 1.0)
    writes = [line for line in alr.log if line.startswith("0 CURR WR")]
    assert len(writes) >= 5 and writes[-1] == "0 CURR WR 1000"
    assert alr.output is True
    supply.shutdown()
    assert alr.output is False



def test_serial_number_err_is_an_empty_string(dev):
    """The rig's firmware (VERSION 1.2) answers SERIAL RD with "0 ERR"
    (2026-11-10): no serial, no exception."""
    alr, _ = dev
    b = SerialALR3203("COM5")
    b.open()
    try:
        assert b.serial_number() == "12345"          # a firmware that has it
        alr.answer = (lambda orig: (lambda line: "0 ERR" if "SERIAL" in line
                                    else orig(line)))(alr.answer)
        assert b.serial_number() == ""
    finally:
        b.close(output_off=False)


def test_one_usb_hiccup_at_close_is_ridden_out(dev):
    """Blue Box, 2026-11-10: OUT WR 0 at shutdown failed once with "WriteFile
    failed ... does not recognize the command"; the port worked again at once.
    One failure: tried again, no error."""
    alr, opened = dev
    b = SerialALR3203("COM5")
    b.open()
    ser = opened[-1]
    real_write, fails = ser.write, [1]

    def flaky(data):
        if b"OUT WR 0" in data and fails:
            fails.pop()
            raise OSError("WriteFile failed (PermissionError(13, 'The device does not "
                          "recognize the command.', None, 22))")
        return real_write(data)
    ser.write = flaky
    b.close()
    assert alr.log[-1] == "0 OUT WR 0" and ser.closed


def test_an_output_off_that_keeps_failing_is_reported_after_letting_go(dev):
    alr, opened = dev
    b = SerialALR3203("COM5")
    b.open()
    ser = opened[-1]
    real_write = ser.write
    ser.write = lambda data: (_ for _ in ()).throw(OSError("gone")) \
        if b"OUT WR 0" in data else real_write(data)
    with pytest.raises(ElcError, match="could not switch the output off"):
        b.close()
    assert ser.closed                          # the port is released anyway


def test_the_console_says_when_the_envelope_is_one_sided(tmp_path, monkeypatch):
    """The start event reaches clients only; a service started by hand shows
    its console. run_service prints the same warning."""
    import subprocess
    from pathlib import Path
    ini = tmp_path / "one_sided.ini"
    ini.write_text("[limits]\ncurrent_max_A = 1.0\n", encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_service.py"
    # a port that is taken makes the service stop right after the banner
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    taken = s.getsockname()[1]
    try:
        r = subprocess.run([sys.executable, str(script), "--config", str(ini),
                            "--cmd-port", str(taken), "--pub-port", str(taken + 1)],
                           capture_output=True, text=True, timeout=60)
    finally:
        s.close()
    assert "WARNING: the safety envelope is NOT the same on both signs" in r.stdout
    assert "current -6 .. +1 A" in r.stdout
