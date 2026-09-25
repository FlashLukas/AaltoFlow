"""suite.py -- the AaltoFlow measurement suite: one window, four tabs.

    Control      raw control of every connected module, built from `describe`
    Scan         define a recipe (the Scan Builder's palette + axis stack)
    Measurement  run it and watch it
    Settings     which modules to use, and where data goes

The split between Scan and Measurement is deliberate. Defining a scan is a
minute of fiddling; running it is an hour of watching. They want different
screens, and the run controls are better big than tucked into a corner of the
builder.

WHICH MODULES: the suite does not keep its own list. It uses the launcher's --
module discovery (suite-common): every module folder on this PC plus the remote
services added in Mission Control, with this PC's port overrides. A background
check every couple of seconds sees which of them answer. With "Follow the
launcher" on, the suite connects to whatever is running and adjusts when that
changes -- but never while a scan runs, and never by throwing away an axis
stack you are building (it says so instead).

It is not a rewrite of the Scan Builder. `ScanBuilder(embedded=True)` builds
exactly as before but leaves its right-hand pane -- Run/Abort, progress, ETA,
the result plot -- unparented, and the Measurement tab adopts those same
widgets. One implementation, driven by the same tested code, shown in two
places.

Run it:
    uv run python apps/suite.py [--theme light] [--no-follow] [--modules clMag,smb]
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # find scan_core
from suite_common import discover, get_setting, probe, set_setting
from suite_common import title as suite_title
from scan_core import build_sim_registry
from scan_core.lab import build_lab_registry
from apps.control_panel import ControlPanel
from aaltoview.apps.viewer import ViewerWidget
from apps.scan_builder import ScanBuilder
from apps.theme import C, DEFAULT_THEME, apply, set_theme

AVAILABILITY_PERIOD_S = 2.0


class _Availability(QtCore.QObject):
    """Carries (Discovery, ids that answer) from the checking thread to the GUI."""
    updated = QtCore.Signal(object, object)


class Suite(QtWidgets.QMainWindow):
    def __init__(self, host: str = "localhost", modules=(), out_dir: Path | None = None,
                 root: Path | None = None, follow: bool = False):
        super().__init__()
        self.host = host
        self.root = root
        self.lab = None
        self.connected_ids: list[str] = []
        self.found = None
        self.available: set[str] = set()
        self._last_note = ""
        self._failed_set: frozenset | None = None
        self.registry = build_sim_registry()
        # Where measurements land, most specific first: --out-dir for this
        # launch, then what was chosen on this PC last time, then the project's
        # own out/. Remembered, because "my data went into the repo folder
        # again" is a thing that happens once per launch otherwise.
        self.out_dir = Path(out_dir) if out_dir else Path(
            get_setting("data_dir", root=self.root)
            or Path(__file__).resolve().parent.parent / "out")

        # "TR-MOKE · Measurement suite": the setup this PC drives, from the installer
        self.setWindowTitle(suite_title("Measurement suite", self.root))
        self.resize(1500, 950)
        root_w = QtWidgets.QWidget(); root_w.setObjectName("root"); self.setCentralWidget(root_w)
        outer = QtWidgets.QVBoxLayout(root_w)
        outer.setContentsMargins(14, 12, 14, 12); outer.setSpacing(10)

        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(suite_title("Measurement suite", self.root).upper()); title.setObjectName("title")
        head.addWidget(title)
        head.addStretch(1)
        self.source_lbl = QtWidgets.QLabel()
        self.source_lbl.setStyleSheet(f"color:{C['muted']};")
        head.addWidget(self.source_lbl)
        outer.addLayout(head)

        # The builder is created first: the Measurement tab is built around the
        # pane it hands over.
        self.builder = ScanBuilder(registry=self.registry, embedded=True)
        self.builder.autosave_dir = self.out_dir      # save runs without being asked
        self.builder.on_log = self.log                # routines say what they are doing

        self.tabs = QtWidgets.QTabWidget()
        self.control = ControlPanel(on_log=self.log)
        self.tabs.addTab(self._wrap(self.control), "Control")
        self.tabs.addTab(self._wrap(self.builder.centralWidget()), "Scan")
        self.tabs.addTab(self._build_measurement(), "Measurement")
        self.tabs.addTab(self._build_data(), "Data")
        self.tabs.addTab(self._build_settings(follow), "Settings")
        # Opening the Scan tab re-reads the live limits: the ranges you are
        # about to type into should be the instrument's current ones.
        self.tabs.currentChanged.connect(self._on_tab_changed)
        outer.addWidget(self.tabs, 1)

        self.logbox = QtWidgets.QPlainTextEdit()
        self.logbox.setObjectName("log")
        self.logbox.setReadOnly(True)
        self.logbox.setFixedHeight(96)
        outer.addWidget(self.logbox)

        self.control.set_source(registry=self.registry)
        self._sync_source_label()
        # Say where the first run will go, and whether it can go there, before
        # anyone presses Run.
        self.builder._refresh_save_target()
        self.log("suite ready - simulated registry"
                 + (" - following the launcher" if follow else " (Settings tab to connect)"))

        if modules:
            QtCore.QTimer.singleShot(200, lambda: self.connect_modules(modules))

        self.clock = QtCore.QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(500)

        # which modules exist, and which answer -- off the GUI thread, because a
        # remote host that is switched off makes every probe wait its timeout
        self._avail = _Availability()
        self._avail.updated.connect(self._on_availability)
        self._avail_stop = threading.Event()
        self._avail_thread = threading.Thread(target=self._availability_loop,
                                              name="suite-availability", daemon=True)
        self._avail_thread.start()

    # ---- small helpers ---------------------------------------------------

    @staticmethod
    def _wrap(widget) -> QtWidgets.QWidget:
        """Put a widget in a plain container so a tab owns it cleanly."""
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.addWidget(widget)
        return page

    def log(self, msg: str) -> None:
        self.logbox.appendPlainText(f"{time.strftime('%H:%M:%S')}  {msg}")

    def scan_running(self) -> bool:
        # A queue counts between its scans too: following the launcher then
        # would swap the registry under the next scan.
        return (self.builder.worker is not None and self.builder.worker.isRunning()
                or self.builder.queue_running())

    # ---- Measurement -----------------------------------------------------

    def _build_measurement(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0); v.setSpacing(10)

        strip = QtWidgets.QFrame(); strip.setObjectName("card")
        h = QtWidgets.QHBoxLayout(strip); h.setContentsMargins(14, 10, 14, 10)
        self.run_state = QtWidgets.QLabel("idle")
        self.run_state.setStyleSheet(f"color:{C['accent']}; font-size:20px; font-weight:800;")
        h.addWidget(self.run_state)
        h.addSpacing(20)
        self.elapsed_lbl = QtWidgets.QLabel("elapsed 0:00")
        self.elapsed_lbl.setStyleSheet(f"color:{C['muted']};")
        h.addWidget(self.elapsed_lbl)
        h.addStretch(1)
        hint = QtWidgets.QLabel("define the scan on the Scan tab")
        hint.setStyleSheet(f"color:{C['muted']};")
        h.addWidget(hint)
        v.addWidget(strip)

        # The builder's own run pane, adopted rather than reimplemented.
        v.addWidget(self.builder.right_pane, 1)
        return page

    # ---- Data ------------------------------------------------------------

    def _build_data(self) -> QtWidgets.QWidget:
        """The data viewer (aaltoview, the AaltoView successor), embedded.

        During a run the Measurement tab's pane is for glancing at the map;
        afterwards you work with it here -- the data folder listed newest first,
        maps with cursors and line cuts, overlaid 1-D curves, and exports to
        files, Origin and Jupyter. The run that just finished is one click away
        (every run is autosaved). The same widget runs on its own, without the
        suite: `uv run python apps/viewer.py`.
        """
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0); v.setSpacing(10)

        card = QtWidgets.QFrame(); card.setObjectName("card")
        g = QtWidgets.QVBoxLayout(card); g.setContentsMargins(14, 12, 14, 12); g.setSpacing(8)

        self.data_view = ViewerWidget()
        self.data_view.default_dir = self.out_dir
        self.last_run_btn = QtWidgets.QPushButton("Show the last run")
        self.last_run_btn.setToolTip("The dataset from the most recent scan in this session.")
        self.last_run_btn.clicked.connect(self._show_last_run)
        self.data_view.add_action(self.last_run_btn)     # one toolbar, not two
        g.addWidget(self.data_view, 1)
        v.addWidget(card, 1)
        return page

    def _show_last_run(self):
        ds = self.builder.dataset
        if ds is None:
            self.data_view.status.setText(
                "No scan has run yet in this session — "
                "the file list on the left has every earlier autosave.")
            return
        self.data_view.set_dataset(ds, self.builder.last_saved)

    def _tick(self):
        if self.scan_running():
            if getattr(self, "_t0", None) is None:
                self._t0 = time.monotonic()
            self.run_state.setText("RUNNING")
            secs = int(time.monotonic() - self._t0)
            self.elapsed_lbl.setText(f"elapsed {secs // 60}:{secs % 60:02d}")
        else:
            if getattr(self, "_t0", None) is not None:
                self.log("scan finished")
                self._t0 = None
            self.run_state.setText("idle")

    # ---- Settings --------------------------------------------------------

    def _build_settings(self, follow: bool) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0); v.setSpacing(12)

        card = QtWidgets.QFrame(); card.setObjectName("card")
        g = QtWidgets.QVBoxLayout(card); g.setContentsMargins(14, 14, 14, 14); g.setSpacing(10)

        g.addWidget(self._tag("MODULES  -  as found by the launcher"))
        note = QtWidgets.QLabel(
            "The module folders on this PC plus the remote services added in Mission "
            "Control, with this PC's ports. Start and stop services, change ports and "
            "add remote ones in Mission Control; this list follows it. Nothing here is "
            "required: the suite works against the simulator with no hardware at all.")
        note.setWordWrap(True); note.setStyleSheet(f"color:{C['muted']};")
        g.addWidget(note)

        self.follow_box = QtWidgets.QCheckBox(
            "Follow the launcher: connect to the running modules automatically")
        self.follow_box.setChecked(follow)
        self.follow_box.setToolTip(
            "Connects when modules start or stop -- but not while a scan runs, and "
            "not if that would throw away the axis stack on the Scan tab.")
        g.addWidget(self.follow_box)

        self.mod_tree = QtWidgets.QTreeWidget()
        self.mod_tree.setHeaderLabels(["module", "status", "where", "ports", "prefix"])
        self.mod_tree.setRootIsDecorated(False)
        self.mod_tree.setMinimumHeight(240)
        g.addWidget(self.mod_tree, 1)

        btns = QtWidgets.QHBoxLayout()
        conn = QtWidgets.QPushButton("Connect ticked"); conn.setObjectName("primary")
        conn.clicked.connect(self._connect_clicked)
        allb = QtWidgets.QPushButton("Connect all running")
        allb.clicked.connect(lambda: self.connect_modules(sorted(self.available)))
        sim = QtWidgets.QPushButton("Use simulator")
        sim.clicked.connect(self._simulator_clicked)
        btns.addWidget(conn); btns.addWidget(allb); btns.addWidget(sim); btns.addStretch(1)
        g.addLayout(btns)
        v.addWidget(card, 1)

        out = QtWidgets.QFrame(); out.setObjectName("card")
        o = QtWidgets.QVBoxLayout(out); o.setContentsMargins(14, 14, 14, 14); o.setSpacing(8)
        o.addWidget(self._tag("DATA"))
        note2 = QtWidgets.QLabel(
            "Every scan is saved here by itself: <date>/<time>_<name>.nc, written when it "
            "finishes and, for scans over 100 points, at every 1/10 of the way -- so an "
            "aborted or interrupted run still leaves the points it measured. The file also "
            "carries the scan definition, so 'Load scan...' on the Scan tab can read it back, "
            "and the Data tab opens it. This folder is remembered for the next launch.")
        note2.setWordWrap(True); note2.setStyleSheet(f"color:{C['muted']};")
        o.addWidget(note2)
        row2 = QtWidgets.QHBoxLayout()
        self.out_edit = QtWidgets.QLineEdit(str(self.out_dir))
        self.out_edit.setToolTip("Type a folder and press Enter, or use Browse...\n"
                                 "The choice is remembered for the next launch.")
        # A typed path must APPLY. A box you can type into that quietly ignores
        # you is worse than a read-only label: the next scan lands somewhere
        # else and nothing said so.
        self.out_edit.editingFinished.connect(self._typed_out_dir)
        row2.addWidget(self.out_edit, 1)
        pick = QtWidgets.QPushButton("Browse...")
        pick.clicked.connect(self._pick_out_dir)
        row2.addWidget(pick)
        o.addLayout(row2)
        v.addWidget(out)

        theme = QtWidgets.QLabel(
            "Theme is chosen at launch: apps/suite.py --theme light. "
            "Switching live would need a re-theme pass over the pyqtgraph plots.")
        theme.setWordWrap(True); theme.setStyleSheet(f"color:{C['muted']};")
        v.addWidget(theme)
        return page

    def _tag(self, text):
        lbl = QtWidgets.QLabel(text)
        lbl.setStyleSheet(f"color:{C['muted']}; font-size:10px; font-weight:700;")
        return lbl

    def _pick_out_dir(self):
        got = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Data directory", str(self.out_dir))
        if got:
            self.set_out_dir(Path(got))

    def _typed_out_dir(self):
        """Apply a path typed into the box (Enter, or leaving the field)."""
        text = self.out_edit.text().strip()
        if not text or Path(text) == self.out_dir:
            return
        path = Path(text).expanduser()
        if not path.is_dir():
            # Do not create it from half-typed text -- that is how a folder
            # called "C:\Data\expe" appears. Browse... makes new folders.
            self.log(f"no such folder: {path} (data directory unchanged)")
            self.out_edit.setText(str(self.out_dir))
            return
        self.set_out_dir(path)

    def set_out_dir(self, path: Path) -> None:
        """Point everything that writes or reads data at `path`, and remember it."""
        self.out_dir = Path(path)
        self.out_edit.setText(str(self.out_dir))
        self.builder.autosave_dir = self.out_dir      # every run lands here from now on
        self.data_view.default_dir = self.out_dir     # ...and the Data tab lists it
        self.builder._refresh_save_target()           # ...and say so, having tried it
        try:
            set_setting("data_dir", str(self.out_dir), root=self.root)
        except OSError as exc:                        # a read-only checkout, say
            self.log(f"data directory set, but not remembered: {exc}")
        self.log(f"data directory: {self.out_dir}")

    def ticked_ids(self) -> list[str]:
        out = []
        for i in range(self.mod_tree.topLevelItemCount()):
            item = self.mod_tree.topLevelItem(i)
            if item.checkState(0) == QtCore.Qt.Checked:
                out.append(item.data(0, QtCore.Qt.UserRole))
        return out

    def _connect_clicked(self):
        wanted = self.ticked_ids()
        if not wanted:
            self.log("tick at least one module first")
            return
        self.connect_modules(wanted)

    def _simulator_clicked(self):
        # Choosing the simulator by hand means: stop following the launcher,
        # or the next availability check would connect straight back.
        if self.follow_box.isChecked():
            self.follow_box.setChecked(False)
            self.log("following the launcher switched off (simulator chosen)")
        self.use_simulator()

    # ---- availability ------------------------------------------------------

    def _availability_loop(self):
        from concurrent.futures import ThreadPoolExecutor
        first = True
        while True:
            try:
                found = discover(self.root)
                if first:
                    # show the list at once; the probes below take a moment
                    self._avail.updated.emit(found, set(self.available))
                    first = False
                mods = found.modules
                if mods:
                    # in parallel: a closed port can take the full timeout
                    with ThreadPoolExecutor(max_workers=min(16, len(mods))) as pool:
                        ups = list(pool.map(lambda m: probe(m.host, m.cmd, 0.3), mods))
                else:
                    ups = []
                up = {m.id for m, ok in zip(mods, ups) if ok}
            except Exception:              # never let the checker thread die
                found, up = None, set()
            if self._avail_stop.is_set():
                return
            if found is not None:
                self._avail.updated.emit(found, up)
            if self._avail_stop.wait(AVAILABILITY_PERIOD_S):
                return

    def _on_availability(self, found, up):
        self.found, self.available = found, set(up)
        self._refresh_module_tree()

        connected = set(self.connected_ids)
        if self.lab is not None:
            gone = sorted(connected - self.available)
            if gone:
                self._note(f"not answering any more: {', '.join(gone)}")

        if not self.follow_box.isChecked() or self.scan_running():
            return
        wanted = set(self.available)
        if not wanted or wanted == connected:
            return
        if frozenset(wanted) == self._failed_set:
            return                         # already tried exactly this; do not loop
        # Conditions and routines count too: a swap drops them as well.
        if self.lab is not None and self.builder.has_definition():
            added, removed = sorted(wanted - connected), sorted(connected - wanted)
            change = ", ".join([f"+{a}" for a in added] + [f"-{r}" for r in removed])
            self._note(f"modules changed ({change}); your axis stack is kept -- "
                       f"press 'Connect all running' on the Settings tab to rebuild")
            return
        self.connect_modules(sorted(wanted), auto=True)

    def _note(self, msg: str):
        """Log a message once, not every availability tick."""
        if msg != self._last_note:
            self._last_note = msg
            self.log(msg)

    def _refresh_module_tree(self):
        ticked = set(self.ticked_ids()) if self.mod_tree.topLevelItemCount() else set(self.connected_ids)
        self.mod_tree.clear()
        if self.found is None:
            return
        for m in self.found.modules:
            up = m.id in self.available
            where = f"remote {m.host}" if m.remote else (m.dir.name if m.dir else "")
            item = QtWidgets.QTreeWidgetItem(
                [m.name, "running" if up else "down", where, f"{m.cmd}/{m.pub}", m.slug])
            item.setData(0, QtCore.Qt.UserRole, m.id)
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(0, QtCore.Qt.Checked if m.id in ticked else QtCore.Qt.Unchecked)
            item.setForeground(1, QtGui.QColor(C["ok"] if up else C["muted"]))
            if m.id in self.connected_ids:
                item.setText(1, "connected" if up else "connected, NOT answering")
                if not up:
                    item.setForeground(1, QtGui.QColor(C["danger"]))
            self.mod_tree.addTopLevelItem(item)
        for col in range(5):
            self.mod_tree.resizeColumnToContents(col)

    # ---- switching what we drive ----------------------------------------

    def _on_tab_changed(self, index: int) -> None:
        tab = self.tabs.tabText(index)
        if tab == "Scan":
            self.builder.refresh_axis_limits()
        elif tab == "Data" and self.data_view.ds is None and self.builder.dataset is not None:
            self._show_last_run()        # the obvious thing to want to look at

    def _refresh_limits(self) -> list:
        """Re-read `describe` where the revision moved; say what changed.

        Cheap: one integer per instrument, already in the status stream.
        """
        if self.lab is None:
            return []
        moved = self.lab.refresh_stale(self.registry, prefix=True, on_warn=self.log)
        if moved:
            self.log(f"limits refreshed: {', '.join(moved)}")
        return moved

    def _group_names(self) -> dict[str, str]:
        """Parameter prefix -> the launcher's name for that module, so the Scan
        tab's palette shows "Power meter · pm16" rather than a bare prefix."""
        if not self.connected_ids:
            return {}
        found = self.found or discover(self.root)
        return {m.slug: m.name for m in found.modules if m.id in self.connected_ids}

    def connect_modules(self, names, auto: bool = False) -> None:
        """Connect to live services and rebuild every tab around them.

        `names` may be discovery ids ("hf2@lab2:5569"), slugs ("hf2_lab2") or
        plain module keys ("hf2").
        """
        found = self.found or discover(self.root)
        specs = []
        for n in names:
            spec = next((m for m in found.modules if n in (m.id, m.slug, m.key)), None)
            if spec is None:
                self.log(f"no module called {n!r} (not in the launcher's list)")
                continue
            if spec not in specs:
                specs.append(spec)
        if not specs:
            return
        endpoints = {}
        for m in specs:
            host = self.host if (not m.remote and self.host not in ("", "localhost")) else m.host
            endpoints[m.slug] = (host, m.cmd, m.pub)
        label = ", ".join(m.slug for m in specs)
        self.log(("following the launcher: " if auto else "") + f"connecting to {label} ...")
        try:
            reg, lab = build_lab_registry(include=tuple(endpoints), endpoints=endpoints,
                                          prefix=True, on_warn=self.log)
        except Exception as exc:
            # A service that is not running is the overwhelmingly common case,
            # so say which and stay where we are rather than dying.
            self.log(f"connect failed: {exc}")
            if auto:
                self._failed_set = frozenset(m.id for m in specs)
            return

        if self.lab is not None:
            self.lab.close()
        self.registry, self.lab = reg, lab
        self.connected_ids = [m.id for m in specs]
        self._failed_set = None
        self._last_note = ""
        self._adopt_source()
        self._refresh_module_tree()
        self.log(f"connected: {len(reg.settables())} settables, "
                 f"{len(reg.gettables())} detectors")

    def use_simulator(self) -> None:
        if self.lab is not None:
            self.lab.close()
            self.lab = None
        self.connected_ids = []
        self.registry = build_sim_registry()
        self._adopt_source()
        self._refresh_module_tree()
        self.log("switched to the simulated registry")

    def _adopt_source(self):
        self.builder.set_registry(self.registry, group_names=self._group_names())
        if self.lab is not None:
            # Abort must reach an instrument that is mid-settle, not only the
            # engine between points.
            self.lab.set_abort(self.builder.is_aborting)
        # ... and the Scan tab's ranges must follow the instruments, not stay
        # as they were when we connected.
        self.builder.limits_refresher = self._refresh_limits
        self.builder.autosave_dir = self.out_dir
        self.control.set_source(registry=self.registry, lab=self.lab, prefix=True)
        self._sync_source_label()

    def _sync_source_label(self):
        if self.lab is None:
            self.source_lbl.setText("source: simulator")
        else:
            names = ", ".join(sorted(self.lab.instruments))
            self.source_lbl.setText(f"source: live  -  {names}")

    def closeEvent(self, event):
        self._avail_stop.set()
        if self.lab is not None:
            self.lab.close()
        super().closeEvent(event)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AaltoFlow measurement suite")
    ap.add_argument("--theme", choices=["dark", "light"], default=None)
    ap.add_argument("--connect", default="localhost",
                    help="host of the LOCAL modules (remote ones carry their own)")
    ap.add_argument("--modules", default="",
                    help="comma-separated modules to connect at startup (keys or slugs)")
    ap.add_argument("--out-dir", default=None,
                    help="data folder for THIS session only (the remembered one, "
                         "set on the Settings tab, is left as it is)")
    ap.add_argument("--no-follow", action="store_true",
                    help="do not connect automatically to what the launcher is running")
    args = ap.parse_args(argv)

    set_theme(args.theme or DEFAULT_THEME)      # BEFORE any widget is built
    import pyqtgraph as pg
    pg.setConfigOption("background", C["code_bg"])
    pg.setConfigOption("foreground", C["text"])
    pg.setConfigOption("imageAxisOrder", "row-major")   # images are (y, x) everywhere

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
    from apps.theme import apply_window_icon
    apply_window_icon(app)
    apply(app)
    modules = [m.strip() for m in args.modules.split(",") if m.strip()]
    # An explicit --modules list is a choice; following would override it.
    win = Suite(host=args.connect, modules=modules,
                out_dir=Path(args.out_dir) if args.out_dir else None,
                follow=not args.no_follow and not modules)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
