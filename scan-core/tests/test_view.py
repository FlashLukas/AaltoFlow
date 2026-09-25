"""Looking at a cube two dimensions at a time.

The arithmetic (scan_core/view.py) is tested without a screen; the widget
(apps/data_view.py) is tested offscreen for the things that are easy to break:
controls built from the data, and choices surviving a live redraw.
"""

import os

import numpy as np
import pytest
import xarray as xr

from scan_core.view import (Slice, apply_part, default_axes, detector,
                            detector_names, reduce_cube)


def _cube() -> xr.Dataset:
    """freq x y x x, with a value that says exactly where it came from."""
    f = np.array([1.0, 2.0, 3.0, 4.0])
    y = np.array([0.0, 10.0, 20.0])
    x = np.array([0.0, 1.0])
    vals = (f[:, None, None] * 100 + y[None, :, None] + x[None, None, :])
    return xr.Dataset(
        {"kerr": (("freq", "y", "x"), vals, {"units": "mdeg"})},
        coords={"freq": ("freq", f, {"units": "GHz"}),
                "y": ("y", y, {"units": "um"}),
                "x": ("x", x, {"units": "um"})})


# ─────────────────────────────── the reduction ────────────────────────────────

def test_hold_a_dim_at_one_value():
    ds = _cube()
    r = reduce_cube(ds["kerr"], x="x", y="y", slices={"freq": Slice("at", 2)})
    assert r.data.dims == ("y", "x")
    assert r.data.values[0, 0] == 300.0            # freq index 2 -> 3 GHz
    assert r.averaged == 1


def test_average_a_whole_dim_away():
    ds = _cube()
    r = reduce_cube(ds["kerr"], x="x", y="y", slices={"freq": Slice("mean", 0, None)})
    # mean of 100,200,300,400 = 250
    assert r.data.values[0, 0] == 250.0
    assert r.averaged == 4


def test_average_only_a_range():
    ds = _cube()
    r = reduce_cube(ds["kerr"], x="x", y="y", slices={"freq": Slice("mean", 1, 2)})
    assert r.data.values[0, 0] == 250.0            # mean of 200 and 300
    assert r.averaged == 2


def test_the_two_image_axes_are_the_operators_choice():
    """freq vs y, averaged over x -- the same cube, a different question."""
    ds = _cube()
    r = reduce_cube(ds["kerr"], x="freq", y="y", slices={"x": Slice("mean")})
    assert r.data.dims == ("y", "freq")
    assert r.data.shape == (3, 4)
    assert r.data.values[0, 0] == 100.5            # mean over x of 100 and 101


def test_y_none_gives_a_line():
    ds = _cube()
    r = reduce_cube(ds["kerr"], x="freq", y=None,
                    slices={"x": Slice("at", 0), "y": Slice("at", 0)})
    assert r.data.dims == ("freq",)
    assert r.data.values.tolist() == [100, 200, 300, 400]


def test_a_hole_in_the_cube_does_not_blank_the_average():
    """A scan that was aborted (or is still running) is full of NaN.

    A plain mean over a missing frequency returns NaN for the whole pixel, so
    ONE unmeasured point would erase the map. Skip them, and say how much of
    the average was real.
    """
    ds = _cube()
    ds["kerr"][3, :, :] = np.nan                   # the last frequency never ran
    r = reduce_cube(ds["kerr"], x="x", y="y", slices={"freq": Slice("mean")})
    assert r.data.values[0, 0] == 200.0            # mean of 100,200,300
    assert np.isfinite(r.data.values).all()
    assert r.coverage == pytest.approx(0.75)


def test_a_one_point_axis_survives():
    """A 1 x N scan is legal; squeezing it away would lose the image."""
    ds = _cube().isel(y=[0])
    r = reduce_cube(ds["kerr"], x="x", y="y", slices={"freq": Slice("at", 0)})
    assert r.data.shape == (1, 2)


def test_bad_axes_are_refused_by_name():
    ds = _cube()
    with pytest.raises(KeyError):
        reduce_cube(ds["kerr"], x="nope", y="y")
    with pytest.raises(ValueError):
        reduce_cube(ds["kerr"], x="x", y="x")


def test_default_axes_are_the_innermost_two():
    """Which is where a part-finished scan already has data."""
    ds = _cube()
    assert default_axes(ds["kerr"]) == ("x", "y")
    assert default_axes(ds["kerr"].isel(freq=0, y=0)) == ("x", None)


# ────────────────────────────────── complex ───────────────────────────────────

def _complex_ds() -> xr.Dataset:
    """A detector whose phase rotates with frequency, stored as a real/imag pair
    the way the engine writes it."""
    f = np.arange(4.0)
    z = np.exp(1j * np.pi * f / 2)[:, None] * np.ones((4, 2))
    return xr.Dataset(
        {"s21_real": (("freq", "x"), z.real, {"complex_pair": "s21", "complex_part": "real"}),
         "s21_imag": (("freq", "x"), z.imag, {"complex_pair": "s21", "complex_part": "imag"})},
        coords={"freq": f, "x": np.arange(2.0)})


def test_a_complex_pair_is_offered_as_one_name():
    ds = _complex_ds()
    assert detector_names(ds) == ["s21"]           # not s21_real / s21_imag
    assert np.iscomplexobj(detector(ds, "s21").values)


def test_averaging_complex_is_coherent_and_says_so():
    """|mean(z)| is not mean(|z|), and the difference is physics.

    Four unit phasors 90 degrees apart cancel: coherent averaging gives 0,
    incoherent gives 1. Reducing BEFORE taking the magnitude is what makes it
    coherent, which is the one you want on a stable phase.
    """
    ds = _complex_ds()
    r = reduce_cube(detector(ds, "s21"), x="x", y=None,
                    slices={"freq": Slice("mean")}, part="abs")
    assert r.data.values == pytest.approx([0.0, 0.0], abs=1e-12)
    assert "coherent" in r.note
    # and the incoherent answer, for contrast, is 1
    assert float(np.abs(detector(ds, "s21")).mean()) == pytest.approx(1.0)


def test_the_parts_of_a_complex_detector():
    ds = _complex_ds()
    da = detector(ds, "s21").isel(freq=1, x=0)     # i
    assert apply_part(da, "abs").values == pytest.approx(1.0)
    assert apply_part(da, "arg").values == pytest.approx(np.pi / 2)
    assert apply_part(da, "real").values == pytest.approx(0.0, abs=1e-12)
    assert apply_part(da, "imag").values == pytest.approx(1.0)
    assert apply_part(da, "arg").attrs["units"] == "rad"


# ─────────────────────────────── the widget ───────────────────────────────────

@pytest.fixture
def view():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from apps.data_view import DataView
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = DataView()
    yield w
    w.close()


def test_the_controls_are_built_from_the_data(view):
    """3 dims, 2 of them plotted -> exactly one slider row, for the third."""
    view.set_dataset(_cube())
    assert [r.dim for r in view._rows] == ["freq"]
    assert view.x_combo.currentText() == "x"
    assert view.y_combo.currentText() == "y"
    assert view._rows[0].n == 4
    assert "GHz" in view._rows[0].value.text()      # the value, not the index


def test_choosing_other_axes_moves_the_slider_row(view):
    view.set_dataset(_cube())
    view.x_combo.setCurrentText("freq")             # plot freq vs y, slice x
    assert [r.dim for r in view._rows] == ["x"]
    assert view.plot.getAxis("bottom").labelText.startswith("freq")


def test_a_live_redraw_does_not_reset_the_operators_choices(view):
    """set_dataset runs several times a second during a scan. If it rebuilt the
    controls each time, the slider would snap back while you were dragging it."""
    ds = _cube()
    view.set_dataset(ds)
    view._rows[0].slider.setValue(3)
    view.x_combo.setCurrentText("freq")
    rows_before = view._rows[0]
    view.set_dataset(ds)                            # the next live snapshot
    assert view.x_combo.currentText() == "freq"
    assert view._rows[0] is rows_before             # the SAME widget, not a new one


def test_two_dims_need_no_slider_rows(view):
    view.set_dataset(_cube().isel(freq=0))
    assert view._rows == []


def test_a_complex_detector_shows_the_part_chooser(view):
    view.set_dataset(_complex_ds())
    assert view.part_combo.isVisible() or view.part_combo.isVisibleTo(view)
    view.part_combo.setCurrentText("arg z")
    view.refresh()                                  # must not raise
    view.set_dataset(_cube())
    assert not view.part_combo.isVisibleTo(view)    # a real detector hides it


def test_a_scan_that_ran_downwards_is_not_drawn_mirrored(view):
    """A magnet is usually swept 100 -> 0 mT, so column 0 is the HIGH field. The
    image is stretched over min..max and always draws column 0 on the left, so
    without a flip the map came out mirrored: a Kittel line rising with field
    appeared to fall (found on a fake YIG field sweep, 2026-09-17)."""
    field = np.array([100.0, 50.0, 0.0])            # the order the scan measured
    freq = np.array([3.0, 2.0])                     # and a descending inner axis too
    vals = field[:, None] + 1000 * freq[None, :]    # value says where it came from
    ds = xr.Dataset({"u": (("field", "freq"), vals)},
                    coords={"field": ("field", field, {"units": "mT"}),
                            "freq": ("freq", freq, {"units": "GHz"})})
    view.set_dataset(ds)
    view.x_combo.setCurrentText("field")
    view.y_combo.setCurrentText("freq")
    view.refresh()
    img = view.img.image                            # row-major: [y, x]
    assert img[0, 0] == 0.0 + 2000                  # bottom-left = lowest field, lowest freq
    assert img[-1, -1] == 100.0 + 3000              # top-right = highest of both
    rect = view.img.mapRectToParent(view.img.boundingRect())
    # pixels are CENTRED on their coordinates (half a 50 mT step of margin), so
    # the first and last columns sit on 0 and 100 mT, not beside them
    assert rect.left() == pytest.approx(-25.0) and rect.right() == pytest.approx(125.0)
    assert rect.left() + rect.width() / 6 == pytest.approx(0.0)   # centre of column 0


def test_the_status_line_reports_a_partly_measured_scan(view):
    ds = _cube()
    ds["kerr"][2:, :, :] = np.nan
    view.set_dataset(ds)
    view._rows[0].mode.setCurrentText("mean all")
    assert "measured so far" in view.status.text()
    assert "mean of 4" in view.status.text()
