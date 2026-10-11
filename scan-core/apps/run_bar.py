"""
run_bar.py -- the small widgets of the compact run pane (2026-10-12).

Lukas, with a screenshot of the Measurement tab at 2000x1280: "way too cramped
to see any data" -- the RESULT plot had ~90 px, everything else was a tall
SCAN card, banners, a progress row, a save line and a queue card. The run pane
was rebuilt around the plot: ONE run bar on top, the rarely-touched things
(run info, options, files) in pop-up panels, the plot in a splitter taking at
least half the height. These are the three generic pieces it needs:

  ElidedLabel  one line that never wraps and never pushes the layout wider:
               too long a text ends in "...", the whole text is the tooltip.
               `text()` still returns the WHOLE text (tests and callers read it).
  WrapBar      groups of widgets in ONE row, wrapped onto more rows when the
               pane is too narrow for one (the standalone builder's column, a
               small window) -- nothing is cut off, nothing scrolls.
  PopupPanel   a non-modal panel opened by a button: the SAME widget objects as
               before are moved into it, so every piece of logic (and every
               test) that holds them keeps working. Esc closes it; it opens
               where it was last left.

Colours are read from COLORS when something is drawn or (re)styled, never
cached (docs/DEVELOPER_NOTES.md gotcha #6).
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets


class ElidedLabel(QtWidgets.QLabel):
    """A one-line label that elides instead of wrapping or widening.

    QLabel can only wrap (taller) or demand its full width (a long ETA line
    then makes the whole window wider than the screen). This one paints the
    text that fits, ending in "...", and puts the full text in the tooltip.
    Line breaks in the text become wide gaps on the line, the tooltip keeps
    them.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setWordWrap(False)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.setMinimumWidth(20)
        self.setText(text)

    def setText(self, text: str) -> None:                    # noqa: N802 (Qt name)
        text = "" if text is None else str(text)
        super().setText(text)
        self.setToolTip(text if text else "")
        self.update()

    def minimumSizeHint(self) -> QtCore.QSize:                # noqa: N802
        return QtCore.QSize(20, super().minimumSizeHint().height())

    def sizeHint(self) -> QtCore.QSize:                       # noqa: N802
        fm = self.fontMetrics()
        return QtCore.QSize(min(fm.horizontalAdvance(self._line()) + 4, 600),
                            fm.height() + 2)

    def _line(self) -> str:
        return "   ".join(s.strip() for s in self.text().splitlines() if s.strip())

    def paintEvent(self, _ev):                                # noqa: N802
        p = QtGui.QPainter(self)
        r = self.contentsRect()
        line = self.fontMetrics().elidedText(self._line(), QtCore.Qt.ElideRight, r.width())
        self.style().drawItemText(p, r, int(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter),
                                  self.palette(), self.isEnabled(), line,
                                  self.foregroundRole())

    def mousePressEvent(self, ev):                            # noqa: N802
        # a click shows the whole text at once (no hover wait): the line is
        # where errors and warnings are, and they must be readable in full
        if self.toolTip():
            QtWidgets.QToolTip.showText(ev.globalPosition().toPoint(), self.toolTip(), self)
        super().mousePressEvent(ev)


class WrapBar(QtWidgets.QWidget):
    """Groups of widgets in ONE row, wrapped onto more rows when they do not fit.

    Why not a flow layout: the first group (state, name, progress bar) must
    STRETCH, and a flow layout gives every item exactly its size hint. Here
    each group is a plain row of its own (`rows[i]`, a QHBoxLayout) and only
    the packing of whole groups into lines changes, greedily, left to right.
    The bar reports the widest group as its minimum width, so the window can
    be made narrow and the bar then wraps -- a plain row would report the sum
    of everything and hold the window wide.
    """

    def __init__(self, n_groups: int = 2, parent=None):
        super().__init__(parent)
        self.groups: list[QtWidgets.QWidget] = []
        self.rows: list[QtWidgets.QHBoxLayout] = []
        for _ in range(n_groups):
            g = QtWidgets.QWidget(self)
            row = QtWidgets.QHBoxLayout(g)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            self.groups.append(g)
            self.rows.append(row)
        self._v = QtWidgets.QVBoxLayout(self)
        self._v.setContentsMargins(0, 0, 0, 0)
        self._v.setSpacing(6)
        self._v.setSizeConstraint(QtWidgets.QLayout.SetNoConstraint)
        self.spacing = 14
        self._lines: list[list[int]] | None = None
        self._arrange([list(range(n_groups))])
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)

    # how wide a group wants to be: the first one may shrink (it stretches),
    # the others keep their buttons whole
    def _width(self, i: int) -> int:
        g = self.groups[i]
        return g.minimumSizeHint().width() if i == 0 else g.sizeHint().width()

    def _plan(self, width: int) -> list[list[int]]:
        lines, line, used = [], [], 0
        for i, g in enumerate(self.groups):
            if g.isHidden():
                continue
            w = self._width(i)
            if line and used + self.spacing + w > width:
                lines.append(line)
                line, used = [i], w
            else:
                used += (self.spacing if line else 0) + w
                line.append(i)
        if line:
            lines.append(line)
        return lines or [[]]

    def _arrange(self, lines: list[list[int]]) -> None:
        if lines == self._lines:
            return
        self._lines = lines
        while self._v.count():
            item = self._v.takeAt(0)
            lay = item.layout()
            if lay is not None:
                while lay.count():
                    lay.takeAt(0)
                lay.setParent(None)
                lay.deleteLater()
        for line in lines:
            h = QtWidgets.QHBoxLayout()
            h.setSpacing(self.spacing)
            for i in line:
                h.addWidget(self.groups[i], 1 if i == 0 else 0)
            if 0 not in line:
                h.addStretch(1)              # a wrapped line of buttons: left-aligned
            self._v.addLayout(h)
        self.updateGeometry()

    def wrapped(self) -> bool:
        return len(self._lines or []) > 1

    def minimumSizeHint(self) -> QtCore.QSize:                # noqa: N802
        widths = [self._width(i) for i, g in enumerate(self.groups) if not g.isHidden()]
        return QtCore.QSize(max(widths or [0]), self._height())

    def sizeHint(self) -> QtCore.QSize:                       # noqa: N802
        widths = [self.groups[i].sizeHint().width() for line in (self._lines or [])
                  for i in line]
        return QtCore.QSize(sum(widths) + self.spacing * max(0, len(widths) - 1),
                            self._height())

    def _height(self) -> int:
        hs = [max([self.groups[i].sizeHint().height() for i in line] or [0])
              for line in (self._lines or [])]
        return sum(hs) + self._v.spacing() * max(0, len(hs) - 1)

    def relayout(self, width: int | None = None) -> None:
        """As few lines as fit `width` (default: the bar's own)."""
        self._arrange(self._plan(self.width() if width is None else width))

    def resizeEvent(self, ev):                                # noqa: N802
        self.relayout(ev.size().width())
        super().resizeEvent(ev)

    def event(self, ev):
        # a child changed (a button shown / hidden, a longer text): re-decide
        if ev.type() == QtCore.QEvent.LayoutRequest:
            self.relayout()
        return super().event(ev)


class PopupPanel(QtWidgets.QDialog):
    """A non-modal panel a button opens (Run info, Options, Files).

    The widgets put into it are the ones the run pane always had -- moved,
    not copied -- so nothing that reads or drives them changes. It is a tool
    window of the run pane's window: above it, not in the taskbar. Esc (or the
    button again) closes it; it reopens where it was left, or, the first time,
    just under its button.
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent, QtCore.Qt.Tool)
        self.setWindowTitle(title)
        self.setModal(False)
        self.setObjectName("root")              # the theme's window background
        self.body = QtWidgets.QVBoxLayout(self)
        self.body.setContentsMargins(14, 12, 14, 12)
        self.body.setSpacing(8)
        self._pos: QtCore.QPoint | None = None
        self.button: QtWidgets.QAbstractButton | None = None
        #: called just before the panel shows (e.g. refresh a list)
        self.on_open = None

    def open_at(self, button: QtWidgets.QWidget | None = None) -> None:
        if self.on_open is not None:
            self.on_open()
        self.adjustSize()
        if self._pos is not None:
            self.move(self._pos)
        elif button is not None and button.isVisible():
            below = button.mapToGlobal(QtCore.QPoint(0, button.height() + 4))
            # keep it on the screen the button is on
            scr = (button.screen() or QtGui.QGuiApplication.primaryScreen()).availableGeometry()
            x = min(max(scr.left(), below.x() + button.width() - self.width()),
                    scr.right() - self.width())
            y = min(below.y(), scr.bottom() - self.height())
            self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    def toggle(self) -> None:
        if self.isVisible():
            self.close()
        else:
            self.open_at(self.button)

    def hideEvent(self, ev):                                  # noqa: N802
        self._pos = self.pos()                  # reopen where it was left
        super().hideEvent(ev)
