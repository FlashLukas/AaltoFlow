"""run_info_card.py -- the RUN INFO card of the run pane (Measurement tab).

What was measured and by whom: typed once, remembered on this PC
(scan_core/run_info.py), and written into every data file as attributes.
Collapsed it is one line ("sample ... · operator ...") so the run pane stays
tidy; the arrow opens the fields.

Which fields (2026-10-12, the suite is for any lab): this PC's list (suite
setting run_info_fields; default sample, operator, project, series, tags,
comment), changed with "Fields...", plus EXTRA fields added for the runs at
hand with "+ field" (a name and a value, e.g. substrate = GGG). Every field
lands in the file and in the run catalogue, where `where substrate == "GGG"`
finds it.

Saved on every edit, not when the suite closes: a suite that crashes must not
forget who was measuring what.

Every one-line field is a drop-down of the values already in the data
folder's files (Lukas, 2026-10-05), still typeable; tags get a "+" menu of the
known tags. The lists come from the run catalogue (scan_core/catalogue.py),
brought up to date in a background thread when the card is opened or the data
folder changes -- the window never waits for it.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from scan_core import run_info
from apps.theme import C

#: Placeholder texts: what each standard field is for, without a manual.
HINTS = {
    "sample": "e.g. film S-14, piece B",
    "operator": "who measures",
    "project": "e.g. thin-film transport",
    "series": "e.g. temperature dependence, run 2",
    "tags": "comma-separated keywords",
    "comment": "anything else worth knowing later",
}


class RunInfoCard(QtWidgets.QFrame):
    """The fields, a collapse arrow, and persistence."""

    changed = QtCore.Signal()
    #: how many fields are filled in, after ANY change (typed, loaded,
    #: restored) -- the run pane's "Run info" button shows it
    filled_changed = QtCore.Signal(int)
    _suggested = QtCore.Signal(object)       # {field: [values], "tags": [...]}, from a thread

    def __init__(self, root=None, parent=None):
        super().__init__(parent)
        self.root = root
        self.data_dir = None
        self.edits: dict[str, QtWidgets.QLineEdit] = {}
        self.boxes: dict[str, QtWidgets.QComboBox] = {}
        self.known_tags: list[str] = []
        self.tag_btn: QtWidgets.QToolButton | None = None
        #: this PC's field list, and the extra fields shown on top of it
        self.fields: tuple = run_info.fields(root)
        self.extras: list[str] = []
        self._lists: dict = {}                # the last suggestions, for a rebuild
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

        # The body keeps ONE grid layout for its whole life (the Measurement
        # tab sets its margins); a change of fields refills that grid.
        self.body = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout(self.body)
        self.grid.setContentsMargins(22, 0, 0, 0)
        self.grid.setHorizontalSpacing(8); self.grid.setVerticalSpacing(4)
        self.body.setToolTip(
            "Written into every data file (empty fields are left out) and\n"
            "searchable in the Catalogue. Remembered on this PC between scans\n"
            "and launches. The comment is the scan definition's comment.")
        v.addWidget(self.body)
        self.body.hide()
        self.load()

    # ---- building the fields ---------------------------------------------------

    def _shown(self) -> list[str]:
        """Every field on screen, in order: this PC's list (comment last), the
        extras before the comment."""
        listed = [f for f in self.fields if f != "comment"]
        return listed + [f for f in self.extras if f not in listed] + ["comment"]

    def _rebuild(self) -> None:
        """Refill the grid for the current fields, keeping what is typed."""
        keep = {f: e.text() for f, e in self.edits.items()}
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            elif item.layout() is not None:
                _drop_layout(item.layout())
        self.edits, self.boxes, self.tag_btn = {}, {}, None
        g = self.grid
        listed = [f for f in self.fields if f not in ("tags", "comment")]
        # two columns of this PC's short fields
        for i, f in enumerate(listed):
            r, c = divmod(i, 2)
            g.addWidget(QtWidgets.QLabel(f), r, 2 * c)
            g.addWidget(self._edit(f), r, 2 * c + 1)
        row = (len(listed) + 1) // 2
        if "tags" in self.fields:
            g.addWidget(QtWidgets.QLabel("tags"), row, 0)
            # the known tags in a menu: picking one APPENDS it (the field
            # holds several, so a plain drop-down would replace them)
            trow = QtWidgets.QHBoxLayout(); trow.setSpacing(4)
            trow.addWidget(self._edit("tags"), 1)
            self.tag_btn = QtWidgets.QToolButton()
            self.tag_btn.setText("+")
            self.tag_btn.setToolTip("Add a tag used in earlier runs")
            self.tag_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
            self.tag_btn.setMenu(QtWidgets.QMenu(self.tag_btn))
            trow.addWidget(self.tag_btn)
            g.addLayout(trow, row, 1, 1, 3)
            row += 1
        # the extra fields, one per line, each with a remove button
        for f in self.extras:
            if f in self.fields:
                continue
            g.addWidget(QtWidgets.QLabel(f), row, 0)
            erow = QtWidgets.QHBoxLayout(); erow.setSpacing(4)
            erow.addWidget(self._edit(f), 1)
            rm = QtWidgets.QToolButton(); rm.setText("x"); rm.setAutoRaise(True)
            rm.setToolTip(f"Remove the field '{f}' (from the next runs)")
            rm.clicked.connect(lambda _=False, name=f: self.remove_field(name))
            erow.addWidget(rm)
            g.addLayout(erow, row, 1, 1, 3)
            row += 1
        g.addWidget(QtWidgets.QLabel("comment"), row, 0)
        g.addWidget(self._edit("comment"), row, 1, 1, 3)
        row += 1
        brow = QtWidgets.QHBoxLayout(); brow.setSpacing(6)
        self.add_btn = QtWidgets.QPushButton("+ field")
        self.add_btn.setToolTip("Add a field of your own for the next runs (a name and a value,\n"
                                "e.g. substrate = GGG). It goes into the file as info_<name>\n"
                                "and the Catalogue can search it: substrate == \"GGG\".")
        self.add_btn.clicked.connect(self._ask_field)
        brow.addWidget(self.add_btn)
        self.fields_btn = QtWidgets.QPushButton("Fields...")
        self.fields_btn.setToolTip("Choose the fields this PC always shows (suite setting\n"
                                   "run_info_fields). The comment is always there.")
        self.fields_btn.clicked.connect(self._ask_pc_fields)
        brow.addWidget(self.fields_btn)
        self.field_msg = QtWidgets.QLabel("")
        self.field_msg.setStyleSheet(f"color:{C['muted']};")
        brow.addWidget(self.field_msg, 1)
        g.addLayout(brow, row, 0, 1, 4)
        g.setColumnStretch(1, 1); g.setColumnStretch(3, 1)
        for f, e in self.edits.items():
            e.setText(keep.get(f, ""))
        self._apply_suggestions(self._lists)

    def _edit(self, field: str) -> QtWidgets.QWidget:
        """The field's widget. A one-line field is an editable combo whose
        line edit is what `self.edits` holds, so reading, writing and saving
        work the same for every field."""
        if field not in ("tags", "comment"):
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

    # ---- fields of one's own -------------------------------------------------

    def add_field(self, text: str) -> str | None:
        """Add an extra field; returns its (cleaned) name, or None with the
        reason shown next to the buttons."""
        name = run_info.clean_name(text)
        if name is None:
            self.field_msg.setText("a field name starts with a letter: letters, digits, _")
            return None
        if name in self._shown():
            self.field_msg.setText(f"'{name}' is already there")
            return name
        self.extras.append(name)
        self._rebuild()
        self.field_msg.setText("")
        self._save()
        self._refresh_summary()
        return name

    def remove_field(self, name: str) -> None:
        if name in self.extras:
            self.extras.remove(name)
            self.edits.pop(name, None)
            self._rebuild()
            self._edited()

    def set_pc_fields(self, names) -> None:
        """This PC's field list (suite setting run_info_fields). A field that
        leaves the list but holds a value stays, as an extra."""
        vals = self.values()
        run_info.set_fields(names, self.root)
        self.fields = run_info.fields(self.root)
        self.extras = [f for f in self.extras if f not in self.fields]
        self.extras += [f for f, v in vals.items()
                        if v and f not in self.fields and f not in self.extras]
        self._rebuild()
        self._edited()

    def _ask_field(self):
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Add a field", "Field name (e.g. substrate, thickness_nm):")
        if ok and text.strip():
            name = self.add_field(text)
            if name in self.edits:
                self.edits[name].setFocus()

    def _ask_pc_fields(self):
        current = ", ".join(f for f in self.fields if f != "comment")
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Run info fields on this PC",
            "The fields this PC always shows, comma-separated\n"
            "(standard: " + ", ".join(run_info.DEFAULT_FIELDS[:-1]) + ").\n"
            "Empty = the standard list. The comment is always shown.",
            text=current)
        if ok:
            self.set_pc_fields([t for t in text.split(",") if t.strip()])

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
        wanted = list(self.boxes)

        def work():
            from scan_core import catalogue
            out = {}
            try:
                catalogue.scan(folder)              # incremental: only new files
                for f in wanted:
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
        self._lists = dict(lists or {})
        for f, box in self.boxes.items():
            keep = box.lineEdit().text()
            box.blockSignals(True)
            box.clear()
            box.addItems([v for v in self._lists.get(f, []) if v])
            box.setCurrentIndex(-1)
            box.lineEdit().setText(keep)            # what was typed stays
            box.blockSignals(False)
        self.known_tags = [t for t in self._lists.get("tags", []) if t]
        if self.tag_btn is not None:
            menu = self.tag_btn.menu()
            menu.clear()
            for t in self.known_tags:
                menu.addAction(t, lambda t=t: self.add_tag(t))
            self.tag_btn.setEnabled(bool(self.known_tags))

    def add_tag(self, tag: str) -> None:
        """Append one tag (not twice) to the tags field."""
        e = self.edits.get("tags")
        if e is None:
            return
        have = [x.strip() for x in e.text().split(",") if x.strip()]
        if tag.lower() not in (x.lower() for x in have):
            have.append(tag)
        e.setText(", ".join(have))
        self._edited()

    # ---- values ------------------------------------------------------------

    def values(self) -> dict:
        return run_info.normalise({f: e.text() for f, e in self.edits.items()},
                                  keys=self._shown())

    def set_values(self, values: dict, save: bool = True) -> None:
        """Show these values. Its fields beyond this PC's list become the
        extra fields (the ones there before go)."""
        vals = run_info.normalise(values or {}, keys=self.fields)
        extras = [f for f in vals if f not in self.fields]
        if extras != self.extras or set(self.edits) != set(self._shown()) | set(extras):
            self.extras = extras
            self._rebuild()
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
        into the recipe; a non-standard field as info_<name>)."""
        return run_info.run_info_attrs(self.values())

    def set_root(self, root) -> None:
        """The suite's root (suite_local.json lives there); re-reads it."""
        self.root = root
        self.fields = run_info.fields(root)
        self.load()

    def load(self) -> None:
        self.set_values(run_info.load(self.root), save=False)

    def _edited(self):
        # show the normalised form (tags "a,b" -> "a, b") right away
        tags = self.edits.get("tags")
        if tags is not None:
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
        parts = [f"{f} {v}" for f, v in vals.items()
                 if v and f not in ("tags", "comment")][:3]
        text = "  ·  ".join(parts) if parts else "(nothing filled in)"
        self.summary.setText(text)
        self.summary.setToolTip("\n".join(f"{f}: {v}" for f, v in vals.items() if v))
        self.filled_changed.emit(self.filled_count())

    def filled_count(self) -> int:
        """The number of run-info fields with something in them."""
        return sum(1 for v in self.values().values() if v)


def _drop_layout(lay) -> None:
    """Delete a nested layout and the widgets in it (a grid refill)."""
    while lay.count():
        item = lay.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()
        elif item.layout() is not None:
            _drop_layout(item.layout())
    lay.deleteLater()
