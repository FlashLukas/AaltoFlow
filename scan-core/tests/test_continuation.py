"""CONTINUE a measurement from a point of an earlier file (2026-10-14).

Lukas: "continue measuring a data file from a point ... in case of a crash,
lost template ... the new file would include the original data and only the
data from a selected point would be overwritten."

The detector here COUNTS its reads (plus an offset per run), so every value in
a file says which run measured it and in which order -- a test can tell a kept
point from a re-measured one exactly.
"""

from __future__ import annotations

import numpy as np
import pytest

from scan_core import build_sim_registry
from scan_core import continuation as CT
from scan_core.autosave import write_dataset
from scan_core.engine import run
from scan_core.recipe import Recipe
from scan_core.registry import Gettable, Registry, Settable


def _registry(offset: float):
    state = {"x": 0.0, "y": 0.0, "n": 0}
    reg = Registry()
    for name in ("x", "y"):
        reg.add(Settable(name, name.upper(), "um", (0, 10),
                         set_fn=lambda v, _n=name: state.__setitem__(_n, v),
                         get_fn=lambda _n=name: state[_n]))

    def read():
        state["n"] += 1
        return offset + state["n"]
    reg.add(Gettable("count", "reads", "", read))
    reg.add(Gettable("where", "10y+x", "", lambda: 10 * state["y"] + state["x"]))
    return reg, state


def _recipe(zigzag=True, hooks=()):
    return Recipe(name="cont", detectors=["count", "where"], hooks=list(hooks), axes=[
        {"type": "linear", "param": "y", "start": 0, "stop": 3, "num": 4},
        {"type": "linear", "param": "x", "start": 0, "stop": 4, "num": 5,
         "zigzag": zigzag}])


def _partial(tmp_path, n_points, recipe=None):
    """An earlier file whose scan stopped after `n_points` points."""
    reg, state = _registry(0)
    ds = run(recipe or _recipe(), reg, should_abort=lambda: state["n"] >= n_points)
    path = tmp_path / "early.nc"
    write_dataset(ds, path)
    return path


def _in_order(ds, prog, var="count"):
    return np.array([ds[var].values[i] for i in prog.order])


def test_the_file_tells_how_far_it_got(tmp_path):
    prog = CT.read_progress(_partial(tmp_path, 7))
    assert prog.total == 20 and prog.n_measured == 7 and prog.first_missing == 7
    # step 7 (0-based) is the 3rd point of the SECOND row, which zig-zags back
    assert prog.order[7] == (1, 2)
    assert "x = 2 um (3/5)" in prog.where_text(7) and "y = 1 um (2/4)" in prog.where_text(7)


def test_continuing_at_the_first_missing_point_completes_the_scan(tmp_path):
    prog = CT.read_progress(_partial(tmp_path, 7))
    reg, _ = _registry(1000)
    ds = run(prog.recipe, reg, continue_from=CT.Continuation(prog, prog.first_missing))
    got = _in_order(ds, prog)
    assert list(got[:7]) == [1, 2, 3, 4, 5, 6, 7]                 # kept as measured
    assert list(got[7:]) == [1000 + k for k in range(1, 14)]       # the rest, in order
    # every point measured where its coordinates say (zig-zag included)
    yy, xx = np.meshgrid(ds["y"].values, ds["x"].values, indexing="ij")
    assert np.allclose(ds["where"].values, 10 * yy + xx)
    assert ds.attrs["continued_from"] == "early.nc"
    assert ds.attrs["continued_at_point"] == 8
    assert ds.attrs["continued_kept_points"] == 7


def test_continuing_earlier_remeasures_from_that_point(tmp_path):
    """Lost template at point 4: the points from there on are measured again."""
    prog = CT.read_progress(_partial(tmp_path, 9))
    reg, _ = _registry(1000)
    ds = run(prog.recipe, reg, continue_from=CT.Continuation(prog, 4))
    got = _in_order(ds, prog)
    assert list(got[:4]) == [1, 2, 3, 4]
    assert list(got[4:]) == [1000 + k for k in range(1, 17)]
    assert ds.attrs["continued_kept_points"] == 4


def test_stopping_the_continued_scan_keeps_the_earlier_values_not_reached(tmp_path):
    prog = CT.read_progress(_partial(tmp_path, 12))
    reg, state = _registry(1000)
    ds = run(prog.recipe, reg, continue_from=CT.Continuation(prog, 3),
             should_abort=lambda: state["n"] >= 4)            # 4 new points, then stop
    got = _in_order(ds, prog)
    assert list(got[:3]) == [1, 2, 3]
    assert list(got[3:7]) == [1001, 1002, 1003, 1004]
    assert list(got[7:12]) == [8, 9, 10, 11, 12]                   # not reached: kept
    assert np.isnan(got[12:]).all()


def test_a_sweep_under_way_gets_its_start_routine_at_the_point(tmp_path):
    """An autofocus 'at the start of each row' also runs where the scan
    continues in the middle of a row -- and at every later row as usual."""
    fired = []
    hook = {"when": "each_sweep", "axis": "x", "edge": "start", "action": "call",
            "args": {"set": {}}}
    recipe = _recipe(hooks=[hook])
    prog = CT.read_progress(_partial(tmp_path, 7, recipe))
    reg, state = _registry(1000)
    import scan_core.hooks as H
    real = H.ACTIONS["call"]
    try:
        H.ACTIONS["call"] = lambda ctx, **kw: fired.append(ctx["flat"])
        run(prog.recipe, reg, continue_from=CT.Continuation(prog, 7))
    finally:
        H.ACTIONS["call"] = real
    assert fired == [7, 10, 15]


def test_a_different_grid_is_refused_before_anything_moves(tmp_path):
    prog = CT.read_progress(_partial(tmp_path, 5))
    other = _recipe()
    other.axes[1]["num"] = 6
    reg, state = _registry(1000)
    with pytest.raises(ValueError, match="not the same grid"):
        run(other, reg, continue_from=CT.Continuation(prog, 5))
    assert state["n"] == 0
    shifted = _recipe()
    shifted.axes[1]["stop"] = 5
    with pytest.raises(ValueError, match="points differ"):
        run(shifted, reg, continue_from=CT.Continuation(prog, 5))


def test_what_cannot_be_continued_says_why(tmp_path):
    reg = build_sim_registry()
    fly = Recipe(detectors=["lockin_r"], axes=[
        {"type": "linear", "param": "pos_y", "start": 0, "stop": 2, "num": 2},
        {"type": "fly", "param": "pos_x", "start": 0, "stop": 2, "num": 3, "speed": 5}])
    assert any("fly" in p for p in CT.problems(fly, fly.compile(reg), reg))
    rep = Recipe(detectors=["lockin_r"], axes=[
        {"type": "repeat", "num": 2, "mode": "average"},
        {"type": "linear", "param": "field", "start": 0, "stop": 2, "num": 3}])
    assert any("AVERAGED" in p for p in CT.problems(rep, rep.compile(reg), reg))


def test_a_complex_trace_on_an_up_and_down_axis(tmp_path):
    """The VNA case of 2026-10-14: a field loop against a complex trace."""
    reg = build_sim_registry()
    recipe = Recipe(name="loop", detectors=["s21"], axes=[
        {"type": "linear", "param": "field",
         "segments": [{"start": -20, "stop": 20, "num": 5},
                      {"start": 20, "stop": -20, "num": 5}]}])
    done = {"n": 0}
    early = run(recipe, reg, should_abort=lambda: done["n"] >= 4,
                on_progress=lambda d, t, eta: done.__setitem__("n", d))
    path = tmp_path / "loop.nc"
    write_dataset(early, path)
    prog = CT.read_progress(path)
    assert prog.n_measured == 4 and prog.total == 9
    # the same field is on both runs: the position names its run
    assert prog.where_text(1).endswith("(2/9, up)")
    assert prog.where_text(6).endswith("(7/9, down)")
    ds = run(prog.recipe, build_sim_registry(), continue_from=CT.Continuation(prog, 4))
    write_dataset(ds, tmp_path / "loop_cont.nc")
    back = CT.read_progress(tmp_path / "loop_cont.nc")
    assert back.n_measured == 9
    s_old = early["s21_real"].values + 1j * early["s21_imag"].values
    s_new = ds["s21_real"].values + 1j * ds["s21_imag"].values
    assert np.array_equal(s_new[:4], s_old[:4])                     # kept bit for bit
    assert "field_segment" in ds.coords


def test_a_script_continues_a_file(tmp_path):
    from scan_core import api
    early = Recipe(name="scripted", detectors=["lockin_r"], axes=[
        {"type": "linear", "param": "field", "start": 0, "stop": 40, "num": 6}])
    done = {"n": 0}
    ds = run(early, build_sim_registry(), should_abort=lambda: done["n"] >= 3,
             on_progress=lambda d, t, eta: done.__setitem__("n", d))
    write_dataset(ds, tmp_path / "early.nc")
    with api.connect(simulate=True, data_dir=tmp_path / "data", log=None) as lab:
        out = lab.continue_scan(tmp_path / "early.nc")
        assert out.attrs["continued_at_point"] == 4
        assert np.isfinite(out["lockin_r"].values).all()
        assert np.array_equal(out["lockin_r"].values[:3], ds["lockin_r"].values[:3])
        assert api.path_of(out).exists()
