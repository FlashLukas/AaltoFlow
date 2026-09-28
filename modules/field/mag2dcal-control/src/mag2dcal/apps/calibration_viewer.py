"""A dialog that plots the measured calibration: field against drive voltage.

Pops up after "Run calibration" finishes, or after loading a saved curve. It
shows both axes and, for each, BOTH hysteresis legs -- the up leg solid, the
down leg dashed. The gap between the two lines IS the hysteresis, and it is the
quickest sanity check there is: if the two legs lie on top of each other the
magnet has (almost) none and the undershoot is doing nothing; if they are far
apart, a controller that does not know which leg it is on cannot be accurate to
better than that gap.

On a 200 mT curve a gap of half a millitesla is invisible, so the gap gets a
second, small panel of its own underneath: up minus down, in millitesla, against
the same voltage axis. That panel is the one to look at.

Shaped after clMag-control/src/clMag/apps/calibration_viewer.py, which plots the
same picture for the 1-axis magnet.
"""

from __future__ import annotations

from PySide6 import QtWidgets
import pyqtgraph as pg

from ..calibration import Calibration
from .theme import COLORS


class CalibrationViewer(QtWidgets.QDialog):
    def __init__(self, cal: Calibration, parent=None, title="Calibration curve"):
        super().__init__(parent)
        self.cal = cal
        self.setWindowTitle(title)
        self.resize(760, 560)

        root = QtWidgets.QVBoxLayout(self)

        head = QtWidgets.QLabel(cal.summary())
        head.setStyleSheet(f"color:{COLORS['text']}; font-weight:700; font-size:14px;")
        head.setWordWrap(True)
        sub = QtWidgets.QLabel(
            f"recorded with Hall  X {cal.hall.x_offset_V:.4f} V, "
            f"{cal.hall.x_mV_per_mT:+.5f} mV/mT   -   "
            f"Y {cal.hall.y_offset_V:.4f} V, {cal.hall.y_mV_per_mT:+.5f} mV/mT"
            + (f"   -   {cal.note}" if cal.note else ""))
        sub.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        sub.setWordWrap(True)
        root.addWidget(head)
        root.addWidget(sub)

        pg.setConfigOptions(antialias=True)
        plot = self._new_plot("field", "mT")
        # The legend goes TOP-LEFT: a magnet curve rises left to right, so that
        # corner is the empty one and the legend does not sit on the data.
        legend = plot.addLegend(offset=(10, 10))
        legend.setLabelTextColor(COLORS["muted"])

        # X in the accent colour, Y in the text colour -- the same pairing the
        # main window's strip chart uses, so the two read as one instrument.
        from PySide6.QtCore import Qt
        for i, (name, key) in enumerate((("X", "accent"), ("Y", "text"))):
            if i >= len(cal.axes):
                break
            axis = cal.axes[i]
            for leg, style, label in ((axis.up, Qt.SolidLine, "up"),
                                      (axis.down, Qt.DashLine, "down")):
                if not leg:
                    continue
                plot.plot([v for v, _ in leg], [b for _, b in leg],
                          pen=pg.mkPen(COLORS[key], width=2, style=style),
                          symbol="o", symbolSize=4,
                          symbolBrush=pg.mkBrush(COLORS[key]),
                          symbolPen=pg.mkPen(None),
                          name=f"{name} {label}")
        # Crosshairs at the origin: "zero volts -> zero field" is easy to check,
        # and the vertical gap between the two legs there is the remanence.
        plot.addLine(x=0, pen=pg.mkPen(COLORS["border"], width=1))
        plot.addLine(y=0, pen=pg.mkPen(COLORS["border"], width=1))
        root.addWidget(plot, 3)

        # No `units` here on purpose: pyqtgraph would helpfully re-prefix a
        # 0.8 mT axis into "mmT". The unit goes in the label text instead.
        gap_plot = self._new_plot("up - down (mT)", None)
        gap_plot.setMaximumHeight(170)
        gap_plot.setXLink(plot)
        gap_plot.addLine(y=0, pen=pg.mkPen(COLORS["border"], width=1))
        for i, key in enumerate(("accent", "text")):
            if i >= len(cal.axes):
                break
            axis = cal.axes[i]
            if not (axis.up and axis.down):
                continue
            # Sampled on the UP leg's voltages and interpolated on the down leg,
            # because the two legs need not share x values on a real sweep.
            vs = [v for v, _ in axis.up]
            gaps = [b - axis.field_for_volts(v, "down") for v, b in axis.up]
            gap_plot.plot(vs, gaps, pen=pg.mkPen(COLORS[key], width=2),
                          symbol="o", symbolSize=4,
                          symbolBrush=pg.mkBrush(COLORS[key]), symbolPen=pg.mkPen(None))
        caption = QtWidgets.QLabel(
            "Below: the separation of the two legs -- the hysteresis the seek's "
            "undershoot exists to pin down. Flat and near zero means the magnet "
            "barely has any.")
        caption.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        caption.setWordWrap(True)
        root.addWidget(caption)
        root.addWidget(gap_plot, 1)

        bar = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save...")
        save.clicked.connect(self._save)
        bar.addWidget(save)
        bar.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        bar.addWidget(close)
        root.addLayout(bar)

    @staticmethod
    def _new_plot(y_label: str, y_units: str | None):
        """A themed pyqtgraph plot with the drive voltage on the bottom."""
        plot = pg.PlotWidget()
        plot.setBackground(COLORS["panel"])
        plot.showGrid(x=True, y=True, alpha=0.15)
        plot.setLabel("bottom", "drive", units="V")
        if y_units:
            plot.setLabel("left", y_label, units=y_units)
        else:
            # No units -> no SI prefixing either, or pyqtgraph rescales a
            # 0.8 mT axis to 800 and writes "(x1.000)" next to the label.
            plot.getAxis("left").enableAutoSIPrefix(False)
            plot.setLabel("left", y_label)
        for ax in ("left", "bottom"):
            a = plot.getAxis(ax)
            a.setPen(pg.mkPen(COLORS["muted"]))
            a.setTextPen(pg.mkPen(COLORS["muted"]))
        return plot

    def _save(self):
        if not can_view(self.cal):
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save calibration", "mag2dcal_calibration.json", "JSON (*.json)")
        if path:
            self.cal.save(path)


def can_view(cal) -> bool:
    """True if there is actually a curve to plot."""
    return cal is not None and not getattr(cal, "is_empty", True)
