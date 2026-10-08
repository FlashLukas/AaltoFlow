"""The XY MASK (Lukas, 2026-10-07): measure only the magnetic parts of a map.

Since 2026-10-08 the mask is the 2-D case of the SCOUT PASS (scout.py). These
tests still write the OLD `mask:` block -- every recipe here goes through the
translation (scout.from_mask) -- so they pin both that old recipes behave
exactly as they did and the scout's numbers on an XY map. The N-D features
are in test_scout.py.

"In the XY scan we could perform a quick scan where we only look on the
reflectivity and then create a mask ... and measure again", the quick scan at
every 3rd point with the mask interpolated onto the real grid, the mask in
camera or absolute coordinates, and a mask he can make himself from anything
(a grayscale image).

Layers:
  * mask.py's numbers on their own: coarse grid, Otsu, interpolation, growth;
  * the engine on the simulator (its islands reflect more than the substrate):
    masked points are never VISITED, stored as NaN, the mask in the file, no
    island point lost, routines fire on measured points;
  * mask sources: a picture, a matrix, an earlier scan -- and the refusals.
No ports are used.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from scan_core import Recipe, run
from scan_core import scout as M
from scan_core.errors import ScanAborted
from scan_core.registry import Gettable, build_sim_registry


def _raster(num=31, span=45.0):
    return {"type": "raster",
            "x": {"param": "pos_x", "start": -span, "stop": span, "num": num},
            "y": {"param": "pos_y", "start": -span, "stop": span, "num": num}}


def _recipe(mask=None, num=31, **kw):
    m = {"detector": "reflectivity", "step": 3}
    m.update(mask or {})
    return Recipe(axes=[_raster(num)], detectors=["lockin_r"], mask=m, **kw)


def _counting(reg):
    """Wrap pos_x / pos_y so every move is recorded."""
    moves = []
    for pid in ("pos_x", "pos_y"):
        p = reg.get(pid)
        orig = p.set

        def set_(v, *a, _orig=orig, _pid=pid, **k):
            moves.append((_pid, float(v)))
            return _orig(v, *a, **k)
        p.set = set_
    return moves


# ───────────────────────────── the numbers ──────────────────────────────────

def test_coarse_grid_always_ends_on_the_last_point():
    assert M.coarse_indices(10, 3).tolist() == [0, 3, 6, 9]
    assert M.coarse_indices(11, 3).tolist() == [0, 3, 6, 9, 10]
    assert M.coarse_indices(1, 3).tolist() == [0]
    assert M.coarse_indices(5, 1).tolist() == [0, 1, 2, 3, 4]


def test_otsu_splits_two_populations():
    rng = np.random.default_rng(0)
    v = np.concatenate([0.3 + 0.01 * rng.standard_normal(500),
                        0.75 + 0.01 * rng.standard_normal(100)])
    t = M.otsu(v)
    assert 0.35 < t < 0.70
    assert M.otsu([1.0, 1.0]) == 1.0                  # nothing to split
    with pytest.raises(ValueError):
        M.otsu([np.nan])


def test_auto_margin_is_half_the_source_step_in_grid_points():
    names = ["a", "b"]
    assert M.margin_radii(M.spec_of({}), names, [3, 2]) == [1.5, 1.0]
    assert M.margin_radii(M.spec_of({"margin": 0.7}), names, [3, 2]) == [0.7, 0.7]
    assert M.margin_radii(M.spec_of({"margin": {"a": 2}}), names, [3, 2]) == [2.0, 1.0]


def test_threshold_forms():
    v = np.array([0.0, 1.0, 2.0, 10.0])
    assert M.threshold_of(M.spec_of({"threshold": 4.0}), v) == 4.0
    assert M.threshold_of(M.spec_of({"threshold": {"fraction": 0.25}}), v) == 2.5


def test_interpolation_is_exact_on_the_coarse_points_and_linear_between():
    ca, cb = np.array([0.0, 3.0]), np.array([0.0, 3.0, 6.0])
    V = np.array([[0.0, 3.0, 6.0], [3.0, 6.0, 9.0]])          # = a + b
    fa, fb = np.arange(4.0), np.arange(7.0)
    Vf, unknown = M.interpolate([ca, cb], V, [fa, fb])
    assert np.allclose(Vf, fa[:, None] + fb[None, :])
    assert not unknown.any()


def test_a_nan_reading_and_the_outside_are_unknown():
    ca = cb = np.array([0.0, 1.0, 2.0])
    V = np.ones((3, 3))
    V[1, 1] = np.nan
    fa = np.array([0.0, 0.5, 1.0, 2.0, 3.0])                  # 3.0: outside
    fb = np.array([0.0, 1.0, 2.0])
    _, unknown = M.interpolate([ca, cb], V, [fa, fb])
    assert unknown[2, 1] and unknown[1, 1]                    # on / next to the NaN
    assert not unknown[0, 0]
    assert unknown[4].all()                                   # outside the source


def test_unknown_points_are_always_measured():
    ca = cb = np.array([0.0, 1.0])
    V = np.array([[0.0, np.nan], [0.0, 0.0]])
    res = M.build(M.spec_of({"threshold": 0.5}), [ca, cb], V, [ca, cb], [0, 0])
    assert res.keep[0, 1] and not res.keep[1, 0]


def test_grow_is_an_ellipse_in_grid_points():
    keep = np.zeros((11, 11), bool)
    keep[5, 5] = True
    g = M.grow(keep, [2, 2])
    assert g[5, 3] and g[5, 7] and g[3, 5] and g[7, 5]        # 2 along an axis
    assert g[4, 4] and not g[3, 3]                            # sqrt 8 > 2: a disc
    # one point of margin on the second axis
    g2 = M.grow(keep, [2, 1])
    assert g2[5, 6] and not g2[5, 7]
    assert (M.grow(keep, [0, 0]) == keep).all()
    # an old margin of 2.0 um on axes stepped 1 and 2 um is [2, 1] points --
    # the same disc in micrometres the old mask grew
    old = M.from_mask({"detector": "r", "margin": 2.0, "axes": ["x", "y"]},
                      [{"type": "linear", "param": "y", "start": 0, "stop": 20, "num": 11},
                       {"type": "linear", "param": "x", "start": 0, "stop": 10, "num": 11}])
    assert old["margin"] == {"x": 2.0, "y": 1.0}


# ───────────────────────────── the engine ───────────────────────────────────

def test_masked_points_are_never_visited_and_stored_as_nan():
    reg = build_sim_registry()
    moves = _counting(reg)
    reads = []
    g = reg.get("lockin_r")
    orig = g.get
    g.get = lambda: (reads.append(1), orig())[1]
    ds = run(_recipe(), reg)
    keep = ds.scan_mask.values.astype(bool)
    assert 0 < keep.sum() < keep.size
    v = ds.lockin_r.values
    assert np.isnan(v[~keep]).all() and np.isfinite(v[keep]).all()
    assert len(reads) == int(keep.sum())                      # one read per kept point
    # pass 2 moves only to kept points: every x it was sent to belongs to a
    # row/column holding a kept point, and there are far fewer moves than points
    pass1 = 11 * 11
    assert len(moves) < pass1 * 2 + 2 * int(keep.sum())
    assert ds.attrs["mask_points"] == f"{int(keep.sum())} of {keep.size}"
    assert ds["mask_reflectivity"].shape == (11, 11)          # 31 points, step 3


def _missed(num):
    """Island points the mask leaves out, and the kept fraction."""
    reg = build_sim_registry()
    ds = run(_recipe(num=num), reg)
    s = reg._state
    keep = ds.scan_mask.values.astype(bool)
    missed = 0
    for i, y in enumerate(ds.pos_y.values):
        for j, x in enumerate(ds.pos_x.values):
            s.x_um, s.y_um = x, y
            if s._pattern()[0] > 0 and not keep[i, j]:
                missed += 1
    return missed, keep.mean()


@pytest.mark.parametrize("num", [41, 61])
def test_no_island_is_lost(num):
    # pass 1 at 6.75 / 4.5 um; the smallest island is 13 um across
    missed, kept = _missed(num)
    assert missed == 0
    assert kept < 0.6                                         # and it does save time


def test_the_limit_an_element_smaller_than_the_pass_1_pitch():
    # pass 1 at 13.5 um: the smallest islands (13 um across) can fall between
    # the coarse points entirely. This is the documented limit -- a finer
    # pass 1 (smaller step) for such a sample -- kept as a test so the
    # documentation and the code cannot drift apart.
    missed, _ = _missed(21)
    assert missed > 0


def test_keep_below_measures_the_other_side():
    a = run(_recipe(), build_sim_registry()).scan_mask.values.astype(bool)
    b = run(_recipe({"keep": "below", "margin": 0.0}),
            build_sim_registry()).scan_mask.values.astype(bool)
    # without margin, "below" is (nearly) the complement of "above"'s core
    assert (a | b).all()


def test_the_mask_survives_netcdf(tmp_path):
    import xarray as xr
    ds = run(_recipe(), build_sim_registry())
    p = tmp_path / "m.nc"
    ds.to_netcdf(p, engine="h5netcdf")
    with xr.open_dataset(p, engine="h5netcdf") as back:
        assert (back.scan_mask.values == ds.scan_mask.values).all()
        assert back.mask_pos_x.attrs["param"] == "pos_x"
        assert json.loads(back.attrs["mask_json"])["detector"] == "reflectivity"


def test_zigzag_measures_exactly_its_mask():
    # the readings' noise depends on the visiting order, so the two masks may
    # differ by a point at a rim -- but each scan measures exactly ITS mask
    a = run(_recipe(), build_sim_registry())
    b = run(_recipe(zigzag=True), build_sim_registry())
    for ds in (a, b):
        keep = ds.scan_mask.values.astype(bool)
        assert (np.isfinite(ds.lockin_r.values) == keep).all()
    assert (a.scan_mask.values == b.scan_mask.values).mean() > 0.98


def test_an_outer_axis_reuses_the_one_mask():
    r = _recipe(num=21)
    r.axes = [{"type": "array", "param": "field", "values": [10.0, 20.0]}] + r.axes
    ds = run(r, build_sim_registry())
    v = ds.lockin_r.values
    assert (np.isnan(v[0]) == np.isnan(v[1])).all()
    assert ds.scan_mask.dims == ("pos_y", "pos_x")


def test_routines_fire_on_measured_points():
    reg = build_sim_registry()
    r = _recipe(num=21)
    # a comment at the start of each row: the file's comment log counts them
    r.hooks = [{"when": "each_sweep", "axis": "pos_x", "edge": "start",
                "action": "call", "args": {"steps": [{"comment": {"text": "row"}}]}}]
    ds = run(r, reg)
    keep = ds.scan_mask.values.astype(bool)
    rows_with_points = int(keep.any(axis=1).sum())
    comments = json.loads(ds.attrs.get("comments", "[]")) if isinstance(
        ds.attrs.get("comments"), str) else ds.attrs.get("comments", [])
    assert rows_with_points < keep.shape[0]                   # some rows are all substrate
    assert len(comments) == rows_with_points


def test_visit_counts_measured_points():
    keep = np.array([[False, True, False], [False, False, False], [True, True, False]])
    v = M.visit_of(keep, M.visiting_order((3, 3), False))
    assert v.measured.tolist() == keep.ravel().tolist()
    assert v.before.tolist() == [0, 0, 1, 1, 1, 1, 1, 2, 3]
    assert v.any_in(0, 3) and not v.any_in(3, 6) and not v.any_in(2, 2)
    z = M.visit_of(keep, M.visiting_order((3, 3), True))
    # row 1 is reversed in visiting order
    assert z.measured.tolist() == [False, True, False, False, False, False,
                                   True, True, False]


def test_abort_in_the_mask_pass():
    n = {"k": 0}

    def stop():
        n["k"] += 1
        return n["k"] > 5
    with pytest.raises(ScanAborted):
        run(_recipe(), build_sim_registry(), should_abort=stop)


def test_a_mask_with_nothing_in_it_stops_before_pass_2():
    with pytest.raises(ValueError, match="no point to measure"):
        run(_recipe({"threshold": 99.0}), build_sim_registry())


# ───────────────────────────── sources ──────────────────────────────────────

def test_a_grayscale_image_is_a_mask(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    img = np.zeros((20, 40), np.uint8)          # 20 rows (Y) x 40 columns (X)
    img[:, 30:] = 255                           # white = measure: the right quarter
    p = tmp_path / "mask.png"
    Image.fromarray(img).save(p)
    reg = build_sim_registry()
    ds = run(_recipe({"from": str(p), "detector": None, "margin": 0.0}), reg)
    keep = ds.scan_mask.values.astype(bool)      # [y, x]
    x = ds.pos_x.values
    # the white columns start at 30/39 of the way across -45..45 (centres)
    edge = -45 + 90 * 29.5 / 39
    assert keep[:, x > edge + 3].all() and not keep[:, x < edge - 3].any()
    assert "mask_file" in ds.data_vars


def test_extent_places_and_flips_a_picture(tmp_path):
    m = np.zeros((10, 10))
    m[0, :] = 1                                  # the FIRST row only
    p = tmp_path / "m.csv"
    np.savetxt(p, m, delimiter=",")
    base = {"from": str(p), "detector": None, "margin": 0.0, "threshold": 0.5}
    a = run(_recipe(dict(base)), build_sim_registry()).scan_mask.values
    assert a[0].all() and not a[-1].any()        # first row = start of Y
    b = run(_recipe(dict(base, extent={"x": [-45, 45], "y": [45, -45]})),
            build_sim_registry()).scan_mask.values
    assert b[-1].all() and not b[0].any()        # flipped
    # a picture covering only part of the scan: outside it is measured
    c = run(_recipe(dict(base, extent={"x": [-10, 10], "y": [-10, 10]})),
            build_sim_registry()).scan_mask.values
    assert c[0].all() and c[:, 0].all()


def test_a_text_matrix_and_npy(tmp_path):
    m = np.eye(5)
    np.savetxt(tmp_path / "m.txt", m)
    np.save(tmp_path / "m.npy", m)
    for name in ("m.txt", "m.npy"):
        ds = run(_recipe({"from": str(tmp_path / name), "detector": None,
                          "margin": 0.0, "threshold": 0.5}), build_sim_registry())
        keep = ds.scan_mask.values.astype(bool)
        assert keep[0, 0] and keep[-1, -1] and not keep[0, -1]


def test_an_earlier_scan_is_a_mask_source(tmp_path):
    coarse = Recipe(axes=[_raster(11)], detectors=["reflectivity"])
    p = tmp_path / "pass1.nc"
    run(coarse, build_sim_registry()).to_netcdf(p, engine="h5netcdf")
    reg = build_sim_registry()
    moves = _counting(reg)
    ds = run(_recipe({"from": str(p)}), reg)
    measured = run(_recipe(), build_sim_registry())
    # nothing re-measured in pass 1 -- the first move is already pass 2
    assert len(moves) <= 2 * int(ds.scan_mask.sum())
    # the same coarse grid (11 points = every 3rd of 31) -> nearly the same mask
    agree = (ds.scan_mask.values == measured.scan_mask.values).mean()
    assert agree > 0.97


def test_a_mask_from_other_coordinates_is_refused(tmp_path):
    other = Recipe(axes=[{"type": "linear", "param": "field", "start": 0, "stop": 1, "num": 3},
                         {"type": "linear", "param": "rf_freq", "start": 100, "stop": 200,
                          "num": 3}], detectors=["reflectivity"])
    p = tmp_path / "other.nc"
    run(other, build_sim_registry()).to_netcdf(p, engine="h5netcdf")
    errs = _recipe({"from": str(p)}).validate(build_sim_registry())
    assert any("coordinates it was measured in" in e for e in errs)


# ───────────────────────────── validation ───────────────────────────────────

@pytest.mark.parametrize("bad, text", [
    ({"step": 0}, "step"),
    ({"step": 1.5}, "step"),
    ({"keep": "inside"}, "keep"),
    ({"threshold": "otsu"}, "threshold"),
    ({"threshold": {"fraction": 2}}, "fraction"),
    ({"margin": -1}, "margin"),
    ({"margin": "big"}, "margin"),
    ({"bogus": 1}, "unknown key"),
    ({"detector": "s21"}, "array"),
    ({"detector": "nope"}, "not known"),
    ({"detector": "sample_region"}, "not a number"),
    ({"extent": {"x": [0, 1]}}, "extent"),
    ({"axes": ["pos_x", "pos_x"]}, "different axes"),
    ({"axes": ["pos_x", "nope"]}, "no axis"),
    ({"from": "C:/does/not/exist.png", "detector": None}, "does not exist"),
])
def test_validation_refuses(bad, text):
    errs = _recipe(bad).validate(build_sim_registry())
    assert any(text in e for e in errs), errs


def test_two_linear_axes_need_axes_named():
    r = Recipe(axes=[{"type": "linear", "param": "pos_y", "start": -5, "stop": 5, "num": 4},
                     {"type": "linear", "param": "pos_x", "start": -5, "stop": 5, "num": 4}],
               detectors=["lockin_r"], mask={"detector": "reflectivity"})
    reg = build_sim_registry()
    assert any("say which axes" in e for e in r.validate(reg))
    r.scout["axes"] = {"pos_x": 3, "pos_y": 3}
    assert r.validate(reg) == []
    run(r, reg)


def test_a_fly_scan_is_refused():
    r = Recipe(axes=[{"type": "linear", "param": "pos_y", "start": -5, "stop": 5, "num": 3},
                     {"type": "fly", "param": "pos_x", "start": -5, "stop": 5, "num": 5,
                      "speed": 50}],
               detectors=["lockin_r"],
               mask={"detector": "reflectivity", "axes": ["pos_x", "pos_y"]})
    assert any("fly scan" in e for e in r.validate(build_sim_registry()))


def test_a_recipe_without_a_mask_is_unchanged():
    r = Recipe(axes=[_raster(5)], detectors=["lockin_r"])
    assert "mask" not in r.to_dict() and "scout" not in r.to_dict()
    ds = run(r, build_sim_registry())
    assert "scan_mask" not in ds and "mask_json" not in ds.attrs
    # an old `mask` block is written back as `scout`, and reads back the same
    d = _recipe().to_dict()
    assert "mask" not in d and d["scout"]["axes"] == {"pos_x": 3, "pos_y": 3}
    assert Recipe.from_dict(d).scout == d["scout"]


def test_the_schema_accepts_a_mask():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).parents[1] / "schema" /
                         "scan.schema.json").read_text(encoding="utf-8"))
    d = _recipe({"threshold": {"fraction": 0.4}, "from": "a.png",
                 "extent": {"x": [0, 1], "y": [1, 0]}}).to_dict()
    jsonschema.validate(d, schema)
    assert "scout" in d
    d["scout"]["nonsense"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(d, schema)
