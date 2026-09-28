"""The Copper Mountain C1209 backend against a FAKE S2VNA (tests/fake_cmt.py).

What this proves: the SCPI sequence the backend sends (Copper Mountain's
documented single-sweep recipe: TRIG:SOUR BUS, TRIG:SING, *OPC?), ASCII over a
socket, and that SDAT is parsed into the right complex numbers. What it cannot
prove: that S2VNA accepts the sequence -- the hardware pass (# VERIFY in cmt.py).
"""

import numpy as np
import pytest

from fake_cmt import FakeCmt
from vna.analyzer import Analyzer
from vna.backends.cmt import C1209_MAX_HZ, CmtVna, limit_envelope
from vna.config import Config
from vna.field import FieldReading

F = FieldReading(50.0, True, "manual", 0.0)


def _backend(cfg=None, **fake_kw):
    cfg = cfg or Config()
    cfg.hardware.driver = "cmt"
    fake = FakeCmt(**fake_kw)
    return CmtVna(cfg, resource=fake, sleep=lambda s: None), fake, cfg


def _pos(log, cmd):
    return log.index(cmd)


def _writes(log):
    """The commands in a log that are not queries (every query ends in '?')."""
    return [c for c in log if "?" not in c]


def test_open_only_looks_and_changes_nothing():
    """Lukas's rule (2026-09-27): connecting reads S2VNA, it does not set it up.
    The only write allowed is *CLS (it empties the error queue)."""
    b, fake, _ = _backend()
    b.open()
    st = b.read_state()
    assert b.idn().startswith("CMT,C1209")
    assert b.warnings == []
    # a raw socket has no end-of-message: both terminations must be newline
    assert fake.read_termination == "\n" and fake.write_termination == "\n"
    assert _writes(fake.log) == ["*CLS"]
    assert fake.traces == 2 and fake.trig_source == "INT" and fake.sparam == "S11"
    assert st["start_Hz"] == 100e3 and st["stop_Hz"] == 9e9 and st["points"] == 201
    assert st["ifbw_Hz"] == 10e3 and st["power_dBm"] == 0.0 and st["sparam"] == "S11"
    assert st["trigger_source"] == "INT" and st["continuous"] is False
    b.close()
    assert _writes(fake.log) == ["*CLS"], "close of an analyser only looked at writes nothing"


def test_the_first_sweep_sets_up_one_trace_bus_trigger_and_ascii():
    b, fake, _ = _backend()
    b.open()
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0, "S21", F)
    log = fake.log
    assert _pos(log, "*IDN?") < _pos(log, "*CLS") < _pos(log, ":TRIG:SOUR BUS")
    assert fake.traces == 1 and fake.trig_source == "BUS" and fake.cont
    assert fake.fmt == "ASC", "binary is HiSLIP-only on Copper Mountain"
    assert ":SENS1:AVER OFF" in log
    assert fake.sparam == "S21"
    assert log[-1] == ":TRIG:SING" and fake.sweeps == 1


def test_the_brain_adopts_what_s2vna_holds_and_writes_nothing_at_start():
    cfg = Config()
    cfg.hardware.driver = "cmt"
    cfg.field.source = "manual"
    fake = FakeCmt()
    fake.state.update(start=2e9, stop=4e9, points=301, ifbw=1e3, power=-7.0)
    vna = Analyzer(CmtVna(cfg, resource=fake), cfg)
    vna.start(run=False)
    try:
        st = vna.status()
        assert (st.start_Hz, st.stop_Hz, st.points, st.ifbw_Hz, st.power_dBm, st.sparam) == (
            2e9, 4e9, 301, 1e3, -7.0, "S11")
        assert st.continuous is False and st.instrument["trigger_source"] == "INT"
        assert vna.step() is False
        assert _writes(fake.log) == ["*CLS"] and fake.trig_source == "INT"
    finally:
        vna.shutdown()
    assert _writes(fake.log) == ["*CLS"]


def test_a_sweep_writes_settings_reads_back_triggers_once_and_parses_sdat():
    b, fake, _ = _backend()
    b.open()
    fake.log.clear()
    f = np.linspace(1e9, 6e9, 101)
    b.start_sweep(f, 1e3, -10.0, "S21", F)
    log = list(fake.log)
    for cmd in (":SENS1:FREQ:STAR 1000000000", ":SENS1:FREQ:STOP 6000000000",
                ":SENS1:SWE:POIN 101", ":SENS1:BWID 1000", ":SOUR1:POW -10"):
        assert cmd in log, cmd
    assert _pos(log, ":SENS1:SWE:POIN 101") < _pos(log, ":SENS1:SWE:POIN?")
    assert log[-1] == ":TRIG:SING" and fake.sweeps == 1       # trigger LAST, once
    z, meta = b.finish_sweep()
    assert fake.log[-2:] == ["*OPC?", ":CALC1:TRAC1:DATA:SDAT?"]
    np.testing.assert_allclose(z, (np.arange(101) + 1.0) * (0.25 + 0.5j))
    assert meta["ifbw_actual_Hz"] == 1e3 and meta["power_actual_dBm"] == -10.0
    assert meta["correction_on"] is False


def test_opc_waits_longer_than_the_sweep_then_the_timeout_is_restored():
    b, fake, cfg = _backend()
    b.open()
    before = fake.timeout
    f = np.linspace(1e9, 2e9, 1001)
    b.start_sweep(f, 100.0, -10.0, "S21", F)        # ~12 s by the rule of thumb
    b.finish_sweep()
    assert fake.opc_timeouts[-1] >= 3 * b.sweep_time_s(1001, 100.0) * 1000
    assert fake.timeout == before


def test_unchanged_settings_are_not_rewritten():
    b, fake, _ = _backend()
    b.open()
    f = np.linspace(1e9, 6e9, 101)
    b.start_sweep(f, 1e3, -10.0, "S21", F); b.finish_sweep()
    fake.log.clear()
    b.start_sweep(f, 1e3, -10.0, "S21", F)
    assert fake.log == [":TRIG:SING"]


def test_changing_sparam_redefines_the_trace():
    b, fake, _ = _backend()
    b.open()
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0, "S12", F)
    assert fake.sparam == "S12"
    assert ":CALC1:PAR1:DEF S12" in fake.log


def test_a_grid_the_analyser_did_not_take_is_refused():
    b, fake, _ = _backend(snap_points=lambda n: n - 1)
    b.open()
    with pytest.raises(RuntimeError, match="did not take the sweep"):
        b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0, "S21", F)
    assert fake.sweeps == 0


def test_an_instrument_error_stops_the_sweep_before_it_is_triggered():
    b, fake, _ = _backend()
    b.open()
    fake.errors.append('-222,"Data out of range"')
    with pytest.raises(RuntimeError, match="Data out of range"):
        b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0, "S21", F)
    assert fake.sweeps == 0


def test_abort_leaves_the_channel_ready_for_the_next_trigger():
    b, fake, _ = _backend()
    b.open()
    f = np.linspace(1e9, 2e9, 11)
    b.start_sweep(f, 1e3, -10.0, "S21", F)
    b.abort_sweep()
    assert fake.log[-1] == ":ABOR" and not fake.pending
    b.start_sweep(f, 1e3, -10.0, "S21", F)
    assert fake.sweeps == 2 and not fake.errors      # TRIG:SING accepted again


def test_close_hands_s2vna_back_free_running():
    b, fake, _ = _backend()
    b.open()
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0, "S21", F)
    b.close()
    assert fake.log[-2:] == [":ABOR", ":TRIG:SOUR INT"]
    assert fake.closed
    b.close()                                         # twice is fine


def test_a_different_model_is_a_warning_not_a_failure():
    b, _, _ = _backend(idn="CMT,S5048,1,1")
    b.open()
    assert b.warnings and "C1209" in b.warnings[0]


def test_the_envelope_is_pulled_inside_the_c1209():
    cfg = Config()
    cfg.limits.freq_max_Hz = 20e9
    cfg.sweep.stop_Hz = 12e9
    limit_envelope(cfg)
    assert cfg.limits.freq_max_Hz == C1209_MAX_HZ
    assert cfg.sweep.stop_Hz == C1209_MAX_HZ
    assert cfg.sweep.start_Hz < cfg.sweep.stop_Hz


def test_the_brain_acquires_averages_and_files_the_field_on_the_c1209():
    """The whole chain on the fake: brain -> backend -> averaged sample."""
    cfg = Config()
    cfg.hardware.driver = "cmt"
    cfg.field.source = "manual"
    cfg.field.manual_mT = 123.0
    cfg.sweep.averages = 2                  # the brain's own: not an instrument setting
    fake = FakeCmt()
    vna = Analyzer(CmtVna(cfg, resource=fake), cfg)
    vna.start()
    try:
        # the analyser's sweep was adopted at start; a user now sets ours
        vna.set_start(1e9); vna.set_stop(2e9); vna.set_points(21)
        vna.set_ifbw(1e6)                   # rule-of-thumb sweep: 25 us
        acq = vna.acquire()
        import time
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10:
            st = vna.status()
            if st.acq_id == acq and not st.acquiring:
                break
            time.sleep(0.02)
        sample = vna.get_sample()
        assert sample["acq_id"] == acq
        assert sample["field_mT"] == 123.0
        # sweeps 1 and 2 averaged: (1 + 2) / 2 = 1.5 x the base trace
        z = np.asarray(vna.get_trace("sample", "s")["s"])
        np.testing.assert_allclose(z, 1.5 * (np.arange(21) + 1.0) * (0.25 + 0.5j))
        assert fake.sweeps == 2
    finally:
        vna.shutdown()
