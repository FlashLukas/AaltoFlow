"""The real backend against a FAKE VISA instrument that answers like the 455
manual says it should. This checks the command strings we send and the
parsing / unit conversion of the replies -- not the instrument itself."""

import pytest

from ls455.backends.base import FLAG_NO_PROBE, FLAG_OK, FLAG_OVERLOAD, GaussmeterBackend
from ls455.backends.ls455 import LakeShore455, LS455Error, range_index_for


class FakeInstrument:
    """`writes` logs every message (commands AND queries, in order, as the
    tests below always did); `commands` only the ones that CHANGE something.
    With `strict=True` any command fails the test -- used to prove that
    start-up only asks (Lukas's rule, 2026-09-27)."""

    def __init__(self, answers, strict=False):
        self.answers = dict(answers)
        self.writes = []
        self.commands = []
        self.strict = strict
        self.closed = False

    def write(self, cmd):
        if self.strict:
            raise AssertionError(f"start-up sent a state-changing command: {cmd!r}")
        self.commands.append(cmd)
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
           "OPST?": "4", "RANGE?": "3", "AUTO?": "1", "RDGMODE?": "1,2,1,2,3",
           "REL?": "0,1", "RELSP?": "+0.0000E+00", "RDGPEAK?": "+5.0000E+02,-3.0000E+02"}


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



# ---- start-up only ASKS (2026-09-27) ---------------------------------------------

# a meter somebody set up on the front panel: RMS narrow band, tesla on the
# display, manual range 2 of an HSE probe (3.5 mT), relative on at 0.04 T
FRONT_PANEL = dict(ANSWERS, **{"TYPE?": "40", "UNIT?": "2", "AUTO?": "0",
                               "RANGE?": "2", "RDGMODE?": "2,3,2,2,3",
                               "REL?": "1,1", "RELSP?": "+4.0000E-02"})


def test_open_sends_no_command():
    inst = FakeInstrument(FRONT_PANEL, strict=True)
    m = LakeShore455(command_gap_s=0.0, resource_manager=FakeRM(inst))
    m.open()                                          # raises if anything is written
    assert inst.commands == []
    m.close()


def test_brain_start_on_the_real_driver_writes_nothing_and_adopts_everything():
    from ls455.config import Config
    from ls455.gaussmeter import Gaussmeter
    inst = FakeInstrument(FRONT_PANEL, strict=True)
    cfg = Config()                                    # defaults DC/auto/G: must not be pushed
    meter = Gaussmeter(LakeShore455(command_gap_s=0.0, resource_manager=FakeRM(inst)), cfg)
    meter.start(poll=False)
    try:
        assert inst.commands == []
        s = meter.status()
        assert (s.mode, s.rms_band) == ("rms", "narrow")
        assert s.auto_range is False and s.range_mT == 3.5      # HSE range 2
        assert s.display_unit == "T"
        assert s.relative is True and s.rel_setpoint_mT == pytest.approx(40.0)  # 0.04 T
        assert s.probe == "HSE" and s.probe_serial == "H00000"
        assert s.ranges_mT == [0.35, 3.5, 35.0, 350.0, 3500.0]
        # a live reading in tesla is converted in SOFTWARE, the unit left alone
        inst.answers["RDGFIELD?"] = "+1.2000E-03"
        inst.answers["OPST?"] = "0"
        meter.poll_once()
        assert meter.status().field_mT == pytest.approx(1.2)
        assert inst.commands == []
    finally:
        meter.shutdown()


def test_peak_mode_is_read_with_rdgpeak(meter):
    m, inst = meter
    inst.answers["RDGMODE?"] = "3,2,1,1,2"            # peak, periodic, NEGATIVE display
    assert m.get_mode()[0] == "peak"
    assert m.get_peak() == ("periodic", "negative")
    inst.answers["OPST?"] = "0"
    assert m.read_field() == (pytest.approx(-30.0), FLAG_OK)     # -300 G
    inst.answers["RDGMODE?"] = "3,2,1,1,3"            # both: the larger magnitude
    m.get_peak()
    assert m.read_field()[0] == pytest.approx(50.0)


def test_probe_is_reread_on_request(meter):
    m, inst = meter
    assert m.probe_info()["family"] == "HST"
    inst.answers["TYPE?"] = "42"                      # a UHS probe plugged in
    info = m.probe_info()
    assert info["family"] == "UHS" and info["sensitivity_mV_per_kG"] == 1.0
    assert m.ranges_mT() == [0.0035, 0.035, 0.35, 3.5]


def test_relative_is_read_in_display_units(meter):
    m, inst = meter
    inst.answers["REL?"] = "1,1"
    inst.answers["RELSP?"] = "+4.2000E+02"            # 420 G
    assert m.get_relative() == (True, pytest.approx(42.0))


def test_unit_changed_by_hand_is_followed_by_the_software_conversion():
    """G -> T on the front panel while running: without the re-read every
    reading would be 10^4 off. The re-read only ASKS (strict instrument)."""
    from ls455.config import Config
    from ls455.gaussmeter import Gaussmeter
    inst = FakeInstrument(dict(ANSWERS, **{"OPST?": "0", "RDGMODE?": "1,2,1,1,1"}),
                          strict=True)
    meter = Gaussmeter(LakeShore455(command_gap_s=0.0, resource_manager=FakeRM(inst)),
                       Config())
    meter.start(poll=False)
    try:
        meter.poll_once()
        assert meter.status().field_mT == pytest.approx(42.0)       # 420 G
        inst.answers["UNIT?"] = "2"                                   # now tesla
        inst.answers["RDGFIELD?"] = "+4.2000E-02"
        meter._sync_now = True
        meter.poll_once()
        s = meter.status()
        assert s.display_unit == "T" and s.field_mT == pytest.approx(42.0)
        assert inst.commands == []
    finally:
        meter.shutdown()
