"""Several S-parameters from ONE sweep (2026-11-10, Lukas: "S11 and S21 in a
single go"; config sweep.extra_sparams).

What this proves, in simulation and against the fake S2VNA:
  * one trigger delivers every configured S-parameter (no second sweep);
  * each one is averaged, latched and handed out on its own (get_trace sparam);
  * the reference is PER PARAMETER, and u / ln of an extra divide by the
    reference's own trace of that parameter;
  * with only `sparam` configured nothing changes (the older tests, unchanged,
    are the proof of that; a few direct checks are here too);
  * describe lists s_/u_/ln_ratio_<sp> in the same acquire group;
  * the Copper Mountain backend defines one trace per parameter and reads
    TRAC1..n after ONE TRIG:SING.
What it cannot prove: that S2VNA fills both traces from one sweep and that
TRAC2 equals S2VNA's own marker -- the Blue Box check (# VERIFY in cmt.py).
"""

import time

import numpy as np
import pytest

from fake_cmt import FakeCmt
from vna import model
from vna.analyzer import Analyzer, parse_sparams, source_ports
from vna.backends.cmt import CmtVna
from vna.config import Config
from vna.field import FieldReading
from vna.net.describe import build_manifest
from vna.sim_system import build_sim_system

CMD_PORT = 15790          # non-default ports: never a running service's
PUB_PORT = 15791


@pytest.fixture
def vna():
    cfg = Config()
    cfg.field.source = "manual"
    cfg.acquisition.continuous = False
    cfg.sweep.points = 401
    events = []
    v, _sim = build_sim_system(cfg, realtime=False, seed=11)
    v._on_event = lambda lvl, msg: events.append((lvl, msg))
    v.start(run=False)
    v.events = events
    yield v
    v.shutdown()


def _finish(v, limit=50):
    for _ in range(limit):
        if not v.status().acquiring:
            return
        v.step()
    raise AssertionError("acquisition did not finish")


# ---- parsing and the sweep-time rule ------------------------------------------

def test_parse_sparams_accepts_any_spelling_and_refuses_nonsense():
    assert parse_sparams("s11, S22") == ("S11", "S22")
    assert parse_sparams("S11;S12 S22") == ("S11", "S12", "S22")
    assert parse_sparams("") == ()
    assert parse_sparams(["s21"]) == ("S21",)
    with pytest.raises(ValueError, match="S33"):
        parse_sparams("S11,S33")
    with pytest.raises(ValueError, match="twice"):
        parse_sparams("S11,s11")


def test_s11_and_s21_share_the_forward_sweep_s22_needs_the_reverse_one():
    assert source_ports(("S21",)) == 1
    assert source_ports(("S21", "S11")) == 1          # both: source at port 1
    assert source_ports(("S12", "S22")) == 1          # both: source at port 2
    assert source_ports(("S21", "S22")) == 2
    assert source_ports(("S21", "S11", "S12", "S22")) == 2


# ---- one parameter: unchanged -------------------------------------------------

def test_one_parameter_is_what_it_always_was(vna):
    st = vna.status()
    assert st.sparam == "S21" and st.extra_sparams == "" and st.sparams == ["S21"]
    vna.acquire(); _finish(vna)
    t = vna.get_trace("sample")
    assert t["sparam"] == "S21" and t["sparams"] == ["S21"] and "traces" not in t
    # asking for the primary by name is the same as not asking
    np.testing.assert_array_equal(vna.get_trace("sample", "s", "S21")["s"], t["s"])
    with pytest.raises(ValueError, match="no S11"):
        vna.get_trace("sample", "s", "S11")


# ---- several from one sweep ------------------------------------------------------

def test_one_acquisition_one_sweep_two_parameters(vna):
    vna.set_manual_field(70.0)
    vna.set_extra_sparams("s11")
    st = vna.status()
    assert st.extra_sparams == "S11" and st.sparams == ["S21", "S11"]
    sweeps0 = st.sweeps
    n = vna.acquire(); _finish(vna)
    assert vna.status().sweeps == sweeps0 + 1, "one trigger, not one per parameter"
    s21 = vna.get_trace("sample")
    s11 = vna.get_trace("sample", "s", "S11")
    assert s21["acq_id"] == s11["acq_id"] == n
    assert s21["sparam"] == "S21" and s11["sparam"] == "S11"
    assert s11["sparams"] == ["S21", "S11"]
    assert s21["field_mT"] == s11["field_mT"] == 70.0
    # really two different quantities: S11 is a small reflection, S21 the line
    cfg = vna.cfg
    f = s21["freqs_Hz"]
    clean21 = model.s_clean(f, 70.0, cfg.sample, cfg.line, "S21")
    clean11 = model.s_clean(f, 70.0, cfg.sample, cfg.line, "S11")
    noise = 10 * model.noise_rms(cfg.sweep.ifbw_Hz, cfg.sweep.power_dBm, cfg.line)
    assert np.max(np.abs(s21["s"] - clean21)) < noise
    assert np.max(np.abs(s11["s"] - clean11)) < noise
    assert vna.status().sample["sparams"] == ["S21", "S11"]


def test_each_parameter_is_averaged_on_its_own(vna):
    vna.set_extra_sparams("S11")
    vna.set_averages(4)
    sweeps0 = vna.status().sweeps
    vna.acquire(); _finish(vna)
    assert vna.status().sweeps == sweeps0 + 4
    s11 = vna.get_trace("sample", "s", "S11")
    assert s11["averages"] == 4
    cfg = vna.cfg
    clean = model.s_clean(s11["freqs_Hz"], cfg.field.manual_mT, cfg.sample, cfg.line, "S11")
    one_sweep = model.noise_rms(cfg.sweep.ifbw_Hz, cfg.sweep.power_dBm, cfg.line)
    # the mean of 4 has half the noise of one sweep
    assert np.std(s11["s"] - clean) == pytest.approx(one_sweep / 2, rel=0.25)


def test_the_reference_is_per_parameter(vna):
    vna.set_extra_sparams("S11")
    vna.set_manual_field(150.0)
    vna.take_reference(); _finish(vna)
    ref = vna.status().reference
    assert ref["present"] and ref["sparams"] == ["S21", "S11"]
    vna.set_manual_field(60.0)
    vna.acquire(); _finish(vna)
    for sp in ("S21", "S11"):
        t = vna.get_trace("sample", "s", sp)
        r = vna.get_trace("reference", "s", sp)
        u = vna.get_trace("sample", "u", sp)
        ln = vna.get_trace("sample", "ln", sp)
        assert u["sparam"] == ln["sparam"] == sp
        np.testing.assert_allclose(u["u"], (t["s"] - r["s"]) / r["s"])
        np.testing.assert_allclose(ln["ln"], np.log(t["s"] / r["s"]))


def test_a_reference_without_the_parameter_is_refused_for_it_only(vna):
    vna.take_reference(); _finish(vna)                 # S21 only
    vna.set_extra_sparams("S11")
    vna.acquire(); _finish(vna)
    vna.get_trace("sample", "u")                       # S21: still fine
    with pytest.raises(ValueError, match="S-parameter S11 vs reference S21"):
        vna.get_trace("sample", "u", "S11")


def test_a_new_primary_finds_its_trace_in_a_two_parameter_reference(vna):
    vna.set_extra_sparams("S11")
    vna.take_reference(); _finish(vna)
    vna.set_sparam("S11")                              # S11 is the primary now ...
    assert vna.status().sparams == ["S11"], "... and no longer listed as an extra"
    vna.acquire(); _finish(vna)
    u = vna.get_trace("sample", "u")                   # divided by the reference's S11
    assert u["sparam"] == "S11"


def test_the_primary_is_dropped_from_the_extras(vna):
    vna.set_extra_sparams("S21, S11")
    assert vna.status().extra_sparams == "S11"
    vna.set_extra_sparams("")
    assert vna.status().sparams == ["S21"]
    with pytest.raises(ValueError):
        vna.set_extra_sparams("S11, X")


def test_a_reverse_parameter_doubles_the_sweep_time(vna):
    one = vna.status().sweep_time_s
    vna.set_extra_sparams("S11")
    assert vna.status().sweep_time_s == pytest.approx(one)
    vna.set_extra_sparams("S11,S22")
    assert vna.status().sweep_time_s == pytest.approx(2 * one)


def test_a_change_of_the_list_restarts_an_acquisition(vna):
    n = vna.acquire()
    vna.set_extra_sparams("S11")
    assert any("restarted" in m for _l, m in vna.events)
    _finish(vna)
    assert vna.get_trace("sample", "s", "S11")["acq_id"] == n


def test_a_stream_carries_the_primary_and_refuses_two_directions(vna):
    vna.set_extra_sparams("S11,S22")
    with pytest.raises(ValueError, match="one-direction"):
        vna.stream_start()
    vna.set_extra_sparams("S11")
    vna.stream_start()
    vna.step(); vna.step()
    chunk = vna.stream_stop()
    assert chunk["values"]["s"].shape == (2, 401)
    assert chunk["settings"]["sparams"] == "S21,S11"


def test_the_list_change_breaks_a_running_stream(vna):
    vna.stream_start()
    vna.step()
    vna.set_extra_sparams("S11")
    vna.step()
    with pytest.raises(ValueError, match="sweep changed"):
        vna.stream_read()


# ---- a backend that reads one trace per sweep ---------------------------------------

class _OneTrace:
    """A minimal real-looking backend with NO multi_sparams (like pna.py)."""

    simulated = False

    def __init__(self):
        self.calls = []

    def open(self): pass
    def close(self): pass
    def idn(self): return "one-trace analyser"
    def read_state(self): return {}
    def sweep_time_s(self, points, ifbw): return 0.0
    def abort_sweep(self): pass

    def start_sweep(self, freqs, ifbw, power, sparam, field):   # no sparams=
        self.calls.append(sparam)
        self._n = len(freqs)

    def finish_sweep(self):
        return np.ones(self._n, dtype=complex), {}


def test_a_one_trace_backend_refuses_extras_and_ignores_them_in_the_ini():
    cfg = Config()
    cfg.field.source = "manual"
    cfg.acquisition.continuous = False
    cfg.sweep.extra_sparams = "S11"
    events = []
    v = Analyzer(_OneTrace(), cfg)
    v._on_event = lambda lvl, msg: events.append((lvl, msg))
    v.start(run=False)
    try:
        assert cfg.sweep.extra_sparams == ""
        assert any("ignored" in m for _l, m in events)
        with pytest.raises(ValueError, match="one S-parameter per sweep"):
            v.set_extra_sparams("S11")
        v.acquire(); _finish(v)
        assert v.backend.calls == ["S21"]
        ids = {p["id"] for p in build_manifest(v)["parameters"]}
        assert "extra_sparams" not in ids and "s_s11" not in ids
    finally:
        v.shutdown()


# ---- describe ------------------------------------------------------------------------

def test_describe_lists_one_detector_triple_per_extra_parameter(vna):
    p0 = {d["id"]: d for d in build_manifest(vna)["parameters"]}
    assert "extra_sparams" in p0 and "s_s11" not in p0
    r0 = build_manifest(vna)["revision"]
    vna.set_extra_sparams("S11")
    m = build_manifest(vna)
    assert m["revision"] != r0, "clients re-fetch when the detectors change"
    p = {d["id"]: d for d in m["parameters"]}
    for qid, quantity in (("s_s11", "s"), ("u_s11", "u"), ("ln_ratio_s11", "ln")):
        d = p[qid]
        assert d["read"] == {"verb": "get_trace", "key": quantity,
                             "args": {"which": "sample", "quantity": quantity, "sparam": "S11"}}
        # the same acquisition as the primary's detectors: one trigger, one wait
        assert d["acquire"] == p["s_s21"]["acquire"]
        assert d["dims"] == p["s_s21"]["dims"] and d["dtype"] == "complex"
        assert "stream" not in d, "extras are acquired, not streamed"
    assert p["s_s21"]["label"] == "S-parameter S21" and p["s_s11"]["label"] == "S-parameter S11"
    # the primary listed first, and only its three stream
    order = [k for k in p if k.startswith(("s_s", "u_s", "ln_ratio_s"))]
    assert order[:3] == ["s_s21", "u_s21", "ln_ratio_s21"]
    assert "stream" in p["s_s21"]


# ---- over the wire ----------------------------------------------------------------------

def test_the_client_gets_both_traces_of_one_acquisition():
    from vna.net.client import VnaClient
    from vna.net.service import VnaService
    cfg = Config()
    cfg.field.source = "manual"
    cfg.sweep.points = 201
    v, _sim = build_sim_system(cfg, realtime=False, seed=5)
    svc = VnaService(v, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, status_hz=20.0)
    svc.start()
    cli = VnaClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    try:
        time.sleep(0.3)
        cli.set_extra_sparams("S11")
        t21 = cli.acquire_blocking(timeout_s=10)
        t11 = cli.get_trace("sample", "s", "S11")
        assert t21["acq_id"] == t11["acq_id"]
        assert t21["sparam"] == "S21" and t11["sparam"] == "S11"
        assert t11["s"].shape == (201,) and not np.allclose(t11["s"], t21["s"])
        assert cli.status().sparams == ["S21", "S11"]
        with pytest.raises(ValueError, match="S33"):
            cli.set_extra_sparams("S33")
    finally:
        cli.shutdown()
        svc.stop()
        time.sleep(0.2)


# ---- the Copper Mountain backend against the fake S2VNA ---------------------------------

F = FieldReading(50.0, True, "manual", 0.0)

#: each S-parameter its own numbers, so a mix-up of traces cannot pass
_CODE = {"S11": 1.0, "S12": 2.0, "S21": 3.0, "S22": 4.0}


def _sp_fn(sp, n, k):
    return (np.arange(n) + 1.0) * _CODE[sp] + 1j * k


def _cmt(**kw):
    cfg = Config()
    cfg.hardware.driver = "s5085"
    fake = FakeCmt(idn="CMT,S5085,00000001,24.3.4/1.0", sp_fn=_sp_fn, **kw)
    return CmtVna(cfg, resource=fake, sleep=lambda s: None), fake, cfg


def test_cmt_defines_one_trace_per_parameter_and_triggers_once():
    b, fake, _ = _cmt()
    b.open()
    st = b.read_state()
    assert st["trace_count"] == 2                      # what S2VNA showed, informative
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0, "S21", F, sparams=("S21", "S11"))
    assert fake.traces == 2 and fake.defs == {1: "S21", 2: "S11"}
    assert fake.log.count(":TRIG:SING") == 1 and fake.sweeps == 1
    z, meta = b.finish_sweep()
    reads = [c for c in fake.log if c.endswith(":DATA:SDAT?")]
    assert reads == [":CALC1:TRAC1:DATA:SDAT?", ":CALC1:TRAC2:DATA:SDAT?"]
    assert fake.sweeps == 1, "both traces from the one sweep"
    np.testing.assert_array_equal(z, _sp_fn("S21", 11, 1))
    assert list(meta["traces"]) == ["S21", "S11"]
    np.testing.assert_array_equal(meta["traces"]["S11"], _sp_fn("S11", 11, 1))


def test_cmt_back_to_one_parameter_is_the_old_single_trace():
    b, fake, _ = _cmt()
    b.open()
    f = np.linspace(1e9, 2e9, 11)
    b.start_sweep(f, 1e3, -10.0, "S21", F, sparams=("S21", "S11", "S22"))
    b.finish_sweep()
    assert fake.traces == 3 and fake.defs == {1: "S21", 2: "S11", 3: "S22"}
    n = len(fake.log)
    b.start_sweep(f, 1e3, -10.0, "S21", F)
    z, meta = b.finish_sweep()
    assert ":CALC1:PAR:COUN 1" in fake.log[n:] and fake.traces == 1
    assert "traces" not in meta
    assert [c for c in fake.log[n:] if c.endswith(":DATA:SDAT?")] == [":CALC1:TRAC1:DATA:SDAT?"]


def test_cmt_a_one_parameter_sweep_never_touches_the_trace_count_again():
    """The single-parameter sequence is the one verified on 2026-11-10:
    COUN 1 once at take-over, then DEF + SEL of trace 1 -- nothing more."""
    b, fake, _ = _cmt()
    b.open()
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0, "S21", F)
    assert [c for c in fake.log if "PAR:COUN " in c] == [":CALC1:PAR:COUN 1"]
    assert [c for c in fake.log if ":DEF " in c] == [":CALC1:PAR1:DEF S21"]


def test_cmt_binary_transfer_reads_every_trace_too():
    b, fake, cfg = _cmt()
    cfg.hardware.cmt_resource = "TCPIP0::127.0.0.1::hislip0::INSTR"
    cfg.hardware.data_format = "REAL,64"
    b.open()
    b.start_sweep(np.linspace(1e9, 2e9, 11), 1e3, -10.0, "S21", F, sparams=("S21", "S11"))
    _z, meta = b.finish_sweep()
    np.testing.assert_array_equal(meta["traces"]["S11"], _sp_fn("S11", 11, 1))


def test_the_brain_over_the_cmt_latches_both_averaged():
    cfg = Config()
    cfg.hardware.driver = "s5085"
    cfg.field.source = "manual"
    cfg.sweep.averages = 2
    fake = FakeCmt(idn="CMT,S5085,00000001,24.3.4/1.0", sp_fn=_sp_fn)
    v = Analyzer(CmtVna(cfg, resource=fake), cfg)
    v.start(run=False)
    try:
        v.set_sparam("S21")                  # S2VNA held S11 on trace 1 (adopted)
        v.set_start(1e9); v.set_stop(2e9); v.set_points(21); v.set_ifbw(1e6)
        v.set_extra_sparams("S11")
        v.take_reference(); _finish(v)
        assert fake.sweeps == 2
        for sp in ("S21", "S11"):
            z = v.get_trace("sample", "s", sp)["s"]
            # sweeps 1 and 2 averaged: the imaginary part (1 + 2) / 2
            np.testing.assert_allclose(z, (np.arange(21) + 1.0) * _CODE[sp] + 1.5j)
            np.testing.assert_allclose(v.get_trace("sample", "u", sp)["u"], 0.0)
        assert v.status().reference["sparams"] == ["S21", "S11"]
    finally:
        v.shutdown()
