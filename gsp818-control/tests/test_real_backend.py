"""The real backend against a fake instrument (tests/fake_gsp.py): the SCPI it
sends, the order of the safety commands, parsing, and the brain on top.
Offline; pyvisa is never imported."""

import numpy as np
import pytest

from fake_gsp import FakeGsp
from gsp818.analyzer import SpectrumAnalyzer
from gsp818.backends.gsp import GspAnalyzer, _parse_trace
from gsp818.config import Config
from gsp818.model import resolve


def _backend(cfg=None, **fake):
    cfg = cfg or Config()
    inst = FakeGsp(**fake)
    return GspAnalyzer(cfg, resource=inst, sleep=lambda s: None), inst, cfg


def test_open_switches_the_tg_off_and_sets_dbm():
    b, inst, _ = _backend()
    b.open()
    assert "GSP-818" in b.idn()
    assert inst.log.index(":OUTP:TRAC OFF") < inst.log.index(":UNIT:POW DBM")
    # whatever the front panel left: a live trace, no instrument averaging
    assert ":TRAC1:MODE WRIT" in inst.log and ":AVER OFF" in inst.log
    assert b.simulated is False


def test_configure_sends_only_changes_and_reads_back():
    b, inst, cfg = _backend()
    b.open()
    rb = b.configure(resolve(cfg))
    assert ":FREQ:STAR 9000" in inst.log and ":FREQ:STOP 1800000000" in inst.log
    assert ":BAND:AUTO ON" in inst.log and ":DET AUTO" in inst.log
    assert ":OUTP:TRAC OFF" in inst.log
    assert rb["rbw_Hz"] == 3e6 and rb["sweep_time_s"] == pytest.approx(0.02)
    n = len(inst.log)
    b.configure(resolve(cfg))
    assert not [c for c in inst.log[n:] if not c.endswith("?")]     # nothing re-sent
    cfg.sweep.rbw_auto, cfg.sweep.rbw_Hz = False, 30e3
    cfg.sweep.sweep_time_auto, cfg.sweep.sweep_time_s = False, 0.0125
    cfg.sweep.detector = "pos_peak"
    cfg.tracking.tg_on, cfg.tracking.level_dBm = True, -12.0
    b.configure(resolve(cfg))
    new = inst.log[n:]
    assert ":BAND:AUTO OFF" in new and ":BAND 30000" in new
    assert ":SWE:TIME 12.500 ms" in new and ":DET POS" in new
    # the level is set BEFORE the output goes on
    assert new.index(":SOUR:POW:TRAC -12.0") < new.index(":OUTP:TRAC ON")


def test_close_switches_the_tg_off_and_closes():
    b, inst, cfg = _backend()
    b.open()
    cfg.tracking.tg_on = True
    b.configure(resolve(cfg))
    b.close()
    assert inst.log[-1] == ":OUTP:TRAC OFF" and inst.state["OUTP:TRAC"] == "OFF"
    b.close()                                       # twice is fine


def test_wait_mode_waits_whole_sweeps_and_reads_trace1():
    b, inst, cfg = _backend()
    b.open()
    s = resolve(cfg)
    b.configure(s)
    wait = b.start_sweep(s)
    # the fake reports 20 ms (longer than the brain's 10 ms estimate): the
    # instrument's own number wins
    assert wait == pytest.approx(2 * (0.02 + cfg.hardware.sweep_margin_s))
    assert ":INIT:IMM" not in inst.log
    y, _ = b.finish_sweep()
    assert y.shape == (601,) and ":TRAC? TRACE1" in inst.log


def test_single_mode_uses_init_imm():
    cfg = Config()
    cfg.hardware.sweep_mode = "single"
    b, inst, _ = _backend(cfg)
    b.open()
    s = resolve(cfg)
    b.configure(s)
    b.start_sweep(s); b.finish_sweep()
    b.start_sweep(s); b.finish_sweep()
    assert inst.log.count(":INIT:CONT OFF") == 1 and inst.log.count(":INIT:IMM") == 2
    b.close()
    assert ":INIT:CONT ON" in inst.log               # the front panel sweeps again


def test_trace_parsing_and_length_check():
    assert np.array_equal(_parse_trace(">-64.73,-68.16, -36.2\n"), [-64.73, -68.16, -36.2])
    b, inst, cfg = _backend(points_override=600)
    b.open()
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
    assert sa.status().simulated is False and sa.status().dut == ""
    sa.shutdown()
    assert inst.state["OUTP:TRAC"] == "OFF"


class _FastClock:
    """A clock that jumps 1 s per read, so the brain's sweep wait ends at once."""
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def test_wait_mode_uses_the_instruments_sweep_time_and_waits_out_a_change():
    """The wait follows the instrument's REPORTED sweep time (not the brain's
    estimate), and after a settings change the old sweep is waited out once."""
    b, inst, cfg = _backend()
    b.open()
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
