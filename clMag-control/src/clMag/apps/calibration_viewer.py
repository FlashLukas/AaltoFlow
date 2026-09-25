"""A dialog that plots a field-vs-current calibration curve.

Used two ways: pop it up right after "Run Calibration" finishes, or after
loading an old calibration from a file. It shows the measured B(I) points and
the interpolated line, the range, and the Hall-probe parameters the curve was
recorded with -- and lets you save the curve to a text file.
"""

from __future__ import annotations

from PySide6 import QtWidgets
import pyqtgraph as pg

from ..calibration import FieldCalibration
from .theme import COLORS


class CalibrationViewer(QtWidgets.QDialog):
    def __init__(self, cal: FieldCalibration, parent=None, title="Calibration curve"):
        super().__init__(parent)
        self.cal = cal
        self.setWindowTitle(title)
        self.resize(620, 480)

        root = QtWidgets.QVBoxLayout(self)

        # header: counts, range, and the Hall params it was taken with
        lo, hi = cal.range_mT
        head = QtWidgets.QLabel(
            f"{len(cal.currents_A)} points   ·   {lo:.2f} … {hi:.2f} mT")
        head.setStyleSheet(f"color:{COLORS['text']}; font-weight:700; font-size:14px;")
        sub = QtWidgets.QLabel(
            f"recorded with  sensitivity {cal.hall.sensitivity_mT_per_mV:.4f} mT/mV  ·  "
            f"correction {cal.hall.correction:.3f}  ·  offset {cal.hall.offset_mV:.1f} mV")
        sub.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        root.addWidget(head)
        root.addWidget(sub)

        # the plot
        pg.setConfigOptions(antialias=True)
        plot = pg.PlotWidget()
        plot.setBackground(COLORS["panel"])
        plot.showGrid(x=True, y=True, alpha=0.15)
        plot.setLabel("bottom", "current", units="A")
        plot.setLabel("left", "field", units="mT")
        for ax in ("left", "bottom"):
            a = plot.getAxis(ax)
            a.setPen(pg.mkPen(COLORS["muted"]))
            a.setTextPen(pg.mkPen(COLORS["muted"]))
        plot.plot(
            list(cal.currents_A), list(cal.fields_mT),
            pen=pg.mkPen(COLORS["accent"], width=2),
            symbol="o", symbolSize=5,
            symbolBrush=pg.mkBrush(COLORS["accent"]),
            symbolPen=pg.mkPen(None))
        # crosshairs at the origin so "zero current -> zero field" is easy to check
        plot.addLine(x=0, pen=pg.mkPen(COLORS["border"], width=1))
        plot.addLine(y=0, pen=pg.mkPen(COLORS["border"], width=1))
        root.addWidget(plot, 1)

        # buttons
        bar = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save…"); save.clicked.connect(self._save)
        bar.addWidget(save); bar.addStretch(1)
        close = QtWidgets.QPushButton("Close"); close.setObjectName("primary")
        close.clicked.connect(self.accept)
        bar.addWidget(close)
        root.addLayout(bar)

    def _save(self):
        if not self.cal or not self.cal.currents_A:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save calibration", "calibration.txt", "Text (*.txt)")
        if path:
            self.cal.save(path)


def can_view(cal) -> bool:
    """True if we actually have the curve data to plot (a remote client's
    calibration facade carries only the range, not the points)."""
    return bool(cal) and hasattr(cal, "fields_mT") and bool(getattr(cal, "fields_mT", None))
