"""The dialog of Mission Control's "Start for a data file..." (the logic is in
datafile_start.py, tested without Qt; this file only shows it).

Colours come from theme.COLORS at the moment something is drawn (never a
constant here), so the dialog follows the light and the dark theme.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from theme import COLORS as C
from datafile_start import (MISSING, OTHER_PC, REAL_TEXT, REMOTE, RUNNING, STOPPED,
                            UP_ELSEWHERE, FileRow, source_text)


class DataFileDialog(QtWidgets.QDialog):
    """Lists the modules a data file used and asks what to do with them.

    After exec(): `action` is "start", "start_guis", "profile" or None
    (Cancel), and `ticked_ids()` the card ids that were ticked.
    """

    COLS = ("", "Module", "In the file as", "Here", "File used", "Card", "Note")

    def __init__(self, path, result: dict, rows: list[FileRow], parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.rows = rows
        self.action: str | None = None
        self.setWindowTitle("Start the modules of a data file")
        self.resize(1080, 420)
        lay = QtWidgets.QVBoxLayout(self)

        head = QtWidgets.QLabel(f"<b>{self.path.name}</b>")
        head.setToolTip(str(self.path))
        lay.addWidget(head)
        sub = QtWidgets.QLabel(
            f"{len(rows)} module(s), {source_text(result)}. Ticked modules are started "
            f"in their start order, as a profile would. The <i>real</i> box of a card is "
            f"never changed from here: tick it on the card if you want the instrument.")
        sub.setWordWrap(True); sub.setObjectName("meta")
        lay.addWidget(sub)

        self.table = QtWidgets.QTableWidget(len(rows), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(len(self.COLS) - 1, QtWidgets.QHeaderView.Stretch)
        self._fill()
        self.table.itemChanged.connect(lambda _item: self._sync_buttons())
        lay.addWidget(self.table, 1)

        self.warn = QtWidgets.QLabel(); self.warn.setWordWrap(True)
        self.warn.setStyleSheet(f"color:{C['accent_hi']};")
        n_warn = sum(bool(r.warning) for r in rows)
        if n_warn:
            self.warn.setText(f"{n_warn} card(s) are set to the other real/sim mode than "
                              f"the file was measured in (see the Note column).")
        else:
            self.warn.hide()
        lay.addWidget(self.warn)

        bar = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start ticked")
        self.start_btn.setObjectName("primary")
        self.start_btn.setToolTip("Start the ticked services (dependencies first, staggered). "
                                  "One that already runs is left alone.")
        self.guis_btn = QtWidgets.QPushButton("Start + open GUIs")
        self.guis_btn.setToolTip("As 'Start ticked', then open the GUI of every ticked module")
        self.profile_btn = QtWidgets.QPushButton("Save as profile...")
        self.profile_btn.setToolTip("Make a profile chip of the ticked modules, to bring "
                                    "them up again with one click")
        cancel = QtWidgets.QPushButton("Cancel")
        for b, act in ((self.start_btn, "start"), (self.guis_btn, "start_guis"),
                       (self.profile_btn, "profile")):
            b.clicked.connect(lambda _=False, a=act: self.choose(a))
        cancel.clicked.connect(self.reject)
        bar.addWidget(self.start_btn); bar.addWidget(self.guis_btn)
        bar.addWidget(self.profile_btn); bar.addStretch(1); bar.addWidget(cancel)
        lay.addLayout(bar)
        self._sync_buttons()

    def _fill(self):
        kind_color = {RUNNING: C["ok"], UP_ELSEWHERE: C["accent"], STOPPED: C["muted"],
                      MISSING: C["danger"], REMOTE: C["accent"], OTHER_PC: C["muted"]}
        self.table.blockSignals(True)
        for r, row in enumerate(self.rows):
            tick = QtWidgets.QTableWidgetItem()
            flags = QtCore.Qt.ItemIsUserCheckable
            if row.tick_enabled:
                flags |= QtCore.Qt.ItemIsEnabled
            tick.setFlags(flags)
            tick.setCheckState(QtCore.Qt.Checked if row.ticked and row.tick_enabled
                               else QtCore.Qt.Unchecked)
            self.table.setItem(r, 0, tick)
            card = "-" if row.card_real is None else REAL_TEXT[row.card_real]
            used = REAL_TEXT[row.file_real]
            if row.idn_model:
                used += f"  ({row.idn_model})"
            name = row.name + ("" if row.in_recipe else "   (connected, not scanned)")
            cells = (name, row.slug, row.state, used, card, row.warning or row.note)
            for c, text in enumerate(cells, start=1):
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(text)
                if not row.tick_enabled:
                    item.setForeground(QtGui.QColor(C["muted"]))
                if c == 3:
                    item.setForeground(QtGui.QColor(kind_color.get(row.state_kind, C["muted"])))
                if c == 6 and row.warning:
                    item.setForeground(QtGui.QColor(C["accent_hi"]))
                self.table.setItem(r, c, item)
        self.table.blockSignals(False)

    def ticked_ids(self) -> list[str]:
        out = []
        for r, row in enumerate(self.rows):
            item = self.table.item(r, 0)
            if row.card_id and item is not None and item.checkState() == QtCore.Qt.Checked:
                out.append(row.card_id)
        return out

    def set_ticked(self, ids) -> None:
        """Tick exactly these card ids (tests, and a future "tick all")."""
        ids = set(ids)
        for r, row in enumerate(self.rows):
            item = self.table.item(r, 0)
            if item is not None and row.tick_enabled:
                item.setCheckState(QtCore.Qt.Checked if row.card_id in ids
                                   else QtCore.Qt.Unchecked)

    def _sync_buttons(self):
        ids = set(self.ticked_ids())
        startable = any(r.can_start for r in self.rows if r.card_id in ids)
        self.start_btn.setEnabled(startable)
        self.guis_btn.setEnabled(bool(ids))
        self.profile_btn.setEnabled(bool(ids))

    def choose(self, action: str):
        self.action = action
        self.accept()
