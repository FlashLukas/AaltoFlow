"""suite.py -- the AaltoFlow measurement suite: one window, seven tabs.

    Control      raw control of every connected module, built from `describe`
    Navigator    a design file (GDS / image) registered to the stage: click, go
    Scan         define a recipe (the Scan Builder's palette + axis stack)
    Measurement  run it and watch it
    Data         open and plot any .nc file (the AaltoView viewer)
    Catalogue    search every run in the data folder (sample, operator, tags,
                 conditions, instrument values); double-click opens it in Data
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

THE SCAN SERVER (2026-10-05, scan_core/scan_server.py): scans can also run in
a service of their own instead of in this window. Settings tab: "Run scans on
this PC's scan server" sends Run there, and "Watch scan server" shows any
server's scan -- this PC's, or the lab PC's from the office -- on the
Measurement tab (apps/scan_server_view.py). Off by default: then everything is
exactly as before.

Run it:
    uv run python apps/suite.py [--theme light] [--no-follow] [--modules clMag,smb]
    uv run python apps/suite.py --scan-server lab-pc:5551     # watch that server
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
from apps.catalogue_view import CatalogueWidget
from apps.control_panel import ControlPanel
from apps.navigator import NavigatorWidget
from aaltoview.apps.viewer import ViewerWidget
from apps.scan_builder import ScanBuilder
from apps.scan_server_view import ServerWatch, parse_target
from apps.theme import C, DEFAULT_THEME, apply, set_theme

#: suite_local.json setting: Run sends scans to THIS PC's scan server
SETTING_RUN_ON_SERVER = "run_on_scan_server"

AVAILABILITY_PERIOD_S = 2.0


class _Availability(QtCore.QObject):
    """Carries (Discovery, ids that answer) from the checking thread to the GUI."""
    updated = QtCore.Signal(object, object)


class Suite(QtWidgets.QMainWindow):
    def __init__(self, host: str = "localhost", modules=(), out_dir: Path | None = None,
                 root: Path | None = None, follow: bool = False,
                 scan_server: str | None = None):
        super().__init__()
        self.host = host
        self.root = root
        self.lab = None
        self.connected_ids: list[str] = []
        self.found = None
        self.available: set[str] = set()
        #: the SCAN SERVER the Measurement tab is watching (ServerWatch), or None
        self.watch: ServerWatch | None = None
        #: scan servers discovery knows ("this PC" and the ones added with
        #: Add remote... in Mission Control): [(label, "host:cmd:pub")]
        self.server_choices: list[tuple[str, str]] = []
        self._last_note = ""
        self._failed_set: frozenset | None = None
        #: the watched lab's instruments, while this suite shows them (None:
        #: this PC's own) -- see _mirror_lab
        self._mirror_key: frozenset | None = None
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
        self.title_lbl = title
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
        # the RUN INFO is remembered in THIS root's suite_local.json
        self.builder.run_info.set_root(self.root)
        self.builder.run_info.set_data_dir(self.out_dir)   # its lists come from these files

        self.tabs = QtWidgets.QTabWidget()
        self.control = ControlPanel(on_log=self.log)
        self.tabs.addTab(self._wrap(self.control), "Control")
        # on a scan server's PC the Control tab's panel goes out with the plot
        # choice; on a PC watching another lab's server it is followed
        self.builder.view_extra = lambda: {"panel": self.control.panel_state()}
        self.builder.on_server_view = self._on_server_view_extra
        self.control.panel_changed.connect(self.builder._publish_view)
        self.navigator = NavigatorWidget(on_log=self.log, is_busy=self.scan_running)
        self.tabs.addTab(self._wrap(self.navigator), "Navigator")
        self.tabs.addTab(self._wrap(self.builder.centralWidget()), "Scan")
        self.tabs.addTab(self._build_measurement(), "Measurement")
        self.tabs.addTab(self._build_data(), "Data")
        # The run catalogue: an index of the data folder, searchable by sample,
        # operator, tags, conditions and instrument values. A tab of its own
        # rather than a pane in Data: the viewer already has its file list on
        # the left, and a search over a year of runs wants the full width.
        self.catalogue = CatalogueWidget(self.out_dir, open_file=self.open_in_viewer,
                                         on_log=self.log)
        self.tabs.addTab(self.catalogue, "Catalogue")
        # in a scroll area: with the SCAN SERVER card (2026-10-05) the page no
        # longer fits a short window, and a squeezed page draws the module
        # list's buttons over the list
        settings_scroll = QtWidgets.QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        settings_scroll.setWidget(self._build_settings(follow))
        self.tabs.addTab(settings_scroll, "Settings")
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
        self.navigator.set_source(registry=self.registry)
        self._sync_source_label()
        # Say where the first run will go, and whether it can go there, before
        # anyone presses Run.
        self.builder._refresh_save_target()
        self.log("suite ready - simulated registry"
                 + (" - following the launcher" if follow else " (Settings tab to connect)"))

        if modules:
            QtCore.QTimer.singleShot(200, lambda: self.connect_modules(modules))
        if scan_server:
            # opened from the "Scan server" card: watch it, on the Measurement tab
            self.watch_server(scan_server)
            self.tabs.setCurrentIndex(
                [self.tabs.tabText(i) for i in range(self.tabs.count())].index("Measurement"))
        elif self.run_on_server_box.isChecked():
            # this PC runs its scans on its scan server: show that one
            QtCore.QTimer.singleShot(0, self._watch_local_server)

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
        # would swap the registry under the next scan. (A scan on a SCAN SERVER
        # does not count: it runs on the server's own connections, and this
        # window's registry can change under it without harm.)
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
        h.addSpacing(20)
        # WHERE the running scan is -- point n / N, each axis's value (i/len),
        # the MEASURED time left, the routine step in progress, the queue
        # position (ScanBuilder.run_status_text, pushed at every point through
        # builder.on_status, and again by _tick). Before 2026-10-02 the only
        # number here was the elapsed time.
        self.where_lbl = QtWidgets.QLabel("")
        self.where_lbl.setWordWrap(True)
        self.where_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        h.addWidget(self.where_lbl, 1)
        self.hint = QtWidgets.QLabel("define the scan on the Scan tab")
        self.hint.setStyleSheet(f"color:{C['muted']};")
        h.addWidget(self.hint)
        v.addWidget(strip)
        self.builder.on_status = self._show_status

        # WATCHING A SCAN SERVER: whose scan this tab shows, who has control of
        # the server, and the way back to running scans in this window. Hidden
        # unless a server is watched (Settings tab, or the Scan server card).
        self.server_strip = QtWidgets.QFrame(); self.server_strip.setObjectName("card")
        self.server_strip.setStyleSheet(
            f"QFrame#card {{ border: 1px solid {C['accent']}; border-radius: 6px; }}")
        sh = QtWidgets.QHBoxLayout(self.server_strip); sh.setContentsMargins(14, 6, 14, 6)
        self.server_lbl = QtWidgets.QLabel("")
        self.server_lbl.setStyleSheet(f"color:{C['accent']}; font-weight:700;")
        self.server_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        sh.addWidget(self.server_lbl)
        sh.addSpacing(16)
        self.server_ctrl_lbl = QtWidgets.QLabel("")
        self.server_ctrl_lbl.setStyleSheet(f"color:{C['muted']};")
        sh.addWidget(self.server_ctrl_lbl, 1)
        self.server_ctrl_btn = QtWidgets.QPushButton("Take control")
        self.server_ctrl_btn.setToolTip(
            "Control of the scan server: who may answer its pause questions, clear\n"
            "faults and start scans. Abort and Stop queue are always allowed, for\n"
            "every PC. Nobody holding control = everyone may act.")
        self.server_ctrl_btn.clicked.connect(self._server_control_clicked)
        sh.addWidget(self.server_ctrl_btn)
        unwatch = QtWidgets.QPushButton("Stop watching")
        unwatch.setToolTip("Back to running scans in this window. The server's scan\n"
                           "goes on: closing a window never stops it.")
        unwatch.clicked.connect(self.stop_watching)
        sh.addWidget(unwatch)
        self.server_strip.hide()
        v.addWidget(self.server_strip)

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
        self.recall_btn = QtWidgets.QPushButton("Recall settings…")
        self.recall_btn.setToolTip(
            "The instrument settings stored in the file shown here (or one you\n"
            "pick), next to the instruments' settings now: choose which to set back.")
        self.recall_btn.clicked.connect(self._recall_from_data)
        self.data_view.add_action(self.recall_btn)
        g.addWidget(self.data_view, 1)
        v.addWidget(card, 1)
        return page

    def open_in_viewer(self, path) -> None:
        """Open a file in the Data tab (the Catalogue's double-click)."""
        names = [self.tabs.tabText(i) for i in range(self.tabs.count())]
        self.tabs.setCurrentIndex(names.index("Data"))
        self.data_view.load_file(Path(path))
        self.log(f"opened {Path(path).name}")

    def _show_last_run(self):
        ds = self.builder.dataset
        if ds is None:
            self.data_view.status.setText(
                "No scan has run yet in this session — "
                "the file list on the left has every earlier autosave.")
            return
        self.data_view.set_dataset(ds, self.builder.last_saved)

    def _recall_from_data(self):
        """Recall from the file open in the Data tab, else ask for one."""
        from apps.recall import open_recall
        path = getattr(self.data_view, "path", None)
        ds = getattr(self.data_view, "ds", None)
        if path:
            return open_recall(self, self.lab, str(path))
        if ds is not None:
            return open_recall(self, self.lab, ds=ds)
        return open_recall(self, self.lab, start_dir=str(self.out_dir))

    def _show_status(self, text: str):
        """The header's WHERE line; the "define the scan" hint when idle."""
        self.where_lbl.setText(text)
        self.hint.setVisible(not text)

    def _tick(self):
        if self.watch is not None and self.builder.worker is None:
            self._tick_server()
            return
        if self.scan_running():
            if getattr(self, "_t0", None) is None:
                self._t0 = time.monotonic()
            self.run_state.setText("RUNNING")
            secs = int(time.monotonic() - self._t0)
            self.elapsed_lbl.setText(f"elapsed {secs // 60}:{secs % 60:02d}")
            self._show_status(self.builder.run_status_text())
        else:
            self._show_status("")
            if getattr(self, "_t0", None) is not None:
                self.log("scan finished")
                self._t0 = None
            self.run_state.setText("idle")

    # ---- the SCAN SERVER (scan_core/scan_server.py) ------------------------
    #
    # A scan server runs scans in its own process on the lab PC; this window
    # can WATCH one (phase 1): its Measurement tab then shows the server's
    # scan with the same widgets as a scan of its own. On the server's own PC,
    # with "Run scans on this PC's scan server" ticked, Run submits there.

    def watch_server(self, target: str) -> ServerWatch | None:
        """Show the scan of the server at 'host[:cmd[:pub]]' on the Measurement tab."""
        if self.builder.worker is not None or self.builder.queue_running():
            self.log("a scan is running in this window -- let it end (or Abort it) "
                     "before watching a scan server")
            return None
        if self.watch is not None:
            self.stop_watching(quiet=True)
        host, cmd, pub = parse_target(target)
        w = ServerWatch(host, cmd, pub, parent=self)
        w.status.connect(self._on_server_status)
        w.log_lines.connect(self._on_server_log)
        w.live.connect(self.builder.show_server_dataset)
        w.connection.connect(self._on_server_connection)
        self.watch = w
        self.builder.attach_server(w, can_submit=self._may_submit(w))
        self.server_strip.show()
        self._sync_server_header()
        if hasattr(self, "watch_edit"):
            self.watch_edit.setText(f"{host}:{cmd}" + (f":{pub}" if pub else ""))
        self.log(f"watching the scan server on {host}:{cmd}")
        return w

    def stop_watching(self, quiet: bool = False) -> None:
        """Back to running scans in this window; the server's scan goes on."""
        w, self.watch = self.watch, None
        if w is None:
            return
        self.builder.detach_server()
        w.close()
        w.deleteLater()
        self._end_mirror()
        self.server_strip.hide()
        if not quiet:
            self.log(f"stopped watching the scan server on {w.label} "
                     f"(its scan, if any, goes on)")

    def _may_submit(self, w) -> bool:
        return bool(self.run_on_server_box.isChecked() and w is not None and w.is_local())

    def _local_server_target(self) -> str | None:
        found = self.found or discover(self.root)
        m = next((m for m in found.modules if m.key == "scanserver" and not m.remote), None)
        return f"localhost:{m.cmd}:{m.pub}" if m is not None else None

    def _watch_local_server(self) -> None:
        target = self._local_server_target()
        if target is None:
            self.log("no scan server on this PC (scan-core/module.toml missing?)")
            return
        if self.watch is not None and self.watch.is_local():
            self.builder.server_submit = self._may_submit(self.watch)
            self.builder.attach_server(self.watch, can_submit=self.builder.server_submit)
            return
        self.watch_server(target)

    def _run_on_server_toggled(self, on: bool) -> None:
        try:
            set_setting(SETTING_RUN_ON_SERVER, bool(on) or None, root=self.root)
        except OSError as exc:
            self.log(f"setting not remembered: {exc}")
        if on:
            self.log("Run now starts scans on this PC's scan server (start its card in "
                     "Mission Control if it is not running)")
            self._watch_local_server()
        elif self.watch is not None:
            self.builder.attach_server(self.watch, can_submit=False)
            self.log("Run no longer goes to the scan server (still watching it; "
                     "'Stop watching' to run scans in this window)")

    def _watch_clicked(self) -> None:
        text = self.watch_edit.text().strip()
        if not text:
            self.log("type host:port of a scan server, or pick one from the list")
            return
        self.watch_server(text)

    def _on_server_status(self, st: dict) -> None:
        if self.watch is None:
            return
        self.builder.server_submit = self._may_submit(self.watch)
        self.builder.show_server_status(st)
        self._sync_server_header()
        if not self.watch.is_local():
            self._mirror_lab(st)

    # ---- watching a server on ANOTHER PC: show that lab as the lab sees it ----
    #
    # Lukas 2026-10-06, the office and lab suites side by side: "they are very
    # different". The office suite had its own instruments (the remotes added
    # on the office PC, under long names like kim_130_233_203_119), its own
    # layouts and its own title. While it watches a scan server on another PC
    # it now uses THAT server's instruments -- the same services, under the
    # same names -- the lab's Control-tab layouts, follows the lab's panel,
    # and wears the lab's setup name. Stop watching gives it all back.

    def _mirror_lab(self, st: dict) -> None:
        inst = st.get("instruments")
        if not isinstance(inst, dict):
            return                          # a server from before this change
        host = self.watch.host
        endpoints = {slug: ((e.get("host") or host), int(e["cmd"]), int(e["pub"]))
                     for slug, e in inst.items() if isinstance(e, dict) and "cmd" in e}
        key = frozenset((s, ep) for s, ep in endpoints.items())
        first = self._mirror_key is None
        if key == self._mirror_key or self.scan_running():
            return
        self._mirror_key = key
        where = st.get("pc") or host
        if first:
            self.log(f"showing the instruments of {where} as its suite does "
                     f"(this PC's own come back with 'Stop watching')")
            try:
                layouts = self.watch.client.get_layouts()
            except Exception as exc:
                layouts = {}
                self.log(f"layouts of {where} not available: {exc}")
            self.control.use_layouts_of(layouts, where)
        self._set_title(st.get("setup_name") or where, f"watching {where}")
        if endpoints:
            self.log(f"connecting to the instruments of {where}: "
                     f"{', '.join(sorted(endpoints))} ...")
            self._connect_endpoints(endpoints, {})
        else:
            self.log(f"the scan server on {where} has no instrument connected")
        # the lab's panel, if one was already published
        view = (getattr(self.watch, "last_view", None) or {}).get("view") or {}
        if self.builder.follow_view_box.isChecked() and view.get("panel"):
            self.control.apply_panel_state(view["panel"])

    def _end_mirror(self) -> None:
        if self._mirror_key is None:
            return
        self._mirror_key = None
        self.control.use_layouts_of(None)
        self._set_title(None)
        if self.lab is not None:
            self.lab.close()
            self.lab = None
        self.connected_ids = []
        self._failed_set = None
        self.registry = build_sim_registry()
        self._adopt_source()
        self._refresh_module_tree()
        self.log("back to this PC's own instruments"
                 + (" (following the launcher)" if self.follow_box.isChecked() else
                    " -- connect them on the Settings tab"))

    def _set_title(self, setup: str | None, note: str = "") -> None:
        """The header and window title: this PC's setup, or a watched lab's."""
        if setup:
            text = f"{setup} · Measurement suite"
        else:
            text = suite_title("Measurement suite", self.root)
        self.title_lbl.setText(text.upper())
        self.setWindowTitle(text + (f"  ({note})" if note else ""))

    def _on_server_view_extra(self, view: dict) -> None:
        """The lab's Control-tab panel, followed while mirroring."""
        if self._mirror_key is not None and isinstance(view.get("panel"), dict):
            self.control.apply_panel_state(view["panel"])

    def _on_server_log(self, lines) -> None:
        for line in lines:
            stamp, sep, msg = str(line).partition("  ")
            self.log(f"[server {stamp}] {msg}" if sep else f"[server] {line}")

    def _on_server_connection(self, ok: bool, why: str) -> None:
        if self.watch is None:
            return
        if ok:
            self.log(f"scan server {self.watch.label} answers")
        else:
            self.log(f"scan server {self.watch.label} NOT answering ({why}) -- "
                     f"its scan, if any, is not affected; retrying")
        self._sync_server_header()

    def _sync_server_header(self) -> None:
        w = self.watch
        if w is None:
            return
        st = w.last or {}
        where = "this PC" if w.is_local() and st else (st.get("pc") or w.host)
        setup = st.get("setup_name") or ""
        text = f"watching scan server on {where}" + (f" (setup {setup})" if setup else "")
        if not w.answering:
            text += "  --  NOT ANSWERING"
        self.server_lbl.setText(text)
        state, ctrl = w.control_text()
        mode = ("Run starts scans on it" if self.builder.server_submit else
                "watch only: starting scans from here is phase 2")
        self.server_ctrl_lbl.setText(f"{ctrl}   ·   {mode}")
        self.server_ctrl_btn.setText("Release control" if state == "you" else "Take control")

    def _server_control_clicked(self) -> None:
        w = self.watch
        if w is None:
            return
        state, text = w.control_text()
        if state == "you":
            ok, why = w.release_control()
            self.log("scan server: control released" if ok else f"release failed: {why}")
        else:
            force = False
            if state == "other":
                ans = QtWidgets.QMessageBox.question(
                    self, "Take control of the scan server",
                    f"{text}.\n\nTake control from them? They become a viewer "
                    "(Abort and Stop queue stay allowed for everyone).")
                if ans != QtWidgets.QMessageBox.Yes:
                    return
                force = True
            ok, why = w.take_control(force=force)
            self.log("scan server: you have control" if ok else
                     f"scan server: control not taken {why}".rstrip())
        self._sync_server_header()

    def _tick_server(self) -> None:
        w = self.watch
        st = w.last or {}
        if not w.answering:
            self.run_state.setText("NOT ANSWERING")
        else:
            self.run_state.setText({"running": "RUNNING", "paused": "PAUSED",
                                    "waiting_operator": "YOUR TURN"}.get(
                                        st.get("state"), "idle"))
        secs = int(st.get("elapsed_s") or 0)
        self.elapsed_lbl.setText(f"elapsed {secs // 60}:{secs % 60:02d}")
        self._show_status(st.get("where") or "" if st.get("busy") else "")

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
        # The instrument SNAPSHOT in every file (scan_core/snapshot.py) holds
        # each instrument's idn, which may include its serial number. Data
        # files are the lab's own, so the default is to keep it.
        self.idn_box = QtWidgets.QCheckBox(
            "Store each instrument's identity (idn / serial) in the settings snapshot")
        self.idn_box.setToolTip(
            "Every scan file records every connected instrument's settings and state\n"
            "(for 'Recall settings...'). Untick to leave the idn / serial entries out,\n"
            "e.g. before sharing files. Suite setting: snapshot_include_idn.")
        from scan_core.snapshot import include_idn_setting
        self.idn_box.setChecked(include_idn_setting(self.root))
        self.idn_box.toggled.connect(
            lambda on: set_setting("snapshot_include_idn", bool(on), root=self.root))
        o.addWidget(self.idn_box)
        v.addWidget(out)

        # THE SCAN SERVER: run scans in a service instead of this window, and
        # watch a server's scan (this PC's, or the lab PC's from the office)
        srv = QtWidgets.QFrame(); srv.setObjectName("card")
        s = QtWidgets.QVBoxLayout(srv); s.setContentsMargins(14, 14, 14, 14); s.setSpacing(8)
        s.addWidget(self._tag("SCAN SERVER"))
        note3 = QtWidgets.QLabel(
            "A scan server runs scans in its own process (Mission Control: the 'Scan "
            "server' card). A scan there keeps running when this window closes, and any "
            "PC can watch it: progress, the live map, the log, the pause banners, Abort. "
            "Starting scans works from the server's own PC (watching works from anywhere). "
            "To watch the lab PC from the office: Mission Control > Add remote... with the "
            "lab PC's name and port 5551, then pick it below.")
        note3.setWordWrap(True); note3.setStyleSheet(f"color:{C['muted']};")
        s.addWidget(note3)
        self.run_on_server_box = QtWidgets.QCheckBox(
            "Run scans on this PC's scan server (instead of in this window)")
        self.run_on_server_box.setToolTip(
            "Run and a loaded queue go to the scan server of this PC; the Measurement\n"
            "tab shows its scan. Off (the default): scans run in this window, as always.\n"
            "Remembered on this PC (suite setting run_on_scan_server).")
        self.run_on_server_box.setChecked(bool(get_setting(SETTING_RUN_ON_SERVER, False,
                                                           root=self.root)))
        self.run_on_server_box.toggled.connect(self._run_on_server_toggled)
        s.addWidget(self.run_on_server_box)
        wrow = QtWidgets.QHBoxLayout()
        wrow.addWidget(QtWidgets.QLabel("Watch scan server"))
        self.watch_combo = QtWidgets.QComboBox()
        self.watch_combo.setMinimumWidth(240)
        self.watch_combo.setToolTip("Scan servers the launcher knows: this PC's, and the ones\n"
                                    "added with Add remote... in Mission Control.")
        self.watch_combo.activated.connect(
            lambda i: self.watch_edit.setText(self.watch_combo.itemData(i) or ""))
        wrow.addWidget(self.watch_combo)
        self.watch_edit = QtWidgets.QLineEdit("")
        self.watch_edit.setPlaceholderText("host:port, e.g. lab-pc:5551")
        self.watch_edit.returnPressed.connect(self._watch_clicked)
        wrow.addWidget(self.watch_edit, 1)
        wb = QtWidgets.QPushButton("Watch"); wb.setObjectName("primary")
        wb.clicked.connect(self._watch_clicked)
        wrow.addWidget(wb)
        sb = QtWidgets.QPushButton("Stop watching")
        sb.clicked.connect(self.stop_watching)
        wrow.addWidget(sb)
        s.addLayout(wrow)
        v.addWidget(srv)

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
        self.catalogue.set_data_dir(self.out_dir)     # ...and the Catalogue indexes it
        self.builder.run_info.set_data_dir(self.out_dir)   # ...and RUN INFO lists its values
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
        if self._refuse_while_scanning():
            return
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
        # Only INSTRUMENTS are connected: a scan server (a service that drives
        # instruments, suite_common COORDINATOR_KEYS) is watched, not built
        # into the registry -- it goes into the Watch list instead.
        instruments = {m.id for m in found.modules if m.is_instrument}
        self.found, self.available = found, set(up) & instruments
        self._refresh_server_choices(found, set(up))
        self._refresh_module_tree()

        connected = set(self.connected_ids)
        if self.lab is not None:
            gone = sorted(connected - self.available)
            if gone:
                self._note(f"not answering any more: {', '.join(gone)}")

        if not self.follow_box.isChecked() or self.scan_running():
            return
        if self._mirror_key is not None:
            return                         # the watched lab's instruments, not this PC's
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

    def _refresh_server_choices(self, found, up: set) -> None:
        """The Watch list: every scan server discovery knows, answering or not."""
        choices = []
        for m in found.modules:
            if m.is_instrument:
                continue
            where = f"{m.host}" if m.remote else "this PC"
            state = "running" if m.id in up else "down"
            host = m.host if m.remote else "localhost"
            choices.append((f"{m.name} - {where} ({state})", f"{host}:{m.cmd}:{m.pub}"))
        if choices == self.server_choices:
            return
        self.server_choices = choices
        self.watch_combo.clear()
        for label, target in choices:
            self.watch_combo.addItem(label, target)
        if choices and not self.watch_edit.text().strip():
            self.watch_edit.setText(choices[0][1])

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
            if not m.is_instrument:
                continue                   # the scan server: Watch list, below
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
        if self._refuse_while_scanning():
            return
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
        ids = {m.slug: m.id for m in specs}
        self._connect_endpoints(endpoints, ids, auto)

    def _connect_endpoints(self, endpoints: dict, ids: dict, auto: bool = False) -> bool:
        """Build the registry from {slug: (host, cmd, pub)} and adopt it in
        every tab. `ids` {slug: id} is what connected_ids records (discovery
        ids; empty for a mirrored lab). True when anything was connected."""
        def build(slugs):
            return build_lab_registry(include=tuple(slugs),
                                      endpoints={s: endpoints[s] for s in slugs},
                                      prefix=True, on_warn=self.log)
        try:
            reg, lab = build(list(endpoints))
        except Exception as exc:
            # One bad service (not running, or -- found on the lab PC
            # 2026-10-05 -- another service on its port) must not keep the
            # good ones out: try each alone, then connect the ones that work.
            good = []
            for s in endpoints:
                try:
                    _r, _l = build([s])
                    _l.close()
                    good.append(s)
                except Exception as one:
                    self.log(f"could not connect {s}: {one}")
            if not good or len(endpoints) == 1:
                self.log(f"connect failed: {exc}")
                if auto:
                    self._failed_set = frozenset(ids.values())
                return False
            try:
                reg, lab = build(good)
            except Exception as exc2:
                self.log(f"connect failed: {exc2}")
                if auto:
                    self._failed_set = frozenset(ids.values())
                return False
            tried = frozenset(ids.values())
            ids = {s: i for s, i in ids.items() if s in good}
        else:
            tried = None

        if self.lab is not None:
            self.lab.close()
        reg.settings_root = self.root      # snapshot_include_idn is read there
        self.registry, self.lab = reg, lab
        self.connected_ids = list(ids.values())
        # connected only PART of what was asked: remember the whole set, so
        # following the launcher does not rebuild this every few seconds
        self._failed_set = tried
        self._last_note = ""
        self._adopt_source()
        self._refresh_module_tree()
        self.log(f"connected: {len(reg.settables())} settables, "
                 f"{len(reg.gettables())} detectors")
        return True

    def _refuse_while_scanning(self) -> bool:
        """True (and says so) while a scan or a queue is running.

        Switching modules CLOSES the Lab: the sockets the running scan is
        using, closed from this (GUI) thread while the scan thread may be
        inside a request on them -- a ZeroMQ socket must never be used from two
        threads -- and it drops the scan's axis stack. Following the launcher
        already waited for the scan; the buttons did not (2026-09-28).
        """
        if self.scan_running():
            self.log("a scan is running -- stop it (Abort) before switching modules")
            return True
        return False

    def use_simulator(self) -> None:
        if self._refuse_while_scanning():
            return
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
        # the PAUSED banner's "Clear fault on <module>" buttons
        self.builder.fault_lab = self.lab
        # "Recall settings..." compares a file with these live instruments
        self.builder.lab = self.lab
        self.builder.autosave_dir = self.out_dir
        self.control.set_source(registry=self.registry, lab=self.lab, prefix=True)
        self.navigator.set_source(registry=self.registry, lab=self.lab)
        self._sync_source_label()

    def _sync_source_label(self):
        if self.lab is None:
            self.source_lbl.setText("source: simulator")
        else:
            names = ", ".join(sorted(self.lab.instruments))
            self.source_lbl.setText(f"source: live  -  {names}")

    def closeEvent(self, event):
        self._avail_stop.set()
        self.catalogue.cancel_scan()     # stops between two files
        # A running scan is ABORTED and waited for BEFORE the connections are
        # closed: its thread must not be left using sockets closed under it,
        # and the after-scan routine must get to run (2026-09-28).
        if not self.builder.stop_for_close():
            self.log("the scan did not stop within 30 s; closing anyway")
        # A scan on a SCAN SERVER is NOT stopped: only our connection closes.
        # That the scan outlives the window is the whole point of the server.
        if self.watch is not None:
            self.watch.close()
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
    ap.add_argument("--scan-server", default=None, metavar="HOST[:CMD[:PUB]]",
                    help="watch the scan server there on the Measurement tab "
                         "(what the 'Scan server' card opens)")
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
                follow=not args.no_follow and not modules,
                scan_server=args.scan_server)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
