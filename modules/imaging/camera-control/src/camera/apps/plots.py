"""MiniPlot -- a tiny dependency-free line plot (QPainter only).

We keep the dependency list lean (no matplotlib/pyqtgraph), so the few plots
this GUI needs -- the autofocus focus-vs-Z sweep, the alignment-accuracy trace,
the spot intensity histogram and the spot-area trace -- are drawn by hand.

Axes (reworked 2026-09-13, the first version was hard to read): "nice" round
tick values (1/2/5 x 10^n) with light gridlines on both axes, numbers formatted
for their size (1.5 k, 2.1 M, 0.025), the axis names outside the tick labels,
and optional FIXED ranges -- a histogram wants x 0..255 and y from 0, not a
range padded below zero around whatever the data happens to be.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import theme as T

MARKER_MAX_POINTS = 60   # dots on each data point only for short series


def nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """Round tick values covering [lo, hi]: steps of 1, 2 or 5 x 10^n."""
    span = hi - lo
    if not math.isfinite(span) or span <= 0:
        return [lo]
    raw = span / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    # the round step whose tick COUNT is closest to the target (rounding the step
    # up instead gave 0..255 only three ticks)
    step = min((m * mag for m in (1, 2, 5, 10)), key=lambda s: abs(span / s - target))
    first = math.ceil(lo / step - 1e-9) * step
    ticks, v = [], first
    while v <= hi + step * 1e-9:
        ticks.append(0.0 if abs(v) < step * 1e-9 else v)
        v += step
    return ticks


def fmt_tick(v: float, step: float) -> str:
    """A tick label: k / M for big numbers, just enough decimals for small steps."""
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.{max(0, -math.floor(math.log10(step / 1e6)))}f} M" if step < 1e6 \
            else f"{v / 1e6:.0f} M"
    if a >= 1e4:
        return f"{v / 1e3:.{max(0, -math.floor(math.log10(step / 1e3)))}f} k" if step < 1e3 \
            else f"{v / 1e3:.0f} k"
    if step >= 1:
        return f"{v:.0f}"
    return f"{v:.{min(6, -math.floor(math.log10(step)))}f}"


class MiniPlot(QWidget):
    def __init__(self, xlabel: str = "", ylabel: str = "", parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self._series: list[tuple] = []      # (xs, ys, colour_hex, label)
        self._vline = None
        self._xlabel = xlabel
        self._ylabel = ylabel
        self._x_range = None                # (min, max) or None = autoscale
        self._y_min = None                  # fixed lower y bound, or None
        self._y_max = None

    def set_series(self, series, vline=None, x_range=None, y_min=None, y_max=None) -> None:
        """series: list of (xs, ys, colour_hex, label). vline: x to mark, or None.
        x_range: fixed (xmin, xmax). y_min / y_max: fixed y bounds (None = data)."""
        self._series = [s for s in series if s and len(s[0]) > 0]
        self._vline = vline
        self._x_range = x_range
        self._y_min, self._y_max = y_min, y_max
        self.update()

    def clear(self) -> None:
        self._series = []
        self._vline = None
        self.update()

    # -- ranges ---------------------------------------------------------------------
    def _ranges(self):
        finite = lambda vals: [v for v in vals if v is not None and math.isfinite(v)]
        xs = finite([x for s in self._series for x in s[0]])
        ys = finite([y for s in self._series for y in s[1]])
        if self._x_range is not None:
            xmin, xmax = self._x_range
        else:
            xmin, xmax = (min(xs), max(xs)) if xs else (0.0, 1.0)
        if xmax - xmin < 1e-12:
            xmin, xmax = xmin - 0.5, xmax + 0.5
        ymin_d, ymax_d = (min(ys), max(ys)) if ys else (0.0, 1.0)
        if ymax_d - ymin_d < 1e-12:                 # flat data: show a band around it
            half = max(abs(ymax_d) * 0.05, 1e-6 if ymax_d else 1.0)
            ymin_d, ymax_d = ymin_d - half, ymax_d + half
        pad = 0.06 * (ymax_d - ymin_d)
        ymin = self._y_min if self._y_min is not None else ymin_d - pad
        ymax = self._y_max if self._y_max is not None else ymax_d + pad
        if ymax - ymin < 1e-12:
            ymax = ymin + 1.0
        return xmin, xmax, ymin, ymax

    # -- painting -------------------------------------------------------------------
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(T.PANEL))
        fm = p.fontMetrics()
        th = fm.height()

        if not self._series:
            p.setPen(QColor(T.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "no data")
            p.end()
            return

        xmin, xmax, ymin, ymax = self._ranges()
        yt = nice_ticks(ymin, ymax, max(2, min(6, int((self.height() - 3 * th) / (2 * th)))))
        ystep = yt[1] - yt[0] if len(yt) > 1 else max(abs(ymax - ymin), 1e-9)
        ylabels = [fmt_tick(v, ystep) for v in yt]

        # plot rectangle: room for tick labels left/bottom, the y name on top
        left = 8 + max(fm.horizontalAdvance(s) for s in ylabels)
        has_legend = any(s[3] for s in self._series)
        top = th + 6 if (self._ylabel or has_legend) else 6
        right = self.width() - 10
        bottom = self.height() - (2 * th + 6 if self._xlabel else th + 6)
        if right - left < 20 or bottom - top < 20:
            p.end()
            return

        def sx(x):
            return left + (x - xmin) / (xmax - xmin) * (right - left)

        def sy(y):
            return bottom - (y - ymin) / (ymax - ymin) * (bottom - top)

        grid = QPen(QColor(T.GRID), 1)
        text = QColor(T.MUTED)

        # y grid + labels
        for v, s in zip(yt, ylabels):
            yy = sy(v)
            if top - 0.5 <= yy <= bottom + 0.5:
                p.setPen(grid)
                p.drawLine(QPointF(left, yy), QPointF(right, yy))
                p.setPen(text)
                p.drawText(QRectF(0, yy - th / 2, left - 5, th),
                           Qt.AlignRight | Qt.AlignVCenter, s)

        # x grid + labels (fewer ticks: labels are wider than tall)
        n_x = max(2, min(8, int((right - left) / (7 * fm.horizontalAdvance("0")))))
        xt = nice_ticks(xmin, xmax, n_x)
        xstep = xt[1] - xt[0] if len(xt) > 1 else max(abs(xmax - xmin), 1e-9)
        for v in xt:
            xx = sx(v)
            if left - 0.5 <= xx <= right + 0.5:
                p.setPen(grid)
                p.drawLine(QPointF(xx, top), QPointF(xx, bottom))
                p.setPen(text)
                s = fmt_tick(v, xstep)
                w = fm.horizontalAdvance(s)
                p.drawText(QRectF(xx - w / 2 - 2, bottom + 2, w + 4, th), Qt.AlignCenter, s)

        # frame + axis names
        p.setPen(QPen(QColor(T.BORDER), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(left, top, right - left, bottom - top))
        p.setPen(text)
        if self._ylabel:
            p.drawText(QRectF(4, 2, right, th), Qt.AlignLeft | Qt.AlignVCenter, self._ylabel)
        if self._xlabel:
            p.drawText(QRectF(left, bottom + th + 2, right - left, th),
                       Qt.AlignRight | Qt.AlignVCenter, self._xlabel)

        # zero line
        if ymin < 0 < ymax:
            p.setPen(QPen(QColor(T.BORDER), 1, Qt.DashLine))
            p.drawLine(QPointF(left, sy(0)), QPointF(right, sy(0)))

        # optional vertical marker
        if self._vline is not None and xmin <= self._vline <= xmax:
            p.setPen(QPen(QColor(T.ACCENT), 1, Qt.DashLine))
            xv = sx(self._vline)
            p.drawLine(QPointF(xv, top), QPointF(xv, bottom))

        # series, clipped to the plot area
        p.save()
        p.setClipRect(QRectF(left, top, right - left, bottom - top))
        for xs, ys, colour, _label in self._series:
            p.setPen(QPen(QColor(colour), 2))
            pts = [QPointF(sx(x), sy(y)) for x, y in zip(xs, ys)
                   if x is not None and y is not None and math.isfinite(x) and math.isfinite(y)]
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)
            if len(pts) <= MARKER_MAX_POINTS:
                p.setBrush(QColor(colour))
                for pt in pts:
                    p.drawEllipse(pt, 1.8, 1.8)
                p.setBrush(Qt.NoBrush)
        p.restore()

        # legend in the header row, right-aligned: never on top of the data
        lx = right
        for _xs, _ys, colour, label in reversed(self._series):
            if not label:
                continue
            w = fm.horizontalAdvance(label)
            lx -= w
            p.setPen(QColor(colour))
            p.drawText(QRectF(lx, 2, w + 2, th), Qt.AlignLeft | Qt.AlignVCenter, label)
            p.setPen(QPen(QColor(colour), 2))
            p.drawLine(QPointF(lx - 16, 2 + th / 2), QPointF(lx - 4, 2 + th / 2))
            lx -= 28
        p.end()
