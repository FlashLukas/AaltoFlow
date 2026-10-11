"""Zig-zag (serpentine) order: sweep every other pass of an inner axis backwards,
so the stage does not fly back to the start of each row.

The point of the tests: the ORDER of visiting changes, the DATA does not.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from scan_core.engine import _unravel, _zigzag, run, visit_index
from scan_core.recipe import Recipe
from scan_core.registry import Gettable, Registry, Settable


def _recording_registry():
    """x and y settables that remember every value they were given."""
    state = {"x": 0.0, "y": 0.0}
    order = []
    reg = Registry()

    def make(name):
        def setter(v, _n=name):
            state[_n] = v
            order.append((_n, v))
        return setter

    reg.add(Settable("x", "X", "", (0, 10), set_fn=make("x"), get_fn=lambda: state["x"]))
    reg.add(Settable("y", "Y", "", (0, 10), set_fn=make("y"), get_fn=lambda: state["y"]))
    # a detector that tells you exactly where it was measured
    reg.add(Gettable("xy", "X+10Y", "", lambda: state["x"] + 10 * state["y"]))
    return reg, order


def _recipe(zigzag: bool) -> Recipe:
    return Recipe(name="zz", zigzag=zigzag, detectors=["xy"], axes=[
        {"type": "linear", "param": "y", "start": 0, "stop": 2, "num": 3},   # outer
        {"type": "linear", "param": "x", "start": 0, "stop": 2, "num": 3}])  # inner


def test_zigzag_reverses_every_other_row():
    idx = [_zigzag(_unravel(f, (3, 3)), (3, 3)) for f in range(9)]
    assert idx == [(0, 0), (0, 1), (0, 2),      # row 0 forwards
                   (1, 2), (1, 1), (1, 0),      # row 1 BACKWARDS
                   (2, 0), (2, 1), (2, 2)]      # row 2 forwards again


def test_the_stage_does_not_fly_back_between_rows():
    reg, order = _recording_registry()
    run(_recipe(zigzag=True), reg, created_iso="t")
    xs = [v for name, v in order if name == "x"]
    assert xs == [0, 1, 2, 1, 0, 1, 2]          # no jump back to 0 at a row end
    # ... which the ordinary order DOES do, twice
    reg2, order2 = _recording_registry()
    run(_recipe(zigzag=False), reg2, created_iso="t")
    xs2 = [v for name, v in order2 if name == "x"]
    assert xs2 == [0, 1, 2, 0, 1, 2, 0, 1, 2]


def test_the_data_is_identical_either_way():
    """Every point is stored at its own coordinate, so the cube must match."""
    reg_a, _ = _recording_registry()
    straight = run(_recipe(zigzag=False), reg_a, created_iso="t")
    reg_b, _ = _recording_registry()
    zig = run(_recipe(zigzag=True), reg_b, created_iso="t")
    np.testing.assert_array_equal(straight["xy"].values, zig["xy"].values)
    # and the values really are x + 10y at each (y, x)
    assert zig["xy"].values.tolist() == [[0, 1, 2], [10, 11, 12], [20, 21, 22]]


def test_off_by_default_and_carried_in_the_definition():
    assert Recipe().zigzag is False
    assert Recipe.from_dict(Recipe(zigzag=True).to_dict()).zigzag is True
    assert Recipe.from_dict({"axes": []}).zigzag is False       # an older file


def test_the_builder_offers_it_unticked():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder
    from scan_core import build_sim_registry

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ScanBuilder(build_sim_registry())
    try:
        # per axis since 2026-10-11: no scan-wide box any more
        assert not hasattr(win, "zigzag_box")
        win.add_axis("pos_y"); win.add_axis("pos_x")
        assert all(not r.is_zigzag() for r in win.rows)
        assert win.build_recipe().zigzag is False
        assert all("zigzag" not in ax for ax in win.build_recipe().axes)
        win.rows[1].set_zigzag(True)
        assert win.build_recipe().axes[1]["zigzag"] is True
        assert "zig-zag: pos_x" in win.detail.text()
        win.load_recipe(Recipe(axes=[], detectors=[], zigzag=False))
        assert win.rows == []
    finally:
        win.close()


# ───────────────────── zig-zag PER AXIS (2026-10-11) ─────────────────────────
#
# Lukas: "Zig zag goes to the advanced settings of each axis." An axis with
# `zigzag: true` runs backwards on every other sweep of it; the old scan-wide
# switch still loads and runs exactly as before.

def _registry_3d():
    """z, y, x settables; the detector notes WHERE it was read (z, y, x)."""
    state = {"x": 0.0, "y": 0.0, "z": 0.0}
    visited = []
    reg = Registry()
    for n in ("x", "y", "z"):
        reg.add(Settable(n, n.upper(), "", (0, 10),
                         set_fn=lambda v, _n=n: state.__setitem__(_n, v),
                         get_fn=lambda _n=n: state[_n]))

    def read():
        visited.append((int(state["z"]), int(state["y"]), int(state["x"])))
        return state["x"] + 10 * state["y"] + 100 * state["z"]

    reg.add(Gettable("pos", "100Z+10Y+X", "", read))
    return reg, visited


def _recipe_3d(zz=(False, False, False), legacy=False, ny=2) -> Recipe:
    axes = [{"type": "linear", "param": "z", "start": 0, "stop": 1, "num": 2},
            {"type": "linear", "param": "y", "start": 0, "stop": ny - 1, "num": ny},
            {"type": "linear", "param": "x", "start": 0, "stop": 2, "num": 3}]
    for ax, on in zip(axes, zz):
        if on:
            ax["zigzag"] = True
    return Recipe(name="zz3", detectors=["pos"], axes=axes, zigzag=legacy)


def _visit(recipe):
    reg, visited = _registry_3d()
    ds = run(recipe, reg, created_iso="t")
    return visited, ds


def test_zigzag_on_the_inner_axis_only():
    order, _ = _visit(_recipe_3d(zz=(False, False, True)))
    # x reversed on every other SWEEP of x (4 sweeps: F, B, F, B); y and z plain
    assert order == [(0, 0, 0), (0, 0, 1), (0, 0, 2),
                     (0, 1, 2), (0, 1, 1), (0, 1, 0),
                     (1, 0, 0), (1, 0, 1), (1, 0, 2),
                     (1, 1, 2), (1, 1, 1), (1, 1, 0)]


def test_zigzag_on_the_middle_axis_only():
    order, _ = _visit(_recipe_3d(zz=(False, True, False), ny=3))
    ys = [y for _, y, _ in order[::3]]               # y of each x-sweep
    assert ys == [0, 1, 2, 2, 1, 0]                  # backwards on z = 1
    xs = [x for _, _, x in order]
    assert xs == [0, 1, 2] * 6                       # x itself stays one-way


def test_zigzag_on_every_axis_is_one_continuous_path():
    """Every step moves ONE axis by ONE point -- also where the outer axis
    advances (with an even middle axis the old scan-wide rule did not)."""
    order, _ = _visit(_recipe_3d(zz=(False, True, True)))
    assert len(set(order)) == 12
    for a, b in zip(order, order[1:]):
        assert sum(abs(p - q) for p, q in zip(a, b)) == 1, (a, b)


def test_the_data_are_the_same_whatever_zigzags():
    _, plain = _visit(_recipe_3d())
    for zz in ((False, False, True), (False, True, False), (True, True, True)):
        _, ds = _visit(_recipe_3d(zz=zz))
        np.testing.assert_array_equal(ds["pos"].values, plain["pos"].values)


def test_zigzag_on_the_outermost_axis_does_nothing():
    a, _ = _visit(_recipe_3d(zz=(True, False, False)))
    b, _ = _visit(_recipe_3d())
    assert a == b


def test_an_old_recipe_with_the_scan_wide_switch_runs_exactly_as_before():
    """The OLD rule (engine._zigzag over every dim) is kept for it, even where
    the per-axis rule would order a 3-D scan differently."""
    order, _ = _visit(_recipe_3d(legacy=True))
    shape = (2, 2, 3)
    want = [_zigzag(_unravel(f, shape), shape) for f in range(12)]
    assert order == want
    # (and that IS different from all-axes-per-axis here: an even middle axis)
    per_axis, _ = _visit(_recipe_3d(zz=(True, True, True)))
    assert per_axis != order
    # in 2-D the two rules agree, so an old 2-D recipe is unchanged either way
    two_d = [_zigzag(_unravel(f, (3, 3)), (3, 3)) for f in range(9)]
    assert [visit_index(_unravel(f, (3, 3)), (3, 3), [False, True])
            for f in range(9)] == two_d


def test_the_old_switch_loads_from_a_yaml_and_still_validates(tmp_path):
    p = tmp_path / "old.yaml"
    p.write_text("name: old\nzigzag: true\ndetectors: [pos]\naxes:\n"
                 "  - {type: linear, param: y, start: 0, stop: 2, num: 3}\n"
                 "  - {type: linear, param: x, start: 0, stop: 2, num: 3}\n",
                 encoding="utf-8")
    r = Recipe.load(p)
    reg, visited = _registry_3d()
    assert r.zigzag is True and r.validate(reg) == []
    run(r, reg, created_iso="t")
    assert [x for _, _, x in visited] == [0, 1, 2, 2, 1, 0, 0, 1, 2]


def test_per_axis_zigzag_round_trips_through_yaml_and_nc(tmp_path):
    xr = pytest.importorskip("xarray")
    r = _recipe_3d(zz=(False, False, True))
    r.save(tmp_path / "zz.yaml")
    back = Recipe.load(tmp_path / "zz.yaml")
    assert back.axes == r.axes and back.axes[2]["zigzag"] is True
    assert "zigzag" not in back.axes[0] and back.zigzag is False
    _, ds = _visit(back)
    ds.to_netcdf(tmp_path / "zz.nc")
    with xr.open_dataset(tmp_path / "zz.nc") as f:
        assert int(f["x"].attrs["zigzag"]) == 1          # said on the coordinate
        assert "zigzag" not in f["y"].attrs and "zigzag" not in f["z"].attrs
        again = Recipe.from_dict(json.loads(f.attrs["recipe_json"]))
    assert again.axes == r.axes
    order, _ = _visit(again)
    first, _ = _visit(r)
    assert order == first


def test_the_schema_knows_the_axis_key():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).parents[1] / "schema" /
                         "scan.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(_recipe_3d(zz=(False, True, True)).to_dict(), schema)
    bad = _recipe_3d().to_dict()
    bad["axes"][1]["zigzag"] = "yes"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_validate_refuses_a_non_bool_or_a_repeat_zigzag():
    reg, _ = _registry_3d()
    r = _recipe_3d()
    r.axes[2]["zigzag"] = "yes"
    assert any("true or false" in e for e in r.validate(reg))
    r = _recipe_3d()
    r.axes.insert(1, {"type": "repeat", "num": 2, "zigzag": True})
    assert any("repeat cannot zig-zag" in e for e in r.validate(reg))


def test_a_raster_axis_zigzags_its_fast_dim():
    reg = Registry()
    state = {"x": 0.0, "y": 0.0}
    seen = []
    for n in ("x", "y"):
        reg.add(Settable(n, n, "", (0, 10), set_fn=lambda v, _n=n: state.__setitem__(_n, v),
                         get_fn=lambda _n=n: state[_n]))
    reg.add(Gettable("p", "p", "", lambda: seen.append((state["y"], state["x"])) or 0.0))
    r = Recipe(detectors=["p"], axes=[{"type": "raster", "zigzag": True,
                                       "x": {"param": "x", "start": 0, "stop": 2, "num": 3},
                                       "y": {"param": "y", "start": 0, "stop": 1, "num": 2}}])
    run(r, reg, created_iso="t")
    assert [x for _, x in seen] == [0, 1, 2, 2, 1, 0]


def test_the_scout_visiting_order_matches_the_engine():
    from scan_core.scout import visiting_order
    shape = (2, 2, 3)
    for flags in ([False, False, True], [False, True, False], [False, True, True]):
        want = [np.ravel_multi_index(visit_index(_unravel(f, shape), shape, flags), shape)
                for f in range(12)]
        assert visiting_order(shape, flags).tolist() == want
    # the old switch: the old order
    want = [np.ravel_multi_index(_zigzag(_unravel(f, shape), shape), shape)
            for f in range(12)]
    assert visiting_order(shape, True).tolist() == want


def test_a_per_axis_fly_zigzag_flies_every_other_row_back():
    """3-D: two outer axes, the fly axis zig-zags on its own. Every row then
    starts where the last one ended -- so the stage speed is switched to the
    fly speed once and back once, never to the approach speed in between
    (the check test_flyscan uses for the old switch)."""
    from scan_core.registry import build_sim_registry
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.004
    sets = []
    p = reg.get("stage_speed")
    orig = p._set
    p._set = lambda v: (sets.append(v), orig(v))[1]
    r = Recipe(name="fly", fixed={"field": 40.0, "rf_freq": 890.0}, detectors=["lockin_r"],
               axes=[{"type": "array", "param": "pos_z", "values": [0.0, 0.0]},
                     {"type": "array", "param": "pos_y", "values": [0.0, 0.0]},
                     {"type": "fly", "param": "pos_x", "start": -4, "stop": 4, "num": 9,
                      "speed": 40, "speed_param": "stage_speed", "zigzag": True}])
    ds = run(r, reg)
    assert np.all(ds["lockin_r_n"].values >= 2)
    assert sets == [40.0, 0.0], sets
    assert int(ds["pos_x"].attrs["fly_zigzag"]) == 1
