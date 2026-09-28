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


# ---- start-up reads only (Lukas, 2026-09-27) --------------------------------

class StrictInst(FakeInst):
    """Fails the test on ANY write except *CLS (which only clears the error
    queue): opening the connection must not change the instrument."""

    ALLOWED = {"*CLS"}

    def write(self, cmd):
        if cmd not in self.ALLOWED:
            raise AssertionError(f"state-changing write at start: {cmd!r}")
        super().write(cmd)


def test_open_and_read_state_issue_no_state_changing_writes(monkeypatch):
    import sys
    import types
    replies = {"*IDN?": "KEPCO,BIT 4886 20-10,E1234,2.0-1.0",
               "FUNC:MODE?": "1", "OUTP?": "1",
               "VOLT?": "8.00000E+00", "CURR?": "-1.20000E+00"}
    inst = StrictInst(replies)

    class RM:
        def open_resource(self, name):
            return inst

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "pyvisa",
                        types.SimpleNamespace(ResourceManager=RM))
    b = VisaBOP("GPIB0::6::INSTR")
    b.open()
    state = b.read_state()
    assert state == {"mode": "current", "output": True,
                     "voltage_V": 8.0, "current_A": -1.2}
    assert [w for w in inst.writes if not w.endswith("?")] == ["*CLS"]
    assert b.idn().startswith("KEPCO")


def test_brain_on_the_real_backend_writes_nothing_at_start(monkeypatch):
    """The whole start path -- VisaBOP.open(), read_state(), the brain's
    adoption and several worker steps (which measure) -- against a fake GPIB
    instrument that fails on any write but *CLS. A live 1.2 A output found in
    current mode must be adopted and left exactly as it is."""
    import sys
    import types
    from kepco.config import Config
    from kepco.supply import BipolarSupply
    replies = {"*IDN?": "KEPCO,BIT 4886 20-10,E1234,2.0-1.0",
               "FUNC:MODE?": "1", "OUTP?": "1",
               "VOLT?": "8.00000E+00", "CURR?": "1.20000E+00",
               "MEAS:VOLT?": "2.40000E+00", "MEAS:CURR?": "1.19900E+00"}
    inst = StrictInst(replies)

    class RM:
        def open_resource(self, name):
            return inst

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "pyvisa",
                        types.SimpleNamespace(ResourceManager=RM))
    cfg = Config()
    cfg.output.mode = "voltage"            # an .ini that disagrees with the unit
    cfg.output.voltage_V = 3.0
    supply = BipolarSupply(VisaBOP("GPIB0::6::INSTR"), cfg)
    supply.start(poll=False)
    for _ in range(5):
        supply.step(dt=0.05)
    assert [w for w in inst.writes if not w.endswith("?")] == ["*CLS"]
    s = supply.status()
    assert s.mode == "current" and s.output is True
    assert s.current_set_A == 1.2 and s.programmed == 1.2
    assert s.voltage_limit_V == 8.0
    assert s.current_A == pytest.approx(1.199)
