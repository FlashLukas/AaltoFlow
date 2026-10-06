"""The real AFG1062 backend against a fake SCPI instrument (fake_visa.py).

What must hold whatever the instrument does:
  * open() claims the address, then sends *CLS and QUERIES only;
  * read_channel() turns the replies into the brain's units (Vpp whatever the
    front panel's unit, degrees from radians, high-Z from 9.9E+37);
  * the setters send the commands the programmer manual names;
  * close() switches both outputs off and gives the address back;
  * the brain on top of it works end to end: adopt, set, read back, settle.
"""

import math
import time

import pytest

from afg.backends.tek_afg import TekAFG, afg1062_envelope
from afg.config import Config
from afg.generator import Generator

from fake_visa import fake_visa  # noqa: F401  (pytest fixture)

RES = "USB0::0x0699::0x0353::C000001::INSTR"


def test_open_only_reads(fake_visa):
    b = TekAFG(RES)
    b.open()
    inst = fake_visa[0]
    assert inst.writes == ["*CLS"]
    for n in (0, 1):
        b.read_channel(n)
    assert inst.writes == ["*CLS"], "reading must not write"
    assert "AFG1062" in b.idn()
    b.close()


def test_read_channel_units(fake_visa):
    b = TekAFG(RES)
    b.open()
    inst = fake_visa[0]
    c1 = b.read_channel(0)
    assert c1["output"] is True and c1["waveform"] == "sine"
    assert c1["frequency_Hz"] == pytest.approx(30.0)
    assert c1["load_ohm"] == 50.0 and c1["mode"] == "continuous"
    c2 = b.read_channel(1)
    assert c2["output"] is False and c2["waveform"] == "square"
    assert c2["phase_deg"] == pytest.approx(90.0)          # pi/2 rad
    assert c2["load_ohm"] is None                           # 9.9E+37 = high-Z
    # front panel left in Vrms: still reported in Vpp
    inst.ch[1]["UNIT"], inst.ch[1]["AMPL"] = "VRMS", 1.0
    assert b.read_channel(0)["amplitude_Vpp"] == pytest.approx(2 * math.sqrt(2))
    # a burst left on is reported, an unknown shape is "arb"
    inst.ch[1]["BURS"], inst.ch[1]["SHAP"] = "1", "USER1"
    c1 = b.read_channel(0)
    assert c1["mode"] == "burst" and c1["waveform"] == "arb"
    assert c1["unread"] == []
    b.close()


def test_phase_unit_degrees(fake_visa):
    b = TekAFG(RES, phase_unit="deg")
    b.open()
    fake_visa[0].ch[2]["PHAS"] = 45.0
    assert b.read_channel(1)["phase_deg"] == pytest.approx(45.0)
    b.set_phase(1, 30.0)
    assert fake_visa[0].writes[-1] == "SOUR2:PHAS:ADJ 30"
    b.close()


def test_setters_send_the_manual_commands(fake_visa):
    b = TekAFG(RES)
    b.open()
    b.set_waveform(0, "ramp")
    b.set_frequency(0, 1234.5)
    b.set_amplitude(0, 0.5)
    b.set_offset(0, -0.1)
    b.set_phase(1, 90.0)
    b.set_duty(1, 25.0)
    b.set_symmetry(0, 80.0)
    b.set_load(1, None)
    b.set_load(0, 50.0)
    b.set_output(1, True)
    b.align_phase()
    w = fake_visa[0].writes[1:]
    assert w[:4] == ["SOUR1:FUNC:SHAP RAMP", "SOUR1:FREQ:FIX 1234.5",
                     "SOUR1:VOLT:LEV:IMM:AMPL 0.5VPP", "SOUR1:VOLT:LEV:IMM:OFFS -0.1"]
    assert w[4].startswith("SOUR2:PHAS:ADJ 1.5707963")
    assert w[5:] == ["SOUR2:PULS:DCYC 25", "SOUR1:FUNC:RAMP:SYMM 80", "OUTP2:IMP INF",
                     "OUTP1:IMP 50", "OUTP2:STAT ON", "SOUR1:PHAS:INIT"]
    with pytest.raises(ValueError):
        b.set_waveform(0, "arb")
    b.close()


def test_close_switches_off_and_hands_back_the_panel(fake_visa):
    b = TekAFG(RES)
    b.open()
    inst = fake_visa[0]
    b.close()
    assert inst.writes[-2:] == ["OUTP1:STAT OFF", "OUTP2:STAT OFF"]
    assert inst.ren_calls == [6] and inst.closed


def test_errors_are_drained(fake_visa):
    b = TekAFG(RES)
    b.open()
    fake_visa[0].errors = ['-222,"Data out of range"', '-221,"Settings conflict"']
    assert len(b.drain_errors()) == 2
    assert b.drain_errors() == []
    b.close()


def test_envelope_follows_the_load():
    e50 = afg1062_envelope("sine", 50.0)
    ez = afg1062_envelope("sine", None)
    assert e50["amp_max_Vpp"] == 10.0 and e50["peak_max_V"] == 5.0
    assert ez["amp_max_Vpp"] == 20.0 and ez["peak_max_V"] == 10.0
    assert afg1062_envelope("ramp", 50.0)["freq_max_Hz"] == 1e6
    assert afg1062_envelope("dc", 50.0)["freq_max_Hz"] is None


def test_the_brain_on_the_real_backend(fake_visa):
    """Generator + TekAFG + fake instrument: the whole read-back path."""
    gen = Generator(TekAFG(RES), Config())
    gen.start()
    inst = fake_visa[0]
    try:
        assert inst.writes == ["*CLS"], "start must only read"
        s = gen.status()
        assert s["ch2_load"] == "high-Z" and s["ch2_phase_deg"] == pytest.approx(90.0)
        gen.set_frequency("ch1", 1000.0)
        gen.set_amplitude("ch1", 1.0)
        t_end = time.monotonic() + 3
        while time.monotonic() < t_end:
            s = gen.status()
            if s["ch1_frequency_Hz"] == 1000.0 and s["ch1_amplitude_Vpp"] == 1.0 \
                    and s["ch1_settled"]:
                break
            time.sleep(0.02)
        assert s["ch1_settled"] and s["ch1_mismatch"] == ""
        assert "SOUR1:FREQ:FIX 1000" in inst.writes
    finally:
        gen.shutdown()
    assert inst.ch[1]["OUTP"] == "0" and inst.ch[2]["OUTP"] == "0"
