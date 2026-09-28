"""WINDOWED acquisition (2026-09-28): `acquire` with window [i0, i1] sweeps only
those bins of the full grid -- the contract scan-core relies on for FMR in
field. Proved here: the bins land exactly on the full grid, the window is
clamped and widened to min_bins, the arrays stay full-length with NaN (null)
outside, transmission uses the reference's own bins, the scalars come from
the measured bins only, a grid that does not line up falls back to the whole
band (never interpolated), and all of it over the wire.

Scratch ports 18060-18079 (fake owner) and 18090/18091 (service)."""

import itertools
import math
import time

import numpy as np
import pytest

from fake_owner import FakeOwner
from shsna.analyzer import Analyzer, WINDOW_MIN_BINS, resolve_window
from shsna.backends.remote_sa import RemoteSa
from shsna.config import Config
from shsna.net.client import ShsnaClient
from shsna.net.describe import build_manifest
from shsna.net.service import ShsnaService
from shsna.sim_system import build_sim_system

_OWNER_PORTS = itertools.count(18060, 2)


# ---- helpers -------------------------------------------------------------------

@pytest.fixture
def sna():
    """700-1300 MHz in 601 points: 1 MHz bins, the DUT's pass band at bin 300."""
    cfg = Config()
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz, cfg.sweep.points = 700e6, 1300e6, 601
    v, sim = build_sim_system(cfg, realtime=False, seed=5)
    events = []
    v._on_event = lambda level, msg: events.append((level, msg))
    v.start(run=False)
    v.events, v.sim = events, sim
    # record what the brain asks the backend to sweep
    calls = []
    real_start, real_est = sim.start_sweep, sim.estimate_time_s

    def start_sweep(*a):
        calls.append(a)
        return real_start(*a)

    est = []

    def estimate(points, averages):
        est.append(int(points))
        return real_est(points, averages)
    sim.start_sweep, sim.estimate_time_s = start_sweep, estimate
    v.calls, v.est = calls, est
    yield v
    v.shutdown()


def _finish(v, n=None, limit=50):
    for _ in range(limit):
        if not v.status().acquiring:
            break
        v.step()
    st = v.status()
    assert not st.acquiring
    if n is not None:
        assert st.acq_id == n
    return st


def _thru(v):
    v.set_sim("dut_inserted", False)
    _finish(v, v.take_reference())
    v.set_sim("dut_inserted", True)


# ---- resolving a window ---------------------------------------------------------

def test_resolve_window_clamps_widens_and_refuses():
    assert WINDOW_MIN_BINS == 11
    assert resolve_window(None, 601) is None
    assert resolve_window([250, 350], 601) == (250, 350)
    # narrower than 11 bins: widened symmetrically around the request
    assert resolve_window([300, 302], 601) == (296, 306)
    assert resolve_window([300, 300], 601) == (295, 305)
    # ... and shifted back inside at an edge, still 11 bins
    assert resolve_window([0, 2], 601) == (0, 10)
    assert resolve_window([-50, 5], 601) == (0, 10)
    assert resolve_window([599, 600], 601) == (590, 600)
    assert resolve_window([700, 900], 601) == (590, 600)      # wholly beyond: clamped
    # the whole grid (or more) is the whole band
    assert resolve_window([0, 600], 601) is None
    assert resolve_window([-5, 1000], 601) is None
    assert resolve_window([2, 5], 11) is None                 # the grid IS min_bins
    # whole numbers given as floats are fine (JSON has no int/float difference)
    assert resolve_window([250.0, 350.0], 601) == (250, 350)
    for bad in ([5, 2], [1.5, 3], "ab", [1], [1, 2, 3], [float("nan"), 3]):
        with pytest.raises(ValueError):
            resolve_window(bad, 601)


# ---- the sweep itself -------------------------------------------------------------

def test_the_window_bins_land_exactly_on_the_full_grid(sna):
    _thru(sna)
    full = sna.frequencies()
    sna.calls.clear()
    n = sna.acquire(window=[250, 350])
    assert sna.status().acquiring
    _finish(sna, n)
    # the backend was asked for f[250]..f[350] in 101 points: bins on the grid
    start, stop, points, _rbw, _avg = sna.calls[-1]
    assert (start, stop, points) == (full[250], full[350], 101)
    # the sweep's own estimate follows the window (status still shows the
    # full band's, which is what the Sweep settings describe)
    assert 101 in sna.est
    t = sna.get_trace("raw")
    assert t["window"] == [250, 350] and t["points"] == 601
    np.testing.assert_array_equal(t["freqs_Hz"], full)       # the FULL grid, unchanged
    raw = t["raw"]
    assert raw.shape == (601,)
    assert np.isnan(raw[:250]).all() and np.isnan(raw[351:]).all()
    assert np.isfinite(raw[250:351]).all()
    st = sna.status()
    assert st.sample["window"] == [250, 350] and st.sample["window_fallback"] == ""
    assert st.sample["window_requested"] == [250, 350]


def test_min_bins_widening_is_what_is_swept(sna):
    _thru(sna)
    sna.calls.clear()
    _finish(sna, sna.acquire(window=[300, 302]))
    assert sna.calls[-1][2] == 11
    t = sna.get_trace("raw")
    assert t["window"] == [296, 306] and np.isfinite(t["raw"]).sum() == 11
    assert sna.status().sample["window_requested"] == [300, 302]    # what was asked


def test_clamping_at_the_edge(sna):
    _thru(sna)
    _finish(sna, sna.acquire(window=[590, 5000]))
    t = sna.get_trace("raw")
    assert t["window"] == [590, 600]
    assert t["freqs_Hz"][600] == pytest.approx(1300e6)


def test_no_window_or_the_whole_grid_is_a_full_sweep(sna):
    _thru(sna)
    for w in (None, [0, 600], [-3, 9999]):
        sna.calls.clear()
        _finish(sna, sna.acquire(window=w))
        assert sna.calls[-1][2] == 601
        t = sna.get_trace("raw")
        assert t["window"] == [0, 600] and np.isfinite(t["raw"]).all()


def test_transmission_of_a_window_is_the_raw_window_minus_the_references_bins(sna):
    _thru(sna)
    _finish(sna, sna.acquire(window=[250, 350]))
    ref = sna.get_trace("reference")
    assert ref["window"] == [0, 600] and np.isfinite(ref["reference"]).all()   # full band
    raw = sna.get_trace("raw")["raw"]
    t = sna.get_trace("transmission")
    assert t["window"] == [250, 350]
    tx = t["transmission"]
    assert np.isnan(tx[:250]).all() and np.isnan(tx[351:]).all()
    np.testing.assert_allclose(tx[250:351], raw[250:351] - ref["reference"][250:351])
    # it IS the filter: the thru's ripple and cable cancel inside the window
    assert np.nanmax(tx) == pytest.approx(-sna.cfg.sim.dut_loss_dB, abs=0.3)


def test_get_result_uses_the_measured_bins_only(sna):
    _thru(sna)
    _finish(sna, sna.acquire(window=[250, 350]))            # 950-1050 MHz: the whole filter
    r = sna.get_result()
    assert r["window"] == [250, 350]
    assert r["peak_freq_hz"] == pytest.approx(1e9, abs=5e6)
    assert r["bw3_hz"] == pytest.approx(60e6, abs=3e6)
    assert math.isfinite(r["mean_transmission_db"])
    # a window INSIDE the pass band: the -3 dB edges are not in the measured
    # bins, so the width is "not measured" -- not the window's width
    _finish(sna, sna.acquire(window=[290, 310]))
    r = sna.get_result()
    assert math.isnan(r["bw3_hz"])
    assert r["mean_transmission_db"] == pytest.approx(-sna.cfg.sim.dut_loss_dB, abs=0.3)
    raw = sna.get_result("raw")
    assert math.isfinite(raw["peak_db"]) and math.isfinite(raw["mean_db"])
    # and the status scalars agree
    assert sna.status().sample["mean_transmission_db"] == pytest.approx(r["mean_transmission_db"])


def test_a_window_without_a_matching_reference_is_refused(sna):
    # no reference: the raw window is fine, transmission is refused
    _finish(sna, sna.acquire(window=[250, 350]))
    assert np.isfinite(sna.get_trace("raw")["raw"][250:351]).all()
    with pytest.raises(ValueError, match="needs a thru reference"):
        sna.get_trace("transmission")
    with pytest.raises(ValueError, match="needs a thru reference"):
        sna.get_result()
    # a reference on another grid: refused, like a full trace
    _thru(sna)
    sna.set_points(301)
    _finish(sna, sna.acquire(window=[100, 150]))
    assert sna.get_trace("raw")["points"] == 301
    with pytest.raises(ValueError, match="does not match"):
        sna.get_trace("transmission")


def test_take_reference_ignores_a_window_over_the_wire_too(sna):
    svc = ShsnaService(sna)                                 # only _dispatch is used
    sna.set_sim("dut_inserted", False)
    n = svc._dispatch({"cmd": "take_reference", "window": [10, 30]})["acq_id"]
    _finish(sna, n)
    assert sna.calls[-1][2] == 601
    assert sna.get_trace("reference")["window"] == [0, 600]
    assert any("ignores the window" in m for _l, m in sna.events)


def test_a_malformed_window_is_refused_at_the_trigger(sna):
    svc = ShsnaService(sna)
    for bad in ([5, 2], [1.5, 3], "all", [1]):
        r = svc._dispatch({"cmd": "acquire", "window": bad})
        assert r["ok"] is False and "window" in r["error"], (bad, r)
    assert sna.status().acquiring is False                   # nothing was started


def test_failure_and_abort_are_latched_as_before(sna):
    _thru(sna)
    sna.sim.fail_next = "TG lost"
    n = sna.acquire(window=[250, 350])
    st = _finish(sna, n)
    assert st.acq_error == "TG lost" and st.sample["failed"] is True
    with pytest.raises(ValueError, match="TG lost"):
        sna.get_trace("raw")
    n = sna.acquire(window=[250, 350])
    sna.abort()
    st = sna.status()
    assert st.acq_id == n and not st.acquiring and st.acq_error == "aborted"


def test_status_shows_the_window_being_swept(sna):
    """acq_window: the resolved window while the sweep runs, [] otherwise."""
    _thru(sna)
    sna.sim.time_scale = 1.0                                 # a sweep that takes time
    n = sna.acquire(window=[300, 302])
    seen = []
    orig_poll = sna.sim.poll

    def poll():
        seen.append(list(sna.status().acq_window))
        sna.sim._pending["t_done"] = 0.0                    # finish at the first look
        return orig_poll()
    sna.sim.poll = poll
    _finish(sna, n)
    assert seen and seen[0] == [296, 306]
    assert sna.status().acq_window == []


# ---- a real owner whose grid does not line up -------------------------------------

def _cfg(cmd, pub):
    cfg = Config()
    hw = cfg.hardware
    hw.owner_host, hw.owner_cmd_port, hw.owner_pub_port = "127.0.0.1", cmd, pub
    hw.timeout_ms, hw.alive_s = 400, 0.5
    # 100-200 MHz in 101 points: 1 MHz bins on whole kHz, so the fake owner
    # (which starts on a whole kHz) puts a window's bins exactly where asked
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz, cfg.sweep.points = 100e6, 200e6, 101
    return cfg


def _wait(pred, timeout=3.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


@pytest.fixture
def owner_and_sna():
    cmd = next(_OWNER_PORTS)
    owner = FakeOwner(cmd, cmd + 1, sweep_s=0.05).start()
    cfg = _cfg(cmd, cmd + 1)
    v = Analyzer(RemoteSa(cfg), cfg)
    events = []
    v._on_event = lambda level, msg: events.append((level, msg))
    v.start(run=False)
    v.events = events
    assert _wait(lambda: v.backend.health() == ""), v.backend.health()
    yield owner, v
    v.shutdown()
    owner.stop()


def _finish_remote(v, n, limit=100):
    for _ in range(limit):
        st = v.status()
        if st.acq_id == n and not st.acquiring:
            return st
        v.step()
    raise AssertionError("acquisition did not finish")


def _sweeps(owner):
    return [r for r in owner.requests if r.get("cmd") == "tg_sweep_acquire"]


def test_an_owner_that_puts_the_bins_where_asked_gets_a_window(owner_and_sna):
    owner, v = owner_and_sna
    _finish_remote(v, v.take_reference())
    n = v.acquire(window=[40, 60])
    st = _finish_remote(v, n)
    assert st.acq_error == ""
    req = _sweeps(owner)[-1]
    assert (req["start_hz"], req["stop_hz"], req["points"]) == (140e6, 160e6, 21)
    t = v.get_trace("transmission")
    assert t["window"] == [40, 60] and t["transmission"].shape == (101,)
    assert np.isnan(t["transmission"][:40]).all() and np.isnan(t["transmission"][61:]).all()
    np.testing.assert_allclose(t["transmission"][40:61], 0.0, atol=1e-9)   # the fake is flat
    assert st.sample["window_fallback"] == ""


def test_a_shifted_owner_grid_falls_back_to_the_whole_band_and_says_so(owner_and_sna):
    owner, v = owner_and_sna
    owner.grid_shift_hz = 500.0          # every sweep starts 500 Hz off: > 1e-6 of 140 MHz
    _finish_remote(v, v.take_reference())
    n = v.acquire(window=[40, 60])
    st = _finish_remote(v, n)
    assert st.acq_error == ""
    sweeps = _sweeps(owner)
    assert sweeps[-2]["points"] == 21 and sweeps[-1]["points"] == 101   # window, then full
    t = v.get_trace("raw")
    assert t["window"] == [0, 100] and np.isfinite(t["raw"]).all()      # NOT interpolated
    assert "another grid" in t["window_fallback"]
    assert t["window_requested"] == [40, 60]
    assert st.sample["window_fallback"] and st.sample["window"] == [0, 100]
    assert any(level == "warn" and "not on the full grid" in m for level, m in v.events)
    # the whole band still divides by the reference: transmission is there
    assert np.isfinite(v.get_trace("transmission")["transmission"]).all()


# ---- describe and the wire ---------------------------------------------------------

def test_describe_declares_the_window_on_both_trace_detectors(sna):
    params = {p["id"]: p for p in build_manifest(sna)["parameters"]}
    for pid in ("transmission", "raw"):
        assert params[pid]["window"] == {"arg": "window", "unit": "bin", "min_bins": 11}
    for pid in ("peak_transmission", "bw3", "raw_peak"):
        assert "window" not in params[pid]


def test_window_round_trip_over_the_wire():
    cfg = Config()
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz, cfg.sweep.points = 700e6, 1300e6, 201
    v, _sim = build_sim_system(cfg, realtime=False, seed=6)
    svc = ShsnaService(v, host="127.0.0.1", cmd_port=18090, pub_port=18091, status_hz=20.0)
    svc.start()
    cli = ShsnaClient(host="127.0.0.1", cmd_port=18090, pub_port=18091, timeout_ms=2000)
    try:
        time.sleep(0.3)
        cli.set_sim("dut_inserted", False)
        cli.take_reference_blocking(timeout_s=5)
        cli.set_sim("dut_inserted", True)
        t = cli.acquire_blocking(timeout_s=5, which="transmission", window=[90, 110])
        assert t["window"] == [90, 110]
        tx = t["transmission"]
        assert tx.shape == (201,) and np.isnan(tx[:90]).all() and np.isnan(tx[111:]).all()
        assert np.isfinite(tx[90:111]).all()
        local = v.get_trace("transmission")
        np.testing.assert_array_equal(np.isnan(tx), np.isnan(local["transmission"]))
        np.testing.assert_allclose(tx[90:111], local["transmission"][90:111])
        r = cli.get_result()
        assert r["window"] == [90, 110] and r["peak_freq_hz"] == pytest.approx(1e9, abs=6e6)
        # the raw wire reply: null outside the window, not NaN tokens
        reply = cli._cmd({"cmd": "get_trace", "which": "raw", "source": "sample"})
        assert reply["raw"][0] is None and reply["raw"][100] is not None
        assert reply["window"] == [90, 110]
        d = {p["id"]: p for p in cli.describe()["parameters"]}
        assert d["transmission"]["window"]["min_bins"] == 11
        # a malformed window is an error reply, not a crash
        assert cli._cmd({"cmd": "acquire", "window": [9, 3]})["ok"] is False
    finally:
        cli.shutdown()
        svc.stop()
