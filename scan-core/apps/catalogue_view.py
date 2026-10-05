"""catalogue_view.py -- the suite's Catalogue tab: search every run in the data folder.

A view over scan_core/catalogue.py (the index; no Qt there). What it adds:

  * a search bar (free text over name, sample, structure, comment, tags) and
    filter fields (sample, operator, project, series, instrument, tags, a date
    range) plus a `where` line for conditions and snapshot values
    ("ppms.temperature between 4 and 6");
  * the results, newest first: date, name, sample, structure, axes, detectors,
    operator, tags; a file that could not be read is listed in red with the
    reason as its tooltip;
  * a details line for the selected run (comment, conditions, instruments,
    the full path);
  * "Rescan folder" with a progress bar. The scan runs in a WORKER THREAD:
    a year of data on a network share takes a while to stat, and the suite must
    stay usable -- a scan may be running on the Measurement tab meanwhile.
    The tab also rescans on its own whenever it is opened (incremental, so
    normally a fraction of a second).

Double-click a run (or Enter) -> `open_file(path)`: the suite opens it in the
Data tab's viewer. The index lives in the data folder (`catalogue.sqlite`) and
is rebuilt from the files -- deleting it loses nothing.
"""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from scan_core import catalogue as cat
from apps.theme import C

#: results columns: (header, row key)
COLUMNS = [("date", "created"), ("name", "name"), ("sample", "sample"),
           ("structure", "structure"), ("axes", "dims_text"),
           ("detectors", "detectors_text"), ("operator", "operator"),
           ("tags", "tags_text"), ("setup", "setup_name")]

#: filters offered as a drop-down of the values the catalogue holds
PICK_FIELDS = ("setup", "sample", "operator", "project", "series")

#: the filter fields in the second row: (attribute, placeholder, search kwarg)
FILTERS = [("setup_edit", "setup", "setup"),
           ("sample_edit", "sample", "sample"),
           ("operator_edit", "operator", "operator"),
           ("project_edit", "project", "project"),
           ("series_edit", "series", "series"),
           ("instrument_edit", "instrument", "instrument"),
           ("tags_edit", "tags (a, b)", "tags")]


class _ScanSignals(QtCore.QObject):
    """Carries the worker thread's news to the GUI thread (queued: a signal
    emitted from a Python thread is delivered in the receiver's thread)."""
    progress = QtCore.Signal(int, int, str)
    finished = QtCore.Signal(object)          # the counts dict, or an Exception


class CatalogueWidget(QtWidgets.QWidget):
    def __init__(self, data_dir=None, open_file=None, on_log=None, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir) if data_dir else None
        #: called with the absolute path of a double-clicked run
        self.open_file = open_file or (lambda path: None)
        self.on_log = on_log or (lambda msg: None)
        self.rows: list[dict] = []
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._signals = _ScanSignals()
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0); v.setSpacing(10)

        card = QtWidgets.QFrame(); card.setObjectName("card")
        g = QtWidgets.QVBoxLayout(card); g.setContentsMargins(14, 12, 14, 12); g.setSpacing(8)

        # row 1: free text + the folder actions
        r1 = QtWidgets.QHBoxLayout()
        self.text_edit = QtWidgets.QLineEdit()
        self.text_edit.setPlaceholderText(
            "search name, sample, structure, comment, tags  (every word must match)")
        self.text_edit.setClearButtonEnabled(True)
        r1.addWidget(self.text_edit, 1)
        self.clear_btn = QtWidgets.QPushButton("Clear filters")
        self.clear_btn.clicked.connect(self.clear_filters)
        r1.addWidget(self.clear_btn)
        self.rescan_btn = QtWidgets.QPushButton("Rescan folder")
        self.rescan_btn.setObjectName("primary")
        self.rescan_btn.setToolTip(
            "Read the attributes of every new or changed .nc file in the data folder.\n"
            "Only the headers are read, never the data. The index (catalogue.sqlite\n"
            "in the data folder) is rebuilt from the files and can be deleted any time.")
        self.rescan_btn.clicked.connect(self.rescan)
        r1.addWidget(self.rescan_btn)
        g.addLayout(r1)

        # row 2: field filters + the date range
        r2 = QtWidgets.QHBoxLayout(); r2.setSpacing(6)
        # setup / sample / operator / project / series are PICK LISTS of the
        # values the files really hold (Lukas, 2026-10-05: "offer the Setup,
        # User, Sample in a list"), still typeable to narrow. The attribute
        # is the combo's line edit, so typing, clearing and the debounce work
        # exactly as for the plain fields.
        self._picks = {}
        for attr, placeholder, kw in FILTERS:
            if kw in PICK_FIELDS:
                box = QtWidgets.QComboBox()
                box.setEditable(True)
                box.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
                box.setMinimumContentsLength(8)
                box.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
                edit = box.lineEdit()
                box.setToolTip(f"{placeholder}: pick one of the values in your files, "
                               f"or type part of one")
                self._picks[kw] = box
                widget = box
            else:
                edit = widget = QtWidgets.QLineEdit()
            edit.setPlaceholderText(placeholder)
            edit.setClearButtonEnabled(True)
            setattr(self, attr, edit)
            r2.addWidget(widget, 1)
        self.from_edit = QtWidgets.QLineEdit(); self.from_edit.setPlaceholderText("from YYYY-MM-DD")
        self.to_edit = QtWidgets.QLineEdit(); self.to_edit.setPlaceholderText("to YYYY-MM-DD")
        for e in (self.from_edit, self.to_edit):
            e.setClearButtonEnabled(True); e.setMinimumWidth(150); e.setMaximumWidth(170)
            e.setToolTip("YYYY-MM-DD; the 'to' day is included")
            r2.addWidget(e)
        g.addLayout(r2)

        # row 3: conditions / snapshot values
        r3 = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel("where")
        lbl.setStyleSheet(f"color:{C['muted']};")
        r3.addWidget(lbl)
        self.where_edit = QtWidgets.QLineEdit()
        self.where_edit.setPlaceholderText(
            "conditions and instrument values, e.g.  ppms.temperature between 4 and 6"
            "  and  clMag.field == 50")
        self.where_edit.setToolTip(
            "Terms joined by 'and' or ',':\n"
            "  key == value   != < <= > >=   key between A and B   key contains text\n"
            "A key is a fixed condition of the scan (rf_power), a run column\n"
            "(n_points, duration) or an instrument value from the snapshot:\n"
            "'ppms.temperature' finds ppms.status.temperature as well.")
        self.where_edit.setClearButtonEnabled(True)
        r3.addWidget(self.where_edit, 1)
        g.addLayout(r3)

        # the scan's progress (hidden when idle) + the count / error line
        r4 = QtWidgets.QHBoxLayout()
        self.progress = QtWidgets.QProgressBar()
        self.progress.setMaximumHeight(14); self.progress.setTextVisible(False)
        self.progress.hide()
        r4.addWidget(self.progress, 1)
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet(f"color:{C['muted']};")
        r4.addWidget(self.status, 2)
        g.addLayout(r4)

        self.table = QtWidgets.QTreeWidget()
        self.table.setHeaderLabels([h for h, _ in COLUMNS])
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setAlternatingRowColors(False)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, QtCore.Qt.DescendingOrder)
        self.table.itemActivated.connect(self._activated)        # double-click / Enter
        self.table.currentItemChanged.connect(self._show_details)
        hdr = self.table.header()
        hdr.setStretchLastSection(True)
        for i, w in enumerate((130, 200, 90, 130, 170, 200, 80)):
            self.table.setColumnWidth(i, w)
        g.addWidget(self.table, 1)

        self.details = QtWidgets.QLabel("")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.details.setStyleSheet(f"color:{C['muted']};")
        g.addWidget(self.details)
        v.addWidget(card, 1)

        # typing searches after a short pause (a search is a few ms; this just
        # avoids redrawing the table for every keystroke)
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self.refresh)
        for e in self._edits():
            e.textChanged.connect(lambda _t: self._debounce.start())
            e.returnPressed.connect(self.refresh)

        self._say_folder()

    # ---- data folder ---------------------------------------------------------

    def _edits(self):
        return [self.text_edit, self.where_edit, self.from_edit, self.to_edit] + \
            [getattr(self, a) for a, _p, _k in FILTERS]

    def set_data_dir(self, path) -> None:
        """The suite's data folder changed: show THAT folder's runs."""
        new = Path(path) if path else None
        if new == self.data_dir:
            return
        self.cancel_scan()
        self.data_dir = new
        self.rows = []
        self.table.clear()
        self._say_folder()
        if self.isVisible():
            self.rescan()

    def _say_folder(self):
        if self.data_dir is None:
            self.status.setText("no data folder set (Settings tab)")
        else:
            self.status.setText(f"{self.data_dir}  -  'Rescan folder' to index it")

    def showEvent(self, ev):
        # opening the tab brings the index up to date (incremental: cheap)
        super().showEvent(ev)
        if self.data_dir is not None and not self.scanning():
            self.rescan()

    # ---- scanning (worker thread) -----------------------------------------

    def scanning(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def rescan(self) -> None:
        if self.data_dir is None or self.scanning():
            return
        if not self.data_dir.is_dir():
            self.status.setText(f"no such folder: {self.data_dir}")
            return
        self._cancel.clear()
        self.rescan_btn.setEnabled(False)
        self.progress.setRange(0, 0)               # busy until the first file
        self.progress.show()
        self.status.setText("looking for new and changed files ...")
        folder = self.data_dir
        sig = self._signals

        def work():
            try:
                counts = cat.scan(folder,
                                  progress=lambda d, n, rp: sig.progress.emit(d, n, rp),
                                  cancel=self._cancel.is_set)
                counts["folder"] = folder
                sig.finished.emit(counts)
            except Exception as exc:               # reported, never fatal
                sig.finished.emit(exc)

        self._thread = threading.Thread(target=work, name="catalogue-scan", daemon=True)
        self._thread.start()

    def cancel_scan(self, wait_s: float = 5.0) -> None:
        if self.scanning():
            self._cancel.set()
            self._thread.join(wait_s)

    def wait_scan(self, timeout_s: float = 30.0) -> None:
        """Block until a running scan ends and its result is shown (tests)."""
        if self._thread is not None:
            self._thread.join(timeout_s)
        QtCore.QCoreApplication.processEvents()
        QtCore.QCoreApplication.sendPostedEvents()
        QtCore.QCoreApplication.processEvents()

    def _on_progress(self, done: int, total: int, relpath: str):
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.status.setText(f"reading {done}/{total}  {relpath}")

    def _on_finished(self, result):
        self.progress.hide()
        self.rescan_btn.setEnabled(True)
        if isinstance(result, Exception):
            self.status.setText(f"rescan failed: {result}")
            self.status.setStyleSheet(f"color:{C['danger']};")
            self.on_log(f"catalogue: rescan failed: {result}")
            return
        if result.get("folder") != self.data_dir:
            return                                  # the folder changed meanwhile
        if result["read"] or result["removed"]:
            self.on_log(f"catalogue: {result['files']} files, {result['read']} read, "
                        f"{result['removed']} removed, {result['errors']} unreadable")
        self.fill_picks()
        self.refresh()

    def fill_picks(self) -> None:
        """Refill the drop-downs with the values the catalogue holds now,
        keeping what is typed or chosen. The first entry is empty = any."""
        for kw, box in self._picks.items():
            try:
                values = cat.distinct(self.data_dir, kw)
            except Exception:
                continue
            keep = box.lineEdit().text()
            box.blockSignals(True)
            box.clear()
            box.addItem("")
            box.addItems(values)
            box.lineEdit().setText(keep)
            box.blockSignals(False)

    # ---- searching ---------------------------------------------------------

    def query(self) -> dict:
        """The search() keyword arguments the fields describe now."""
        kw = {"text": self.text_edit.text().strip() or None,
              "where": self.where_edit.text().strip() or None,
              "date_from": self.from_edit.text().strip() or None,
              "date_to": self.to_edit.text().strip() or None}
        for attr, _p, key in FILTERS:
            kw[key] = getattr(self, attr).text().strip() or None
        return kw

    def clear_filters(self) -> None:
        for e in self._edits():
            e.blockSignals(True); e.clear(); e.blockSignals(False)
        self.refresh()

    def refresh(self) -> None:
        """Run the search and fill the table."""
        self._debounce.stop()
        self.status.setStyleSheet(f"color:{C['muted']};")
        if self.data_dir is None:
            return
        kw = self.query()
        for key in ("date_from", "date_to"):
            if kw[key]:
                try:
                    date.fromisoformat(kw[key][:10])
                except ValueError:
                    self._error(f"{key.replace('_', ' ')}: write the date as YYYY-MM-DD")
                    return
        try:
            self.rows = cat.search(self.data_dir, **kw)
        except cat.WhereError as exc:
            self._error(f"where: {exc}")
            return
        except Exception as exc:                    # a locked/odd index, say
            self._error(f"search failed: {exc}")
            return
        self._fill()
        n_bad = sum(1 for r in self.rows if r.get("error"))
        text = f"{len(self.rows)} run(s)"
        if n_bad:
            text += f"  -  {n_bad} unreadable (red)"
        text += f"   in {self.data_dir}"
        self.status.setText(text)

    def _error(self, msg: str):
        self.status.setText(msg)
        self.status.setStyleSheet(f"color:{C['danger']};")

    def _fill(self):
        from PySide6 import QtGui
        self.table.setSortingEnabled(False)       # or rows reorder while adding
        self.table.clear()
        for r in self.rows:
            vals = []
            for _h, key in COLUMNS:
                v = r.get(key) or ""
                if key == "created":
                    v = str(v)[:16].replace("T", " ")
                vals.append(str(v))
            item = QtWidgets.QTreeWidgetItem(vals)
            item.setData(0, QtCore.Qt.UserRole, r["path"])
            if r.get("error"):
                for i in range(len(COLUMNS)):
                    item.setForeground(i, QtGui.QBrush(QtGui.QColor(C["danger"])))
                    item.setToolTip(i, f"could not be read: {r['error']}")
            else:
                item.setToolTip(1, r["path"])
            self.table.addTopLevelItem(item)
        self.table.setSortingEnabled(True)

    def _row_of(self, item) -> dict | None:
        if item is None:
            return None
        path = item.data(0, QtCore.Qt.UserRole)
        return next((r for r in self.rows if r["path"] == path), None)

    def _show_details(self, item, _prev=None):
        r = self._row_of(item)
        if r is None:
            self.details.setText("")
            return
        if r.get("error"):
            self.details.setText(f"{r['path']}\ncould not be read: {r['error']}")
            return
        bits = []
        if r.get("comment"):
            bits.append(f"comment: {r['comment']}")
        if r.get("status"):
            bits.append(f"status: {r['status']}")
        if r.get("duration") is not None:
            bits.append(f"{r.get('n_points') or '?'} points in {r['duration']:.0f} s")
        if r.get("project") or r.get("series"):
            bits.append(f"project {r.get('project') or '-'}, series {r.get('series') or '-'}")
        if r.get("instruments_text"):
            bits.append(f"instruments: {r['instruments_text']}")
        ranges = []
        for d in r.get("dims") or []:
            if "min" in d:
                ranges.append(f"{d['name']} {d['min']:g}..{d['max']:g} {d.get('units', '')}".strip())
        if ranges:
            bits.append("; ".join(ranges))
        self.details.setText("   |   ".join(bits) + f"\n{r['path']}")

    def _activated(self, item, _col=0):
        r = self._row_of(item)
        if r is None:
            return
        if r.get("error"):
            self.status.setText(f"cannot open {Path(r['path']).name}: {r['error']}")
            return
        self.open_file(r["path"])
