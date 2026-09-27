"""The REAL backend, driven against a fake link (no hardware, no pyserial):
the command strings it sends and the tolerance of its reply parsers."""

import pytest

from dssg.backends.dsi_scpi import (DsiSG12000L, parse_bool, parse_number,
                                    parse_reference)


class FakeLink:
    """Records what is written; answers queries from a dict."""
    def __init__(self, answers):
        self.answers = dict(answers)
        self.sent = []

    def write(self, line):
        self.sent.append(line)

    def query(self, line):
        self.sent.append(line)
        a = self.answers.get(line)
        if a is None:
            raise TimeoutError(line)
        return a() if callable(a) else a

    def close(self):
        self.sent.append("<close>")


def _backend(answers):
    b = DsiSG12000L()
    b._link = FakeLink(answers)
    return b


@pytest.mark.parametrize("text,scale,value", [
    ("1000000000", 1.0, 1e9), ("1000.000MHZ", 1.0, 1e9), ("2.45GHz", 1.0, 2.45e9),
    ("-7.5", 1.0, -7.5), ("-7.5 dBm", 1.0, -7.5), ("5.07V", 1.0, 5.07),
    ("+1.2E+01", 1.0, 12.0), ("400", 1e6, 400e6),
])
def test_parse_number(text, scale, value):
    assert parse_number(text, scale) == pytest.approx(value)


def test_parse_number_rejects_garbage():
    with pytest.raises(ValueError):
        parse_number("hello")


def test_parse_bool_and_reference():
    assert parse_bool("ON") and parse_bool("1") and parse_bool("YES")
    assert not parse_bool("OFF") and not parse_bool("0") and not parse_bool("")
    assert parse_reference("A") == "auto"
    assert parse_reference("EXTERNAL") == "external"
    assert parse_reference("0") == "external"
    assert parse_reference("1") == "internal"


def test_command_spelling():
    b = _backend({})
    b._has_phase = True
    b.set_frequency(2.45e9)
    b.set_power(-12.5)
    b.set_output(True)
    b.set_phase(90)
    b.set_reference("auto")
    assert b._link.sent == ["FREQ:CW 2450.000000MHZ", "POWER -12.50", "OUTP:STAT ON",
                            "PHASE 90.00", "*INTERNALREF A", "*REFUPDATE"]


def test_readback_queries():
    b = _backend({"FREQ:CW?": "2450000000", "POWER?": "-12.5", "OUTP:STAT?": "1",
                  "FREQ:MIN?": "25000000", "FREQ:MAX?": "12000000000",
                  "POWER:MIN?": "-21.5", "POWER:MAX?": "10",
                  "*SYSVOLTS?": "5.07", "*EXTREF?": "0", "*REFMODE?": "A",
                  "SYST:ERR?": '0,"No error"'})
    assert b.read_frequency() == 2.45e9
    assert b.read_power() == -12.5
    assert b.read_output() is True
    assert b.freq_range() == (25e6, 12e9)
    assert b.power_range() == (-21.5, 10.0)
    assert b.usb_volts() == 5.07
    assert b.external_ref_detected() is False
    assert b.read_reference() == "auto"
    assert b.errors() == []


def test_error_queue_drains_and_stops():
    q = iter(['-113,"Undefined header"', '0,"No error"'])
    b = _backend({"SYST:ERR?": lambda: next(q)})
    assert b.errors() == ['-113,"Undefined header"']


def test_phase_probe():
    ok = _backend({"PHASE?": "0.00", "SYST:ERR?": "0"})
    assert ok._probe_phase() is True
    missing = _backend({"SYST:ERR?": "0"})           # PHASE? times out
    assert missing._probe_phase() is False
    off = _backend({"PHASE?": "0"})
    off._phase_mode = "off"
    assert off._probe_phase() is False


def test_no_phase_refuses_set():
    b = _backend({})
    with pytest.raises(RuntimeError):
        b.set_phase(10)


def test_close_turns_rf_off_first():
    b = _backend({})
    link = b._link
    b.close()
    assert link.sent[0] == "OUTP:STAT OFF" and link.sent[-1] == "<close>"
    b.close()                                         # idempotent


def test_bad_transport_refused():
    with pytest.raises(ValueError):
        DsiSG12000L(transport="gpib").open()


def test_tcp_without_host_refused():
    with pytest.raises(ValueError):
        DsiSG12000L(transport="tcp", host="").open()


def test_error_drain_stops_on_a_repeating_reply():
    """An unexpected "no error" wording must not turn into ten warnings per
    drain: a reply that repeats means the queue is not being emptied."""
    b = _backend({"SYST:ERR?": "ERR: none pending"})
    errs = b.errors()
    assert errs == ["ERR: none pending"]
    assert b._link.sent.count("SYST:ERR?") == 2


def test_error_drain_collects_distinct_errors_until_clean():
    replies = iter(["-113,Undefined header", "-222,Data out of range", "0,No error"])
    b = _backend({"SYST:ERR?": lambda: next(replies)})
    assert b.errors() == ["-113,Undefined header", "-222,Data out of range"]
