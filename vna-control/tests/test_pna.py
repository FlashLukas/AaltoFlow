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


def test_open_puts_the_channel_in_single_sweep_and_owns_one_named_measurement():
    b, fake, _ = _backend()
    b.open()
    log = fake.log
    assert b.idn().startswith("Keysight Technologies,N5222A")
    assert fake.timeout == 10000 and fake.read_termination == "\n"
    # order matters: clear the error queue before anything can fail
    assert _pos(log, "*IDN?") < _pos(log, "*CLS") < _pos(log, "SENS1:SWE:MODE HOLD")
    for cmd in ("TRIG:SOUR IMM", "SENS1:AVER OFF", "FORM:DATA REAL,64", "FORM:BORD SWAP"):
        assert cmd in log
    assert f"CALC1:PAR:DEF:EXT '{MEAS}','S21'" in log     # a NEW measurement of our own...
    assert not any(c.startswith("CALC1:PAR:DEL") for c in log)   # ...and nobody else's deleted
    assert _pos(log, f"CALC1:PAR:DEF:EXT '{MEAS}','S21'") < _pos(log, f"DISP:WIND1:TRAC2:FEED '{MEAS}'")
    assert log[-2:] == [f"CALC1:PAR:SEL '{MEAS}'", "SYST:ERR?"]
    assert not any("CSET" in c for c in log)               # no cal set asked for: untouched


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
    assert "FORM:DATA ASCII,0" in fake.log and "FORM:BORD SWAP" not in fake.log
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0)
    z, _ = b.finish_sweep()
    assert z[3] == pytest.approx(4 * (0.25 + 0.5j))


def test_a_cal_set_is_activated_when_configured():
    cfg = Config()
    cfg.hardware.cal_set = "CalSet_1"
    b, fake, _ = _backend(cfg)
    b.open()
    assert "SENS1:CORR:CSET:ACT 'CalSet_1',0" in fake.log


def test_an_instrument_error_is_raised_not_ignored():
    b, fake, _ = _backend()
    fake.errors = ['-113,"Undefined header"']
    with pytest.raises(RuntimeError, match="Undefined header"):
        b.open()


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
    cfg.sweep.points, cfg.sweep.averages = 21, 3
    fake = FakePna(sweep_polls=1)
    vna = Analyzer(PnaVna(cfg, resource=fake, sleep=lambda s: None), cfg)
    vna.start(run=False)
    try:
        st = vna.status()
        assert st.simulated is False and math.isnan(st.f_res_model_Hz)
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
