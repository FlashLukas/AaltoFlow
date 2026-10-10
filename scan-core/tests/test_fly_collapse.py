"""ONE MEAN PER ROW of a fly axis: `collapse: mean` (2026-10-10).

Lukas approved "collapsing a flown axis into one mean per row". The file then
ALSO holds, per row, <det>_rowmean / _rowmean_n / _rowmean_std without the
fly dimension, the pixels of the row pooled weighted by their SAMPLES
(flyscan.pool_bins -- the formula that pools repeats):

    N   = sum_p n_p
    M   = sum_p n_p m_p / N
    std = sqrt( sum_p n_p (s_p^2 + |m_p - M|^2) / N )

Proved here: the row mean is the mean of every sample of the row (brute
force), NOT the mean of the pixel means when the counts differ; traces give
one mean trace per row; repeats are pooled first, then the row (= all at
once); `collapse_keep_pixels: false` drops the pixel variables from the file
but not from the live plot; the attributes on the coordinate and in the
header survive a netCDF round trip; and the Scan Builder writes, reloads and
tags the two options.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, run                                       # noqa: E402
from scan_core.data import as_complex                                   # noqa: E402
from scan_core.flyscan import collapse_rows, pool_bins                  # noqa: E402
from scan_core.registry import Registry, build_sim_registry             # noqa: E402
from scan_core.repeat import collapse                                   # noqa: E402
from test_fly_average import _noisy_rig, _stats                         # noqa: E402


def _binned(samples, cplx=False):
    """Pixel statistics (m, n, s) from a list of per-pixel sample arrays, as
    bin_samples gives them (population std)."""
    m = np.array([_stats(s)[1] for s in samples], dtype=complex if cplx else float)
    n = np.array([float(len(s)) for s in samples])
    s = np.array([_stats(s)[2] for s in samples])
    return m, n, s


# ─────────────────────────── the formula, brute force ────────────────────────

@pytest.mark.parametrize("cplx", [False, True])
def test_row_mean_is_the_mean_of_every_sample_not_of_the_pixel_means(cplx):
    rng = np.random.default_rng(11)
    # two rows of 5 pixels, VERY different counts per pixel and a gradient
    # along the row: the weighted and the unweighted mean differ clearly
    sizes = [[1, 2, 20, 3, 0], [8, 1, 1, 1, 30]]

    def draw(p, k):
        v = p + rng.normal(0, 0.2, k)
        return v * np.exp(1j * 0.3 * p) if cplx else v
    samples = [[draw(p, k) for p, k in enumerate(row)] for row in sizes]
    m, n, s = zip(*[_binned(row, cplx) for row in samples])
    data = {"d": np.stack(m), "d_n": np.stack(n), "d_std": np.stack(s)}
    out, axes, attrs, row_vars = collapse_rows(data, 1, ["d"], {}, Registry())
    for r in range(2):
        allz = np.concatenate(samples[r])
        nn, mm, ss = _stats(allz)
        assert out["d_rowmean_n"][r] == nn
        assert np.isclose(out["d_rowmean"][r], mm)
        assert np.isclose(out["d_rowmean_std"][r], ss)
        # ... and NOT the plain mean of the pixel means
        assert not np.isclose(out["d_rowmean"][r], np.nanmean(data["d"][r]))
    assert row_vars == {"d_rowmean", "d_rowmean_n", "d_rowmean_std"}
    assert set(out) >= {"d", "d_n", "d_std"}          # the pixels stay by default
    assert attrs["d_rowmean_n"]["fly_stat"] == "count"
    assert attrs["d_rowmean"]["fly_collapse"] == "mean"


def test_a_row_not_flown_yet_has_no_mean_and_keep_pixels_false_drops_the_pixels():
    data = {"d": np.array([[1.0, 3.0], [np.nan, np.nan]]),
            "d_n": np.array([[1.0, 3.0], [np.nan, np.nan]]),
            "d_std": np.array([[0.0, 0.0], [np.nan, np.nan]])}
    out, axes, _, _ = collapse_rows(data, 1, ["d"], {}, Registry(), keep_pixels=False)
    assert set(out) == {"d_rowmean", "d_rowmean_n", "d_rowmean_std"}
    assert out["d_rowmean"][0] == pytest.approx(2.5)        # (1*1 + 3*3) / 4
    assert out["d_rowmean_n"].tolist() == [4.0, 0.0]
    assert np.isnan(out["d_rowmean"][1]) and np.isnan(out["d_rowmean_std"][1])


def test_a_trace_gets_one_mean_trace_per_row_and_one_count():
    rng = np.random.default_rng(3)
    counts = [2, 7, 4]
    traces = [rng.normal(size=(k, 5)) + 1j * rng.normal(size=(k, 5)) for k in counts]
    m = np.stack([t.mean(axis=0) for t in traces])[None]                 # (1, 3, 5)
    s = np.stack([np.sqrt(np.mean(np.abs(t - t.mean(axis=0)) ** 2, axis=0))
                  for t in traces])[None]
    data = {"s": m, "s_n": np.array([counts], dtype=float), "s_std": s}
    axes_in = {"s": ["FREQ"], "s_std": ["FREQ"]}
    out, axes, _, _ = collapse_rows(data, 1, ["s"], axes_in, Registry())
    n, mm, ss = _stats(np.concatenate(traces))
    assert out["s_rowmean"].shape == (1, 5) and out["s_rowmean_n"].shape == (1,)
    assert out["s_rowmean_n"][0] == n == sum(counts)
    assert np.allclose(out["s_rowmean"][0], mm) and np.allclose(out["s_rowmean_std"][0], ss)
    assert axes["s_rowmean"] == ["FREQ"] and axes["s_rowmean_n"] == []


def test_repeats_first_then_the_row_is_the_same_as_everything_at_once():
    rng = np.random.default_rng(8)
    sizes = [[3, 0, 5], [1, 6, 2]]                    # 2 repeats x 3 pixels
    samples = [[rng.normal(p, 0.5, k) for p, k in enumerate(rep)] for rep in sizes]
    m, n, s = zip(*[_binned(rep) for rep in samples])
    data = {"d": np.stack(m), "d_n": np.stack(n), "d_std": np.stack(s)}
    pooled, axes, _ = collapse(data, 0, {"d"}, {}, Registry())       # the repeats
    out, _, _, _ = collapse_rows(pooled, 0, ["d"], axes, Registry())  # then the row
    nn, mm, ss = _stats(np.concatenate([x for rep in samples for x in rep]))
    assert out["d_rowmean_n"] == nn
    assert np.isclose(out["d_rowmean"], mm) and np.isclose(out["d_rowmean_std"], ss)


def test_pool_bins_is_the_shared_formula():
    m, n, s = pool_bins(np.array([1.0, 3.0]), np.array([1.0, 3.0]), np.array([0.0, 0.0]), 0)
    assert m == pytest.approx(2.5) and n == 4.0
    assert s == pytest.approx(np.sqrt((1 * 1.5 ** 2 + 3 * 0.5 ** 2) / 4))


# ───────────────────────────────── the engine ─────────────────────────────────

def _rows(collapse="mean", keep=None, repeat=None):
    fly = {"type": "fly", "param": "x", "start": 0.0, "stop": 20.0, "num": 21,
           "speed": 20.0, "speed_param": "speed"}
    if collapse:
        fly["collapse"] = collapse
    if keep is not None:
        fly["collapse_keep_pixels"] = keep
    axes = [{"type": "repeat", "num": 2, "mode": "keep", "name": "row"}, fly]
    if repeat:
        axes.insert(0, {"type": "repeat", "num": repeat, "mode": "average"})
    return Recipe(name="rows", axes=axes, detectors=["d"], zigzag=True)


def test_engine_row_mean_next_to_the_pixels_and_in_the_file(tmp_path):
    ds = run(_rows(), _noisy_rig(5))
    assert ds["d"].dims == ("row", "x") and ds["d_rowmean"].dims == ("row",)
    assert ds["d_rowmean_n"].dims == ("row",) and ds["d_rowmean_std"].dims == ("row",)
    n = ds["d_n"].values
    assert np.array_equal(ds["d_rowmean_n"].values, n.sum(axis=1))
    weighted = np.nansum(ds["d"].values * n, axis=1) / n.sum(axis=1)
    assert np.allclose(ds["d_rowmean"].values, weighted)
    # pure noise around 5.0: the row mean is 5 to within sigma / sqrt(N)
    assert np.all(np.abs(ds["d_rowmean"].values - 5.0)
                  < 5 / np.sqrt(ds["d_rowmean_n"].values))
    a = ds["x"].attrs
    assert a["fly_collapse"] == "mean" and a["fly_collapse_keep_pixels"] == 1
    assert ds.attrs["fly_row_mean"] == "x" and ds.attrs["fly_pixels_kept"] == 1
    assert ds["d_rowmean"].attrs["units"] == "V"
    assert ds["d_rowmean_n"].attrs["fly_stat"] == "count"
    path = tmp_path / "rows.nc"
    ds.to_netcdf(path)
    with xr.open_dataset(path) as back:
        assert back["x"].attrs["fly_collapse"] == "mean"
        assert int(back["x"].attrs["fly_collapse_keep_pixels"]) == 1
        assert back["d_rowmean_n"].encoding["dtype"] == np.uint32
        assert np.allclose(back["d_rowmean"].values, ds["d_rowmean"].values)


def test_an_ordinary_fly_scan_has_no_row_means_and_no_new_attrs():
    ds = run(_rows(collapse=None), _noisy_rig(6))
    assert not any(k.endswith("_rowmean") for k in ds.data_vars)
    assert "fly_collapse" not in ds["x"].attrs and "fly_row_mean" not in ds.attrs


def test_keep_pixels_false_drops_them_from_the_file_but_not_from_the_live_plot():
    live = []

    def on_point(done, total, snapshot):
        live.append(snapshot())
    ds = run(_rows(keep=False), _noisy_rig(7), on_point=on_point)
    assert set(ds.data_vars) == {"d_rowmean", "d_rowmean_n", "d_rowmean_std"}
    assert "x" in ds.coords                  # the grid and its settings stay
    assert ds["x"].attrs["fly_collapse_keep_pixels"] == 0
    assert ds.attrs["fly_pixels_kept"] == 0
    assert live and all("d" in snap.data_vars for snap in live)
    assert live[-1]["d"].dims == ("row", "x")
    assert np.all(np.isfinite(ds["d_rowmean"].values))


def test_an_aborted_scan_keeps_the_row_means_of_the_rows_flown():
    stop = {"n": 0}

    def on_point(done, total, snapshot):
        stop["n"] = done
    ds = run(_rows(keep=False), _noisy_rig(9), on_point=on_point,
             should_abort=lambda: stop["n"] >= 21)
    rm = ds["d_rowmean"].values
    assert np.isfinite(rm[0]) and np.isnan(rm[1])


def test_repeat_average_then_row_mean_in_the_engine():
    ds = run(_rows(repeat=2), _noisy_rig(10))
    assert "repeat" not in ds.dims and ds["d_rowmean"].dims == ("row",)
    n = ds["d_n"].values                     # already pooled over the 2 repeats
    assert np.array_equal(ds["d_rowmean_n"].values, n.sum(axis=1))
    m, cnt, s = pool_bins(ds["d"].values, n, ds["d_std"].values, 1)
    assert np.allclose(ds["d_rowmean"].values, m)
    assert np.allclose(ds["d_rowmean_std"].values, s)


def test_vna_trace_row_mean_per_frequency_with_zigzag():
    reg = build_sim_registry()
    r = Recipe(name="vna_rows", fixed={"pos_x": 30.0, "pos_y": 50.0}, detectors=["s21"],
               zigzag=True,
               axes=[{"type": "repeat", "num": 2, "mode": "keep", "name": "row"},
                     {"type": "fly", "param": "field", "start": 40.0, "stop": 60.0,
                      "num": 6, "speed": 10.0, "collapse": "mean"}])
    assert r.validate(reg) == []
    ds = run(r, reg)
    assert ds["s21_rowmean_real"].dims == ("row", "vna_freq")
    assert ds["s21_rowmean_n"].dims == ("row",)
    assert ds["s21_rowmean_real"].attrs["complex_pair"] == "s21_rowmean"
    z = as_complex(ds, "s21_rowmean")
    pix = as_complex(ds, "s21").values
    n = ds["s21_n"].values
    expect = np.nansum(pix * n[..., None], axis=1) / n.sum(axis=1)[:, None]
    assert np.allclose(z.values, expect)


def test_lag_corrected_sim_rows_have_a_row_mean():
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.01
    r = Recipe(name="lag", fixed={"pos_x": 30.0, "pos_y": 50.0}, detectors=["lockin_r"],
               zigzag=True,
               axes=[{"type": "repeat", "num": 2, "mode": "keep", "name": "row"},
                     {"type": "fly", "param": "field", "start": 30.0, "stop": 70.0,
                      "num": 11, "speed": 20.0, "collapse": "mean"}])
    ds = run(r, reg)
    rm = ds["lockin_r_rowmean"].values
    assert np.all(np.isfinite(rm))
    # forward and backward rows see the same line: their row means agree
    assert abs(rm[0] - rm[1]) < 0.2 * abs(rm).max()


def test_validation_of_the_two_options():
    reg = _noisy_rig(0)
    bad = _rows(collapse="median")
    assert any("collapse must be" in e for e in bad.validate(reg))
    lone = _rows(collapse=None, keep=False)
    assert any("needs collapse: mean" in e for e in lone.validate(reg))
    odd = _rows(keep="no")
    assert any("true or false" in e for e in odd.validate(reg))
    assert _rows(keep=False).validate(reg) == []


# ───────────────────────────────── the builder ────────────────────────────────

@pytest.fixture
def builder():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtCore, QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.004
    win = ScanBuilder(reg)
    for it in win._det_items():
        it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole)
                         == "lockin_r" else QtCore.Qt.Unchecked)
    yield win
    win.close()


def _fly_row(builder):
    builder.add_axis("pos_y")
    outer = builder.rows[-1]
    outer.start.setValue(0.0); outer.stop.setValue(10.0); outer.num.setValue(2)
    builder.add_axis("pos_x")
    row = builder.rows[-1]
    row.start.setValue(-10.0); row.stop.setValue(10.0); row.num.setValue(21)
    builder.open_advanced(row)
    row.fly.setChecked(True)
    row.speed.setValue(60.0)
    return row


def test_builder_round_trip_tag_and_eta(builder):
    row = _fly_row(builder)
    assert row.collapse_box.isEnabled() and not row.keep_box.isEnabled()
    eta_before = builder.detail.text()
    row.collapse_box.setChecked(True)
    assert row.keep_box.isEnabled()
    assert "row mean" in row.tag_texts()
    ax = builder.build_recipe().axes[-1]
    assert ax["collapse"] == "mean" and "collapse_keep_pixels" not in ax
    row.keep_box.setChecked(False)
    assert "row mean only" in row.tag_texts()
    recipe = builder.build_recipe()
    assert recipe.axes[-1]["collapse_keep_pixels"] is False
    assert recipe.validate(builder.registry) == []
    # the rows are flown exactly as before: the estimate does not move
    assert builder.detail.text() == eta_before
    builder.load_recipe(recipe.__class__(name="other", axes=[]))
    assert builder.load_recipe(recipe) == []
    back = builder.rows[-1]
    assert back.collapse_box.isChecked() and not back.keep_box.isChecked()
    assert builder.build_recipe().axes == recipe.axes
    # reset: back to plain stepping, the options off again
    back.reset_advanced()
    assert not back.collapse_box.isChecked() and back.keep_box.isChecked()
    assert "collapse" not in builder.build_recipe().axes[-1]


def test_builder_run_keeps_the_live_pixel_map_and_saves_the_row_means(builder):
    row = _fly_row(builder)
    row.collapse_box.setChecked(True)
    row.keep_box.setChecked(False)
    builder.per_pt.setValue(0.0)
    builder.run_scan(block=True)
    ds = builder.dataset
    assert "lockin_r" not in ds.data_vars
    assert ds["lockin_r_rowmean"].dims == ("pos_y",)
    assert np.all(np.isfinite(ds["lockin_r_rowmean"].values))
