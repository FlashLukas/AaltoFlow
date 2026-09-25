"""Reading a value off the live RESULT plot: hover shows it, a click holds it.

From the rig, 2026-09-24: "i want to read the value from the plot... just by
pointing or adding a clicking capability which shows the value".
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


def _view(ds):
    from PySide6 import QtWidgets
    from apps.data_view import DataView
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    v = DataView(); v.resize(700, 500); v.show()
    v.set_dataset(ds)
    for _ in range(5):
        app.processEvents()
    return v


def _scene(v, x, y):
    from PySide6 import QtCore
    return v.plot.vb.mapViewToScene(QtCore.QPointF(x, y))


class _Click:
    def __init__(self, pos):
        from PySide6 import QtCore
        self._pos, self._b = pos, QtCore.Qt.LeftButton
    def scenePos(self): return self._pos
    def button(self): return self._b


def test_pointing_at_a_pixel_reads_its_value_and_a_click_holds_it():
    # z = 10*x + y, so every pixel's value says where it is; y runs DOWNWARDS
    # (a magnet sweep 100 -> 0), which the view flips -- the cursor must follow
    x = np.array([0.0, 1.0, 2.0, 3.0]); y = np.array([30.0, 20.0, 10.0])
    z = 10 * x[None, :] + y[:, None]
    z[0, 3] = np.nan                                    # not measured yet
    ds = xr.Dataset({"sig": (("y", "x"), z, {"units": "V"})},
                    coords={"y": y, "x": x})
    v = _view(ds)
    try:
        v.x_combo.setCurrentText("x"); v.y_combo.setCurrentText("y")
        # each pixel is centred on its coordinate
        pos = _scene(v, 2.2, 21)                        # x pixel 2, y pixel 1
        v._hover(pos)
        assert "x = 2   y = 20" in v.readout.text()
        assert "sig = 40 V" in v.readout.text()

        v._clicked(_Click(pos))
        assert v._held == (2.0, 20.0) and v.vline.isVisible()
        v._hover(_scene(v, 0.1, 11))                    # a held cursor ignores hovering
        assert "sig = 40 V" in v.readout.text()

        z2 = z.copy(); z2[1, 2] = 99                    # live redraw: the value updates
        v.set_dataset(ds.assign(sig=(("y", "x"), z2, {"units": "V"})))
        assert "sig = 99 V" in v.readout.text()

        v._clicked(_Click(_scene(v, 2.9, 29.9)))        # top-right: x=3, y=30 -> NaN
        assert "not measured yet" in v.readout.text()

        v._clicked(_Click(_scene(v, 50, 500)))          # off the data: release
        assert v._held is None and not v.vline.isVisible()
    finally:
        v.close(); v.deleteLater()


def test_a_line_plot_reads_the_nearest_point():
    x = np.linspace(0, 10, 11)
    ds = xr.Dataset({"sig": (("x",), x ** 2)}, coords={"x": x})
    v = _view(ds)
    try:
        v._hover(_scene(v, 3.2, 50))
        assert "x = 3" in v.readout.text() and "sig = 9" in v.readout.text()
        v._clicked(_Click(_scene(v, 7.4, 0)))
        assert v.dot.isVisible() and "sig = 49" in v.readout.text()
    finally:
        v.close(); v.deleteLater()
