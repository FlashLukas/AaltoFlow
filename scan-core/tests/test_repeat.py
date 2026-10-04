"""The REPEAT axis (scan_core/repeat.py): do everything inside it N times.

mode keep    -> a real `repeat` dimension, every run in the file
mode average -> <det> (mean), <det>_std, <det>_n, the dimension collapsed

Every test is offline: a tiny registry of fake parameters whose readings are
KNOWN (a counter, a seeded random sequence), so a shape or a mean can be
checked number for number, plus the simulator for the typed/array detectors.
"""

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import build_sim_registry                               # noqa: E402
from scan_core.engine import run                                       # noqa: E402
from scan_core.recipe import Recipe                                    # noqa: E402
from scan_core.registry import AxisSpec, Gettable, Registry, Settable  # noqa: E402
from scan_core.storage import Storage                                  # noqa: E402


# ───────────────────────────── a known registry ───────────────────────────────

class Fake:
    """Two settables (a, b) that log every set, and detectors with known values:
    `count` = 0, 1, 2, ... in reading order; `noise` = a seeded normal
    sequence; `holey` = the counter, but NaN on every third reading."""

    def __init__(self, seed=1):
        self.sets = []
        self.n = 0
        self.rng = np.random.default_rng(seed)
        self.times = []
        self.reg = Registry()
        for pid in ("a", "b"):
            self.reg.add(Settable(pid, pid.upper(), "V", (-100, 100),
                                  set_fn=lambda v, p=pid: self.sets.append((p, v)),
                                  get_fn=lambda: 0.0))
        self.reg.add(Gettable("count", "Count", "", self._count))
        self.reg.add(Gettable("noise", "Noise", "V", lambda: float(self.rng.normal())))
        self.reg.add(Gettable("holey", "Holey", "V", self._holey))
        self.reg.add(Gettable("state", "State", "", lambda: "ok", dtype="enum",
                              storage=Storage("enum", options=["ok", "bad"])))
        self.reg.add(Gettable("flag", "Flag", "", lambda: bool(self.rng.random() > 0.5),
                              dtype="bool", storage=Storage("bool")))
        self.reg.add(Gettable("level", "Level", "", lambda: int(self.rng.integers(0, 10)),
                              dtype="int", storage=Storage("int", lo=0, hi=9)))
        # a complex TRACE of 3 points: element-wise, coherent average
        ax = AxisSpec("f", "Frequency", "Hz", values_fn=lambda: np.array([1.0, 2.0, 3.0]))
        self.reg.add(Gettable("trace", "Trace", "", self._trace, axes=[ax],
                              dtype="complex"))

    def _count(self):
        self.times.append(time.monotonic())
        v = float(self.n)
        self.n += 1
        return v

    def _holey(self):
        v = self._count()
        return float("nan") if int(v) % 3 == 2 else v

    def _trace(self):
        z = self.rng.normal(size=3) + 1j * self.rng.normal(size=3)
        return z + np.array([1, 2, 3])


def lin(pid, num, start=0.0, stop=None):
    return {"type": "linear", "param": pid, "start": start,
            "stop": float(num - 1) if stop is None else stop, "num": num}


def rep(num, mode=None, **kw):
    ax = {"type": "repeat", "num": num}
    if mode:
        ax["mode"] = mode
    ax.update(kw)
    return ax


# ───────────────────────────── keep ───────────────────────────────────────────

def test_keep_outermost_repeats_whole_scans():
    f = Fake()
    ds = run(Recipe(axes=[rep(3), lin("a", 4)], detectors=["count"]), f.reg)
    assert ds["count"].dims == ("repeat", "a")
    assert ds["count"].shape == (3, 4)
    np.testing.assert_array_equal(ds["count"].values, np.arange(12).reshape(3, 4))
    np.testing.assert_array_equal(ds["repeat"].values, [0, 1, 2])
    assert ds["repeat"].attrs["repeat_mode"] == "keep"
    # the whole sweep of a, three times over
    assert [v for p, v in f.sets if p == "a"] == [0.0, 1.0, 2.0, 3.0] * 3


def test_keep_middle_repeats_each_sweep():
    f = Fake()
    ds = run(Recipe(axes=[lin("a", 2), rep(3), lin("b", 2)], detectors=["count"]),
             f.reg)
    assert ds["count"].dims == ("a", "repeat", "b")
    np.testing.assert_array_equal(ds["count"].values, np.arange(12).reshape(2, 3, 2))
    # a is set once per value, not once per repeat (the odometer sets only
    # what changed), b once per point
    assert [v for p, v in f.sets if p == "a"] == [0.0, 1.0]
    assert len([1 for p, _ in f.sets if p == "b"]) == 12


def test_keep_innermost_repeats_each_point_without_moving():
    f = Fake()
    ds = run(Recipe(axes=[lin("a", 4), rep(3)], detectors=["count"]), f.reg)
    assert ds["count"].dims == ("a", "repeat")
    np.testing.assert_array_equal(ds["count"].values, np.arange(12).reshape(4, 3))
    assert [v for p, v in f.sets if p == "a"] == [0.0, 1.0, 2.0, 3.0]


def test_several_repeats_are_numbered_and_a_name_wins():
    f = Fake()
    r = Recipe(axes=[rep(2), lin("a", 2), rep(3)], detectors=["count"])
    assert [d.name for d in r.compile(f.reg).dims] == ["repeat_1", "a", "repeat_2"]
    r = Recipe(axes=[rep(2, name="run"), lin("a", 2)], detectors=["count"])
    ds = run(r, f.reg)
    assert ds["count"].dims == ("run", "a")


def test_keep_applies_typed_storage(tmp_path):
    f = Fake()
    ds = run(Recipe(axes=[rep(3), lin("a", 2)], detectors=["flag", "level"]), f.reg)
    p = tmp_path / "keep.nc"
    ds.to_netcdf(p)
    with xr.open_dataset(p) as back:
        assert back["flag"].encoding["dtype"] == np.dtype("uint8")
        assert back["level"].encoding["dtype"] == np.dtype("int8")
        assert back["level"].dims == ("repeat", "a")


def _fired(axes, axis):
    f = Fake()
    fired = []
    from scan_core import hooks
    hooks.ACTIONS["_t_mark"] = lambda ctx, **kw: fired.append(ctx["index"])
    try:
        r = Recipe(axes=axes, detectors=["count"],
                   hooks=[{"when": "each_sweep", "axis": axis, "action": "_t_mark"}])
        assert r.validate(f.reg) == []
        run(r, f.reg)
    finally:
        hooks.ACTIONS.pop("_t_mark", None)
    return fired


def test_each_sweep_routines_see_the_repeat_like_any_axis():
    # "autofocus before every run": each sweep of the axis INSIDE the repeat
    assert _fired([rep(3), lin("a", 2)], "a") == [(0, 0), (1, 0), (2, 0)]
    # a sweep of an innermost repeat = the N repeats of one point
    assert _fired([lin("a", 2), rep(3)], "repeat") == [(0, 0), (1, 0)]


# ───────────────────────────── average ────────────────────────────────────────

def _keep_and_average(axes_keep, axes_avg, dets, seed=7):
    keep = run(Recipe(axes=axes_keep, detectors=dets), Fake(seed).reg)
    avg = run(Recipe(axes=axes_avg, detectors=dets), Fake(seed).reg)
    return keep, avg


@pytest.mark.parametrize("where", [0, 1])
def test_average_matches_numpy_over_the_same_keep_run(where):
    axes = [lin("a", 4), lin("b", 2)]
    keep_axes = list(axes); keep_axes.insert(where, rep(5))
    avg_axes = list(axes); avg_axes.insert(where, rep(5, "average"))
    keep, avg = _keep_and_average(keep_axes, avg_axes, ["noise"])
    x = keep["noise"].values
    assert "repeat" not in avg.dims
    assert avg["noise"].dims == ("a", "b")
    np.testing.assert_allclose(avg["noise"].values, x.mean(axis=where))
    np.testing.assert_allclose(avg["noise_std"].values, x.std(axis=where, ddof=1))
    np.testing.assert_array_equal(avg["noise_n"].values, 5)
    assert avg.attrs["repeat_averaged"] == "repeat" and avg.attrs["repeat_num"] == 5


def test_average_is_nan_aware():
    f = Fake()
    ds = run(Recipe(axes=[lin("a", 3), rep(4, "average")], detectors=["holey"]), f.reg)
    raw = np.arange(12, dtype=float).reshape(3, 4)
    raw[raw % 3 == 2] = np.nan
    np.testing.assert_allclose(ds["holey"].values, np.nanmean(raw, axis=1))
    np.testing.assert_allclose(ds["holey_std"].values, np.nanstd(raw, axis=1, ddof=1))
    np.testing.assert_array_equal(ds["holey_n"].values, np.isfinite(raw).sum(axis=1))


def test_std_is_nan_with_fewer_than_two_values():
    f = Fake()
    ds = run(Recipe(axes=[lin("a", 2), rep(1, "average")], detectors=["count"]), f.reg)
    np.testing.assert_array_equal(ds["count"].values, [0.0, 1.0])
    assert np.all(np.isnan(ds["count_std"].values))
    np.testing.assert_array_equal(ds["count_n"].values, [1, 1])


def test_aborted_average_keeps_the_partial_mean_and_says_how_many():
    f = Fake()
    # outermost average: 3 runs of a 4-point sweep; stop after 6 points =
    # one full run and half of the second
    r = Recipe(axes=[rep(3, "average"), lin("a", 4)], detectors=["count"])
    ds = run(r, f.reg, should_abort=lambda: f.n >= 6)
    np.testing.assert_allclose(ds["count"].values, [2.0, 3.0, 2.0, 3.0])
    np.testing.assert_array_equal(ds["count_n"].values, [2, 2, 1, 1])


def test_live_snapshot_shows_the_running_mean():
    f = Fake()
    seen = []
    r = Recipe(axes=[rep(3, "average"), lin("a", 2)], detectors=["count"])
    run(r, f.reg, on_point=lambda done, total, snap: seen.append(snap()))
    assert "repeat" not in seen[0].dims
    np.testing.assert_allclose(seen[1]["count"].values, [0.0, 1.0])     # run 1 done
    np.testing.assert_allclose(seen[3]["count"].values, [1.0, 2.0])     # mean of 2
    np.testing.assert_array_equal(seen[3]["count_n"].values, [2, 2])


def test_average_of_typed_and_complex_array_detectors(tmp_path):
    keep, avg = _keep_and_average([lin("a", 2), rep(4)],
                                  [lin("a", 2), rep(4, "average")],
                                  ["flag", "level", "trace"])
    # bool -> fraction of True, int -> float mean
    np.testing.assert_allclose(avg["flag"].values, keep["flag"].values.mean(axis=1))
    np.testing.assert_allclose(avg["level"].values, keep["level"].values.mean(axis=1))
    # complex trace: element-wise COHERENT mean, std of |z|
    z = keep["trace_real"].values + 1j * keep["trace_imag"].values      # (a, repeat, f)
    assert avg["trace_real"].dims == ("a", "f")
    np.testing.assert_allclose(avg["trace_real"].values + 1j * avg["trace_imag"].values,
                               z.mean(axis=1))
    np.testing.assert_allclose(avg["trace_std"].values, np.abs(z).std(axis=1, ddof=1))
    assert avg["trace_std"].dims == ("a", "f")
    p = tmp_path / "avg.nc"
    avg.to_netcdf(p)
    with xr.open_dataset(p) as back:
        # a mean of ints is not an int: float64, the declared type kept
        assert back["level"].encoding["dtype"] == np.dtype("float64")
        assert back["level"].attrs["declared_type"] == "int"
        assert back["flag"].attrs["declared_type"] == "bool"
        assert back["level_n"].encoding["dtype"] == np.dtype("uint32")
        assert back["level_std"].encoding["dtype"] == np.dtype("float64")
        assert back["level_std"].attrs["repeat_stat"] == "std"


def test_the_viewer_averaging_a_keep_repeat_gives_the_same_mean():
    from scan_core.view import Slice, reduce_cube
    keep, avg = _keep_and_average([rep(5), lin("a", 4)],
                                  [rep(5, "average"), lin("a", 4)], ["noise"])
    red = reduce_cube(keep["noise"], "a", None, {"repeat": Slice("avg", 0, None)})
    np.testing.assert_allclose(red.data.values, avg["noise"].values)
    assert red.averaged == 5


# ───────────────────────────── validation ─────────────────────────────────────

@pytest.mark.parametrize("num", [0, -2, 2.5, "five", None, True])
def test_num_must_be_a_whole_number_of_at_least_one(num):
    f = Fake()
    errs = Recipe(axes=[rep(num), lin("a", 2)], detectors=["count"]).validate(f.reg)
    assert any("whole number >= 1" in e for e in errs)


def test_mode_and_interval_are_checked():
    f = Fake()
    errs = Recipe(axes=[rep(2, "median")], detectors=["count"]).validate(f.reg)
    assert any("'keep' or 'average'" in e for e in errs)
    errs = Recipe(axes=[rep(2, interval_s=-1)], detectors=["count"]).validate(f.reg)
    assert any("interval_s" in e for e in errs)


def test_only_one_average_repeat():
    f = Fake()
    errs = Recipe(axes=[rep(2, "average"), lin("a", 2), rep(3, "average")],
                  detectors=["count"]).validate(f.reg)
    assert any("only one repeat axis" in e for e in errs)
    assert Recipe(axes=[rep(2), lin("a", 2), rep(3, "average")],
                  detectors=["count"]).validate(f.reg) == []


def test_enum_detector_under_average_is_refused_but_keep_records_it():
    f = Fake()
    errs = Recipe(axes=[lin("a", 2), rep(3, "average")],
                  detectors=["count", "state"]).validate(f.reg)
    assert any("'state' is enum" in e for e in errs)
    assert Recipe(axes=[lin("a", 2), rep(3)],
                  detectors=["count", "state"]).validate(f.reg) == []


def test_repeat_inside_a_fly_axis_is_refused_outside_allowed():
    reg = build_sim_registry()
    fly = {"type": "fly", "param": "pos_x", "start": -5, "stop": 5, "num": 11,
           "speed": 50, "speed_param": "stage_speed"}
    errs = Recipe(axes=[fly, rep(3)], detectors=["lockin_r"]).validate(reg)
    assert any("cannot sit inside a fly axis" in e for e in errs)
    assert Recipe(axes=[rep(2), fly], detectors=["lockin_r"]).validate(reg) == []
    errs = Recipe(axes=[rep(2, "average"), fly], detectors=["lockin_r"]).validate(reg)
    assert any("fly axis" in e and "average" in e for e in errs)


def test_keep_repeat_outside_a_fly_axis_runs():
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.004
    fly = {"type": "fly", "param": "pos_x", "start": -5, "stop": 5, "num": 11,
           "speed": 200, "speed_param": "stage_speed"}
    ds = run(Recipe(axes=[rep(2), fly], detectors=["lockin_r"]), reg)
    assert ds["lockin_r"].dims == ("repeat", "pos_x")
    assert ds["lockin_r_n"].shape == (2, 11)


# ───────────────────────────── interval ───────────────────────────────────────

def test_interval_paces_the_start_of_each_repeat():
    f = Fake()
    dt = 0.08
    r = Recipe(axes=[rep(4, interval_s=dt), lin("a", 2)], detectors=["count"])
    run(r, f.reg)
    starts = np.array(f.times[::2])            # first reading of each repeat
    since = starts - starts[0]
    for k in range(1, 4):
        assert since[k] >= k * dt - 0.005, (k, since)
    # and not much more than asked (the sweep itself is instant here)
    assert since[-1] < 3 * dt + 0.5


def test_interval_restarts_with_each_pass_and_can_be_aborted():
    f = Fake()
    r = Recipe(axes=[lin("a", 2), rep(2, interval_s=0.05)], detectors=["count"])
    t0 = time.monotonic()
    run(r, f.reg)
    # two passes x one wait each, not a wait growing across the passes
    assert 0.09 <= time.monotonic() - t0 < 0.6
    f = Fake()
    r = Recipe(axes=[rep(3, interval_s=30), lin("a", 1)], detectors=["count"])
    t0 = time.monotonic()
    from scan_core.errors import ScanAborted
    with pytest.raises(ScanAborted) as info:
        run(r, f.reg, should_abort=lambda: time.monotonic() - t0 > 0.2)
    assert time.monotonic() - t0 < 5          # not the 30 s interval
    # the first run was measured and is kept
    assert info.value.dataset["count"].values[0, 0] == 0.0


# ───────────────────────────── round trips ────────────────────────────────────

def test_yaml_and_nc_round_trip(tmp_path):
    f = Fake()
    r = Recipe(name="rep", axes=[lin("a", 2), rep(3, "average", interval_s=0.0),
                                 ], detectors=["count"])
    r.axes.insert(0, rep(2, name="run", interval_s=0.01))
    y = tmp_path / "r.yaml"
    r.save(y)
    back = Recipe.load(y)
    assert back.axes == r.axes
    ds = run(back, f.reg)
    p = tmp_path / "r.nc"
    ds.to_netcdf(p)
    with xr.open_dataset(p) as d:
        again = Recipe.from_dict(json.loads(d.attrs["recipe_json"]))
        assert again.axes == r.axes
        assert d["run"].attrs["interval_s"] == 0.01
        assert d["count"].dims == ("run", "a")


@pytest.mark.parametrize("name", ["repeat_runs_keep", "repeat_point_average",
                                  "repeat_time_series"])
def test_example_recipes_are_valid(name):
    root = Path(__file__).resolve().parent.parent
    r = Recipe.load(root / "recipes" / f"{name}.yaml")
    assert r.validate(build_sim_registry()) == []
    try:
        import jsonschema
    except ImportError:
        return                    # the schema check needs the optional library
    schema = json.loads((root / "schema" / "scan.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(r.to_dict(), schema)


# ───────────────────────────── the builder ────────────────────────────────────

@pytest.fixture
def builder():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtCore, QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    reg = build_sim_registry()
    win = ScanBuilder(reg)
    for it in win._det_items():
        it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole)
                         == "lockin_r" else QtCore.Qt.Unchecked)
    yield win
    win.close()


def test_builder_repeat_row_round_trip(builder):
    builder.add_axis("field")
    builder.rows[-1].num.setValue(11)
    row = builder.add_repeat(num=4, mode="average", interval_s=2.5, index=0)
    assert builder.rows[0] is row
    assert row.what_lbl.text() == "the whole scan, N times"
    ax = builder.build_recipe().axes
    assert ax[0] == {"type": "repeat", "num": 4, "mode": "average", "interval_s": 2.5}
    assert "4×11 = 44 pts" in builder.summary.text()
    assert "average of 4 -> 11 stored" in builder.summary.text()
    # move it to the bottom: now it repeats each point
    builder._move_row(row, +1)
    assert row.what_lbl.text() == "each point, N times in a row"
    recipe = builder.build_recipe()
    builder.load_recipe(Recipe.from_dict(recipe.to_dict()))
    assert [r.to_axis() for r in builder.rows] == recipe.axes
    assert builder.rows[-1].mode.currentData() == "average"


@pytest.mark.parametrize("mode", ["keep", "average"])
def test_running_from_the_builder(builder, mode):
    builder.per_pt.setValue(0.0)
    builder.add_axis("field")
    builder.rows[-1].num.setValue(3)
    builder.add_repeat(num=2, mode=mode)
    builder.run_scan(block=True)
    ds = builder.dataset
    assert ("repeat" in ds.dims) == (mode == "keep")
    assert np.all(np.isfinite(ds["lockin_r"].values))
    if mode == "average":
        np.testing.assert_array_equal(ds["lockin_r_n"].values, 2)


def test_builder_eta_counts_the_interval(builder):
    builder.per_pt.setValue(0.1)
    builder.add_axis("field")
    builder.rows[-1].num.setValue(2)
    builder.add_repeat(num=3, mode="keep", interval_s=60, index=0)
    assert "paced by the repeat interval" in builder.detail.text()
    assert "ETA ≈ 2m 00s" in builder.detail.text()
