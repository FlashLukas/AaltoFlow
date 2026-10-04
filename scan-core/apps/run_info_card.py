"""run_info_card.py -- the RUN INFO card of the run pane (Measurement tab).

Sample, structure, operator, project, series, tags and comment: typed once,
remembered on this PC (scan_core/run_info.py), and written into every data
file as attributes of exactly those names. Collapsed it is one line
("sample ... · operator ...") so the run pane stays tidy; the arrow opens the
fields.

Saved on every edit, not when the suite closes: a suite that crashes must not
forget who was measuring what.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from scan_core import run_info
from apps.theme import C

#: Placeholder texts: what each field is for, without a manual.
HINTS = {
    "sample": "e.g. YIG-2026-03, piece B",
    "structure": "e.g. disc array 2 um, waveguide W3",
    "operator": "who measures",
    "project": "e.g. magnonic crystals",
    "series": "e.g. field dependence, run 2",
    "tags": "comma-separated keywords",
    "comment": "anything else worth knowing later",
}


class RunInfoCard(QtWidgets.QFrame):
    """The fields, a collapse arrow, and persistence."""

    changed = QtCore.Signal()

    def __init__(self, root=None, parent=None):
        super().__init__(parent)
        self.root = root
        self.edits: dict[str, QtWidgets.QLineEdit] = {}
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(4)

        head = QtWidgets.QHBoxLayout(); head.setSpacing(6)
        self.toggle = QtWidgets.QToolButton()
        self.toggle.setArrowType(QtCore.Qt.RightArrow)
        self.toggle.setCheckable(True)
        self.toggle.setAutoRaise(True)
        self.toggle.setToolTip("Show / hide the run info fields")
        self.toggle.toggled.connect(self._set_open)
        head.addWidget(self.toggle)
        tag = QtWidgets.QLabel("RUN INFO"); tag.setObjectName("tag")
        head.addWidget(tag)
        self.summary = QtWidgets.QLabel("")
        self.summary.setStyleSheet(f"color:{C['muted']};")
        self.summary.setMinimumWidth(10)
        head.addWidget(self.summary, 1)
        v.addLayout(head)

        self.body = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(self.body)
        grid.setContentsMargins(22, 0, 0, 0)
        grid.setHorizontalSpacing(8); grid.setVerticalSpacing(4)
        # two columns of short fields, then tags and comment across the width
        short = [f for f in run_info.FIELDS if f not in ("tags", "comment")]
        for i, f in enumerate(short):
            r, c = divmod(i, 2)
            grid.addWidget(QtWidgets.QLabel(f), r, 2 * c)
            grid.addWidget(self._edit(f), r, 2 * c + 1)
        row = (len(short) + 1) // 2
        for f in ("tags", "comment"):
            grid.addWidget(QtWidgets.QLabel(f), row, 0)
            grid.addWidget(self._edit(f), row, 1, 1, 3)
            row += 1
        grid.setColumnStretch(1, 1); grid.setColumnStretch(3, 1)
        self.body.setToolTip(
            "Written into every data file as attributes of these names\n"
            "(empty fields are left out). Remembered on this PC between scans\n"
            "and launches. The comment is the scan definition's comment.")
        v.addWidget(self.body)
        self.body.hide()
        self.load()

    def _edit(self, field: str) -> QtWidgets.QLineEdit:
        e = QtWidgets.QLineEdit()
        e.setPlaceholderText(HINTS.get(field, ""))
        e.editingFinished.connect(self._edited)
        self.edits[field] = e
        return e

    def _set_open(self, on: bool):
        self.toggle.setArrowType(QtCore.Qt.DownArrow if on else QtCore.Qt.RightArrow)
        self.body.setVisible(on)

    # ---- values ------------------------------------------------------------

    def values(self) -> dict:
        return run_info.normalise({f: e.text() for f, e in self.edits.items()})

    def set_values(self, values: dict, save: bool = True) -> None:
        vals = run_info.normalise(values)
        for f, e in self.edits.items():
            e.setText(vals.get(f, ""))
        self._refresh_summary()
        if save:
            self._save()

    def set_comment(self, text: str) -> None:
        """A loaded scan definition brings its comment (one comment field)."""
        self.edits["comment"].setText(text or "")
        self._edited()

    def attrs(self) -> dict:
        """The file attributes (non-empty fields, comment excluded: it goes
        into the recipe)."""
        return run_info.run_info_attrs(self.values())

    def set_root(self, root) -> None:
        """The suite's root (suite_local.json lives there); re-reads it."""
        self.root = root
        self.load()

    def load(self) -> None:
        self.set_values(run_info.load(self.root), save=False)

    def _edited(self):
        # show the normalised form (tags "a,b" -> "a, b") right away
        tags = self.edits["tags"]
        norm = run_info.normalise_tags(tags.text())
        if norm != tags.text():
            tags.setText(norm)
        self._refresh_summary()
        self._save()
        self.changed.emit()

    def _save(self):
        try:
            run_info.save(self.values(), self.root)
        except Exception:
            pass        # a read-only settings file must not break the run pane

    def _refresh_summary(self):
        vals = self.values()
        parts = [f"{f} {vals[f]}" for f in ("sample", "operator", "project")
                 if vals.get(f)]
        text = "  ·  ".join(parts) if parts else "(nothing filled in)"
        self.summary.setText(text)
        self.summary.setToolTip("\n".join(f"{f}: {v}" for f, v in vals.items() if v))
