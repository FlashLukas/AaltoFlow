"""The live RESULT map keeps one-point features when it is drawn small.

From the rig, 2026-10-03: a spectrum-analyser tone -- ONE frequency point in a
21 000-point sweep -- showed as dashes or not at all on the Measurement tab's
map, while zoomed in it was all there. A map with more points than the screen
has pixels has to combine blocks of points into one pixel; Qt's own shrinking
SAMPLES rows/columns (and drops a one-point line), pyqtgraph's autoDownsample
AVERAGES them (and dilutes it ~100x). AaltoView's MapImage can take the block
MAX (keeps peaks) or MIN (keeps dips) instead; the live map now uses it.

Only the drawing changes -- the data is never touched -- so the test looks at
what the image item hands to Qt, not at the dataset.
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
pg = pytest.importorskip("pyqtgraph")
xr = pytest.importorskip("xarray")

N_FREQ = 21_000
TONE = 10_500                    # the one frequency point that carries the tone


def _spectrum(unit="dBm"):
    """4 rows x 21 000 frequencies of -75 dBm floor, one -20 dBm tone column."""
    f = np.linspace(1e9, 3e9, N_FREQ)
    y = np.arange(4.0)
    z = np.full((4, N_FREQ), -75.0)
    z[:, TONE] = -20.0
    return xr.Dataset({"power": (("y", "frequency"), z, {"units": unit})},
                      coords={"y": y, "frequency": f})


@pytest.fixture
def view():
    from PySide6 import QtWidgets
    from apps.data_view import DataView
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    v = DataView()
    v.resize(420, 400)
    # the PLOT area fixed small: inside it, after the axis and the colour
    # bar, the map itself is ~200 px wide (the controls above have minimum
    # widths of their own, so resizing the whole widget would not do it)
    v.glw.setFixedSize(300, 220)
    v.show()
    app.processEvents()
    yield v
    v.close(); v.deleteLater()


def _drawn(view, monkeypatch):
    """The array the image item actually turns into pixels (after its block
    reduction), captured where pyqtgraph hands it to Qt."""
    from PySide6 import QtWidgets
    from pyqtgraph.graphicsItems import ImageItem as mod
    seen = []
    real = mod.functions_qimage.try_make_qimage

    def spy(image, *a, **kw):
        seen.append(np.array(image, dtype=float))
        return real(image, *a, **kw)

    monkeypatch.setattr(mod.functions_qimage, "try_make_qimage", spy)
    QtWidgets.QApplication.instance().processEvents()
    view.img.render()
    assert seen, "the map was not rendered"
    return seen[-1]


def test_the_live_map_is_aaltoviews_mapimage(view):
    """One copy of the block reduction, the one AaltoView tests -- not a second
    one here to keep in step."""
    from aaltoview.apps.viewer import MapImage
    assert isinstance(view.img, MapImage)
    assert view.img.autoDownsample
    items = [view.reduce_combo.itemData(i) for i in range(view.reduce_combo.count())]
    assert items == ["mean", "max", "min"]


def test_a_one_point_tone_survives_max_but_not_average(view, monkeypatch):
    view.set_dataset(_spectrum(unit="V"))         # V: the default stays "average"
    view.x_combo.setCurrentText("frequency"); view.y_combo.setCurrentText("y")
    assert view.reduce_combo.currentData() == "mean"

    drawn = _drawn(view, monkeypatch)
    # really squeezed: far fewer pixel columns than points
    assert drawn.shape[1] <= 250, drawn.shape
    assert drawn.max() < -60.0                    # averaged into the floor

    view.reduce_combo.setCurrentIndex(view.reduce_combo.findData("max"))
    drawn = _drawn(view, monkeypatch)
    assert drawn.max() == -20.0                   # the tone, exactly
    assert np.median(drawn) == -75.0              # and the floor is still the floor

    # the dataset itself is never touched by the drawing
    assert float(view.ds["power"].max()) == -20.0
    assert int((view.ds["power"].values == -20.0).sum()) == 4


def test_a_dbm_detector_defaults_to_max(view):
    """A dBm map is almost always a spectrum, where the narrow peaks are the
    point -- so max is its default. Anything else starts on average."""
    view.set_dataset(_spectrum(unit="dBm"))
    assert view.reduce_combo.currentData() == "max"
    assert view.img.reduce == "max"
    # the operator's choice is KEPT through the live redraws of a running scan
    view.reduce_combo.setCurrentIndex(view.reduce_combo.findData("min"))
    view.set_dataset(_spectrum(unit="dBm"))
    assert view.reduce_combo.currentData() == "min"


def test_the_measurement_pane_uses_it():
    """The Measurement tab's RESULT pane is this DataView (not a plain
    pg.ImageItem somewhere else)."""
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from aaltoview.apps.viewer import MapImage
    from apps.scan_builder import ScanBuilder
    b = ScanBuilder(embedded=True)
    try:
        assert isinstance(b.img, MapImage)
        assert b.img.autoDownsample
    finally:
        b.close(); b.deleteLater()
