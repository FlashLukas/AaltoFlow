"""continue_dialog.py -- pick the point a measurement is CONTINUED from.

Opened by Files > "Continue a measurement..." (scan_builder). The engine side
is scan_core/continuation.py; this is only the question "from which point?":

    file, how far it got (n of N measured), the first point it is missing,
    a "continue at point" box (default: that first missing point) with the
    axis values there, how many points are kept / measured again, and -- in
    red -- why it cannot be continued, if it cannot.

A point is counted in the order the scan MEASURES them (zig-zag included),
1 = the first: "continue at point 37" means "keep the first 36 measured".
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from scan_core import continuation as CT
from apps.theme import C


class ContinueDialog(QtWidgets.QDialog):
    def __init__(self, progress: CT.Progress, problems: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Continue a measurement")
        self.progress = progress
        p = progress
        v = QtWidgets.QVBoxLayout(self)

        head = QtWidgets.QLabel(
            f"<b>{p.path.name}</b><br>"
            f"{p.n_measured} of {p.total} points measured"
            + ("" if p.first_missing >= p.total else
               f" &nbsp;·&nbsp; first point missing: {p.first_missing + 1}"
               f" ({p.where_text(p.first_missing)})"))
        head.setWordWrap(True)
        head.setTextFormat(QtCore.Qt.RichText)
        v.addWidget(head)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("continue at point"))
        self.point = QtWidgets.QSpinBox()
        self.point.setRange(1, max(1, p.total))
        self.point.setValue(min(p.total, p.first_missing + 1))
        self.point.setToolTip(
            "Counted in the order the scan measures (zig-zag included), 1 = the\n"
            "first. Every point BEFORE it is kept from the file; from it on the\n"
            "scan measures again. Pick an earlier point than the first missing\n"
            "one when the last points went wrong (a lost pattern, a drift).")
        self.point.valueChanged.connect(self._update)
        row.addWidget(self.point)
        row.addWidget(QtWidgets.QLabel(f"of {p.total}"))
        row.addStretch(1)
        v.addLayout(row)

        self.where = QtWidgets.QLabel("")
        self.where.setWordWrap(True)
        v.addWidget(self.where)
        self.counts = QtWidgets.QLabel("")
        self.counts.setObjectName("hint")
        v.addWidget(self.counts)

        note = QtWidgets.QLabel(
            "A NEW file is written (the earlier one is not touched): the earlier "
            "data, with everything from this point measured again. The before-scan "
            "routines run first; a routine at the start of a sweep also runs here.")
        note.setWordWrap(True)
        note.setObjectName("hint")
        v.addWidget(note)

        self.problems = QtWidgets.QLabel("\n".join(problems))
        self.problems.setWordWrap(True)
        # read at build time: the theme is set before any widget (gotcha #6)
        self.problems.setStyleSheet(f"color: {C['danger']};")
        self.problems.setVisible(bool(problems))
        v.addWidget(self.problems)

        bb = QtWidgets.QDialogButtonBox()
        self.go = bb.addButton("Continue scan", QtWidgets.QDialogButtonBox.AcceptRole)
        self.go.setObjectName("primary")
        self.go.setEnabled(not problems)
        bb.addButton(QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.resize(560, self.sizeHint().height())
        self._update()

    @property
    def start(self) -> int:
        """The chosen point, 0-based (Continuation.start)."""
        return int(self.point.value()) - 1

    def _update(self, *_):
        s = self.start
        p = self.progress
        self.where.setText(f"there: {p.where_text(s)}")
        kept = int(p.measured_in_order[:s].sum())
        self.counts.setText(f"{kept} measured points kept  ·  "
                            f"{p.total - s} points measured from here")
