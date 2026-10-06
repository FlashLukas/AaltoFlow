"""The real backend against a fake that behaves like the LAB'S unit.

Measured 2026-10-06 on the lab's AFG1062 (firmware FV:V1.0.2) with read-only
queries and SYST:ERR? after each (fake_visa.py, profile "v1.0.2"):
unknown queries answer EMPTY and queue -102; PULS:DCYC? answers and queues
-102; OUTP:IMP? ends in a GBK Ohm sign; the first *IDN? after *CLS is empty.

Before this was handled, the running service logged -102 three times a
second from its poll, showed "50 ohm" on a high-Z unit, and an empty idn.
What must hold now:
  * the optional queries are probed ONCE in open(); a poll causes NO error;
  * the load reads high-Z (9.9E+37 + Ohm sign), the amplitude 6 Vpp as set;
  * *IDN? is asked again when the first answer is empty;
  * duty / symmetry / the voltage unit, which cannot be read, are reported
    as "not read back" and keep the value last set from here.
"""

import time

import pytest

from afg.backends.tek_afg import NO, NOISY, OK, TekAFG, _decode, _number
from afg.config import Config
from afg.generator import Generator

from fake_visa import fake_visa_v102  # noqa: F401  (pytest fixture)

RES = "USB0::0x0699::0x0353::C000001::INSTR"

#: the queries that are ALLOWED to cause errors: the probe's, once each
_PROBE_ERRORS = {"SOUR1:VOLT:UNIT?", "SOUR2:VOLT:UNIT?",
                 "SOUR1:FUNC:RAMP:SYMM?", "SOUR2:FUNC:RAMP:SYMM?",
                 "SOUR1:PULS:DCYC?", "SOUR2:PULS:DCYC?"}


def _bench(inst):
    """The bench as found on 2026-10-06: CH1 6 Vpp sine at high-Z."""
    inst.ch[1].update(AMPL=6.0, IMP=9.9e37)
    inst.ch[2].update(IMP=9.9e37)


def test_decode_never_fails():
    assert _decode(b"9.9E+37\xa6\xb8\n").startswith("9.9E+37")
    assert _number(_decode(b"9.9E+37\xa6\xb8\n")) == pytest.approx(9.9e37)
    assert _decode(b"\xff\xfe junk") != ""             # latin-1 takes any byte
    assert _number(" 2.7\n") == 2.7
    assert _number("INF") == float("inf")
    with pytest.raises(Exception):
        _number("")


def test_open_probes_once_and_sets_nothing(fake_visa_v102):
    b = TekAFG(RES)
    b.open()
    inst = fake_visa_v102[0]
    try:
        assert inst.writes == ["*CLS"], "open must only read"
        assert "AFG1062" in b.idn(), "empty first *IDN? must be asked again"
        assert inst.queries.count("*IDN?") == 2
        assert b.support["volt_unit"] == NO and b.support["symmetry"] == NO
        assert b.support["duty"] == NOISY
        for key in ("burst", "freq_mode", "am", "fm", "pm", "fsk", "pwm"):
            assert b.support[key] == OK
        assert {q for q, _ in inst.error_log} == _PROBE_ERRORS
        assert inst.errors == [], "the probe drains the errors it causes"
        assert any("VOLT:UNIT" in line for line in b.probe_report())
    finally:
        b.close()


def test_reading_causes_no_errors_and_reads_the_truth(fake_visa_v102):
    b = TekAFG(RES)
    b.open()
    inst = fake_visa_v102[0]
    _bench(inst)
    try:
        n_err = len(inst.error_log)
        first = b.read_channel(0)
        # the noisy duty: its probe answer is used ONCE ...
        assert first["duty_pct"] == pytest.approx(50.0)
        for _ in range(5):
            c1 = b.read_channel(0)
            c2 = b.read_channel(1)
            assert b.drain_errors() == []
        assert len(inst.error_log) == n_err, \
            f"a poll queued errors: {inst.error_log[n_err:]}"
        assert c1["load_ohm"] is None and c2["load_ohm"] is None     # high-Z
        assert c1["amplitude_Vpp"] == pytest.approx(6.0)             # not halved
        assert c1["unread"] == [] and c2["unread"] == []
        # ... and never asked again
        assert "duty_pct" not in c1 and "symmetry_pct" not in c1
        assert c1["not_read_back"] == ["duty_pct", "symmetry_pct"]
        assert inst.queries.count("SOUR1:PULS:DCYC?") == 1
        assert inst.queries.count("SOUR1:FUNC:RAMP:SYMM?") == 1
        assert inst.queries.count("SOUR1:VOLT:UNIT?") == 1
    finally:
        b.close()


def test_the_brain_polls_without_errors(fake_visa_v102):
    """Generator + TekAFG + the measured fake: start, many polls, a change.
    The instrument's error queue gets nothing from any of it."""
    events = []
    cfg = Config()
    cfg.hardware.poll_hz = 50.0                 # many polls in a short test
    backend = TekAFG(RES)
    gen = Generator(backend, cfg)
    gen._on_event = lambda level, msg: events.append((level, msg))

    # the bench state must be in place before start() reads it: patch the
    # instrument the fake opens
    import fake_visa as fv
    orig = fv.FakeAFGInstrument.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        _bench(self)
    fv.FakeAFGInstrument.__init__ = init
    try:
        gen.start()
    finally:
        fv.FakeAFGInstrument.__init__ = orig
    inst = fake_visa_v102[0]
    try:
        # wait for a good number of polls
        t_end = time.monotonic() + 5
        while inst.queries.count("OUTP1:STAT?") < 20 and time.monotonic() < t_end:
            time.sleep(0.02)
        assert inst.queries.count("OUTP1:STAT?") >= 20
        assert {q for q, _ in inst.error_log} <= _PROBE_ERRORS
        assert len(inst.error_log) == len(_PROBE_ERRORS), "only the probe may cause errors"
        assert not [m for lv, m in events if m.startswith("instrument: -")], events
        assert inst.writes == ["*CLS"], "start + polling must only read"

        s = gen.status()
        assert s["ch1_load"] == "high-Z" and s["ch1_load_ohm"] is None
        assert s["ch1_amplitude_Vpp"] == pytest.approx(6.0)
        assert s["ch1_not_read_back"] == "duty_pct, symmetry_pct"
        assert s["idn"].startswith("TEKTRONIX,AFG1062")
        assert s["ch1_settled"] and s["ch1_mismatch"] == ""

        # a duty change: sent, not read back, and still SETTLES (the
        # unreadable knob is not compared) -- and the poll stays error-free
        gen.set_waveform("ch1", "pulse")
        gen.set_duty("ch1", 25.0)
        t_end = time.monotonic() + 3
        while time.monotonic() < t_end:
            s = gen.status()
            if s["ch1_waveform"] == "pulse" and s["ch1_duty_pct"] == 25.0 \
                    and s["ch1_settled"]:
                break
            time.sleep(0.02)
        assert s["ch1_settled"] and s["ch1_duty_pct"] == 25.0
        assert "SOUR1:PULS:DCYC 25" in inst.writes
        assert inst.ch[1]["DCYC"] == 25.0
        assert len(inst.error_log) == len(_PROBE_ERRORS)
        assert inst.queries.count("SOUR1:PULS:DCYC?") == 1
    finally:
        gen.shutdown()
    assert inst.ch[1]["OUTP"] == "0" and inst.ch[2]["OUTP"] == "0"


def test_a_silent_instrument_still_fails_to_open(fake_visa_v102, monkeypatch):
    """Two empty *IDN? answers = nothing we know: open() fails (and the
    retry for the measured quirk does not hide a dead instrument)."""
    from fake_visa import FakeAFGInstrument
    monkeypatch.setattr(FakeAFGInstrument, "read_raw", lambda self: b"")
    with pytest.raises(Exception):
        TekAFG(RES).open()
