"""The real PNA-X backend against a FAKE VISA resource (tests/fake_visa.py).

What this proves: the SCPI sequence the backend sends, and that SDATA is parsed
into the right complex numbers. What it cannot prove: that the N5222A's firmware
accepts that sequence -- that is the hardware pass (every # VERIFY in pna.py).
"""

import math

import numpy as np
import pytest

from fake_visa import FakePna
from vna.analyzer import Analyzer
from vna.backends.pna import MEAS, PnaVna, parse_sdata
from vna.config import Config
from vna.field import FieldReading


def _backend(cfg=None, **fake_kw):
    cfg = cfg or Config()
    fake = FakePna(**fake_kw)
    return PnaVna(cfg, resource=fake, sleep=lambda s: None), fake, cfg


def _pos(log, cmd):
    return log.index(cmd)


def _writes(log):
    """The commands in a log that are not queries (every query ends in '?')."""
    return [c for c in log if "?" not in c]


def test_open_only_looks_and_changes_nothing():
    """Lukas's rule (2026-09-27): connecting reads the analyser, it does not set
    it up. The only write allowed is *CLS (it empties the error queue)."""
    b, fake, _ = _backend()
    b.open()
    st = b.read_state()
    assert b.idn().startswith("Keysight Technologies,N5222A")
    assert fake.timeout == 10000 and fake.read_termination == "\n"
    assert _writes(fake.log) == ["*CLS"]
    assert fake.mode == "CONT" and fake.averaging is True       # still free-running, as found
    assert b.warnings == []
    # ...and it reports what the front panel holds
    assert st["start_Hz"] == 10e6 and st["stop_Hz"] == 26.5e9 and st["points"] == 201
    assert st["ifbw_Hz"] == 100e3 and st["power_dBm"] == -5.0
    assert st["sparam"] == "S11"                 # the selected measurement CH1_S11_1
    assert st["sweep_mode"] == "CONT" and st["averaging_on"] is True
    assert st["continuous"] is False             # driving it would be a change
    b.close()
    assert _writes(fake.log) == ["*CLS"], "close of an analyser only looked at writes nothing"


def test_read_state_prefers_our_own_measurement_from_an_earlier_run():
    b, fake, _ = _backend(catalog=f'"CH1_S11_1,S11,{MEAS},S12"')
    b.open()
    assert b.read_state()["sparam"] == "S12"


def test_a_query_the_firmware_does_not_know_is_a_warning_not_a_failed_connect():
    b, fake, _ = _backend()
    real_query = fake.query

    def query(cmd):
        if cmd == "SENS1:AVER?":
            raise RuntimeError("VI_ERROR_TMO")
        return real_query(cmd)

    fake.query = query
    b.open()
    st = b.read_state()
    assert st["averaging_on"] is None and st["points"] == 201


def test_the_first_sweep_takes_over_single_sweep_and_owns_one_named_measurement():
    b, fake, _ = _backend()
    b.open()
    b.read_state()
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0, "S21")
    log = fake.log
    assert _pos(log, "*CLS") < _pos(log, "SENS1:SWE:MODE HOLD")
    for cmd in ("TRIG:SOUR IMM", "SENS1:AVER OFF", "FORM:DATA REAL,64", "FORM:BORD SWAP"):
        assert cmd in log
    assert f"CALC1:PAR:DEF:EXT '{MEAS}','S21'" in log     # a NEW measurement of our own...
    assert not any(c.startswith("CALC1:PAR:DEL") for c in log)   # ...and nobody else's deleted
    assert _pos(log, f"CALC1:PAR:DEF:EXT '{MEAS}','S21'") < _pos(log, f"DISP:WIND1:TRAC2:FEED '{MEAS}'")
    # the measurement exists before any SENS1 setting is written
    assert _pos(log, f"CALC1:PAR:DEF:EXT '{MEAS}','S21'") < _pos(log, "SENS1:FREQ:STAR 1000000000")
    assert not any("CSET" in c for c in log)               # no cal set asked for: untouched
    assert log[-1] == "SENS1:SWE:MODE SING"


def test_a_first_sweep_with_the_adopted_settings_rewrites_none_of_them():
    b, fake, _ = _backend(sweep_polls=0)
    b.open()
    b.read_state()
    b.start_sweep(np.linspace(10e6, 26.5e9, 201), 100e3, -5.0, "S11")
    assert not any(c.startswith(("SENS1:FREQ:STAR ", "SENS1:FREQ:STOP ", "SENS1:SWE:POIN ",
                                 "SENS1:BAND ", "SOUR1:POW1:LEV:IMM:AMPL ")) for c in fake.log)


def test_a_sweep_writes_settings_reads_them_back_triggers_once_and_parses_sdata():
    b, fake, _ = _backend(sweep_polls=3)
    b.open()
    fake.log.clear()
    f = np.linspace(1e9, 6e9, 101)
    b.start_sweep(f, 1e3, -10.0, "S21", FieldReading(50.0, True, "manual", 0.0))
    log = list(fake.log)
    assert "SENS1:FREQ:STAR 1000000000" in log and "SENS1:FREQ:STOP 6000000000" in log
    assert "SENS1:SWE:POIN 101" in log and "SENS1:BAND 1000" in log
    assert "SOUR1:POW1:LEV:IMM:AMPL -10" in log
    assert _pos(log, "SENS1:SWE:POIN 101") < _pos(log, "SENS1:SWE:POIN?")    # read back
    assert log[-1] == "SENS1:SWE:MODE SING"                                  # trigger LAST
    assert b.sweep_time_s(101, 1e3) == pytest.approx(0.05)                   # the PNA's figure

    fake.log.clear()
    z, meta = b.finish_sweep()
    assert fake.log.count("SENS1:SWE:MODE?") == 4          # SING, SING, SING, HOLD
    assert fake.log[-2:] == [f"CALC1:PAR:SEL '{MEAS}'", "CALC1:DATA? SDATA"]
    expect = (np.arange(101) + 1.0) * (0.25 + 0.5j) * 1
    assert z.dtype == complex and np.array_equal(z, expect)
    assert meta["ifbw_actual_Hz"] == 1e3 and meta["correction_on"] is False


def test_unchanged_settings_are_not_rewritten_and_a_new_sparam_modifies_ours():
    b, fake, _ = _backend(sweep_polls=0)
    b.open()
    f = np.linspace(1e9, 2e9, 51)
    b.start_sweep(f, 1e3, -10.0, "S21"); b.finish_sweep()
    fake.log.clear()
    b.start_sweep(f, 1e3, -10.0, "S21")
    assert fake.log == ["SENS1:SWE:MODE SING"]             # nothing else to say
    b.finish_sweep()
    fake.log.clear()
    b.start_sweep(f, 1e3, -10.0, "S12")
    assert f"CALC1:PAR:SEL '{MEAS}'" in fake.log and "CALC1:PAR:MOD:EXT 'S12'" in fake.log
    assert fake.sparam == "S12"


def test_moving_the_band_up_writes_stop_before_start():
    b, fake, _ = _backend(sweep_polls=0)
    b.open()
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0); b.finish_sweep()
    fake.log.clear()
    b.start_sweep(np.linspace(5e9, 6e9, 11), 1e3, -10.0)
    assert _pos(fake.log, "SENS1:FREQ:STOP 6000000000") < _pos(fake.log, "SENS1:FREQ:STAR 5000000000")


def test_ascii_transfer_is_the_fallback():
    cfg = Config()
    cfg.hardware.data_format = "ASCII"
    b, fake, _ = _backend(cfg, sweep_polls=0)
    b.open()
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0)
    assert "FORM:DATA ASCII,0" in fake.log and "FORM:BORD SWAP" not in fake.log
    z, _ = b.finish_sweep()
    assert z[3] == pytest.approx(4 * (0.25 + 0.5j))


def test_a_cal_set_is_activated_at_the_first_sweep_not_on_connect():
    cfg = Config()
    cfg.hardware.cal_set = "CalSet_1"
    b, fake, _ = _backend(cfg)
    b.open()
    assert not any("CSET" in c for c in fake.log)
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0)
    assert "SENS1:CORR:CSET:ACT 'CalSet_1',0" in fake.log


def test_an_instrument_error_is_raised_not_ignored():
    b, fake, _ = _backend()
    b.open()
    fake.errors = ['-113,"Undefined header"']
    with pytest.raises(RuntimeError, match="Undefined header"):
        b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0)


def test_a_grid_the_instrument_did_not_take_is_refused():
    b, fake, _ = _backend(snap_points=lambda n: n - 1)
    b.open()
    with pytest.raises(RuntimeError, match="did not take the sweep"):
        b.start_sweep(np.linspace(1e9, 2e9, 101), 1e3, -10.0)


def test_parse_sdata_interleaves_and_refuses_the_wrong_length():
    z = parse_sdata([1.0, 2.0, 3.0, -4.0], 2)
    assert np.array_equal(z, np.array([1 + 2j, 3 - 4j]))
    with pytest.raises(RuntimeError, match="expected 6"):
        parse_sdata([1.0, 2.0, 3.0, 4.0], 3)


def test_abort_and_close_leave_the_instrument_free_running():
    b, fake, _ = _backend()
    b.open()
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0)
    fake.log.clear()
    b.abort_sweep()
    assert fake.log == ["ABOR", "SENS1:SWE:MODE HOLD"]
    b.close()
    assert fake.log[-1] == "SENS1:SWE:MODE CONT" and fake.closed
    b.close()                                              # twice is fine


def test_without_pyvisa_open_says_how_to_install(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_pyvisa(name, *a, **kw):
        if name == "pyvisa":
            raise ImportError("no pyvisa here")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pyvisa)
    with pytest.raises(RuntimeError, match="--extra real"):
        PnaVna(Config()).open()


def test_the_brain_on_the_real_backend_averages_and_files_the_field():
    """Real mode end to end (fake instrument): coherent average of single
    sweeps, and the field + angle latched into the sample although nothing is
    simulated."""
    cfg = Config()
    cfg.field.source = "manual"
    cfg.field.manual_mT, cfg.field.manual_angle_deg = 150.0, 45.0
    cfg.acquisition.continuous = False
    cfg.sweep.averages = 3                  # the brain's own: not an instrument setting
    fake = FakePna(sweep_polls=1)
    vna = Analyzer(PnaVna(cfg, resource=fake, sleep=lambda s: None), cfg)
    vna.start(run=False)
    try:
        st = vna.status()
        assert st.simulated is False and math.isnan(st.f_res_model_Hz)
        assert st.points == 201                 # adopted from the analyser...
        vna.set_points(21)                      # ...until a user sets it
        vna.set_sparam("S21")                   # (the front panel showed S11)
        n = vna.take_reference()
        for _ in range(10):
            if not vna.status().acquiring:
                break
            vna.step()
        t = vna.get_trace("sample")
        # sweeps 1, 2, 3 serve k = 1, 2, 3 times the base trace: the mean is 2 x base
        assert np.allclose(t["s"], 2 * (np.arange(21) + 1.0) * (0.25 + 0.5j))
        assert t["acq_id"] == n and t["averages"] == 3 and fake.sweeps == 3
        assert t["field_mT"] == 150.0 and t["angle_deg"] == pytest.approx(45.0)
        assert t["field_ok"] is True and t["sparam"] == "S21"
        assert math.isnan(t["f_res_model_Hz"]) and t["correction_on"] is False
        assert vna.status().reference["present"] and vna.status().reference["acq_id"] == n
    finally:
        vna.shutdown()
    assert fake.closed


def test_the_brain_adopts_what_the_pna_holds_and_writes_nothing_at_start():
    """Status after start = the analyser's pre-existing state; nothing written
    but *CLS; the module starts hands-off (no sweep of its own) even though the
    config asked for continuous; the out-of-envelope stop is announced."""
    cfg = Config()
    cfg.field.source = "manual"
    assert cfg.acquisition.continuous is True and cfg.sweep.points == 1601
    fake = FakePna(sweep_polls=0)
    fake.state.update(start=2e9, stop=26.5e9, points=401, ifbw=3e3, power=-15.0)
    events = []
    vna = Analyzer(PnaVna(cfg, resource=fake, sleep=lambda s: None), cfg)
    vna._on_event = lambda lvl, msg: events.append((lvl, msg))
    vna.start(run=False)
    try:
        st = vna.status()
        assert (st.start_Hz, st.points, st.ifbw_Hz, st.power_dBm, st.sparam) == (
            2e9, 401, 3e3, -15.0, "S11")
        assert st.stop_Hz == cfg.limits.freq_max_Hz           # 26.5 GHz clamped for OUR sweeps...
        assert st.instrument["stop_Hz"] == 26.5e9             # ...but reported as found
        assert any(l == "warn" and "stop_Hz" in m for l, m in events)
        assert st.continuous is False
        assert vna.step() is False                            # hands off: no sweep asked
        assert _writes(fake.log) == ["*CLS"] and fake.mode == "CONT"
    finally:
        vna.shutdown()
    assert _writes(fake.log) == ["*CLS"]                      # and none at shutdown either
