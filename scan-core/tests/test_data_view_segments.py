"""The live RESULT plot with axes that go UP AND DOWN, or are uneven (2026-10-14).

A hysteresis scan visits every field twice; drawn naively, a line plot is one
line folded back on itself and a map puts two values into one cell. The file
says which run each point is on (`<dim>_segment`, DEVELOPER_NOTES 4l): a line
plot draws one line per run, a map shows one run at a time (or every point
against its number), and a held slider says which run it is on. Separately, a
map over an UNEVEN axis (log, a list) draws each cell halfway to its
neighbours instead of pretending the steps were equal.
"""

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
xr = pytest.importorskip("xarray")

from scan_core.recipe import segment_values          # noqa: E402

LOOP = [{"start": 0, "stop": 100, "num": 11},
        {"start": 100, "stop": -100, "num": 21},
        {"start": -100, "stop": 100, "num": 21}]


def _loop_coords():
    vals, seg, info = segment_values(LOOP)
    return vals, {
        "field": ("field", vals, {"units": "mT", "segments_json": json.dumps(info),
                                  "segment_source": "recipe"}),
        "field_segment": ("field", seg),
        "field_direction": ("field", np.array([info[k]["direction"] for k in seg],
                                              dtype=np.int8)),
    }


def _loop_1d():
    vals, coords = _loop_coords()
    return xr.Dataset({"kerr": (("field",), np.tanh(vals / 30.0), {"units": "mrad"})},
                      coords=coords)


def _loop_2d():
    vals, coords = _loop_coords()
    x = np.array([0.0, 1.0, 2.0])
    # every point says where it is: value = field + 1000 * x
    z = vals[:, None] + 1000 * x[None, :]
    return xr.Dataset({"sig": (("field", "x"), z, {"units": "V"})},
                      coords={**coords, "x": ("x", x)})


def _view(ds):
    from PySide6 import QtWidgets
    from apps.data_view import DataView
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    v = DataView(); v.resize(800, 520); v.show()
    v.set_dataset(ds)
    for _ in range(5):
        app.processEvents()
    return v


def _scene(v, x, y):
    from PySide6 import QtCore
    return v.plot.vb.mapViewToScene(QtCore.QPointF(x, y))


# ───────────────────────────── uneven map cells ───────────────────────────

def test_cell_edges_reach_halfway_to_the_neighbours():
    from apps.data_view import DataView
    assert np.allclose(DataView.cell_edges(np.array([1.0, 2.0, 4.0, 8.0])),
                       [0.5, 1.5, 3.0, 6.0, 10.0])
    assert np.allclose(DataView.cell_edges(np.array([0.0, 1.0, 2.0])),
                       [-0.5, 0.5, 1.5, 2.5])
    # an even axis is drawn as it is; an uneven one is resampled
    assert DataView._even_index(DataView.cell_edges(np.linspace(1e9, 3e9, 21000))) is None
    idx = DataView._even_index(np.array([0.5, 1.5, 3.0, 6.0, 10.0]))
    assert idx is not None and len(idx) > 4 and set(idx) == {0, 1, 2, 3}


def test_an_uneven_map_axis_draws_each_cell_where_it_is():
    x = np.array([1.0, 2.0, 4.0, 8.0])                  # a log-ish list
    y = np.array([0.0, 1.0])
    z = 10 * x[None, :] + y[:, None]
    ds = xr.Dataset({"sig": (("y", "x"), z, {"units": "V"})}, coords={"y": y, "x": x})
    v = _view(ds)
    try:
        v.x_combo.setCurrentText("x"); v.y_combo.setCurrentText("y")
        xe, ye = v._edges
        assert np.allclose(xe, [0.5, 1.5, 3.0, 6.0, 10.0])
        r = v.img.mapRectToView(v.img.boundingRect())
        assert r.left() == pytest.approx(0.5) and r.right() == pytest.approx(10.0)
        # the image's columns follow the cells: 5 is in the cell of x = 4
        # (3 .. 6), 9 in the cell of x = 8 -- with an equal-step stretch, 5
        # would have been drawn as x = 4's neighbour
        img = v.img.image
        col = lambda val: int((val - 0.5) / 9.5 * img.shape[1])      # noqa: E731
        assert img[0, col(5.0)] == pytest.approx(40.0)
        assert img[0, col(9.0)] == pytest.approx(80.0)
        assert img[0, col(1.2)] == pytest.approx(10.0)
        v._hover(_scene(v, 5.0, 0.0))
        assert "x = 4" in v.readout.text() and "sig = 40 V" in v.readout.text()
        v._hover(_scene(v, 2.9, 1.0))
        assert "x = 2" in v.readout.text() and "sig = 21 V" in v.readout.text()
    finally:
        v.close(); v.deleteLater()


# ───────────────────────────── one line per run ───────────────────────────

def test_a_line_plot_draws_one_line_per_run():
    v = _view(_loop_1d())
    try:
        assert v.x_combo.currentText() == "field"
        shown = [c for c in v._seg_curves if c.isVisible()]
        assert len(shown) == 3
        assert v.curve.getData()[0] is None or len(v.curve.getData()[0]) == 0
        # the down and the second up run start at the previous run's end,
        # so the loop is closed on screen
        lens = [len(c.getData()[0]) for c in shown]
        assert lens == [11, 21, 21]
        xs, _ = shown[1].getData()
        assert xs[0] == pytest.approx(100.0) and xs[-1] == pytest.approx(-100.0)
        names = [lbl.text for _, lbl in v._legend.items]
        assert names == ["seg 1 up", "seg 2 down", "seg 3 up"]
        # the chooser offers the lines, each run, and the point number
        combo = v._seg_combos["field"]
        texts = [combo.itemText(i) for i in range(combo.count())]
        assert texts[0].startswith("every run") and texts[-1] == "all, by point #"
        # one run alone: a plain line of that run
        v.set_segment_choice("field", 1)
        assert not any(c.isVisible() for c in v._seg_curves)
        x1, _ = v.curve.getData()
        assert len(x1) == 20 and x1[0] == pytest.approx(90.0)
        # pointing between two runs at one field reads the nearer run
        v.set_segment_choice("field", "lines")
        _, y = v.curve.getData()
        v._hover(_scene(v, 10.0, float(np.tanh(10 / 30.0))))
        assert "field = 10" in v.readout.text()
    finally:
        v.close(); v.deleteLater()


def test_a_two_run_axis_is_labelled_up_and_down():
    vals, seg, info = segment_values([{"start": 0, "stop": 10, "num": 6},
                                      {"start": 10, "stop": 0, "num": 6}])
    ds = xr.Dataset({"kerr": (("field",), vals)},
                    coords={"field": ("field", vals, {"segments_json": json.dumps(info)}),
                            "field_segment": ("field", seg)})
    v = _view(ds)
    try:
        assert [lbl.text for _, lbl in v._legend.items] == ["up", "down"]
    finally:
        v.close(); v.deleteLater()


def test_an_older_file_with_a_typed_loop_is_split_too():
    f = np.array([0.0, 50, 100, 50, 0, -50, -100, -50, 0])
    ds = xr.Dataset({"kerr": (("field",), f / 100)}, coords={"field": f})
    v = _view(ds)
    try:
        assert sum(c.isVisible() for c in v._seg_curves) == 3
    finally:
        v.close(); v.deleteLater()


# ───────────────────────────── maps: one run at a time ────────────────────

def test_a_map_shows_one_run_never_the_runs_folded():
    v = _view(_loop_2d())
    try:
        v.x_combo.setCurrentText("x"); v.y_combo.setCurrentText("field")
        combo = v._seg_combos["field"]
        texts = [combo.itemText(i) for i in range(combo.count())]
        assert not any(t.startswith("every run") for t in texts)   # a map cannot
        # default: the first run, 0 .. 100 mT
        kind, xc, yc, arr, _ = v._shown
        assert len(yc) == 11 and yc[0] == 0 and yc[-1] == 100
        assert arr[3, 1] == pytest.approx(30 + 1000)
        # the down run: drawn ascending, the values from the DOWN run
        v.set_segment_choice("field", 1)
        kind, xc, yc, arr, _ = v._shown
        assert len(yc) == 20 and np.all(np.diff(yc) > 0)
        assert yc[0] == pytest.approx(-100) and yc[-1] == pytest.approx(90)
        assert arr[0, 0] == pytest.approx(-100)
        # every point against its number
        v.set_segment_choice("field", "index")
        kind, xc, yc, arr, _ = v._shown
        assert np.array_equal(yc, np.arange(51))
        assert "point # of field" in v.plot.getAxis("left").labelText
        assert v.view_state()["segments"] == {"field": "index"}
    finally:
        v.close(); v.deleteLater()


def test_the_running_point_is_marked_by_index_on_its_own_run():
    v = _view(_loop_2d())
    try:
        v.x_combo.setCurrentText("x"); v.y_combo.setCurrentText("field")
        v.set_segment_choice("field", 1)                 # the down run
        # field 50 at grid index 15 is on the down run: marked there
        v.set_marker({"field": 50.0, "x": 1.0}, index={"field": 15, "x": 1})
        assert v.mark_rect.isVisible()
        r = v.mark_rect.rect()
        assert r.top() < 50 < r.bottom() and r.left() < 1 < r.right()
        # field 50 at grid index 5 is on the FIRST run: not on this picture
        v.set_marker({"field": 50.0, "x": 1.0}, index={"field": 5, "x": 1})
        assert not v.mark_rect.isVisible()
    finally:
        v.close(); v.deleteLater()


def test_a_held_slider_on_an_up_and_down_axis_says_its_run():
    from apps.data_view import NONE_TEXT, SegDimRow
    v = _view(_loop_2d())
    try:
        v.x_combo.setCurrentText("x"); v.y_combo.setCurrentText(NONE_TEXT)
        (row,) = v._rows
        assert isinstance(row, SegDimRow) and row.dim == "field"
        row.slider.setValue(15)                           # by POINT INDEX
        assert row.value.text() == "50 mT · seg 2 down"
        row.slider.setValue(5)
        assert row.value.text() == "50 mT · seg 1 up"
        _, xc, _, arr, _ = v._shown
        assert arr[1] == pytest.approx(50 + 1000)
    finally:
        v.close(); v.deleteLater()
