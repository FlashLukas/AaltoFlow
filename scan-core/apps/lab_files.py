"""lab_files.py -- the saved measurements of a WATCHED scan server's PC.

Lukas 2026-10-06, watching the lab from the office: "would there be a
possibility to view the actually measured file in the data viewer?" The files
stay on the lab PC. This dialog lists them (the server's run catalogue: name,
time, axes, sample) and, on a double-click, fetches a COPY into a local cache
folder and hands it to the Data tab -- where every viewer tool works on it as
on a file of this PC.

Its own connection to the server, used only from worker threads: listing a big
folder or copying a 200 MB map must neither freeze this window nor delay an
Abort on the suite's command connection.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from apps.theme import C


def cache_path(pc: str, rel: str) -> Path:
    """Where a copy of the server PC's file `rel` is kept on this PC."""
    safe_pc = "".join(ch for ch in (pc or "lab") if ch.isalnum() or ch in "-_.") or "lab"
    parts = [p for p in Path(rel).parts if p not in ("..", ".", "/", "\\")]
    return Path(tempfile.gettempdir()) / "aaltoflow-lab-data" / safe_pc / Path(*parts)


def _fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class _Bridge(QtCore.QObject):
    listed = QtCore.Signal(object)             # list_files reply, or {"error": ...}
    progress = QtCore.Signal(int, int)         # bytes done, total
    fetched = QtCore.Signal(object, str)       # local Path or None, error text


class FileFetcher(QtCore.QObject):
    """Copy ONE file of the watched server's data folder to this PC, in a
    thread (phase 2: "Copy to this PC" on a finished scan of the queue).

    The same chunked get_file transfer as the dialog below (ScanServerClient.
    download: .part first, then replaced), on a connection of its own so a
    200 MB map never delays an Abort on the suite's command connection.
    `done(path or None, error text)` arrives on the GUI thread."""

    progress = QtCore.Signal(int, int)
    done = QtCore.Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.busy = False
        self._stop = threading.Event()

    def fetch(self, watch, rel: str) -> Path | None:
        """Start copying `rel` (relative to the server's data folder); the
        local destination, or None when a copy is already running."""
        if self.busy or not rel:
            return None
        pc = str((watch.last or {}).get("pc") or watch.host)
        dest = cache_path(pc, rel)
        self.busy = True
        host, cmd, pub = watch.host, watch.cmd_port, watch.pub_port

        def work():
            from scan_core.scan_server_client import ScanServerClient
            c = ScanServerClient(host, cmd, pub, timeout_ms=15000, kind="gui",
                                 name="measurement suite (copy)")
            try:
                path = c.download(rel, dest,
                                  progress=lambda d, t: self.progress.emit(d, t),
                                  cancel=self._stop.is_set)
                err = ""
            except Exception as exc:
                path, err = None, str(exc)
            finally:
                c.close()
            self.busy = False
            if not self._stop.is_set():
                self.done.emit(path, err)
        threading.Thread(target=work, daemon=True, name="lab-file-copy").start()
        return dest

    def cancel(self):
        self._stop.set()


class LabFilesDialog(QtWidgets.QDialog):
    """The server PC's measurements; double-click one to open a copy."""

    def __init__(self, watch, on_open, parent=None):
        super().__init__(parent)
        self.watch = watch
        self.on_open = on_open
        self.pc = str((watch.last or {}).get("pc") or watch.host)
        self.setWindowTitle(f"Measurements on {self.pc}")
        self.resize(900, 560)
        self._stop = threading.Event()
        self._busy = False
        self._bridge = _Bridge()
        self._bridge.listed.connect(self._show_list)
        self._bridge.progress.connect(self._show_progress)
        self._bridge.fetched.connect(self._fetched)

        v = QtWidgets.QVBoxLayout(self)
        row = QtWidgets.QHBoxLayout()
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("filter: words in name, sample, comment, tags ...")
        self._debounce = QtCore.QTimer(self); self._debounce.setSingleShot(True)
        self._debounce.setInterval(400)
        self._debounce.timeout.connect(self.refresh)
        self.filter.textChanged.connect(lambda *_: self._debounce.start())
        row.addWidget(self.filter, 1)
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        row.addWidget(self.refresh_btn)
        v.addLayout(row)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Measurement", "Measured", "Axes (outer -> inner)",
                                   "Sample", "Size"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(False)
        self.tree.itemDoubleClicked.connect(lambda it, _c: self.open_item(it))
        hdr = self.tree.header()
        hdr.setSectionResizeMode(QtWidgets.QHeaderView.Interactive)
        for col, w in ((0, 240), (1, 140), (2, 250), (3, 120)):
            self.tree.setColumnWidth(col, w)
        v.addWidget(self.tree, 1)

        self.status = QtWidgets.QLabel("asking the scan server ...")
        self.status.setStyleSheet(f"color:{C['muted']};")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.bar = QtWidgets.QProgressBar(); self.bar.hide()
        v.addWidget(self.bar)

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        self.open_btn = QtWidgets.QPushButton("Open a copy"); self.open_btn.setObjectName("primary")
        self.open_btn.clicked.connect(lambda: self.open_item(self.tree.currentItem()))
        close = QtWidgets.QPushButton("Close"); close.clicked.connect(self.close)
        btns.addWidget(self.open_btn); btns.addWidget(close)
        v.addLayout(btns)
        self.refresh()

    # ---- a connection for this dialog's threads -------------------------
    def _client(self):
        from scan_core.scan_server_client import ScanServerClient
        return ScanServerClient(self.watch.host, self.watch.cmd_port, self.watch.pub_port,
                                timeout_ms=15000, kind="gui",
                                name="measurement suite (lab files)")

    # ---- the list --------------------------------------------------------
    def refresh(self):
        text = self.filter.text().strip()

        def work():
            c = self._client()
            try:
                r = c.list_files(text=text)
            except Exception as exc:
                r = {"error": str(exc)}
            finally:
                c.close()
            if not self._stop.is_set():
                self._bridge.listed.emit(r)
        threading.Thread(target=work, daemon=True, name="lab-files-list").start()

    def _show_list(self, r: dict):
        if r.get("error"):
            self.status.setText(f"could not list the files: {r['error']}")
            return
        self.tree.clear()
        for f in r.get("files") or []:
            it = QtWidgets.QTreeWidgetItem([
                f.get("name", ""), str(f.get("measured", ""))[:19].replace("T", " "),
                " > ".join(f.get("dims") or []), f.get("sample", ""),
                _fmt_bytes(f.get("bytes"))])
            it.setData(0, QtCore.Qt.UserRole, f)
            it.setToolTip(0, f.get("path", ""))
            it.setTextAlignment(4, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.tree.addTopLevelItem(it)
        n = self.tree.topLevelItemCount()
        more = "  ·  still indexing, the list grows by itself" if r.get("indexing") else ""
        if r.get("indexing") and not self._stop.is_set():
            # the server indexes in the background (a first look at a big
            # folder takes a while): ask again shortly, until it is done
            QtCore.QTimer.singleShot(1000, self.refresh)
        self.status.setText(f"{n} measurement(s) in {r.get('data_dir', '?')} on "
                            f"{r.get('pc', self.pc)}{more}.  Double-click one to open "
                            f"a copy here.")

    # ---- one file --------------------------------------------------------
    def open_item(self, it) -> None:
        if it is None or self._busy:
            return
        f = it.data(0, QtCore.Qt.UserRole) or {}
        rel = f.get("path")
        if not rel:
            return
        dest = cache_path(self.pc, rel)
        self._busy = True
        self.open_btn.setEnabled(False)
        self.bar.setRange(0, 0); self.bar.show()
        self.status.setText(f"copying {f.get('name', rel)} from {self.pc} ...")

        def work():
            c = self._client()
            try:
                path = c.download(rel, dest,
                                  progress=lambda d, t: self._bridge.progress.emit(d, t),
                                  cancel=self._stop.is_set)
                err = ""
            except Exception as exc:
                path, err = None, str(exc)
            finally:
                c.close()
            if not self._stop.is_set():
                self._bridge.fetched.emit(path, err)
        threading.Thread(target=work, daemon=True, name="lab-files-get").start()

    def _show_progress(self, done: int, total: int):
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)
        self.bar.setFormat(f"{_fmt_bytes(done)} of {_fmt_bytes(total)}")

    def _fetched(self, path, err: str):
        self._busy = False
        self.open_btn.setEnabled(True)
        self.bar.hide()
        if path is None:
            self.status.setText(f"could not copy it: {err}")
            return
        self.status.setText(f"opened a copy: {path}")
        self.on_open(Path(path))

    def closeEvent(self, ev):
        self._stop.set()              # a running copy stops and removes its part file
        super().closeEvent(ev)
