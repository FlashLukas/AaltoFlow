"""catalogue_view.py -- the suite's Catalogue tab: search every run in the data folder.

A view over scan_core/catalogue.py (the index; no Qt there). What it adds:

  * a search bar (free text over name, sample, comment, tags and every
    run-info field) and filter fields (a date range, setup, operator, sample,
    this PC's own run-info fields, name, axis; project, series, instrument,
    tags) plus a `where` line for conditions, run-info fields and snapshot
    values ("cryostat.temperature between 4 and 6");
  * the results, newest first: date, setup, operator, sample, this PC's own
    run-info fields (suite setting run_info_fields, e.g. "substrate"), name,
    axes, detectors, tags (the filter fields sit in the same order); a file
    that could not be read is listed in red with the reason as its tooltip;
  * a details line for the selected run (comment, conditions, instruments,
    the full path);
  * "Rescan folder" with a progress bar. The scan runs in a WORKER THREAD:
    a year of data on a network share takes a while to stat, and the suite must
    stay usable -- a scan may be running on the Measurement tab meanwhile.
    The tab also rescans on its own whenever it is opened (incremental, so
    normally a fraction of a second).

Double-click a run (or Enter) -> `open_file(path)`: the suite opens it in the
Data tab's viewer. Right-click -> "Send to eLabFTW" (one run or every selected
one; scan_core/eln.py) in a worker thread; the "eln" column shows the
experiment a run went to (from `eln_uploads.json` in the data folder -- a file
of its own, because the index is disposable and that fact is not in the .nc). The index lives in the data folder (`catalogue.sqlite`) and
is rebuilt from the files -- deleting it loses nothing.
"""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from scan_core import catalogue as cat
from scan_core import eln, run_info
from apps.theme import C

#: results columns: (header, row key). The order is the order a run is
#: thought of in (Lukas, 2026-10-06): when, on which setup, by whom, which
#: sample (and whatever else this PC records about it), then what the run was.
#: A PC's own run-info fields (suite setting run_info_fields beyond the
#: standard ones) get a column each after "sample", row key "info:<field>".
COLUMNS = [("date", "created"), ("setup", "setup_name"), ("operator", "operator"),
           ("sample", "sample"), ("name", "name"),
           ("axes", "dims_text"), ("detectors", "detectors_text"),
           ("tags", "tags_text"), ("eln", "eln")]
#: where the PC's own columns go in
_EXTRA_AT = [k for _h, k in COLUMNS].index("name")
#: the name / eln column with no extra columns (an instance has name_col /
#: eln_col, which count them)
NAME_COL = [k for _h, k in COLUMNS].index("name")
ELN_COL = [k for _h, k in COLUMNS].index("eln")

#: filters offered as a drop-down of the values the catalogue holds (a PC's
#: own run-info fields are drop-downs too)
PICK_FIELDS = ("setup", "operator", "sample", "project", "series")

#: the filter fields: (attribute, placeholder, search kwarg). FILTERS sits
#: under the columns in THE SAME ORDER (after the date range), so the field
#: for a column is found where the column is; MORE_FILTERS are the fields
#: that have no column of their own.
FILTERS = [("setup_edit", "setup", "setup"),
           ("operator_edit", "operator", "operator"),
           ("sample_edit", "sample", "sample"),
           ("name_edit", "name", "name"),
           ("axis_edit", "axis", "axis")]
MORE_FILTERS = [("project_edit", "project", "project"),
                ("series_edit", "series", "series"),
                ("instrument_edit", "instrument", "instrument"),
                ("tags_edit", "tags (a, b)", "tags")]
ALL_FILTERS = FILTERS + MORE_FILTERS


class _ScanSignals(QtCore.QObject):
    """Carries the worker thread's news to the GUI thread (queued: a signal
    emitted from a Python thread is delivered in the receiver's thread)."""
    progress = QtCore.Signal(int, int, str)
    finished = QtCore.Signal(object)          # the counts dict, or an Exception


class _ElnSignals(QtCore.QObject):
    """The eLabFTW upload worker's news, queued into the GUI thread."""
    progress = QtCore.Signal(str)
    one_done = QtCore.Signal(object)          # (path, UploadResult or Exception)
    finished = QtCore.Signal(object)          # list of (path, result-or-exception)


class CatalogueWidget(QtWidgets.QWidget):
    def __init__(self, data_dir=None, open_file=None, on_log=None, parent=None,
                 settings_root=None):
        super().__init__(parent)
        #: where suite_local.json is (the eLabFTW address and options)
        self.settings_root = settings_root
        self._eln_thread: threading.Thread | None = None
        self._eln = _ElnSignals()
        self._eln.progress.connect(self._eln_progress)
        self._eln.one_done.connect(self._eln_one_done)
        self._eln.finished.connect(self._eln_finished)
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
        # this PC's own run-info fields: a column and a filter each
        try:
            own = [f for f in run_info.fields(settings_root) if f not in run_info.STANDARD]
        except Exception:
            own = []
        self.extra_fields = own
        self.columns = (COLUMNS[:_EXTRA_AT] + [(f, f"info:{f}") for f in own]
                        + COLUMNS[_EXTRA_AT:])
        keys = [k for _h, k in self.columns]
        self.name_col, self.eln_col = keys.index("name"), keys.index("eln")
        at = [a for a, _p, _k in FILTERS].index("name_edit")
        self.filters = (FILTERS[:at] + [(f"info_{f}_edit", f, f"info:{f}") for f in own]
                        + FILTERS[at:])
        self.all_filters = self.filters + MORE_FILTERS
        #: {field: its filter line edit}
        self.info_edits: dict[str, QtWidgets.QLineEdit] = {}

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0); v.setSpacing(10)

        card = QtWidgets.QFrame(); card.setObjectName("card")
        g = QtWidgets.QVBoxLayout(card); g.setContentsMargins(14, 12, 14, 12); g.setSpacing(8)

        # row 1: free text + the folder actions
        r1 = QtWidgets.QHBoxLayout()
        self.text_edit = QtWidgets.QLineEdit()
        self.text_edit.setPlaceholderText(
            "search name, sample, comment, tags, run info  (every word must match)")
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

        # row 2: the date range, then one field per column in column order;
        # row 2b: the filters without a column
        r2 = QtWidgets.QHBoxLayout(); r2.setSpacing(6)
        r2b = QtWidgets.QHBoxLayout(); r2b.setSpacing(6)
        self.from_edit = QtWidgets.QLineEdit(); self.from_edit.setPlaceholderText("from YYYY-MM-DD")
        self.to_edit = QtWidgets.QLineEdit(); self.to_edit.setPlaceholderText("to YYYY-MM-DD")
        for e in (self.from_edit, self.to_edit):
            e.setClearButtonEnabled(True); e.setMinimumWidth(130); e.setMaximumWidth(150)
            e.setToolTip("YYYY-MM-DD; the 'to' day is included")
            r2.addWidget(e)
        # setup / sample / operator / project / series are PICK LISTS of the
        # values the files really hold (Lukas, 2026-10-05: "offer the Setup,
        # User, Sample in a list"), still typeable to narrow. The attribute
        # is the combo's line edit, so typing, clearing and the debounce work
        # exactly as for the plain fields.
        self._picks = {}
        for attr, placeholder, kw in self.all_filters:
            row = r2 if (attr, placeholder, kw) in self.filters else r2b
            if kw in PICK_FIELDS or kw.startswith("info:"):
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
            if kw.startswith("info:"):
                self.info_edits[kw[5:]] = edit
            if kw == "axis":
                edit.setToolTip("a scan axis whose name contains this, e.g. field, "
                                "stage.position")
            row.addWidget(widget, 1)
        r2b.addStretch(1)
        g.addLayout(r2)
        g.addLayout(r2b)

        # row 3: conditions / snapshot values
        r3 = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel("where")
        lbl.setStyleSheet(f"color:{C['muted']};")
        r3.addWidget(lbl)
        self.where_edit = QtWidgets.QLineEdit()
        self.where_edit.setPlaceholderText(
            "conditions and instrument values, e.g.  cryostat.temperature between 4 and 6"
            "  and  magnet.field == 50   (a name alone, e.g.  stage, = the run has it)")
        self.where_edit.setToolTip(
            "Terms joined by 'and' or ',':\n"
            "  key == value   != < <= > >=   key between A and B   key contains text\n"
            "  a name alone = the run has it (stage -> every run that used stage)\n"
            "A key is a fixed condition of the scan (rf_power), a run column\n"
            "(n_points, duration), a run-info field (substrate) or an instrument\n"
            "value from the snapshot: 'cryostat.temperature' finds\n"
            "cryostat.status.temperature as well.")
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
        self.table.setHeaderLabels([h for h, _ in self.columns])
        self.table.setRootIsDecorated(False)
        # several runs can be selected (Ctrl/Shift) -- for "Send to eLabFTW"
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setUniformRowHeights(True)
        self.table.setAlternatingRowColors(False)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, QtCore.Qt.DescendingOrder)
        self.table.itemActivated.connect(self._activated)        # double-click / Enter
        self.table.setToolTip("double-click a run (or Enter) to open it in the data viewer")
        self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.currentItemChanged.connect(self._show_details)
        hdr = self.table.header()
        hdr.setStretchLastSection(True)
        widths = {"created": 130, "setup_name": 90, "operator": 90, "sample": 90,
                  "name": 200, "dims_text": 200, "detectors_text": 200, "tags_text": 160}
        for i, (_h, key) in enumerate(self.columns):
            self.table.setColumnWidth(i, widths.get(key, 110))
        g.addWidget(self.table, 1)

        self.details = QtWidgets.QLabel("")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.details.setStyleSheet(f"color:{C['muted']};")
        g.addWidget(self.details)
        # the last eLabFTW upload: a clickable link to each experiment
        self.eln_lbl = QtWidgets.QLabel("")
        self.eln_lbl.setWordWrap(True)
        self.eln_lbl.setTextFormat(QtCore.Qt.RichText)
        self.eln_lbl.setOpenExternalLinks(True)
        self.eln_lbl.setTextInteractionFlags(QtCore.Qt.TextBrowserInteraction)
        self.eln_lbl.hide()
        g.addWidget(self.eln_lbl)
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
            [getattr(self, a) for a, _p, _k in self.all_filters]

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
                values = cat.distinct(self.data_dir, kw.removeprefix("info:"))
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
        info = {}
        for attr, _p, key in self.all_filters:
            text = getattr(self, attr).text().strip() or None
            if key.startswith("info:"):
                if text:
                    info[key[5:]] = text
            else:
                kw[key] = text
        kw["info"] = info or None
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
        sent = eln.uploads(self.data_dir)
        for r in self.rows:
            rec = sent.get(r["relpath"])
            r["eln"] = (f"#{rec.get('id')}" + ("" if rec.get("complete", True) else " (incomplete)")
                        if rec else "")
            r["eln_url"] = rec.get("url") if rec else None
            vals = []
            for _h, key in self.columns:
                v = ((r.get("info") or {}).get(key[5:]) if key.startswith("info:")
                     else r.get(key)) or ""
                if key == "created":
                    v = str(v)[:16].replace("T", " ")
                vals.append(str(v))
            item = QtWidgets.QTreeWidgetItem(vals)
            item.setData(0, QtCore.Qt.UserRole, r["path"])
            if r.get("error"):
                for i in range(len(self.columns)):
                    item.setForeground(i, QtGui.QBrush(QtGui.QColor(C["danger"])))
                    item.setToolTip(i, f"could not be read: {r['error']}")
            else:
                item.setToolTip(self.name_col, r["path"])
            if r.get("eln_url"):
                item.setToolTip(self.eln_col, f"in eLabFTW: {r['eln_url']}")
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
        other = {k: v for k, v in (r.get("info") or {}).items()
                 if k not in run_info.STANDARD and k not in self.extra_fields}
        if other:
            bits.append(", ".join(f"{k}: {v}" for k, v in other.items()))
        if r.get("instruments_text"):
            bits.append(f"instruments: {r['instruments_text']}")
        ranges = []
        for d in r.get("dims") or []:
            if "min" in d:
                ranges.append(f"{d['name']} {d['min']:g}..{d['max']:g} {d.get('units', '')}".strip())
        if ranges:
            bits.append("; ".join(ranges))
        self.details.setText("   |   ".join(bits) + f"\n{r['path']}")

    def _context_menu(self, pos):
        menu = self.context_menu_for(self.table.itemAt(pos))
        if menu is not None:
            menu.exec(self.table.viewport().mapToGlobal(pos))

    def context_menu_for(self, item) -> "QtWidgets.QMenu | None":
        """The right-click menu of a run (built, not shown: the tests read it)."""
        r = self._row_of(item)
        if r is None:
            return None
        menu = QtWidgets.QMenu(self)
        act = menu.addAction("Open in data viewer")
        act.setEnabled(not r.get("error"))
        act.triggered.connect(lambda: self._activated(item))
        menu.addAction("Copy path").triggered.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(r["path"]))
        menu.addSeparator()
        runs = self.selected_runs() if item.isSelected() else [r]
        runs = [x for x in runs if not x.get("error")]
        send = menu.addAction("Send to eLabFTW" if len(runs) <= 1
                              else f"Send {len(runs)} runs to eLabFTW")
        send.setEnabled(bool(runs) and not self.eln_busy())
        send.setToolTip("Make an eLabFTW experiment of each run: summary, tags, preview\n"
                        "(and the data file if ticked in Settings > ELN)")
        send.triggered.connect(lambda: self.send_to_eln(runs))
        if r.get("eln_url"):
            menu.addAction("Open in eLabFTW").triggered.connect(
                lambda: self._open_url(r["eln_url"]))
        return menu

    # ---- eLabFTW -----------------------------------------------------------

    def selected_runs(self) -> list[dict]:
        out = []
        for it in self.table.selectedItems():
            r = self._row_of(it)
            if r is not None and r not in out:
                out.append(r)
        return out

    def eln_busy(self) -> bool:
        return self._eln_thread is not None and self._eln_thread.is_alive()

    @staticmethod
    def _open_url(url):
        from PySide6 import QtGui
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def _ask_duplicates(self, already: list[dict]) -> str | None:
        """Some runs are in eLabFTW already: update / skip / new? None = cancel.
        (A method of its own so the tests can answer it.)"""
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Send to eLabFTW")
        lines = []
        for r in already[:8]:
            rec = eln.upload_record(self.data_dir, r["path"]) or {}
            lines.append(f"  {r['name']}  ->  experiment {rec.get('id')}")
        more = f"\n  ... and {len(already) - 8} more" if len(already) > 8 else ""
        box.setText(f"{len(already)} of these runs are in eLabFTW already:\n\n"
                    + "\n".join(lines) + more
                    + "\n\nUpdate those entries, skip them, or make new entries?")
        upd = box.addButton("Update them", QtWidgets.QMessageBox.AcceptRole)
        skp = box.addButton("Skip them", QtWidgets.QMessageBox.ActionRole)
        new = box.addButton("New entries", QtWidgets.QMessageBox.ActionRole)
        box.addButton("Cancel", QtWidgets.QMessageBox.RejectRole)
        box.exec()
        return {upd: "update", skp: "skip", new: "new"}.get(box.clickedButton())

    def send_to_eln(self, runs: list[dict]) -> bool:
        """Upload `runs` in a worker thread (the network may be slow; the suite
        must stay usable). Returns False if it did not start (no address or
        key, cancelled, already busy) -- and says why."""
        if self.eln_busy() or not runs:
            return False
        try:
            settings = eln.ElnSettings.load(self.settings_root)
            client = eln.client_from_settings(self.settings_root, settings)
        except eln.ElabError as exc:
            self._eln_say(f"eLabFTW: {exc}", error=True)
            return False
        already = [r for r in runs if eln.upload_record(self.data_dir, r["path"])]
        mode = "error"
        if already:
            mode = self._ask_duplicates(already)
            if mode is None:
                return False
        try:
            from suite_common import setup_name
            setup = setup_name(self.settings_root) or None
        except Exception:
            setup = None
        folder, sig = self.data_dir, self._eln
        paths = [r["path"] for r in runs]

        def work():
            results = []
            for i, path in enumerate(paths):
                name = Path(path).name
                try:
                    res = eln.upload_run(
                        path, client, data_dir=folder, attach_data=settings.attach_data,
                        max_data_mb=settings.max_data_mb, if_exists=mode, setup_name=setup,
                        progress=lambda m, n=name, k=i: sig.progress.emit(
                            f"eLabFTW {k + 1}/{len(paths)} {n}: {m}"))
                except Exception as exc:          # reported; the next run goes on
                    res = exc
                results.append((path, res))
                sig.one_done.emit((path, res))
            sig.finished.emit(results)

        self.eln_lbl.setStyleSheet(f"color:{C['muted']};")
        self.eln_lbl.setText(f"sending {len(paths)} run(s) to eLabFTW ...")
        self.eln_lbl.show()
        self._eln_thread = threading.Thread(target=work, name="eln-upload", daemon=True)
        self._eln_thread.start()
        return True

    def wait_eln(self, timeout_s: float = 30.0) -> None:
        """Block until an upload ends and its result is shown (tests)."""
        if self._eln_thread is not None:
            self._eln_thread.join(timeout_s)
        for _ in range(3):
            QtCore.QCoreApplication.processEvents()
            QtCore.QCoreApplication.sendPostedEvents()

    def _eln_say(self, msg: str, error: bool = False):
        import html as _html
        self.eln_lbl.setStyleSheet(f"color:{C['danger' if error else 'muted']};")
        self.eln_lbl.setText(_html.escape(msg))
        self.eln_lbl.show()
        self.on_log(msg)

    def _eln_progress(self, msg: str):
        import html as _html
        self.eln_lbl.setText(_html.escape(msg))

    def _eln_one_done(self, item):
        path, res = item
        name = Path(path).name
        if isinstance(res, Exception):
            self.on_log(f"eLabFTW: {name} failed: {res}")
        else:
            self.on_log(f"eLabFTW: {name} {res.action} experiment {res.exp_id}  {res.url}")
            for w in res.warnings:
                self.on_log(f"eLabFTW: {name}: {w}")

    def _eln_finished(self, results):
        import html as _html
        lines, bad = [], 0
        for path, res in results:
            name = _html.escape(Path(path).name)
            if isinstance(res, Exception):
                bad += 1
                lines.append(f"{name}: <span style='color:{C['danger']}'>"
                             f"{_html.escape(str(res))}</span>")
            elif res.url:
                lines.append(f"{name}: {res.action} <a href='{_html.escape(res.url)}' "
                             f"style='color:{C['accent']}'>experiment {res.exp_id}</a>")
        self.eln_lbl.setStyleSheet(f"color:{C['muted']};")
        self.eln_lbl.setText("<br>".join(lines) or "nothing sent")
        self.eln_lbl.show()
        if bad and len(results) > 1:
            self.on_log(f"eLabFTW: {bad} of {len(results)} runs failed")
        self.refresh()                           # the eln column

    def _activated(self, item, _col=0):
        r = self._row_of(item)
        if r is None:
            return
        if r.get("error"):
            self.status.setText(f"cannot open {Path(r['path']).name}: {r['error']}")
            return
        self.open_file(r["path"])
