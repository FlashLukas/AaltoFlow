"""The RF source output (2026-11-10, Blue Box).

The user wanted the analyser's output OFF and the module had no way to say so;
switched off behind its back, the module would have gone on "measuring" noise
that looks like a trace. Now: read at start (never changed by connecting or
stopping), switchable on request, and an acquisition with the output off is
REFUSED -- also when it was switched off on the analyser itself.
"""

from __future__ import annotations

import numpy as np
import pytest

from vna.analyzer import Analyzer
from vna.backends.cmt import CmtVna
from vna.config import Config
from vna.field import FieldReading
from vna.net.describe import build_manifest
from vna.sim_system import build_sim_system

from fake_cmt import FakeCmt


def _sim(rf_on=True):
    cfg = Config()
    cfg.field.source = "manual"
    cfg.acquisition.continuous = False
    cfg.sweep.points = 101
    v, sim = build_sim_system(cfg, realtime=False, seed=1)
    if not rf_on:
        sim.set_rf_output(False)
    events = []
    v._on_event = lambda lvl, msg: events.append((lvl, msg))
    v.start(run=False)
    v.events = events
    return v, sim


def _finish(v, limit=50):
    for _ in range(limit):
        if not v.status().acquiring:
            return
        v.step()
    raise AssertionError("acquisition did not finish")


def test_the_state_is_adopted_and_an_acquisition_with_rf_off_is_refused():
    v, sim = _sim(rf_on=False)
    try:
        assert v.status().rf_output is False
        assert any("RF output is OFF" in m for _, m in v.events)
        with pytest.raises(ValueError, match="RF output is OFF"):
            v.acquire()
        with pytest.raises(ValueError, match="RF output is OFF"):
            v.take_reference()
        assert v.status().acq_id == 0, "a refused trigger takes no id"
        v.set_rf_output(True)
        assert v.status().rf_output is True and sim.rf_output() is True
        n = v.acquire()
        _finish(v)
        assert v.get_trace("sample")["acq_id"] == n
    finally:
        v.shutdown()


def test_switching_off_aborts_a_running_acquisition():
    v, sim = _sim()
    try:
        n = v.acquire()
        v.set_rf_output(False)
        st = v.status()
        assert st.rf_output is False and not st.acquiring
        assert st.sample["acq_id"] == n and st.sample["aborted"] is True
        # and nothing sweeps on its own with the source off
        v.cfg.acquisition.continuous = True
        assert v.step() is False and v.status().sweeps == 0
    finally:
        v.shutdown()


def test_a_switch_made_on_the_analyser_itself_is_noticed_at_the_trigger():
    """What the Blue Box did: :OUTP:STAT OFF straight to S2VNA while the
    service ran. The brain still believed "on" -- the trigger re-reads it."""
    v, sim = _sim()
    try:
        assert v.status().rf_output is True
        sim.set_rf_output(False)                 # behind the module's back
        with pytest.raises(ValueError, match="RF output is OFF"):
            v.acquire()
        assert v.status().rf_output is False
    finally:
        v.shutdown()


def test_shutdown_leaves_the_output_as_it_is():
    v, sim = _sim()
    v.set_rf_output(False)
    v.shutdown()
    assert sim.rf_output() is False


def test_describe_offers_the_control_only_where_it_can_be_switched():
    v, _ = _sim()
    try:
        ids = {d["id"]: d for d in build_manifest(v)["parameters"]}
        d = ids["rf_output"]
        assert d["kind"] == "control" and d["type"] == "bool"
        assert d["set"] == {"verb": "set_rf_output", "arg": "on"}
        assert d["read_path"] == ["rf_output"]
    finally:
        v.shutdown()

    class NoRf:                                  # LibreVNA / Harogic: no switch
        simulated = False
    a = Analyzer(NoRf(), Config())
    assert not a.can_switch_rf
    with pytest.raises(ValueError, match="cannot switch"):
        a.set_rf_output(False)


# ---- Copper Mountain (S2VNA): the SCPI ----------------------------------------------

def _cmt():
    cfg = Config()
    cfg.hardware.driver = "cmt"
    fake = FakeCmt()
    return CmtVna(cfg, resource=fake, sleep=lambda s: None), fake


def test_cmt_reads_the_output_at_open_and_writes_it_only_on_request():
    b, fake = _cmt()
    fake.rf_on = False
    b.open()
    st = b.read_state()
    assert st["rf_output"] is False
    assert ":OUTP:STAT?" in fake.log
    assert not any(c.startswith(":OUTP:STAT ") for c in fake.log), "looking writes nothing"
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0, "S21",
                  FieldReading(0.0, True, "manual", 0.0))
    b.finish_sweep()
    assert not any(c.startswith(":OUTP:STAT ") for c in fake.log), "sweeping does not switch it"
    b.set_rf_output(True)
    assert fake.rf_on is True and fake.log[-2:] == [":OUTP:STAT ON", ":SYST:ERR?"]
    assert b.rf_output() is True
    b.set_rf_output(False)
    assert fake.rf_on is False
    b.close()
    assert fake.rf_on is False, "the hand-back on close leaves the output alone"


def test_cmt_through_the_brain_refuses_with_the_output_off():
    cfg = Config()
    cfg.hardware.driver = "cmt"
    cfg.field.source = "manual"
    fake = FakeCmt()
    fake.rf_on = False
    v = Analyzer(CmtVna(cfg, resource=fake, sleep=lambda s: None), cfg)
    v.start(run=False)
    try:
        assert v.status().rf_output is False
        with pytest.raises(ValueError, match="RF output is OFF"):
            v.acquire()
        assert fake.sweeps == 0
        v.set_rf_output(True)
        assert fake.rf_on is True and v.status().rf_output is True
        v.acquire()
        _finish(v)
        assert fake.sweeps >= 1
    finally:
        v.shutdown()
