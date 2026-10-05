"""run_info_card.py -- the RUN INFO card of the run pane (Measurement tab).

Sample, structure, operator, project, series, tags and comment: typed once,
remembered on this PC (scan_core/run_info.py), and written into every data
file as attributes of exactly those names. Collapsed it is one line
("sample ... · operator ...") so the run pane stays tidy; the arrow opens the
fields.

Saved on every edit, not when the suite closes: a suite that crashes must not
forget who was measuring what.

Sample, structure, operator, project and series are drop-downs of the values
already in the data folder's files (Lukas, 2026-10-05), still typeable; tags
get a "+" menu of the known tags. The lists come from the run catalogue
(scan_core/catalogue.py), brought up to date in a background thread when the
card is opened or the data folder changes -- the window never waits for it.
"""

from __future__ import annotations

import threading
from pathlib import Path

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


#: the fields offered as a drop-down of the values the files hold
LIST_FIELDS = ("sample", "structure", "operator", "project", "series")


class RunInfoCard(QtWidgets.QFrame):
    """The fields, a collapse arrow, and persistence."""

    changed = QtCore.Signal()
    _suggested = QtCore.Signal(object)       # {field: [values], "tags": [...]}, from a thread

    def __init__(self, root=None, parent=None):
        super().__init__(parent)
        self.root = root
        self.data_dir = None
        self.edits: dict[str, QtWidgets.QLineEdit] = {}
        self.boxes: dict[str, QtWidgets.QComboBox] = {}
        self.known_tags: list[str] = []
        self._fill_thread = None
        self._suggested.connect(self._apply_suggestions)
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
            if f == "tags":
                # the known tags in a menu: picking one APPENDS it (the field
                # holds several, so a plain drop-down would replace them)
                trow = QtWidgets.QHBoxLayout(); trow.setSpacing(4)
                trow.addWidget(self._edit(f), 1)
                self.tag_btn = QtWidgets.QToolButton()
                self.tag_btn.setText("+")
                self.tag_btn.setToolTip("Add a tag used in earlier runs")
                self.tag_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
                self.tag_btn.setMenu(QtWidgets.QMenu(self.tag_btn))
                self.tag_btn.setEnabled(False)
                trow.addWidget(self.tag_btn)
                grid.addLayout(trow, row, 1, 1, 3)
            else:
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

    def _edit(self, field: str) -> QtWidgets.QWidget:
        """The field's widget. A list field is an editable combo whose line
        edit is what `self.edits` holds, so reading, writing and saving work
        the same for every field."""
        if field in LIST_FIELDS:
            box = QtWidgets.QComboBox()
            box.setEditable(True)
            box.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
            box.setSizeAdjustPolicy(
                QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(10)
            e = box.lineEdit()
            box.activated.connect(lambda _i: self._edited())   # picked from the list
            self.boxes[field] = box
            widget = box
        else:
            e = widget = QtWidgets.QLineEdit()
        e.setPlaceholderText(HINTS.get(field, ""))
        e.editingFinished.connect(self._edited)
        self.edits[field] = e
        return widget

    def _set_open(self, on: bool):
        self.toggle.setArrowType(QtCore.Qt.DownArrow if on else QtCore.Qt.RightArrow)
        self.body.setVisible(on)
        if on:
            self.refresh_suggestions()

    # ---- the lists, from the files in the data folder ------------------------

    def set_data_dir(self, path) -> None:
        """The suite's data folder (where the runs, and their catalogue, are)."""
        self.data_dir = Path(path) if path else None
        if self.body.isVisible():      # closed: the lists are filled when it opens
            self.refresh_suggestions()

    def refresh_suggestions(self, wait: bool = False) -> None:
        """Bring the catalogue of the data folder up to date in a thread, then
        fill the lists. `wait` (tests) blocks until done."""
        folder = self.data_dir
        if folder is None or not folder.is_dir():
            return
        if self._fill_thread is not None and self._fill_thread.is_alive():
            return

        def work():
            from scan_core import catalogue
            out = {}
            try:
                catalogue.scan(folder)              # incremental: only new files
                for f in LIST_FIELDS:
                    out[f] = catalogue.distinct(folder, f)
                out["tags"] = catalogue.distinct(folder, "tag")
            except Exception:
                return                              # no list is better than an error here
            self._suggested.emit(out)
        self._fill_thread = threading.Thread(target=work, daemon=True,
                                             name="run-info-lists")
        self._fill_thread.start()
        if wait:
            self._fill_thread.join(30)
            QtWidgets.QApplication.processEvents()

    def _apply_suggestions(self, lists: dict) -> None:
        for f, box in self.boxes.items():
            keep = box.lineEdit().text()
            box.blockSignals(True)
            box.clear()
            box.addItems([v for v in lists.get(f, []) if v])
            box.setCurrentIndex(-1)
            box.lineEdit().setText(keep)            # what was typed stays
            box.blockSignals(False)
        self.known_tags = [t for t in lists.get("tags", []) if t]
        menu = self.tag_btn.menu()
        menu.clear()
        for t in self.known_tags:
            menu.addAction(t, lambda t=t: self.add_tag(t))
        self.tag_btn.setEnabled(bool(self.known_tags))

    def add_tag(self, tag: str) -> None:
        """Append one tag (not twice) to the tags field."""
        e = self.edits["tags"]
        have = [x.strip() for x in e.text().split(",") if x.strip()]
        if tag.lower() not in (x.lower() for x in have):
            have.append(tag)
        e.setText(", ".join(have))
        self._edited()

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
