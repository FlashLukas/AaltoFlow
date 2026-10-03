"""WHERE the running scan is (Measurement tab, 2026-10-02).

Lukas: with 13:30 elapsed the pane still said "ETA ~ 0m 01s" (the per-point
dwell only, no settling, no autofocus) and nothing said which point the scan
was on. Now:

  * the engine hands on_progress the grid INDEX of the point it just measured
    (zig-zag already applied -- the GUI never recomputes the order) and every
    axis's value there, as `where=`. A callback written for the old
    (done, total, eta) signature keeps working unchanged;
  * the run pane says "point n / N", each axis "value (i/len)", a MEASURED
    remaining time and "now: <routine step>" from the routine log;
  * the live plot marks that point (an outlined cell on a map, a vertical
    line on a 1-D plot);
  * the pre-run ETA is labelled "dwell only" and, while running, replaced by
    the measured remaining time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core.engine import _unravel, _zigzag, run
from scan_core.recipe import Recipe
from scan_core.registry import Gettable, Registry, Settable, build_sim_registry


def _registry():
    state = {"x": 0.0, "y": 0.0}
    reg = Registry()
    for name in ("x", "y"):
        reg.add(Settable(name, name.upper(), "um", (-100, 100),
                         set_fn=lambda v, _n=name: state.__setitem__(_n, v),
                         get_fn=lambda _n=name: state[_n]))
    reg.add(Gettable("xy", "X+10Y", "V", lambda: state["x"] + 10 * state["y"]))
    return reg


def _recipe(zigzag=False):
    return Recipe(name="where", zigzag=zigzag, detectors=["xy"], axes=[
        {"type": "linear", "param": "y", "start": 0, "stop": 20, "num": 3},    # outer
        {"type": "linear", "param": "x", "start": 0, "stop": 3, "num": 4}])    # inner


# ─────────────────────────────── the engine ──────────────────────────────────

@pytest.mark.parametrize("zigzag", [False, True])
def test_on_progress_gets_the_index_and_the_axis_values(zigzag):
    seen = []
    run(_recipe(zigzag), _registry(),
        on_progress=lambda d, t, e, where=None: seen.append((d, t, where)))
    assert [d for d, _, _ in seen] == list(range(1, 13))
    shape = (3, 4)
    for flat, (done, total, where) in enumerate(seen):
        idx = _unravel(flat, shape)
        if zigzag:
            idx = _zigzag(idx, shape)
        assert total == 12
        assert where["index"] == idx                  # the order the engine VISITED
        assert where["flat"] == flat
        y_ax, x_ax = where["axes"]
        assert (y_ax["name"], y_ax["i"], y_ax["n"]) == ("y", idx[0], 3)
        assert (x_ax["name"], x_ax["i"], x_ax["n"]) == ("x", idx[1], 4)
        assert y_ax["value"] == [0.0, 10.0, 20.0][idx[0]]
        assert x_ax["value"] == float(idx[1])
        assert x_ax["unit"] == "um"
    if zigzag:                                        # row 1 really ran backwards
        assert [w["axes"][1]["i"] for _, _, w in seen[4:8]] == [3, 2, 1, 0]


def test_an_old_three_argument_callback_still_works():
    seen = []
    run(_recipe(), _registry(), on_progress=lambda d, t, e: seen.append((d, t)))
    assert seen[-1] == (12, 12)


def test_a_callback_taking_kwargs_gets_where_too():
    seen = []
    run(_recipe(), _registry(), on_progress=lambda *a, **kw: seen.append(kw.get("where")))
    assert seen[-1]["index"] == (2, 3)


def test_a_fly_scan_reports_where_per_row():
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.004
    r = Recipe(name="fly", fixed={"field": 40.0, "rf_freq": 890.0},
               detectors=["lockin_r"], axes=[
                   {"type": "linear", "param": "pos_y", "start": -1, "stop": 1, "num": 3},
                   {"type": "fly", "param": "pos_x", "start": -4, "stop": 4, "num": 9,
                    "speed": 80, "speed_param": "stage_speed"}])
    wheres = []
    run(r, reg, on_progress=lambda d, t, e, where=None: wheres.append(where))
    rows = [w for w in wheres if w is not None]
    assert [w["row"] for w in rows] == [(1, 3), (2, 3), (3, 3)]
    assert [w["axes"][0]["i"] for w in rows] == [0, 1, 2]
    assert rows[-1]["axes"][0]["value"] == 1.0
    assert rows[-1]["axes"][1]["i"] is None           # the fly axis is a whole row


# ─────────────────────────────── the GUI ─────────────────────────────────────

@pytest.fixture
def builder():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from apps.scan_builder import ScanBuilder
    b = ScanBuilder(registry=_registry())
    b.add_axis("y"); b.add_axis("x")
    yield b
    b.close(); b.deleteLater()


def _where(i_y, i_x):
    return {"index": (i_y, i_x), "flat": 4 * i_y + i_x, "row": None, "axes": [
        {"name": "y", "params": ["y"], "i": i_y, "n": 3, "value": 10.0 * i_y, "unit": "um"},
        {"name": "x", "params": ["x"], "i": i_x, "n": 4, "value": float(i_x), "unit": "um"}]}


def test_the_status_line_says_where_the_scan_is(builder):
    builder._run_started()
    builder._on_where(_where(1, 2))
    builder._on_progress(7, 12, 125.0)
    text = builder.run_status_text()
    assert "point 7 / 12" in text
    assert "y 10 um (2/3)" in text
    assert "x 2 um (3/4)" in text
    assert "2m 05s left" in text


def test_now_follows_the_routine_log(builder):
    builder._run_started()
    builder._on_log("start of each sweep of x: run camera.autofocus ...")
    assert "now: run camera.autofocus (start of each sweep of x)" in builder.run_status_text()
    builder._on_log("start of each sweep of x: run camera.autofocus done")
    assert "now:" not in builder.run_status_text()


def test_the_eta_is_dwell_only_before_and_measured_while_running(builder):
    builder._rebuild_summary()
    assert "dwell only" in builder.detail.text()
    builder._run_started()
    builder._on_progress(3, 12, 3723.0)
    assert "1h 02m left (measured)" in builder.detail.text()
    assert "dwell only" not in builder.detail.text()
    builder._run_finished()
    assert "dwell only" in builder.detail.text()


def test_the_queue_position_is_in_the_status_line(builder):
    from scan_core import scan_queue
    builder._queue = [scan_queue.QueueEntry("a", _recipe()),
                      scan_queue.QueueEntry("b", _recipe())]
    builder._queue_i = 1
    builder._run_started()
    try:
        assert "scan 2 of 2" in builder.run_status_text()
    finally:
        builder._queue = None


# ─────────────────────────────── the marker ──────────────────────────────────

@pytest.fixture
def view():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    from apps.data_view import DataView
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    v = DataView(); v.resize(700, 500); v.show()
    yield v
    v.close(); v.deleteLater()


def _ds():
    import xarray as xr
    z = np.arange(12.0).reshape(3, 4)
    return xr.Dataset({"xy": (("y", "x"), z, {"units": "V"})},
                      coords={"y": [0.0, 10.0, 20.0], "x": [0.0, 1.0, 2.0, 3.0]})


def test_the_current_point_is_an_outlined_cell_on_a_map(view):
    view.set_dataset(_ds())
    view.x_combo.setCurrentText("x"); view.y_combo.setCurrentText("y")
    view.set_marker({"y": 10.0, "x": 2.0})
    assert view.mark_rect.isVisible()
    r = view.mark_rect.rect()
    assert r.center().x() == pytest.approx(2.0)
    assert r.center().y() == pytest.approx(10.0)
    assert r.width() == pytest.approx(1.0) and r.height() == pytest.approx(10.0)
    view.set_marker(None)
    assert not view.mark_rect.isVisible()


def test_the_current_point_is_a_vertical_line_on_a_1d_plot(view):
    view.set_dataset(_ds())
    view.x_combo.setCurrentText("x"); view.y_combo.setCurrentText("— none —")
    view.set_marker({"y": 10.0, "x": 3.0})
    assert view.mark_vline.isVisible() and not view.mark_rect.isVisible()
    assert view.mark_vline.value() == pytest.approx(3.0)


def test_a_fly_row_is_a_line_across_the_map(view):
    """A fly scan reports whole rows: only the outer (y) value is known."""
    view.set_dataset(_ds())
    view.x_combo.setCurrentText("x"); view.y_combo.setCurrentText("y")
    view.set_marker({"y": 20.0})
    assert view.mark_hline.isVisible() and not view.mark_rect.isVisible()
    assert view.mark_hline.value() == pytest.approx(20.0)


def test_the_builder_puts_the_marker_on_its_live_plot(builder):
    builder._run_started()
    builder._on_partial(_ds())
    builder.view.x_combo.setCurrentText("x"); builder.view.y_combo.setCurrentText("y")
    builder._on_where(_where(2, 1))
    assert builder.view.mark_rect.isVisible()
    assert builder.view.mark_rect.rect().center().y() == pytest.approx(20.0)
    builder._run_finished()
    assert not builder.view.mark_rect.isVisible()     # a finished scan is not "here"


def test_the_suite_header_shows_it_next_to_running(tmp_path, monkeypatch):
    """The Measurement tab's header -- RUNNING, elapsed, and now WHERE."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import apps.control_panel as cp
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")
    from apps.suite import Suite
    win = Suite()
    try:
        b = win.builder
        monkeypatch.setattr(win, "scan_running", lambda: True)
        b._run_started()
        b._on_where(_where(1, 2))
        b._on_progress(7, 12, 65.0)
        win._tick()
        assert win.run_state.text() == "RUNNING"
        assert "point 7 / 12" in win.where_lbl.text()
        assert "~1m 05s left" in win.where_lbl.text()
        assert b.where_lbl.isHidden()                 # the suite shows it once, up here
        monkeypatch.setattr(win, "scan_running", lambda: False)
        b._run_finished()
        win._tick()
        assert win.where_lbl.text() == ""
    finally:
        win.close()
