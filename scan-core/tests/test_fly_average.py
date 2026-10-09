"""AVERAGING A FLOWN AXIS: a repeat in mode 'average' with a fly axis (2026-10-09).

Lukas: "build the averaging of flown axis". Each repeat flies the rows again;
the file holds, PIXEL BY PIXEL, the combination of the repeats weighted by
the samples each put in the pixel (repeat._pool_fly):

    N   = sum n_i
    M   = sum n_i m_i / N
    std = sqrt( sum n_i (s_i^2 + |m_i - M|^2) / N )

-- exactly the mean and spread of all those samples pooled. Proved here: the
formula against brute force (real, complex, traces, an empty pixel), the
convergence of the mean over 3 noisy repeats (~1/sqrt(3)), zig-zag + lag
correction, VNA traces averaged over repeats, the running mean on the live
plot, and the Scan Builder allowing the combination.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from scan_core import Recipe, run
from scan_core.registry import Gettable, Registry, Settable, StreamSpec, build_sim_registry
from scan_core.repeat import _pool_fly, collapse
from timed_stream import TimedStream, Track


# ─────────────────────────── the formula, brute force ────────────────────────

def _stats(samples):
    """(n, coherent mean, population std) of a list of samples."""
    z = np.asarray(samples)
    if z.size == 0:
        return 0, np.nan, np.nan
    m = z.mean(axis=0)
    return len(z), m, np.sqrt(np.mean(np.abs(z - m) ** 2, axis=0))


def _pooled_by_hand(per_repeat_samples):
    allz = [s for rep in per_repeat_samples for s in rep]
    return _stats(allz)


@pytest.mark.parametrize("cplx", [False, True])
def test_pooling_equals_all_samples_in_one_pixel(cplx):
    rng = np.random.default_rng(5)

    def draw(k):
        v = rng.normal(1.0, 0.3, k)
        return v * np.exp(1j * rng.normal(0, 0.5, k)) if cplx else v
    # 3 repeats x 4 pixels; pixel 2 is EMPTY in repeat 1, pixel 3 in all
    sizes = [[5, 3, 4, 0], [2, 6, 0, 0], [7, 1, 3, 0]]
    samples = [[draw(k) for k in row] for row in sizes]
    shape = (3, 4)
    data = {"d": np.full(shape, np.nan + (1j * np.nan if cplx else 0)),
            "d_n": np.zeros(shape), "d_std": np.full(shape, np.nan)}
    for r in range(3):
        for p in range(4):
            n, m, s = _stats(samples[r][p])
            data["d"][r, p], data["d_n"][r, p], data["d_std"][r, p] = m, n, s
    data["d_n"][1, 3] = np.nan                  # "not flown yet" counts as empty
    out, axes, attrs = {}, {}, {}
    _pool_fly("d", data, 0, {}, Registry(), out, axes, attrs)
    for p in range(3):
        n, m, s = _pooled_by_hand([samples[r][p] for r in range(3)])
        assert out["d_n"][p] == n
        assert np.isclose(out["d"][p], m) and np.isclose(out["d_std"][p], s)
    assert out["d_n"][3] == 0 and np.isnan(out["d"][3]) and np.isnan(out["d_std"][3])
    assert attrs["d"]["fly_stat"] == "mean" and attrs["d_n"]["fly_stat"] == "count"


def test_complex_repeats_average_coherently_and_count_the_spread_between_them():
    """Two repeats that each saw a steady signal, of opposite phase: the pooled
    mean is 0 (coherent), and the pooled spread is the 1 between them."""
    data = {"z": np.array([[1 + 0j], [-1 + 0j]]), "z_n": np.array([[10.0], [10.0]]),
            "z_std": np.array([[0.0], [0.0]])}
    out, _, _ = _pool_fly_out(data, "z")
    assert abs(out["z"][0]) < 1e-12 and np.isclose(out["z_std"][0], 1.0)
    assert out["z_n"][0] == 20


def _pool_fly_out(data, name):
    out, axes, attrs = {}, {}, {}
    _pool_fly(name, data, 0, {}, Registry(), out, axes, attrs)
    return out, axes, attrs


def test_traces_are_pooled_element_wise_with_one_count_per_pixel():
    rng = np.random.default_rng(2)
    reps = [rng.normal(size=(4, 7)) + 1j * rng.normal(size=(4, 7)) for _ in range(2)]
    data = {"s": np.stack([r.mean(axis=0, keepdims=True) for r in reps]),     # (2, 1, 7)
            "s_n": np.array([[4.0], [4.0]]),
            "s_std": np.stack([np.sqrt(np.mean(np.abs(r - r.mean(axis=0)) ** 2, axis=0))[None]
                               for r in reps])}
    out, axes, _ = _pool_fly_out(data, "s")
    n, m, s = _stats(np.concatenate(reps))
    assert out["s"].shape == (1, 7) and out["s_n"].shape == (1,)
    assert out["s_n"][0] == 8
    assert np.allclose(out["s"][0], m) and np.allclose(out["s_std"][0], s)
    assert axes["s_n"] == []


def test_collapse_pools_a_fly_detector_and_averages_a_stepped_one():
    data = {"a": np.array([[1.0, 3.0], [3.0, np.nan]]), "a_n": np.array([[2.0, 1.0], [2.0, 0.0]]),
            "a_std": np.array([[0.0, 0.0], [0.0, np.nan]])}
    out, axes, attrs = collapse(data, 0, {"a"}, {}, Registry())
    assert np.allclose(out["a"], [2.0, 3.0]) and out["a_n"].tolist() == [4.0, 1.0]
    assert np.isclose(out["a_std"][0], 1.0)
    assert set(out) == {"a", "a_n", "a_std"}


# ───────────────────── the engine: noisy rows, 3 repeats ──────────────────────

SIGMA = 1.0


def _noisy_rig(seed=0):
    """A stage (Track) and a detector of pure noise around 5.0 sampled on its own
    clock: the truth of every pixel is 5.0, so what the mean does is visible."""
    track = Track()
    state = {"speed": 20.0}
    rng = np.random.default_rng(seed)
    lock = threading.Lock()

    def move(v, timeout_s=None):
        t1 = track.move(v, state["speed"])
        while time.time() < t1:
            time.sleep(0.002)

    def noise(t):
        with lock:
            return {"d": 5.0 + SIGMA * float(rng.standard_normal())}

    reg = Registry()
    reg.add(Settable("x", "X", "um", (-100, 100), move, lambda: track.pos()))
    reg.add(Settable("speed", "Speed", "um/s", (0.1, 500),
                     lambda v: state.__setitem__("speed", v), lambda: state["speed"]))
    pos = TimedStream(lambda t: {"x": track.pos(t)}, 500.0)
    reg.get("x").stream = StreamSpec("stage", pos.start, pos.read, pos.stop)
    reg.get("x").stream_channel = "x"
    det = TimedStream(noise, 200.0)
    g = reg.add(Gettable("d", "Noise", "V", lambda: 5.0))
    g.stream, g.stream_channel = StreamSpec("det", det.start, det.read, det.stop), "d"
    return reg


def _row(num_repeats=None, mode="average", zigzag=False):
    axes = [{"type": "fly", "param": "x", "start": 0.0, "stop": 20.0, "num": 41,
             "speed": 20.0, "speed_param": "speed"}]
    if num_repeats:
        axes.insert(0, {"type": "repeat", "num": num_repeats, "mode": mode})
    return Recipe(name="noise", axes=axes, detectors=["d"], zigzag=zigzag)


def test_three_repeats_of_a_noisy_row_converge_like_one_over_sqrt3():
    one = run(_row(), _noisy_rig(1))
    three = run(_row(3, zigzag=True), _noisy_rig(2))
    assert "repeat" not in three.dims and three.attrs["repeat_averaged"] == "repeat"
    inner = slice(1, -1)
    n1, n3 = one["d_n"].values[inner], three["d_n"].values[inner]
    # about three times the samples per pixel (every repeat flew the row)
    assert np.median(n3) == pytest.approx(3 * np.median(n1), rel=0.2)
    # the mean's scatter around the truth shrinks like 1/sqrt(3)
    e1 = np.sqrt(np.mean((one["d"].values[inner] - 5.0) ** 2))
    e3 = np.sqrt(np.mean((three["d"].values[inner] - 5.0) ** 2))
    assert 0.4 < e3 / e1 < 0.8, (e1, e3)
    # and each is what n samples of sigma 1 promise: std of the mean = sigma/sqrt(n)
    assert e3 == pytest.approx(SIGMA / np.sqrt(np.median(n3)), rel=0.35)
    # the pooled spread is the noise itself (no drift between repeats here)
    assert np.median(three["d_std"].values[inner]) == pytest.approx(SIGMA, rel=0.15)
    assert three["d_n"].attrs["fly_stat"] == "count"


def test_the_live_plot_shows_the_running_mean_without_the_repeat_dim():
    seen = []

    def on_point(done, total, snapshot):
        if done % 41 == 0:                     # a row (= a repeat) is finished
            ds = snapshot()
            seen.append((done, ds["d_n"].values.copy(), ds.dims))
    ds = run(_row(3), _noisy_rig(3), on_point=on_point)
    assert len(seen) == 3
    for done, n, dims in seen:
        assert "repeat" not in dims
    # the count per pixel grows repeat by repeat: the running mean
    med = [np.median(n[1:-1]) for _, n, _ in seen]
    assert med[0] < med[1] < med[2]
    assert np.allclose(seen[-1][1], ds["d_n"].values)


def test_an_aborted_average_keeps_the_repeats_flown_so_far():
    stop = {"n": 0}

    def on_point(done, total, snapshot):
        stop["n"] = done
    ds = run(_row(3), _noisy_rig(4), on_point=on_point,
             should_abort=lambda: stop["n"] >= 41)
    # one repeat in: every inner pixel has the samples of that one repeat
    assert np.all(ds["d_n"].values[1:-1] > 0)
    assert np.isfinite(ds["d"].values[1:-1]).all()


# ─────────────────────── the simulator: zig-zag, lag, traces ──────────────────

def test_zigzag_and_lag_corrected_rows_average_on_the_simulator():
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.01                 # 20 ms group delay, corrected per row
    r = Recipe(name="avg", fixed={"pos_x": 30.0, "pos_y": 50.0}, detectors=["lockin_r"],
               zigzag=True,
               axes=[{"type": "repeat", "num": 2, "mode": "average"},
                     {"type": "fly", "param": "field", "start": 30.0, "stop": 70.0,
                      "num": 21, "speed": 20.0}])
    assert r.validate(reg) == []
    ds = run(r, reg)
    keep = run(Recipe.from_dict({**r.to_dict(),
                                 "axes": [{"type": "repeat", "num": 2, "mode": "keep"},
                                          r.axes[1]]}), reg)
    # the two kept runs (one forward, one backward) put the line in the same
    # place, and so does their pooled mean
    f = ds["field"].values

    def centroid(y):
        y = np.nan_to_num(y - np.nanmin(y))
        return float(np.sum(f * y) / np.sum(y))
    fwd, bwd = keep["lockin_r"].values
    assert abs(centroid(fwd) - centroid(bwd)) < 0.6
    assert abs(centroid(ds["lockin_r"].values) - centroid(fwd)) < 0.6
    assert np.all(ds["lockin_r_n"].values[1:-1] >= 4)


def test_vna_traces_are_averaged_over_repeats():
    reg = build_sim_registry()
    st = reg._state
    r = Recipe(name="vna_avg", fixed={"pos_x": 30.0, "pos_y": 50.0}, detectors=["s21"],
               zigzag=True,
               axes=[{"type": "repeat", "num": 2, "mode": "average"},
                     {"type": "fly", "param": "field", "start": 40.0, "stop": 60.0,
                      "num": 6, "speed": 10.0}])
    assert r.validate(reg) == []
    ds = run(r, reg)
    assert ds["s21_real"].dims == ("field", "vna_freq")
    assert ds["s21_std"].dims == ("field", "vna_freq") and ds["s21_n"].dims == ("field",)
    s = ds["s21_real"].values + 1j * ds["s21_imag"].values
    f = ds["vna_freq"].values
    bin_hz = f[1] - f[0]
    dip = f[np.argmin(np.abs(s), axis=1)]
    truth = np.array([float(st._f_res(b)) * 1e6 for b in ds["field"].values])
    assert np.all(np.abs(dip - truth) <= 2 * bin_hz)
    assert np.all(ds["s21_n"].values >= 6)            # two repeats' worth of sweeps


# ─────────────────────────────── the builder ──────────────────────────────────

def test_the_builder_allows_average_with_a_fly_axis():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtCore, QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    reg = build_sim_registry()
    win = ScanBuilder(reg)
    try:
        for it in win._det_items():
            it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole)
                             == "lockin_r" else QtCore.Qt.Unchecked)
        win.add_axis("pos_x")
        row = win.rows[-1]
        row.start.setValue(-10.0)
        row.stop.setValue(10.0)
        row.num.setValue(21)
        win.open_advanced(row)
        row.fly.setChecked(True)
        row.speed.setValue(40.0)
        win.add_repeat(num=3, mode="average", index=0)
        recipe = win.build_recipe()
        assert recipe.axes[0]["mode"] == "average" and recipe.axes[1]["type"] == "fly"
        assert recipe.validate(reg) == []
        assert "average of 3" in win.summary.text()
    finally:
        win.close()
