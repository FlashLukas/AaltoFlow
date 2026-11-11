"""S2VNA changed BY HAND while the service runs (Blue Box, 2026-11-11).

The user changed the traces in S2VNA's own window; the backend trusted what it
had set up, read a trace that no longer existed, and every sweep failed with a
raw VISA timeout until the set-up happened to match again. Now every sweep
after the first re-reads the set-up and puts back what differs, and a failed
trace read says what it most likely means and forces a fresh set-up.
"""

from __future__ import annotations

import numpy as np
import pytest

from vna.backends.cmt import CmtVna
from vna.config import Config
from vna.field import FieldReading

from fake_cmt import FakeCmt

F = FieldReading(0.0, True, "manual", 0.0)
FREQS = np.linspace(1e9, 2e9, 11)


def _sweep(b, sparams=("S21", "S11")):
    b.start_sweep(FREQS, 1e3, -10.0, sparams[0], F, sparams=sparams)
    return b.finish_sweep()


def _backend():
    cfg = Config()
    cfg.hardware.driver = "cmt"
    fake = FakeCmt()
    b = CmtVna(cfg, resource=fake, sleep=lambda s: None)
    b.open()
    b.read_state()
    return b, fake


def test_traces_changed_in_s2vna_are_put_back_before_the_next_sweep():
    b, fake = _backend()
    _sweep(b)
    assert fake.traces == 2 and fake.defs == {1: "S21", 2: "S11"}
    # the user, in S2VNA: one trace only, showing S22
    fake.traces, fake.defs = 1, {1: "S22"}
    z, meta = _sweep(b)
    assert fake.traces == 2 and fake.defs == {1: "S21", 2: "S11"}
    assert set(meta["traces"]) == {"S21", "S11"}
    assert any("changed by hand" in w and "traces" in w for w in b.warnings)


def test_trigger_averaging_and_sweep_changed_in_s2vna_are_put_back():
    b, fake = _backend()
    _sweep(b, ("S21",))
    n_warn = len(b.warnings)
    fake.trig_source, fake.aver = "INT", True
    fake.state["points"] = 201
    z, _ = _sweep(b, ("S21",))
    assert fake.trig_source == "BUS" and fake.aver is False
    assert fake.state["points"] == 11 and z.size == 11
    new = " ".join(b.warnings[n_warn:])
    assert "trigger source INT" in new and "averaging on" in new and "points 201" in new


def test_nothing_changed_writes_nothing_more():
    b, fake = _backend()
    _sweep(b)
    n = len(b.warnings)
    fake.log.clear()
    _sweep(b)
    writes = [c for c in fake.log if "?" not in c]
    assert writes == [":TRIG:SING"], writes
    assert len(b.warnings) == n


def test_a_trace_gone_mid_sweep_is_explained_and_the_next_sweep_recovers():
    b, fake = _backend()
    _sweep(b)
    b.start_sweep(FREQS, 1e3, -10.0, "S21", F, sparams=("S21", "S11"))
    fake.traces, fake.defs = 1, {1: "S21"}          # changed while it swept
    with pytest.raises(RuntimeError, match="Were the traces changed in S2VNA"):
        b.finish_sweep()
    z, meta = _sweep(b)                               # set up again, works
    assert fake.traces == 2 and set(meta["traces"]) == {"S21", "S11"}
