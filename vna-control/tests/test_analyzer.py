"""The brain, driven by hand with step() -- no thread, no network, instant sweeps."""

import math
import threading
import time

import numpy as np
import pytest

from vna import model
from vna.config import Config
from vna.sim_system import build_sim_system


@pytest.fixture
def vna():
    cfg = Config()
    cfg.field.source = "manual"
    cfg.acquisition.continuous = False
    cfg.sweep.points = 801
    events = []
    v, sim = build_sim_system(cfg, realtime=False, seed=7)
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


def test_acquire_latches_a_trace_at_the_field(vna):
    vna.set_manual_field(70.0)
    n = vna.acquire()
    st = vna.status()
    assert st.acq_id == n and st.acquiring
    _finish(vna)
    t = vna.get_trace("sample")
    assert t["acq_id"] == n and t["s"].shape == (801,) and t["freqs_Hz"].shape == (801,)
    assert t["field_mT"] == 70.0 and t["field_ok"] is True
    assert t["dip_Hz"] == pytest.approx(model.kittel_Hz(70.0, vna.cfg.sample), abs=3e6)
    assert vna.status().sample["acq_id"] == n


def test_each_acquisition_is_fresh(vna):
    """The trap: a scan must never read the previous point's trace."""
    vna.set_manual_field(30.0); a = vna.acquire(); _finish(vna)
    vna.set_manual_field(90.0); b = vna.acquire()
    with pytest.raises(AssertionError):             # sample still #a while #b runs
        assert vna.get_trace("sample")["acq_id"] == b
    _finish(vna)
    t = vna.get_trace("sample")
    assert t["acq_id"] == b == a + 1
    assert t["dip_Hz"] == pytest.approx(model.kittel_Hz(90.0, vna.cfg.sample), abs=3e6)


def test_averaging_takes_n_sweeps_and_reduces_noise(vna):
    vna.set_manual_field(0.0)                       # no line in the band: pure noise on the line
    vna.cfg.sample.dip_dB = 0.0
    clean = model.line_s21(vna.frequencies(), vna.cfg.line)
    vna.acquire(); _finish(vna)
    one = vna.get_trace("sample")["s"] - clean
    vna.set_averages(16)
    before = vna.status().sweeps
    vna.acquire(); _finish(vna)
    t = vna.get_trace("sample")
    assert vna.status().sweeps - before == 16 and t["averages"] == 16
    many = t["s"] - clean
    assert abs(many).std() < abs(one).std() / 2.5  # ~1/4 expected


def test_a_setting_change_restarts_the_acquisition(vna):
    vna.set_averages(3)
    n = vna.acquire()
    vna.step(); vna.step()                          # two of three sweeps done
    vna.set_points(401)
    assert any("restarted" in m for _, m in vna.events)
    _finish(vna)
    t = vna.get_trace("sample")
    assert t["acq_id"] == n and t["averages"] == 3 and t["s"].shape == (401,)


def test_abort_is_latched_so_nobody_reads_the_previous_trace(vna):
    vna.acquire(); _finish(vna)
    n = vna.acquire()
    vna.abort()
    st = vna.status()
    assert st.acq_id == n and not st.acquiring and st.sample.get("aborted")
    with pytest.raises(ValueError, match="aborted"):
        vna.get_trace("sample")


def test_nothing_to_read_before_the_first_acquisition(vna):
    with pytest.raises(ValueError):
        vna.get_trace("sample")
    with pytest.raises(ValueError):
        vna.get_trace("last")


def test_continuous_mode_sweeps_without_a_trigger(vna):
    assert vna.step() is False                      # continuous off, nothing asked
    vna.set_continuous(True)
    assert vna.step() is True
    st = vna.status()
    assert st.sweeps == 1 and st.acq_id == 0 and math.isfinite(st.dip_Hz)
    assert vna.get_trace("last")["trace_id"] == st.trace_id


def test_start_and_stop_bound_each_other(vna):
    vna.set_stop(3e9)
    vna.set_start(5e9)                              # beyond stop: clamped below it
    st = vna.status()
    assert st.start_Hz == 3e9 - vna.cfg.limits.min_span_Hz
    assert any(lvl == "warn" and "clamped" in m for lvl, m in vna.events)


def test_setters_clamp_and_refuse_nonsense(vna):
    vna.set_points(3); assert vna.status().points == vna.cfg.limits.points_min
    vna.set_ifbw(1e9); assert vna.status().ifbw_Hz == vna.cfg.limits.ifbw_max_Hz
    vna.set_sample("alpha", -1); assert vna.status().alpha > 0
    with pytest.raises(ValueError):
        vna.set_power(float("nan"))
    with pytest.raises(ValueError):
        vna.set_sample("not_a_parameter", 1.0)
    with pytest.raises(ValueError):
        vna.set_geometry("sideways")
    with pytest.raises(ValueError):
        vna.set_field_source("hall probe")


def test_sample_parameters_move_the_line(vna):
    vna.set_manual_field(50.0)
    f0 = vna.status().f_res_model_Hz
    vna.set_sample("ms_mT", 140.0)                  # lower Ms -> lower in-plane resonance
    assert vna.status().f_res_model_Hz < f0
    vna.set_geometry("out_of_plane")
    assert math.isnan(vna.status().f_res_model_Hz)  # 50 mT does not saturate out of plane


def test_clMag_source_without_a_magnet_is_flagged(vna):
    vna.cfg.field.clMag_pub_port = 15999            # nothing publishes there
    vna.set_field_source("clMag")
    st = vna.status()
    assert st.field_ok is False and "not heard" in st.field_source
    assert st.field_mT == vna.cfg.field.manual_mT   # the fallback, and it says so
    vna.acquire(); _finish(vna)
    assert vna.get_trace("sample")["field_ok"] is False
    assert any("not live" in m for _, m in vna.events)


# ---- S-parameter selection ------------------------------------------------------------

def test_sparam_is_selected_carried_into_the_sample_and_restarts(vna):
    vna.set_averages(2)
    n = vna.acquire()
    vna.step()
    vna.set_sparam("s12")                           # case does not matter
    assert any("restarted" in m for _, m in vna.events)
    _finish(vna)
    t = vna.get_trace("sample")
    assert t["acq_id"] == n and t["sparam"] == "S12" and vna.status().sparam == "S12"
    with pytest.raises(ValueError):
        vna.set_sparam("S33")


# ---- the reference and u ------------------------------------------------------------------

def _take_reference(v):
    n = v.take_reference()
    _finish(v)
    return n


def test_take_reference_is_an_acquisition_that_also_becomes_the_reference(vna):
    assert vna.status().reference["present"] is False
    vna.set_manual_field(0.0, angle_deg=45.0)
    n = _take_reference(vna)
    st = vna.status()
    assert st.acq_id == n and not st.acquiring and st.sample["acq_id"] == n
    ref = st.reference
    assert ref["present"] and ref["acq_id"] == n and ref["field_mT"] == 0.0
    assert ref["angle_deg"] == pytest.approx(45.0) and ref["sparam"] == "S21"
    assert ref["points"] == 801 and ref["start_Hz"] == 1e9 and ref["stop_Hz"] == 6e9
    assert ref["age_s"] >= 0
    assert np.array_equal(vna.get_trace("reference")["s"], vna.get_trace("sample")["s"])
    # a plain acquisition afterwards does not touch the reference
    m = vna.acquire()
    _finish(vna)
    assert vna.status().reference["acq_id"] == n and vna.status().sample["acq_id"] == m


def test_the_reference_is_stored_in_the_same_critical_section_as_the_sample(vna):
    """Gotcha #28 for the reference: a status frame must never show
    acq_id n and not acquiring while the reference (or sample) is still old.
    Proof: while the latch runs, a status() call from another thread is BLOCKED,
    so it can only see everything before or everything after."""
    vna.set_manual_field(10.0)
    old = _take_reference(vna)                      # an OLD reference exists
    seen, blocked = {}, {}
    original = vna._latch_locked

    def spying_latch(a, last, meta):
        original(a, last, meta)
        done = threading.Event()

        def reader():
            s = vna.status()
            seen.update(acq_id=s.acq_id, acquiring=s.acquiring,
                        sample=s.sample["acq_id"], ref=s.reference["acq_id"])
            done.set()
        threading.Thread(target=reader, daemon=True).start()
        time.sleep(0.05)                            # still inside the critical section
        blocked["while_latching"] = not done.is_set()

    vna._latch_locked = spying_latch
    vna.set_manual_field(150.0)
    n = vna.take_reference()
    _finish(vna)
    deadline = time.monotonic() + 2
    while "ref" not in seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert n != old
    assert blocked["while_latching"] is True
    assert seen == {"acq_id": n, "acquiring": False, "sample": n, "ref": n}


def test_u_is_s_minus_reference_over_reference(vna):
    vna.set_manual_field(0.0)                       # no line in the band
    _take_reference(vna)
    vna.set_manual_field(50.0)
    vna.acquire()
    _finish(vna)
    s = vna.get_trace("sample")["s"]
    ref = vna.get_trace("reference")["s"]
    t = vna.get_trace("sample", "u")
    assert "s" not in t and t["u"].shape == (801,)
    assert np.allclose(t["u"], (s - ref) / ref, rtol=0, atol=1e-15)
    assert t["reference_field_mT"] == 0.0
    # divided by the reference only the line remains: its deepest point is at Kittel
    f = t["freqs_Hz"]
    kittel = model.kittel_Hz(50.0, vna.cfg.sample)
    assert f[np.argmin(np.abs(1 + t["u"]))] == pytest.approx(kittel, abs=8e6)
    # the reference against itself is exactly zero
    assert np.all(vna.get_trace("reference", "u")["u"] == 0)


def test_ln_is_the_logarithm_of_the_same_ratio(vna):
    """The old LabVIEW program plotted u; a film on a line gives
    S = A exp(i eta chi), so ln(S/S_ref) is what is proportional to chi. The two
    agree only while the line is shallow, which is what the second half checks."""
    vna.set_manual_field(0.0)                       # no line in the band
    _take_reference(vna)
    vna.set_manual_field(50.0)
    vna.acquire()
    _finish(vna)
    s = vna.get_trace("sample")["s"]
    ref = vna.get_trace("reference")["s"]
    t = vna.get_trace("sample", "ln")
    assert "s" not in t and "u" not in t and t["ln"].shape == (801,)
    assert np.allclose(t["ln"], np.log(s / ref), rtol=0, atol=1e-15)
    assert t["reference_field_mT"] == 0.0
    # ln(1 + u) ~ u where the perturbation is small: off resonance they agree
    u = vna.get_trace("sample", "u")["u"]
    f = t["freqs_Hz"]
    off = np.abs(f - model.kittel_Hz(50.0, vna.cfg.sample)) > 1e9
    assert np.allclose(t["ln"][off], u[off], atol=5e-3)
    # the reference against itself is ln(1) = 0 -- to rounding, not exactly:
    # u subtracts and gives a hard zero, log goes through the division first
    assert np.allclose(vna.get_trace("reference", "ln")["ln"], 0, atol=1e-12)


def test_ln_is_refused_like_u(vna):
    vna.acquire()
    _finish(vna)
    with pytest.raises(ValueError, match="take a reference"):
        vna.get_trace("sample", "ln")
    _take_reference(vna)
    vna.set_points(401)
    vna.acquire()
    _finish(vna)
    with pytest.raises(ValueError) as err:
        vna.get_trace("sample", "ln")
    assert "401 points vs reference 801" in str(err.value)


def test_u_is_refused_without_a_reference(vna):
    vna.acquire()
    _finish(vna)
    with pytest.raises(ValueError, match="take a reference"):
        vna.get_trace("sample", "u")
    with pytest.raises(ValueError, match="no reference"):
        vna.get_trace("reference")
    with pytest.raises(ValueError, match="quantity"):
        vna.get_trace("sample", "phase")


@pytest.mark.parametrize("change,words", [
    (lambda v: v.set_sparam("S11"), "S-parameter S11 vs reference S21"),
    (lambda v: v.set_points(401), "401 points vs reference 801"),
    (lambda v: v.set_start(2e9), "start 2 GHz vs reference 1 GHz"),
    (lambda v: v.set_stop(5e9), "stop 5 GHz vs reference 6 GHz"),
])
def test_u_is_refused_when_the_reference_does_not_match(vna, change, words):
    _take_reference(vna)
    change(vna)
    vna.acquire()
    _finish(vna)
    with pytest.raises(ValueError) as err:
        vna.get_trace("sample", "u")
    assert words in str(err.value) and "Take a new reference" in str(err.value)
    vna.get_trace("sample", "s")                    # the raw trace is still fine


def test_clear_reference(vna):
    _take_reference(vna)
    vna.clear_reference()
    assert vna.status().reference["present"] is False
    with pytest.raises(ValueError, match="reference"):
        vna.get_trace("sample", "u")
    vna.clear_reference()                           # twice is fine


def test_take_reference_abort_matches_acquire_and_drops_the_old_reference(vna):
    _take_reference(vna)                            # an old, valid reference
    n = vna.take_reference()
    assert vna.status().acq_is_reference
    vna.abort()
    st = vna.status()
    # exactly the abort semantics of acquire ...
    assert st.acq_id == n and not st.acquiring and st.sample.get("aborted")
    with pytest.raises(ValueError, match="aborted"):
        vna.get_trace("sample")
    # ... and a routine that asked for a NEW reference cannot silently use the old one
    assert st.reference["present"] is False
    assert any("reference cleared" in m for _, m in vna.events)


def test_take_reference_survives_a_restart_but_not_a_new_trigger(vna):
    vna.set_averages(2)
    n = vna.take_reference()
    vna.step()
    vna.set_ifbw(1e3)                               # restart: still a reference acquisition
    _finish(vna)
    assert vna.status().reference["acq_id"] == n
    vna.take_reference()
    vna.acquire()                                   # supersedes it: the old reference goes
    assert vna.status().reference["present"] is False
    _finish(vna)
    assert vna.status().reference["present"] is False
