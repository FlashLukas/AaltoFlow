"""The real backend against a FAKE VISA instrument that answers like the 455
manual says it should. This checks the command strings we send and the
parsing / unit conversion of the replies -- not the instrument itself."""

import pytest

from ls455.backends.base import FLAG_NO_PROBE, FLAG_OK, FLAG_OVERLOAD, GaussmeterBackend
from ls455.backends.ls455 import LakeShore455, LS455Error, range_index_for


class FakeInstrument:
    def __init__(self, answers):
        self.answers = dict(answers)
        self.writes = []
        self.closed = False

    def write(self, cmd):
        self.writes.append(cmd)
        if cmd.startswith("UNIT "):
            self.answers["UNIT?"] = cmd.split()[1]
        if cmd.startswith("RANGE "):
            self.answers["RANGE?"] = cmd.split()[1]

    def query(self, cmd):
        self.writes.append(cmd)
        return self.answers[cmd] + "\r\n"

    def close(self):
        self.closed = True


class FakeRM:
    def __init__(self, inst):
        self.inst = inst
        self.opened = None

    def open_resource(self, name):
        self.opened = name
        return self.inst


ANSWERS = {"*IDN?": "LSCI,MODEL455,0000000,01012020", "UNIT?": "1", "TYPE?": "41",
           "PRBSNUM?": "H00000", "PRBSENS?": "+1.000E+00", "RDGFIELD?": "+4.2000E+02",
           "OPST?": "4", "RANGE?": "3", "AUTO?": "1", "RDGMODE?": "1,2,1,2,3"}


@pytest.fixture
def meter():
    inst = FakeInstrument(ANSWERS)
    m = LakeShore455("GPIB0::12::INSTR", command_gap_s=0.0, zero_time_s=0.0,
                     resource_manager=FakeRM(inst))
    m.open()
    yield m, inst
    m.close()


def test_protocol_and_terminators(meter):
    m, inst = meter
    assert isinstance(m, GaussmeterBackend)
    assert inst.read_termination == "\r\n" and inst.write_termination == "\r\n"
    assert m.idn().startswith("LSCI,MODEL455")


def test_serial_frame_is_7_odd_1():
    inst = FakeInstrument(ANSWERS)
    m = LakeShore455("ASRL3::INSTR", baud_rate=19200, command_gap_s=0.0,
                     resource_manager=FakeRM(inst))
    pytest.importorskip("pyvisa")
    m.open()
    from pyvisa import constants
    assert inst.baud_rate == 19200 and inst.data_bits == 7
    assert inst.parity == constants.Parity.odd and inst.stop_bits == constants.StopBits.one


def test_probe_family_sets_the_ranges(meter):
    m, _ = meter
    assert m.ranges_mT() == [3.5, 35.0, 350.0, 3500.0, 35000.0]   # TYPE? 41 = HST
    assert m.probe_info()["family"] == "HST"


def test_reading_is_converted_to_mT(meter):
    m, inst = meter
    assert m.read_field() == (pytest.approx(42.0), FLAG_OK)      # 420 G = 42 mT
    inst.answers["RDGFIELD?"] = "+4.2000E-02"
    m.set_display_unit("T")
    assert m.read_field()[0] == pytest.approx(42.0)             # 0.042 T = 42 mT


def test_status_bits_become_flags(meter):
    m, inst = meter
    inst.answers["OPST?"] = "2"
    assert m.read_field()[1] == FLAG_OVERLOAD
    inst.answers["OPST?"] = "1"
    assert m.read_field()[1] == FLAG_NO_PROBE
    inst.answers["OPST?"] = "0"
    inst.answers["RDGFIELD?"] = "OL"                  # an unparseable reply
    v, flag = m.read_field()
    assert v != v and flag == FLAG_OVERLOAD


def test_range_commands(meter):
    m, inst = meter
    assert range_index_for([3.5, 35.0, 350.0], 100.0) == 3       # snap UP
    assert range_index_for([3.5, 35.0, 350.0], 1e9) == 3
    m.set_range(100.0)
    assert inst.writes[-2:] == ["AUTO 0", "RANGE 3"]
    assert m.get_range() == 350.0


def test_mode_keeps_the_peak_settings(meter):
    m, inst = meter
    m.set_mode("rms", 5, "narrow")
    assert inst.writes[-1] == "RDGMODE 2,3,2,2,3"
    assert m.get_mode() == ("dc", 4, "wide")          # from the canned RDGMODE? reply


def test_relative_setpoint_in_display_units(meter):
    m, inst = meter
    m.set_relative(True, 42.0)                        # 42 mT = 420 G
    assert inst.writes[-2] == "RELSP 4.200000E+02"
    assert inst.writes[-1] == "REL 1,1"


def test_zero_commands(meter):
    m, inst = meter
    m.start_zero()
    assert inst.writes[-1] == "ZPROBE"
    assert m.zero_running() is False                  # zero_time_s = 0 here
    m.clear_zero()
    assert inst.writes[-1] == "ZCLEAR"


def test_calls_before_open_are_refused():
    m = LakeShore455()
    with pytest.raises(LS455Error, match="not open"):
        m.read_field()
    m.close()                                         # safe without open
