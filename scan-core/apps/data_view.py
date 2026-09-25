"""
data_view.py -- look at an N-dimensional scan two dimensions at a time.

A measurement of frequency x X x Y is a cube; a screen is flat. This widget is
the honest way across: you say which two dims are the picture, and every dim
left over gets a row of its own -- hold it at one value (a slider you can scrub)
or average over it (all of it, or a range).

The rows are BUILT FROM THE DATA, never hardcoded. A 2-D scan gets no rows, a
5-D scan gets three, and a detector that sweeps its own frequency in hardware
(a VNA trace) contributes its axis like any other. Nothing here knows what an
instrument is.

The arithmetic lives in aaltoview (aaltoview.view) and is tested
without a screen; this file is the cockpit for it, used as the RESULT pane
of the Measurement tab (live, while the scan runs). Saved files are opened
in the data viewer (the suite's Data tab).

Why the old pane was not enough: it always drew the innermost two dims and
sliced everything else at index 0 -- so a frequency cube showed you the first
frequency, and said nothing about it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyqtgraph as pg
import xarray as xr
from PySide6 import QtCore, QtWidgets

from aaltoview.apps.widgets import DimRow  # noqa: F401  (one row widget, shared)
from scan_core import view as V
from apps.theme import C

NONE_TEXT = "— none —"


class DataView(QtWidgets.QWidget):
    """Detector + two image axes + one row per leftover dimension + the plot."""

    def __init__(self, allow_open: bool = False, title: str = "RESULT"):
        super().__init__()
        self.ds: xr.Dataset | None = None
        self.path: Path | None = None
        #: Where "Open measurement…" starts looking. The suite points it at the
        #: data directory, which is where every run autosaves itself.
        self.default_dir: Path | None = None
        self._rows: list[DimRow] = []
        self._signature = None
        self._drawing = False
        #: what is on screen now, for the cursor: (kind, xc, yc, arr, z_unit),
        #: coordinates ascending exactly as drawn. None until the first draw.
        self._shown = None
        #: a HELD cursor, stored as COORDINATES (not indices), so it stays on the
        #: same physical point while a live scan redraws and the value fills in
        self._held: tuple[float, float | None] | None = None
        #: manual colour range, or None for auto
        self._z_manual: tuple[float, float] | None = None
        self._setting_levels = False

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        self.top = top = QtWidgets.QHBoxLayout()
        tag = QtWidgets.QLabel(title); tag.setObjectName("tag")
        top.addWidget(tag)
        if allow_open:
            self.open_btn = QtWidgets.QPushButton("Open measurement…")
            self.open_btn.clicked.connect(self._open_dialog)
            top.addWidget(self.open_btn)
        #: where add_action() puts a host's own button, so a page does not need
        #: a second toolbar of its own above this one
        self._action_slot = top.count()
        top.addStretch(1)
        top.addWidget(QtWidgets.QLabel("show"))
        self.det_combo = QtWidgets.QComboBox(); self.det_combo.setMinimumWidth(130)
        self.det_combo.currentIndexChanged.connect(self._det_changed)
        top.addWidget(self.det_combo)
        self.part_combo = QtWidgets.QComboBox()
        self.part_combo.addItems(["|z|", "arg z", "Re z", "Im z"])
        self.part_combo.setFixedWidth(72)
        self.part_combo.setVisible(False)
        self.part_combo.setToolTip(
            "Which part of a complex detector to colour by.\n"
            "An average is taken on the COMPLEX value first, then this is\n"
            "applied — coherent averaging, which cancels noise (and would\n"
            "cancel the signal too if the phase were drifting).")
        self.part_combo.currentIndexChanged.connect(lambda *_: self.refresh())
        top.addWidget(self.part_combo)
        v.addLayout(top)

        axr = QtWidgets.QHBoxLayout()
        axr.addWidget(QtWidgets.QLabel("X"))
        self.x_combo = QtWidgets.QComboBox()
        self.x_combo.setFixedWidth(180)
        self.x_combo.setToolTip("Which dimension runs along the bottom of the plot.")
        self.x_combo.currentIndexChanged.connect(self._axes_changed)
        axr.addWidget(self.x_combo)
        axr.addSpacing(14)
        axr.addWidget(QtWidgets.QLabel("Y"))
        self.y_combo = QtWidgets.QComboBox()
        self.y_combo.setFixedWidth(180)
        self.y_combo.setToolTip(f"Which dimension runs up the side.\n"
                                f"{NONE_TEXT} plots a line against X instead of an image.")
        self.y_combo.currentIndexChanged.connect(self._axes_changed)
        axr.addWidget(self.y_combo)
        axr.addStretch(1)
        # The colour (z) scale. AUTO = 1st..99th percentile of what is shown,
        # recomputed at every redraw. Typing a limit or dragging the colour
        # bar's handles switches to MANUAL, and a manual scale is KEPT through
        # the live redraws of a running scan (auto would undo it twice a second).
        axr.addWidget(QtWidgets.QLabel("colour"))
        self.z_auto = QtWidgets.QCheckBox("auto")
        self.z_auto.setChecked(True)
        self.z_auto.setToolTip("Colour range from the 1st..99th percentile of the map,\n"
                               "so one hot pixel does not flatten it. Untick, type\n"
                               "min/max or drag the colour bar to fix the range.")
        self.z_auto.toggled.connect(self._z_auto_toggled)
        axr.addWidget(self.z_auto)
        self.z_lo = QtWidgets.QLineEdit(); self.z_hi = QtWidgets.QLineEdit()
        for w, tip in ((self.z_lo, "colour scale minimum"),
                       (self.z_hi, "colour scale maximum")):
            w.setFixedWidth(80); w.setToolTip(tip); w.setPlaceholderText(tip.split()[-1])
            # parsed with float(), not a QDoubleValidator: the validator follows
            # the Windows locale and would want "0,004" (root gotcha #18)
            w.editingFinished.connect(self._z_typed)
            axr.addWidget(w)
        v.addLayout(axr)

        self.rows_box = QtWidgets.QVBoxLayout()
        self.rows_box.setSpacing(3)
        v.addLayout(self.rows_box)

        self.glw = pg.GraphicsLayoutWidget()
        self.plot = self.glw.addPlot()
        self.plot.setLabel("bottom", "—"); self.plot.setLabel("left", "—")
        # row-major declared HERE, not inherited from a global set by whoever
        # imported scan_builder first -- without it the map draws transposed
        self.img = pg.ImageItem(axisOrder="row-major")
        self.plot.addItem(self.img)
        self.cbar = pg.ColorBarItem(colorMap=pg.colormap.get("magma"))
        self.cbar.setImageItem(self.img, insert_in=self.plot)
        self.cbar.sigLevelsChangeFinished.connect(self._bar_dragged)
        self.curve = self.plot.plot([], [], pen=pg.mkPen(C["accent"], width=2))
        # the cursor: a dashed crosshair on the map, a dot on a line plot
        pen = pg.mkPen(C["accent"], width=1, style=QtCore.Qt.DashLine)
        self.vline = pg.InfiniteLine(angle=90, pen=pen)
        self.hline = pg.InfiniteLine(angle=0, pen=pen)
        self.dot = pg.ScatterPlotItem(size=9, pen=pg.mkPen(C["text"]),
                                      brush=pg.mkBrush(C["accent"]))
        for it in (self.vline, self.hline, self.dot):
            it.setVisible(False)
            self.plot.addItem(it, ignoreBounds=True)
        self.plot.scene().sigMouseMoved.connect(self._hover)
        self.plot.scene().sigMouseClicked.connect(self._clicked)
        v.addWidget(self.glw, 1)

        self.readout = QtWidgets.QLabel(self.HINT)
        self.readout.setStyleSheet(f"color:{C['accent']};")
        self.readout.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        v.addWidget(self.readout)

        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color:{C['muted']}; font-size:11px;")
        v.addWidget(self.status)

    HINT = "point at the plot to read a value · click to hold the cursor"

    def add_action(self, w: QtWidgets.QWidget) -> None:
        """Put a host's button in this widget's own toolbar, next to Open."""
        self.top.insertWidget(self._action_slot, w)
        self._action_slot += 1

    # ---- data in ----------------------------------------------------------
    def set_dataset(self, ds: xr.Dataset | None, path: Path | None = None) -> None:
        """Show `ds`. Called once for a file, and repeatedly during a live run --
        which is why the controls are only rebuilt when the SHAPE changes. A
        slider that jumped back to zero every redraw would be unusable."""
        self.ds = ds
        self.path = Path(path) if path else None
        if ds is None:
            return
        names = V.detector_names(ds)
        listed = [self.det_combo.itemText(i) for i in range(self.det_combo.count())]
        if names != listed:
            self._fill(self.det_combo, names, keep=self.det_combo.currentText())
        self._rebuild_controls()
        self.refresh()

    def load_file(self, path: str | Path) -> None:
        from scan_core.data import load
        ds = load(path)
        # read it fully now: the file handle is not kept open behind a live plot
        self.set_dataset(ds.load(), Path(path))
        ds.close()

    # ---- controls ---------------------------------------------------------
    @staticmethod
    def _fill(combo: QtWidgets.QComboBox, items: list[str], keep: str | None = None):
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(items)
        if keep and keep in items:
            combo.setCurrentText(keep)
        combo.blockSignals(False)

    def _current_da(self) -> xr.DataArray | None:
        if self.ds is None or not len(self.ds.data_vars):
            return None
        name = self.det_combo.currentText() or V.detector_names(self.ds)[0]
        try:
            return V.detector(self.ds, name)
        except Exception:
            return None

    def _rebuild_controls(self):
        """Axis combos when the SHAPE moves; dim rows when the axis choice does.

        Both are guarded, because this runs on every live redraw. Tearing the
        rows down and building them again several times a second would fight the
        slider the operator is dragging, and reset it while they drag.
        """
        da = self._current_da()
        if da is None:
            return
        dims = list(da.dims)
        sig = (self.det_combo.currentText(), tuple((d, int(da.sizes[d])) for d in dims))
        if sig != self._signature:
            # keep the operator's axis choice when only the detector changed but
            # the cube has the same shape -- switching |z| to arg z should not
            # throw away the view they set up.
            same_dims = self._signature is not None and self._signature[1] == sig[1]
            self._signature = sig
            self.part_combo.setVisible(bool(np.iscomplexobj(da.values)))
            dx, dy = V.default_axes(da)
            x_keep = self.x_combo.currentText() if same_dims else None
            y_keep = self.y_combo.currentText() if same_dims else None
            self._fill(self.x_combo, dims, keep=x_keep or dx)
            self._fill(self.y_combo, [NONE_TEXT] + dims, keep=y_keep or (dy or NONE_TEXT))

        want = [d for d in dims
                if d not in (self.x_combo.currentText(), self.y_combo.currentText())]
        if ([r.dim for r in self._rows] == want
                and all(r.n == da.sizes[r.dim] for r in self._rows)):
            return
        keep_rows = {r.dim: r.state() for r in self._rows}
        for r in self._rows:
            self.rows_box.removeWidget(r)
            # setParent(None) as well: removeWidget only takes it out of the
            # LAYOUT, and a widget that still has a parent keeps painting where
            # it last was -- the old row shows through the new one until the
            # deleteLater finally lands.
            r.setParent(None)
            r.deleteLater()
        self._rows = []
        for d in want:
            coords = (np.asarray(self.ds[d].values) if d in self.ds.coords
                      else np.arange(da.sizes[d]))
            unit = self.ds[d].attrs.get("units", "") if d in self.ds.coords else ""
            row = DimRow(d, coords, unit)
            if d in keep_rows:
                row.restore(keep_rows[d])
            row.changed.connect(self.refresh)
            self.rows_box.addWidget(row)
            self._rows.append(row)

    def _det_changed(self):
        # a fixed range belongs to the detector it was set on -- a lock-in
        # voltage range makes no sense on a stage position
        self.z_auto.setChecked(True)
        self._rebuild_controls()
        self.refresh()

    def _axes_changed(self):
        if self._drawing:
            return
        self._rebuild_controls()
        self.refresh()

    # ---- drawing ----------------------------------------------------------
    def refresh(self):
        da = self._current_da()
        if da is None:
            return
        x = self.x_combo.currentText()
        y = self.y_combo.currentText()
        y = None if y in ("", NONE_TEXT) else y
        if x not in da.dims:
            return
        if y == x:
            self.status.setText("X and Y must be different dimensions.")
            return
        part = {"|z|": "abs", "arg z": "arg", "Re z": "real",
                "Im z": "imag"}.get(self.part_combo.currentText(), "abs")
        slices = {r.dim: r.slice_() for r in self._rows}
        try:
            red = V.reduce_cube(da, x, y, slices, part)
        except Exception as exc:
            self.status.setText(str(exc))
            return

        self._drawing = True
        try:
            self._draw(red)
        finally:
            self._drawing = False

    def _draw(self, red: V.Reduced):
        ds = self.ds
        arr = np.asarray(red.data.values, dtype=float)
        if red.y:
            self.curve.setData([], [])
            xc = np.asarray(self._coord(red.x, arr.shape[1]), dtype=float)
            yc = np.asarray(self._coord(red.y, arr.shape[0]), dtype=float)
            # The image is stretched over min..max of each coordinate, and an
            # ImageItem always draws column 0 at the LEFT. A scan that ran
            # downwards (field 100 -> 0 mT, the usual way to sweep a magnet) has
            # column 0 at 100 mT, so without this flip the whole map is drawn
            # mirrored -- a Kittel line that rises with field appears to fall.
            # Nothing errors; the picture is simply wrong. Put both axes in
            # ascending order before drawing.
            if xc.size > 1 and xc[0] > xc[-1]:
                arr, xc = arr[:, ::-1], xc[::-1]
            if yc.size > 1 and yc[0] > yc[-1]:
                arr, yc = arr[::-1, :], yc[::-1]
            arr = np.ascontiguousarray(arr)        # a reversed view has negative strides
            lo, hi = self._z_manual or self._levels(arr)
            self.img.setImage(arr, levels=(lo, hi))
            # Each pixel CENTRED on its coordinate: half a step of margin at both
            # ends. Stretching n pixels edge-to-edge over min..max instead put
            # pixel k's centre off its coordinate (index 49 of 0..49 drew from
            # 48.0 to 49.0), so pointing at "49" on the axis read another point.
            x0, x1 = self._extent(xc)
            y0, y1 = self._extent(yc)
            self.img.setRect(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0))
            self.img.setVisible(True)
            self._setting_levels = True
            try:
                self.cbar.setImageItem(self.img)
                # pyqtgraph ROUNDS dragged levels to multiples of `rounding`,
                # default 1: on a 0.004..0.014 map the first drag snapped the
                # range to 0..1. Scale it to the data -- ~1/1000 of the span.
                self.cbar.rounding = self._rounding(lo, hi)
                self.cbar.setLevels((lo, hi))
            finally:
                self._setting_levels = False
            if self._z_manual is None:
                self._show_z(lo, hi)
            self.plot.setLabel("bottom", self._axis_label(red.x))
            self.plot.setLabel("left", self._axis_label(red.y))
            self._shown = ("map", xc, yc, arr, red.data.attrs.get("units", ""))
        else:
            self.img.setVisible(False)
            xc = self._coord(red.x, arr.shape[0])
            self.curve.setData(np.asarray(xc, dtype=float), arr)
            self.plot.setLabel("bottom", self._axis_label(red.x))
            unit = red.data.attrs.get("units", "")
            self.plot.setLabel("left", f"{self.det_combo.currentText()} [{unit}]".strip())
            self._shown = ("line", np.asarray(xc, dtype=float), None, arr, unit)
        self._update_cursor()

        bits = []
        if red.averaged > 1:
            bits.append(f"each point is a mean of {red.averaged}")
        if red.coverage < 0.999:
            bits.append(f"{red.coverage * 100:.0f} % measured so far "
                        f"(missing points are skipped, not zero)")
        if red.note:
            bits.append(red.note)
        if self.path:
            bits.append(self.path.name)
        self.status.setText(" · ".join(bits))

    # ---- cursor -------------------------------------------------------------
    def _hit(self, scene_pos):
        """(i, j) of the point under `scene_pos`, or None if it is off the data.

        On a map the answer is the PIXEL under the mouse: the image is stretched
        evenly over min..max, so on an uneven axis the nearest coordinate and
        the pixel you are pointing at can differ, and the pixel is what you see.
        On a line plot it is the nearest measured x.
        """
        if self._shown is None or not self.plot.sceneBoundingRect().contains(scene_pos):
            return None
        p = self.plot.vb.mapSceneToView(scene_pos)
        kind, xc, yc, arr, _ = self._shown
        if kind == "line":
            if xc.size == 0:
                return None
            return int(np.nanargmin(np.abs(xc - p.x()))), None
        r = self.img.mapRectToView(self.img.boundingRect())
        if not r.contains(p):
            return None
        ny, nx = arr.shape
        i = min(nx - 1, int((p.x() - r.left()) / r.width() * nx))
        j = min(ny - 1, int((p.y() - r.top()) / r.height() * ny))
        return i, j

    def _text(self, i: int, j: int | None) -> str:
        kind, xc, yc, arr, unit = self._shown
        x_dim = self.x_combo.currentText()
        z = arr[i] if j is None else arr[j, i]
        zt = "not measured yet" if not np.isfinite(z) else f"{z:.6g} {unit}".strip()
        where = f"{x_dim} = {xc[i]:g}"
        if j is not None:
            where += f"   {self.y_combo.currentText()} = {yc[j]:g}"
        return f"{where}   →   {self.det_combo.currentText()} = {zt}"

    def _hover(self, pos):
        if self._held is not None or self._shown is None:
            return                     # a held cursor keeps the readout
        hit = self._hit(pos)
        self.readout.setText(self.HINT if hit is None
                             else self._text(*hit) + "   (click to hold)")

    def _clicked(self, ev):
        if ev.button() != QtCore.Qt.LeftButton:
            return                     # right-click is pyqtgraph's menu
        hit = self._hit(ev.scenePos())
        if hit is None:                # a click off the data lets go
            self._held = None
            self._update_cursor()
            return
        kind, xc, yc, _, _ = self._shown
        i, j = hit
        self._held = (float(xc[i]), None if j is None else float(yc[j]))
        self._update_cursor()

    def _update_cursor(self):
        """Re-place the held cursor on the current picture (after every redraw)."""
        shown = self._shown
        hold = self._held
        if hold is not None and shown is not None:
            kind, xc, yc, arr, _ = shown
            if (kind == "map") != (hold[1] is not None) or xc.size == 0:
                hold = None            # map <-> line switch: the point means nothing now
        if hold is None:
            self._held = None
            for it in (self.vline, self.hline, self.dot):
                it.setVisible(False)
            self.readout.setText(self.HINT)
            return
        i = int(np.argmin(np.abs(xc - hold[0])))
        if kind == "map":
            j = int(np.argmin(np.abs(yc - hold[1])))
            self.vline.setPos(float(xc[i])); self.hline.setPos(float(yc[j]))
            self.vline.setVisible(True); self.hline.setVisible(True)
            self.dot.setVisible(False)
        else:
            j = None
            y = float(arr[i]) if np.isfinite(arr[i]) else float("nan")
            self.dot.setData([float(xc[i])], [y])
            self.dot.setVisible(bool(np.isfinite(y)))
            self.vline.setVisible(False); self.hline.setVisible(False)
        self.readout.setText(self._text(i, j) + "   (held -- click off the data to release)")

    def _coord(self, dim: str, n: int) -> np.ndarray:
        if self.ds is not None and dim in self.ds.coords:
            return np.asarray(self.ds[dim].values)
        return np.arange(n)

    def _axis_label(self, dim: str) -> str:
        unit = (self.ds[dim].attrs.get("units", "")
                if self.ds is not None and dim in self.ds.coords else "")
        return f"{dim} [{unit}]" if unit else dim

    # ---- colour scale ----------------------------------------------------------
    @staticmethod
    def _rounding(lo: float, hi: float) -> float:
        """A power of ten about 1/1000 of the span (never 0)."""
        span = abs(hi - lo) or abs(hi) or 1.0
        return 10.0 ** np.floor(np.log10(span / 1000.0))

    def _show_z(self, lo: float, hi: float):
        for w, val in ((self.z_lo, lo), (self.z_hi, hi)):
            if not w.hasFocus():           # never overwrite what is being typed
                w.setText(f"{val:.6g}")

    def set_z_range(self, lo: float | None, hi: float | None = None):
        """Fix the colour scale to lo..hi, or back to auto with lo=None."""
        if lo is None or hi is None:
            self._z_manual = None
        else:
            lo, hi = float(lo), float(hi)
            if hi < lo:
                lo, hi = hi, lo
            if hi == lo:
                hi = lo + (abs(lo) * 1e-6 or 1e-12)
            self._z_manual = (lo, hi)
            self._show_z(lo, hi)
        self.z_auto.blockSignals(True)
        self.z_auto.setChecked(self._z_manual is None)
        self.z_auto.blockSignals(False)
        self.refresh()

    def _z_auto_toggled(self, on: bool):
        if on:
            self.set_z_range(None)
        else:                              # freeze what is on screen now
            self.set_z_range(*self.cbar.levels())

    def _z_typed(self):
        try:
            lo, hi = float(self.z_lo.text()), float(self.z_hi.text())
        except ValueError:
            return                         # half-typed: wait for a number
        if self._z_manual != (lo, hi):
            self.set_z_range(lo, hi)

    def _bar_dragged(self, bar):
        """Dragging a colour bar handle is a manual limit."""
        if not self._setting_levels:
            self.set_z_range(*bar.levels())

    @staticmethod
    def _extent(c: np.ndarray) -> tuple[float, float]:
        """Image edges so pixel k is centred on c[k] (for an even grid)."""
        lo, hi = float(np.min(c)), float(np.max(c))
        n = c.size
        # a single-point axis has no step; give it a unit-ish width so it shows
        half = (hi - lo) / (n - 1) / 2 if n > 1 and hi > lo else max(abs(lo) * 1e-3, 0.5)
        return lo - half, hi + half

    @staticmethod
    def _levels(arr: np.ndarray) -> tuple[float, float]:
        """Colour range from percentiles, so one hot pixel does not flatten the map."""
        fin = arr[np.isfinite(arr)]
        lo = float(np.percentile(fin, 1)) if fin.size else 0.0
        hi = float(np.percentile(fin, 99)) if fin.size else 1.0
        return (lo, hi + 1e-9) if hi <= lo else (lo, hi)

    # ---- file -------------------------------------------------------------
    def _open_dialog(self):
        start = str(self.path.parent if self.path else (self.default_dir or ""))
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open measurement", start, "netCDF (*.nc);;All files (*)")
        if not fn:
            return
        try:
            self.load_file(fn)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Open measurement",
                                          f"Could not read {Path(fn).name}:\n{exc}")
