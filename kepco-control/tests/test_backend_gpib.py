"""The real backend's command strings, against a FAKE instrument (offline).

Nothing here proves the BOP understands these commands -- that is what the
# VERIFY markers are for. What it does prove: the strings are the ones the
BIT 4886 manual gives (sec. 4.1.1, appendix B), the numbers go out in the
manual's <exp_value> form, and the replies it documents parse.
"""

import pytest

from kepco.backends.bop_gpib import VisaBOP


class FakeInst:
    """Records writes; answers queries from a table (the manual's reply forms)."""

    def __init__(self, replies=None):
        self.writes = []
        self.replies = replies or {}

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        self.writes.append(cmd)
        return self.replies[cmd]

    def close(self):
        pass


def _bop(replies=None):
    b = VisaBOP("GPIB0::6::INSTR")
    b._inst = FakeInst(replies)          # skip open(): no pyvisa needed
    return b


def test_mode_change_pins_the_range_of_that_mode():
    b = _bop()
    b.set_mode("current")
    assert b._inst.writes == ["FUNC:MODE CURR", "CURR:RANG 1"]     # B.22, B.52
    b._inst.writes.clear()
    b.set_mode("voltage")
    assert b._inst.writes == ["FUNC:MODE VOLT", "VOLT:RANG 1"]     # B.22, B.61
    with pytest.raises(ValueError):
        b.set_mode("resistance")


def test_auto_range_left_alone_when_full_range_is_off():
    b = VisaBOP("GPIB0::6::INSTR", full_range=False)
    b._inst = FakeInst()
    b.set_mode("current")
    assert b._inst.writes == ["FUNC:MODE CURR"]


def test_numbers_go_out_with_decimal_point_and_exponent():
    b = _bop()
    b.program_current(-2.5)
    b.program_voltage(12.0)
    b.set_output(True)
    b.set_output(False)
    assert b._inst.writes == ["CURR -2.50000E+00", "VOLT 1.20000E+01",
                              "OUTP ON", "OUTP OFF"]
    # and the instrument reads them back as the value that was meant
    assert float(b._inst.writes[0].split()[1]) == -2.5


def test_replies_in_the_manuals_forms_parse():
    b = _bop({"MEAS:VOLT?": "+1.23450E+00\n", "MEAS:CURR?": "-2.00000E-01",
              "FUNC:MODE?": "1", "OUTP?": "0",
              "SYST:ERR?": '0,"No error"'})
    assert b.measure_voltage() == pytest.approx(1.2345)
    assert b.measure_current() == pytest.approx(-0.2)
    assert b.read_mode() == "current"          # B.23: 1 = current mode
    assert b.read_output() is False            # B.21: 0/1
    assert b.check_errors() == []              # B.80: 0,"No error" ends the queue


def test_close_switches_the_output_off():
    b = _bop()
    inst = b._inst
    b.close()
    assert inst.writes == ["OUTP OFF"]
    assert b._inst is None


def test_backend_module_does_not_import_pyvisa_at_import_time():
    import kepco.backends.bop_gpib  # noqa: F401  (already imported above)
    src = open(kepco.backends.bop_gpib.__file__, encoding="utf-8").read()
    top = src.split("class VisaBOP", 1)[0]
    assert "import pyvisa" not in top      # lazy: only inside open()
