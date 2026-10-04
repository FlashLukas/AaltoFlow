"""The real backend against a fake instrument (tests/fake_gsp.py): the SCPI it
sends, that START-UP SENDS NOTHING BUT QUERIES (Lukas's rule, 2026-09-27),
the adoption of a non-default front panel, the unit conversion, parsing, and
the brain on top. Offline; pyvisa is never imported."""

import numpy as np
import pytest

from fake_gsp import FakeGsp
from gsp818.analyzer import SpectrumAnalyzer
from gsp818.backends.gsp import GspAnalyzer, _parse_trace, _to_dBm, _from_dBm
from gsp818.config import Config
from gsp818.model import resolve


def _backend(cfg=None, **fake):
    cfg = cfg or Config()
    inst = FakeGsp(**fake)
    return GspAnalyzer(cfg, resource=inst, sleep=lambda s: None), inst, cfg


class _FastClock:
    """A clock that jumps 1 s per read, so the brain's sweep wait ends at once."""
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


#: an analyser somebody left mid-experiment: 100-200 MHz, manual 10 kHz RBW,
#: reference level in dBuV, preamp on, peak detector, Max Hold, its own
#: averaging on -- and the tracking generator ON at -5 dBm
LEFT_BEHIND = {
    "FREQ:STAR": "100000000", "FREQ:STOP": "200000000", "SWE:POIN": "401",
    "BAND:AUTO": "0", "BAND": "10000", "DISP:WIN:TRAC:Y:RLEV": "87.00",
    "POW:ATT:AUTO": "0", "POW:ATT": "0", "POW:GAIN:AUTO": "1", "DET": "POS",
    "SOUR:POW:TRAC": "-5.0", "OUTP:TRAC": "ON", "UNIT:POW": "DBUV",
    "TRAC1:MODE": "MAXH", "AVER": "ON",
}


def test_open_and_read_state_are_queries_only():
    b, inst, _ = _backend(state=LEFT_BEHIND)
    inst.read_only = True                           # any write raises
    b.open()
    st = b.read_state()
    assert inst.writes == [] and "GSP-818" in b.idn() and b.simulated is False
    assert (st["start_Hz"], st["stop_Hz"], st["points"]) == (100e6, 200e6, 401)
    assert st["rbw_auto"] is False and st["rbw_Hz"] == 10e3
    assert st["ref_level_dBm"] == pytest.approx(87.0 - 106.99, abs=0.01)   # dBuV -> dBm
    assert st["atten_auto"] is False and st["atten_dB"] == 0.0
    assert st["preamp"] is True and st["detector"] == "pos_peak"
    assert st["tg_on"] is True and st["tg_level_dBm"] == -5.0
    notes = " ".join(st["notes"])
    assert "DBUV" in notes and "MAXH" in notes and "averaging is on" in notes


def test_start_adopts_the_instrument_and_writes_nothing():
    """The brain on the real backend: start + background sweeps write NOTHING,
    status is the instrument's state; only an acquisition fixes what stops a
    read being a fresh sweep, and the trace comes back in dBm."""
    cfg = Config()                                  # .ini: full span, TG off, dBm ...
    b, inst, _ = _backend(cfg, state=LEFT_BEHIND)
    sa = SpectrumAnalyzer(b, cfg, clock=_FastClock())
    events = []
    sa._on_event = lambda lvl, msg: events.append((lvl, msg))
    inst.read_only = True
    sa.start(run=False)
    for _ in range(3):
        sa.step()                                   # continuous sweeps, wait mode
    assert inst.writes == []
    s = sa.status()
    assert (s.start_Hz, s.stop_Hz, s.points) == (100e6, 200e6, 401)
    assert s.rbw_auto is False and s.rbw_Hz == 10e3
    assert s.ref_level_dBm == pytest.approx(-19.99, abs=0.01)
    assert s.preamp is True and s.detector == "pos_peak"
    assert s.tg_on is True and s.tg_level_dBm == -5.0
    assert s.sweeps >= 1
    last = sa.get_trace("last")["power_dBm"]
    assert last.min() == pytest.approx(-80.0, abs=0.01)   # dBuV on the wire, dBm here
    assert any(lvl == "warn" and "MAXH" in m for lvl, m in events)

    inst.read_only = False
    n = sa.acquire()
    while sa.status().acquiring:
        sa.step()
    assert set(inst.writes) == {":TRAC1:MODE WRIT", ":AVER OFF"}
    assert any("switched to Clear/Write" in m for _, m in events)
    t = sa.get_trace("sample")
    assert t["acq_id"] == n and t["power_dBm"].shape == (401,)
    assert t["power_dBm"].max() == pytest.approx(-74.0, abs=0.01)
    assert "UNIT:POW" not in " ".join(inst.writes)   # the unit is never changed
    sa.shutdown()
    assert inst.writes[-1] == ":OUTP:TRAC OFF"       # shutdown behaviour is kept


def test_a_query_that_is_not_answered_is_not_written_either():
    """No reply for the preamp: the .ini value is shown (with a warning) and
    NOT written -- until the user sets it."""
    cfg = Config()
    cfg.acquisition.continuous = False
    b, inst, _ = _backend(cfg, state={"POW:GAIN:AUTO": "1"},
                          unanswered=(":POW:GAIN:AUTO?",))
    sa = SpectrumAnalyzer(b, cfg, clock=_FastClock())
    events = []
    sa._on_event = lambda lvl, msg: events.append((lvl, msg))
    inst.read_only = True
    sa.start(run=False)
    sa.step()
    assert inst.writes == [] and sa.status().preamp is False
    assert any("did not report preamp" in m for _, m in events)
    inst.read_only = False
    sa.set_preamp(True)
    sa.step()
    assert inst.writes == [":POW:GAIN:AUTO ON"]
    sa.shutdown()


def test_configure_sends_only_user_changes():
    b, inst, cfg = _backend()
    b.open()
    cfg_state = b.read_state()
    assert cfg_state["rbw_auto"] is True
    rb = b.configure(resolve(cfg))                  # .ini == preset: nothing to send
    assert inst.writes == []
    assert rb["rbw_Hz"] == 3e6 and rb["sweep_time_s"] == pytest.approx(0.02)
    cfg.sweep.rbw_auto, cfg.sweep.rbw_Hz = False, 30e3
    cfg.sweep.sweep_time_auto, cfg.sweep.sweep_time_s = False, 0.0125
    cfg.sweep.detector = "pos_peak"
    cfg.tracking.tg_on, cfg.tracking.level_dBm = True, -12.0
    b.configure(resolve(cfg))
    new = inst.writes
    assert ":BAND:AUTO OFF" in new and ":BAND 30000" in new
    assert ":SWE:TIME 12.500 ms" in new and ":DET POS" in new
    assert ":FREQ:STAR 9000" not in new             # unchanged: not re-sent
    # the level is set BEFORE the output goes on
    assert new.index(":SOUR:POW:TRAC -12.0") < new.index(":OUTP:TRAC ON")


def test_the_reference_level_is_written_in_the_instruments_unit():
    b, inst, cfg = _backend(state={"UNIT:POW": "DBMV", "DISP:WIN:TRAC:Y:RLEV": "46.99"})
    b.open()
    st = b.read_state()
    assert st["ref_level_dBm"] == pytest.approx(0.0, abs=0.01)
    cfg.sweep.ref_level_dBm = st["ref_level_dBm"]
    b.mark_in_sync(resolve(cfg))
    b.configure(resolve(cfg))
    assert inst.writes == []                        # the round trip matches the text
    cfg.sweep.ref_level_dBm = -10.0
    b.configure(resolve(cfg))
    assert inst.writes == [":DISP:WIN:TRAC:Y:RLEV 36.99"]


def test_unit_conversions_round_trip():
    for unit in ("DBM", "DBMV", "DBUV", "V", "W"):
        for dbm in (-100.0, -20.0, 0.0, 13.0):
            assert _to_dBm(_from_dBm(dbm, unit), unit) == pytest.approx(dbm, abs=1e-9)
    assert _to_dBm(0.0, "W") == -np.inf
    assert _to_dBm(1.0, "V") == pytest.approx(13.01, abs=0.01)   # 1 V rms on 50 ohm


def test_close_switches_the_tg_off_and_closes():
    b, inst, cfg = _backend(state={"OUTP:TRAC": "ON"})
    b.open()
    b.read_state()
    b.close()
    assert inst.writes[-1] == ":OUTP:TRAC OFF" and inst.state["OUTP:TRAC"] == "OFF"
    b.close()                                       # twice is fine


def test_wait_mode_waits_whole_sweeps_and_reads_trace1():
    b, inst, cfg = _backend()
    b.open(); b.read_state()
    s = resolve(cfg)
    b.configure(s)
    wait = b.start_sweep(s)
    # the fake reports 20 ms (longer than the brain's 10 ms estimate): the
    # instrument's own number wins
    assert wait == pytest.approx(2 * (0.02 + cfg.hardware.sweep_margin_s))
    assert inst.writes == []
    y, _ = b.finish_sweep()
    assert y.shape == (601,) and ":TRAC? TRACE1" in inst.log


def test_single_mode_is_entered_only_for_an_acquisition():
    cfg = Config()
    cfg.hardware.sweep_mode = "single"
    b, inst, _ = _backend(cfg)
    b.open(); b.read_state()
    s = resolve(cfg)
    b.configure(s)
    b.start_sweep(s); b.finish_sweep()              # background sweep: nothing written
    assert inst.writes == []
    assert b.ensure_live()                          # an acquisition begins
    b.start_sweep(s); b.finish_sweep()
    b.start_sweep(s); b.finish_sweep()
    assert inst.writes.count(":INIT:CONT OFF") == 1 and inst.writes.count(":INIT:IMM") == 2
    b.close()
    assert ":INIT:CONT ON" in inst.writes           # the front panel sweeps again


def test_ensure_live_restarts_a_single_sweep_panel_in_wait_mode():
    b, inst, _ = _backend(state={"INIT:CONT": "OFF"})
    b.open()
    assert any("single-sweep" in n for n in b.read_state()["notes"])
    assert inst.writes == []
    b.ensure_live()
    assert inst.writes == [":INIT:CONT ON"]
    assert b.ensure_live() == [] and inst.writes == [":INIT:CONT ON"]   # only once


def test_trace_parsing_and_length_check():
    assert np.array_equal(_parse_trace(">-64.73,-68.16, -36.2\n"), [-64.73, -68.16, -36.2])
    b, inst, cfg = _backend(points_override=600)
    b.open(); b.read_state()
    s = resolve(cfg)
    b.configure(s); b.start_sweep(s)
    with pytest.raises(ValueError, match="600 points, expected 601"):
        b.finish_sweep()


def test_the_brain_on_the_real_backend():
    cfg = Config()
    cfg.acquisition.continuous = False
    b, inst, _ = _backend(cfg, prefix=">")
    sa = SpectrumAnalyzer(b, cfg, clock=_FastClock())
    sa.start(run=False)
    n = sa.acquire()
    while sa.status().acquiring:
        sa.step()
    t = sa.get_trace("sample")
    assert t["acq_id"] == n and t["power_dBm"].shape == (601,)
    assert sa.status().simulated is False and sa.status().dut is None
    assert inst.writes == []                        # preset panel: nothing to fix
    sa.shutdown()
    assert inst.state["OUTP:TRAC"] == "OFF"


def test_wait_mode_uses_the_instruments_sweep_time_and_waits_out_a_change():
    """The wait follows the instrument's REPORTED sweep time (not the brain's
    estimate), and after a settings change the old sweep is waited out once."""
    b, inst, cfg = _backend()
    b.open(); b.read_state()
    s = resolve(cfg)
    b.configure(s)
    b.start_sweep(s); b.finish_sweep()
    inst.state["SWE:TIME"] = "500.000"          # the analyser says 0.5 s (auto)
    cfg.sweep.detector = "pos_peak"             # a change that leaves our estimate alone
    s2 = resolve(cfg)
    b.configure(s2)
    m = cfg.hardware.sweep_margin_s
    assert s2.sweep_time_s < 0.5                # the brain's estimate is shorter
    wait = b.start_sweep(s2)
    # old sweep (0.02 s) waited out once + 2 whole sweeps at the reported 0.5 s
    assert wait == pytest.approx(0.02 + 2 * (0.5 + m))
    b.finish_sweep()
    assert b.start_sweep(s2) == pytest.approx(2 * (0.5 + m))   # only once
