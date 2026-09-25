"""Routines THROUGHOUT a scan: every N points, and once per sweep of an axis.

The case that asked for it: autofocus once per row of a map (or every 100
points), waited on, and if it fails the scan carries on at the old focus.

The rule under test (hooks.py): a sweep of axis A is a block of prod(shape[a:])
consecutive points, so it STARTS at every flat index that is a multiple of the
block. Same rule in 2-D and 5-D, with and without zig-zag.
"""

import itertools
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, run                                     # noqa: E402
from scan_core.errors import ScanAborted                             # noqa: E402
from scan_core.hooks import firings                                  # noqa: E402
from scan_core.registry import Action, Gettable, Registry, Settable  # noqa: E402


def _registry(n_axes=4, fail=None):
    """Settables p0..p{n-1} and c, an action AF, all logged to `events`."""
    events, state = [], {f"p{i}": 0.0 for i in range(n_axes)}
    state["c"] = 0.0
    reg = Registry()
    for pid in list(state):
        def set_fn(v, _p=pid):
            state[_p] = v
            events.append(("set", _p, v))
        reg.add(Settable(pid, pid, "", (-1e3, 1e3), set_fn, lambda _p=pid: state[_p]))

    def read():
        events.append(("read", tuple(state[f"p{i}"] for i in range(n_axes))))
        return 0.0

    def af():
        events.append(("action", "AF"))
        if fail is not None and fail():
            raise TimeoutError("no focus peak")

    reg.add(Gettable("d", "d", "", read))
    reg.add_action(Action("AF", "Autofocus", af))
    return reg, events


def _axes(shape):
    return [{"type": "array", "param": f"p{i}", "values": list(range(n))}
            for i, n in enumerate(shape)]


def _af(axis=None, **kw):
    h = {"when": "each_sweep" if axis else "every_n_points", "action": "call",
         "args": {"action": "AF"}, **kw}
    if axis:
        h["axis"] = axis
    return h


def _read_indices_before_af(events):
    """Index of the point (0-based, in visiting order) each AF came before."""
    out, point = [], 0
    for e in events:
        if e[0] == "read":
            point += 1
        elif e == ("action", "AF"):
            out.append(point)
    return out


# ───────────────────────────── start of each sweep ────────────────────────────

@pytest.mark.parametrize("zigzag", [False, True])
@pytest.mark.parametrize("axis, block", [("p3", 2), ("p2", 6), ("p1", 24), ("p0", 72)])
def test_start_of_each_sweep_4d(axis, block, zigzag):
    """4-D 3x4x3x2 (72 points): AF before point 0, block, 2*block, ...
    whatever the depth, and zig-zag changes nothing -- it changes the PATH,
    not which points begin a sweep."""
    shape = (3, 4, 3, 2)
    reg, events = _registry()
    run(Recipe(axes=_axes(shape), detectors=["d"], hooks=[_af(axis)],
               zigzag=zigzag), reg, created_iso="t")
    assert _read_indices_before_af(events) == list(range(0, 72, block))


def test_zigzag_row_boundary_is_not_missed():
    """The case after_axis gets wrong: in zig-zag the inner index does not
    change at a turnaround, so 'x changed' misses the row. each_sweep does not."""
    reg, events = _registry(2)
    run(Recipe(axes=_axes((3, 4)), detectors=["d"], zigzag=True,
               hooks=[_af("p1")]), reg, created_iso="t")
    assert _read_indices_before_af(events) == [0, 4, 8]
    old, events2 = _registry(2)
    run(Recipe(axes=_axes((3, 4)), detectors=["d"], zigzag=True,
               hooks=[{"when": "after_axis", "axis": "p1", "action": "call",
                       "args": {"action": "AF"}}]), old, created_iso="t")
    # after_axis fires on every inner step and NOT at the turnarounds
    assert _read_indices_before_af(events2) == [1, 2, 3, 5, 6, 7, 9, 10, 11]


def test_af_runs_after_the_new_row_is_set():
    """Focus where the row is measured: the outer axis has moved BEFORE AF."""
    reg, events = _registry(2)
    run(Recipe(axes=_axes((2, 2)), detectors=["d"], hooks=[_af("p1")]),
        reg, created_iso="t")
    i = [k for k, e in enumerate(events) if e == ("action", "AF")][1]
    assert events[i - 1] == ("set", "p1", 0.0)
    assert ("set", "p0", 1.0) in events[:i]


def test_one_firing_when_several_axes_reset_together():
    """When p0 steps, p1 and p2 reset too; a hook on p2 fires ONCE."""
    reg, events = _registry(3)
    run(Recipe(axes=_axes((2, 2, 2)), detectors=["d"], hooks=[_af("p2")]),
        reg, created_iso="t")
    assert _read_indices_before_af(events) == [0, 2, 4, 6]


# ────────────────────────── end edge, every m, count ──────────────────────────

def test_end_of_each_sweep_skips_the_last():
    reg, events = _registry(2)
    run(Recipe(axes=_axes((3, 4)), detectors=["d"],
               hooks=[_af("p1", edge="end")]), reg, created_iso="t")
    # after points 4 and 8, not after 12 (that is after_scan's moment)
    assert _read_indices_before_af(events) == [4, 8]


def test_every_mth_sweep():
    reg, events = _registry(2)
    run(Recipe(axes=_axes((7, 2)), detectors=["d"],
               hooks=[_af("p1", every=3)]), reg, created_iso="t")
    assert _read_indices_before_af(events) == [0, 6, 12]


def test_every_n_points():
    reg, events = _registry(2)
    run(Recipe(axes=_axes((5, 5)), detectors=["d"], hooks=[_af(n=10)]),
        reg, created_iso="t")
    assert _read_indices_before_af(events) == [0, 10, 20]


HOOKS = [_af(n=1), _af(n=7), _af(n=100)] + [
    _af(f"p{a}", edge=e, every=m)
    for a, e, m in itertools.product(range(3), ("start", "end"), (1, 2, 3))]


@pytest.mark.parametrize("hook", HOOKS, ids=lambda h: str(sorted(h.items())))
def test_firings_matches_what_the_engine_does(hook):
    """The builder's 'fires 12x' is computed, not measured -- so check it."""
    shape = (3, 5, 4)
    reg, events = _registry(3)
    run(Recipe(axes=_axes(shape), detectors=["d"], hooks=[hook]), reg, created_iso="t")
    n = sum(1 for e in events if e == ("action", "AF"))
    assert firings(hook, shape, ["p0", "p1", "p2"]) == n


# ─────────────────────────────── on_error ───────────────────────────────────

def test_a_failing_af_stops_the_scan_by_default():
    reg, _ = _registry(1, fail=lambda: True)
    with pytest.raises(TimeoutError):
        run(Recipe(axes=_axes((4,)), detectors=["d"], hooks=[_af(n=2)]),
            reg, created_iso="t")


def test_on_error_continue_carries_on_and_says_so():
    calls = {"n": 0}

    def fail_second():
        calls["n"] += 1
        return calls["n"] == 2

    reg, events = _registry(2, fail=fail_second)
    logs = []
    ds = run(Recipe(axes=_axes((3, 2)), detectors=["d"],
                    hooks=[_af("p1", on_error="continue")]),
             reg, created_iso="t", on_log=logs.append)
    assert sum(1 for e in events if e[0] == "read") == 6       # every point measured
    assert int(ds["d"].notnull().sum()) == 6
    assert any("FAILED (no focus peak); carrying on" in m for m in logs)
    assert any(m.startswith("start of each sweep of p1: run AF") for m in logs)
    assert all(m.isascii() for m in logs)


def test_on_error_continue_still_restores():
    """A routine that moved something and then failed must put it back."""
    reg, events = _registry(2, fail=lambda: True)
    hook = {"when": "each_sweep", "axis": "p1", "on_error": "continue",
            "action": "call", "args": {"set": {"p0": 99.0}, "action": "AF"}}
    run(Recipe(axes=_axes((2, 2)), detectors=["d"], hooks=[hook]), reg, created_iso="t")
    reads = [e[1] for e in events if e[0] == "read"]
    assert reads == [(0, 0), (0, 1), (1, 0), (1, 1)]            # never measured at 99


def test_abort_is_never_swallowed():
    def abort():
        raise ScanAborted("operator")

    reg, _ = _registry(1)
    reg._actions = {}
    reg.add_action(Action("AF", "Autofocus", abort))
    with pytest.raises(ScanAborted):
        run(Recipe(axes=_axes((2,)), detectors=["d"],
                   hooks=[_af(n=1, on_error="continue")]), reg, created_iso="t")


# ─────────────────────────────── validation ─────────────────────────────────

def test_validation_names_a_missing_axis_and_bad_fields():
    reg, _ = _registry(2)
    errs = Recipe(axes=_axes((2, 2)), detectors=["d"],
                  hooks=[_af("gone"), _af("p1", edge="middle"), _af("p1", every=0),
                         _af(n=0), _af("p1", on_error="shrug")]).validate(reg)
    text = "\n".join(errs)
    assert "no axis 'gone'" in text
    assert "edge must be start or end" in text
    assert "every must be a whole number" in text
    assert "every_n_points needs n >= 1" in text
    assert "on_error must be one of" in text


def test_a_raster_gives_two_sweepable_dims():
    reg, events = _registry(2)
    recipe = Recipe(axes=[{"type": "raster", "fast": "x",
                           "x": {"param": "p1", "start": 0, "stop": 2, "num": 3},
                           "y": {"param": "p0", "start": 0, "stop": 1, "num": 2}}],
                    detectors=["d"], hooks=[_af("p1")])
    assert recipe.validate(reg) == []
    run(recipe, reg, created_iso="t")
    assert _read_indices_before_af(events) == [0, 3]
    assert math.prod((2, 3)) == 6


# ─────────────────────────── the Scan Builder column ──────────────────────────

@pytest.fixture
def builder():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder
    from scan_core import build_sim_registry
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ScanBuilder(build_sim_registry())
    yield win
    win.close()


def _xy(builder, ny=3, nx=4):
    builder.add_axis("pos_y"); builder.rows[-1].num.setValue(ny)
    builder.add_axis("pos_x"); builder.rows[-1].num.setValue(nx)


def test_a_new_routine_defaults_to_once_per_row(builder):
    _xy(builder)
    section = builder.add_throughout()
    section.set_action("sim_autofocus")
    hook = builder.build_recipe().hooks[-1]
    assert hook == {"when": "each_sweep", "axis": "pos_x", "edge": "start", "every": 1,
                    "on_error": "continue", "action": "call",
                    "args": {"action": "sim_autofocus"}}
    assert section.count_lbl.text() == "fires 3×"
    assert "start of each sweep of pos_x: sim_autofocus (3×)" in builder.detail.text()


def test_the_axis_list_follows_the_stack(builder):
    _xy(builder)
    section = builder.add_throughout()
    section.set_action("sim_autofocus")
    section.set_trigger("each_sweep", axis="pos_y")
    builder.add_axis("rf_freq"); builder.rows[-1].num.setValue(5)
    assert [section.axis_combo.itemData(i) for i in range(section.axis_combo.count())] \
        == ["pos_y", "pos_x", "rf_freq"]
    assert section.count_lbl.text() == "fires 1×"
    # remove pos_y: the routine keeps its axis, marked, and the scan is invalid
    builder._remove_row(builder.rows[0])
    assert section.axis_combo.currentData() == "pos_y"
    assert "not in the scan" in section.axis_combo.currentText()
    assert builder.summary.text() == "invalid"
    assert "no axis 'pos_y'" in builder.detail.text()


def test_every_n_points_and_stop_on_error(builder):
    _xy(builder)
    section = builder.add_throughout()
    section.set_action("sim_autofocus")
    section.set_trigger("every_n_points", n=5, on_error="stop")
    assert not section.axis_combo.isVisibleTo(section)
    assert builder.build_recipe().hooks[-1] == {
        "when": "every_n_points", "n": 5, "on_error": "stop", "action": "call",
        "args": {"action": "sim_autofocus"}}
    assert section.count_lbl.text() == "fires 3×"          # before 0, 5, 10 of 12


def test_round_trip_through_yaml_keeps_order_and_other_hooks(builder, tmp_path):
    _xy(builder)
    a = builder.add_throughout(); a.set_action("sim_autofocus")
    b = builder.add_throughout(); b.set_trigger("every_n_points", n=7)
    builder.add_throughout_set("pos_z", 12.5, section=b)
    b.set_action("vna_reference")
    builder.add_routine_set("after_scan", "field", 0.0)
    recipe = builder.build_recipe()
    recipe.hooks.insert(0, {"when": "before_point", "action": "wait_ms", "args": {"ms": 0}})
    path = tmp_path / "r.yaml"; recipe.save(path)

    from scan_core import Recipe
    missing = builder.load_recipe(Recipe.load(path))
    assert missing == []
    assert len(builder.throughout) == 2
    assert builder.throughout[1].rows[0].param.id == "pos_z"
    assert builder.throughout[1].rows[0].value() == 12.5
    assert builder.build_recipe().hooks == recipe.hooks
    # a routine removed in the UI is gone from the saved definition
    builder.remove_throughout(builder.throughout[0])
    assert [h.get("args", {}).get("action") for h in builder.build_recipe().hooks] == [
        None, None, "vna_reference"]   # wait_ms, after_scan, b


def test_plus_throughout_adds_to_the_active_routine(builder):
    _xy(builder)
    first = builder.add_throughout()
    second = builder.add_throughout()
    builder._set_active_throughout(first)
    builder.add_throughout_set("pos_z")
    assert [r.param.id for r in first.rows] == ["pos_z"] and second.rows == []


def test_it_runs_in_the_simulator(builder):
    _xy(builder, ny=3, nx=2)
    section = builder.add_throughout(); section.set_action("sim_autofocus")
    builder.run_scan(block=True)
    assert builder.registry._state.n_autofocus == 3
    assert any(m.startswith("start of each sweep of pos_x: run sim_autofocus")
               for m in builder.run_log)


def test_swapping_the_registry_drops_them(builder):
    from scan_core import build_sim_registry
    _xy(builder)
    builder.add_throughout().set_action("sim_autofocus")
    assert builder.has_definition()
    builder.set_registry(build_sim_registry())
    assert builder.throughout == []


# ─────────────────── the old `autofocus` hook is real now ──────────────────────

def test_the_autofocus_hook_runs_the_registry_action():
    """{action: autofocus} used to be a stub that only logged."""
    reg, events = _registry(1)
    reg.add_action(Action("camera.autofocus", "AF", lambda: events.append(("action", "cam"))))
    run(Recipe(axes=_axes((4,)), detectors=["d"],
               hooks=[{"when": "every_n_points", "n": 2, "action": "autofocus"}]),
        reg, created_iso="t")
    assert [e for e in events if e[0] == "action"] == [("action", "cam")] * 2


def test_the_autofocus_hook_is_refused_without_a_camera():
    reg = Registry()
    reg.add(Settable("p0", "p0", "", (0, 9), lambda v: None, lambda: 0.0))
    reg.add(Gettable("d", "d", "", lambda: 0.0))
    errs = Recipe(axes=_axes((2,)), detectors=["d"],
                  hooks=[{"when": "every_n_points", "n": 1, "action": "autofocus"}]).validate(reg)
    assert any("no module here offers an autofocus" in e for e in errs)


def test_a_failed_camera_check_is_carried_on_from():
    """The camera's `check` raises InstrumentError on a failed run: an ordinary
    error, so `carry on if it fails` applies (an Abort would not be caught)."""
    from scan_core.instrument import InstrumentError

    def af():
        raise InstrumentError("camera.autofocus finished but af_error = 'RuntimeError'")

    reg, events = _registry(1)
    reg.add_action(Action("camera.autofocus", "AF", af))
    logs = []
    run(Recipe(axes=_axes((3,)), detectors=["d"],
               hooks=[{"when": "every_n_points", "n": 1, "on_error": "continue",
                       "action": "call", "args": {"action": "camera.autofocus"}}]),
        reg, created_iso="t", on_log=logs.append)
    assert sum(1 for e in events if e[0] == "read") == 3
    assert sum("carrying on" in m for m in logs) == 3
