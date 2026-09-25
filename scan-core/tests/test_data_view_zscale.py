"""The colour (z) scale of the live RESULT map.

From the rig, 2026-09-24: "when i slide the white slider the units change to
large values of 1. Bug? Also where do i fix the z scale?" -- pyqtgraph's colour
bar rounds dragged levels to multiples of `rounding` (default 1), so a map of
0.004..0.014 snapped to 0..1 on the first drag.
"""

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


def _ds(scale=1.0):
    x = np.arange(20.0); y = np.arange(10.0)
    z = (0.004 + 0.010 * np.linspace(0, 1, 200).reshape(10, 20)) * scale
    return xr.Dataset({"r": (("y", "x"), z, {"units": "V"}),
                       "other": (("y", "x"), z * 1000)},
                      coords={"y": y, "x": x})


@pytest.fixture
def view():
    from PySide6 import QtWidgets
    from apps.data_view import DataView
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    v = DataView(); v.resize(700, 500); v.show()
    v.set_dataset(_ds())
    v.det_combo.setCurrentText("r")
    v.x_combo.setCurrentText("x"); v.y_combo.setCurrentText("y")
    yield v
    v.close(); v.deleteLater()


def test_dragging_the_colour_bar_keeps_small_levels(view):
    lo, hi = view.cbar.levels()
    assert 0.003 < lo < hi < 0.015
    assert view.cbar.rounding <= 1e-5                # scaled to the data, not 1
    # what the mouse does: nudge the LOWER handle up a little (its rest position
    # is 63 in the bar's own coordinates), then release. pyqtgraph computes the
    # new levels in _regionChanging -- the step that rounded to whole units.
    view.cbar.region.setRegion((70, 191))
    view.cbar._regionChanged()                       # "drag finished"
    new_lo, new_hi = view._z_manual
    assert lo < new_lo < 0.006                       # moved a bit, stayed on the data
    assert new_hi == pytest.approx(hi, rel=1e-3)     # the upper handle was not touched
    assert not view.z_auto.isChecked()
    assert float(view.z_lo.text()) == pytest.approx(new_lo, rel=1e-5)


def test_the_old_rounding_really_was_the_bug(view):
    """Executable record: with pyqtgraph's default rounding=1 the same nudge
    throws the scale out to whole units."""
    view.cbar.rounding = 1.0
    view.cbar.region.setRegion((70, 191))
    lo, hi = view.cbar.levels()
    assert hi - lo >= 1.0


def test_a_fixed_range_survives_live_redraws_and_auto_gives_it_back(view):
    view.z_lo.setText("0.005"); view.z_hi.setText("0.01")
    view.z_lo.editingFinished.emit()
    assert view.cbar.levels() == pytest.approx((0.005, 0.01))
    assert not view.z_auto.isChecked()

    view.set_dataset(_ds(scale=2.0))                 # a live redraw with new data
    assert view.cbar.levels() == pytest.approx((0.005, 0.01))

    view.z_auto.setChecked(True)                     # back to percentiles of the NEW data
    lo, hi = view.cbar.levels()
    assert lo > 0.008 and hi > 0.02


def test_unticking_auto_freezes_the_range_on_screen(view):
    shown = view.cbar.levels()
    view.z_auto.setChecked(False)
    assert view._z_manual == pytest.approx(shown)


def test_switching_detector_returns_to_auto(view):
    view.set_z_range(0.005, 0.01)
    view.det_combo.setCurrentText("other")
    assert view.z_auto.isChecked() and view._z_manual is None
    assert view.cbar.levels()[1] > 1                 # scaled to "other", not held at 0.01
