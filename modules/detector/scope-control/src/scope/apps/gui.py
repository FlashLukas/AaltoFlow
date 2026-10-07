"""Front panel for the oscilloscope (real scope or the simulated bench).

    uv run scripts/run_gui.py                  # a private simulated bench
    uv run scripts/run_gui.py --connect HOST   # the running service

Architecture in one breath: this window holds a Scope-like object (an
in-process Scope, or a ScopeClient for a service). It sends commands, reads
the status dict on a 60 ms timer, and fetches the live average (`get_trace
("live")`) about eight times a second -- traces never ride in the status.

Layout:
  left    the scope's own settings: CH1, CH2, timebase, trigger. The boxes
          follow the instrument until you edit one; an edited box is outlined
          until you press Set (or Enter) -- so a value nobody chose is never
          sent, and the poll never overwrites what you are typing.
  centre  two tabs (Lukas, 2026-10-07), each the full height:
          "X(t), Y(t)" -- the channels against time (CH2 on its own axis);
          "XY / YX"    -- the scope's XY mode: CH2 against CH1, or swapped.
          Both in the channels' physical units.
          A cursor readout under each plot, in THAT plot's axis units.
  The three columns sit in a splitter: drag the borders to resize; the
  widths, the tab and the XY/YX choice are remembered per PC (QSettings).
  right   averaging (with "312 / 500" and Restart), the filter, the numbers
          (per channel, and the phase of CH2 against CH1), Acquire, and the
          generator card -- greyed when the instrument has no generator.
"""

from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import COUPLINGS, TRIGGER_SOURCES, TRIGGER_SLOPES, TRIGGER_MODES
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog
from .control_bar import ControlBar, mark_always
from ..control import ControlRefused

_TRACE_PERIOD_MS = 120          # how often the live average is fetched


def _gui_settings() -> QtCore.QSettings:
    """Where this PC keeps the window's preferences (tab, splitter, XY/YX):
    the user's QSettings (the registry on Windows), or the .ini file named by
    AALTOFLOW_GUI_SETTINGS -- which the tests set, so they never touch the
    real one."""
    import os
    path = os.environ.get("AALTOFLOW_GUI_SETTINGS")
    if path:
        return QtCore.QSettings(path, QtCore.QSettings.IniFormat)
    return QtCore.QSettings("AaltoFlow", "scope-gui")


class Bridge(QtCore.QObject):
    """Carries events across the thread boundary into the GUI."""
    event = QtCore.Signal(str, str)


def _card(title: str | None = None):
    frame = QtWidgets.QFrame()
    frame.setObjectName("card")
    lay = QtWidgets.QVBoxLayout(frame)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(7)
    if title:
        lbl = QtWidgets.QLabel(title.upper())
        lbl.setObjectName("cardTitle")
        lay.addWidget(lbl)
    return frame, lay


def _num(v):
    """A status number, or None when missing / NaN / null."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fmt(v, spec=".4g", none="--") -> str:
    f = _num(v)
    return none if f is None else format(f, spec)


def _si(v: float | None, unit: str) -> str:
    """0.005 s -> '5 ms'."""
    f = _num(v)
    if f is None:
        return "--"
    for scale, prefix in ((1, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n")):
        if abs(f) >= scale * 0.9999 or scale == 1e-9:
            return f"{f / scale:.4g} {prefix}{unit}"
    return f"{f:g} {unit}"


def _ch_color(ch: str) -> str:
    return COLORS["accent"] if ch == "ch1" else COLORS["text"]


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg, remote: bool = False):
        super().__init__()
        self.ctrl, self.cfg, self._remote = ctrl, cfg, remote
        self._dirty: set = set()
        self._syncing = False
        self._last_trace_t = 0.0
        self._trace = None
        self._view_rev = None
        self.setWindowTitle("Oscilloscope" + ("  (remote)" if remote else ""))
        self.resize(1560, 940)

        root = QtWidgets.QWidget(); root.setObjectName("root")
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 14); outer.setSpacing(12)
        outer.addLayout(self._build_header())
        # A splitter, not a fixed layout (Lukas: "i need to be able to slide
        # enlarge the left panel width"): drag the borders. Each side panel's
        # minimum is what its widest row needs, so the Set buttons are never
        # clipped; the sizes are remembered per PC.
        self._settings = _gui_settings()
        self.body = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.body.setChildrenCollapsible(False)
        self.body.setHandleWidth(8)
        self.left_panel = self._scroll(self._build_left())
        self.right_panel = self._scroll(self._build_right())
        self.body.addWidget(self.left_panel)
        self.body.addWidget(self._build_centre())
        self.body.addWidget(self.right_panel)
        self.body.setStretchFactor(1, 1)
        sizes = self._settings.value("splitter_sizes")
        try:
            sizes = [int(x) for x in sizes] if sizes else None
        except (TypeError, ValueError):
            sizes = None
        self.body.setSizes(sizes if sizes and len(sizes) == 3 else [380, 860, 340])
        self.body.splitterMoved.connect(
            lambda *_: self._remember("splitter_sizes", self.body.sizes()))
        outer.addWidget(self.body, 1)

        self._control_bar = None
        if remote and hasattr(self.ctrl, "take_control"):
            central = QtWidgets.QWidget(); central.setObjectName("root")
            vbox = QtWidgets.QVBoxLayout(central)
            vbox.setContentsMargins(0, 0, 0, 0); vbox.setSpacing(0)
            self._control_bar = ControlBar(self.ctrl, self, log=self._on_event)
            vbox.addWidget(self._control_bar)
            vbox.addWidget(root, 1)
            self.setCentralWidget(central)
        else:
            self.setCentralWidget(root)

        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)
        # start: opens the scope and READS its settings (nothing changes on it)
        self.ctrl.start()
        self._sync_inputs(force=True)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()
        if self._control_bar is not None:
            self._control_bar.claim_if_free()

    # ---- building ------------------------------------------------------------

    @staticmethod
    def _scroll(widget):
        """A side panel: scrolls vertically, never clips horizontally -- its
        minimum width is what the content needs (+ the scroll bar)."""
        sc = QtWidgets.QScrollArea()
        sc.setWidget(widget); sc.setWidgetResizable(True)
        sc.setFrameShape(QtWidgets.QFrame.NoFrame)
        sc.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        bar = sc.style().pixelMetric(QtWidgets.QStyle.PM_ScrollBarExtent)
        sc.setMinimumWidth(widget.minimumSizeHint().width() + bar + 6)
        return sc

    def _remember(self, key, value):
        """Per-PC GUI preferences (QSettings). Failures are harmless."""
        try:
            self._settings.setValue(key, value)
        except Exception:
            pass

    @staticmethod
    def _spin(lo, hi, dec, suffix="", step=None):
        w = QtWidgets.QDoubleSpinBox()
        loc = QtCore.QLocale.c(); loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
        w.setLocale(loc)
        w.setRange(lo, hi); w.setDecimals(dec)
        w.setSingleStep(step if step is not None else 10 ** -dec)
        w.setKeyboardTracking(False)
        if suffix:
            w.setSuffix(" " + suffix)
        return w

    def _row(self, form, label, widget, send=None):
        """A form row; with `send` a Set button, and Enter in the box sends too.
        Editing marks the box dirty (see module docstring)."""
        row = QtWidgets.QHBoxLayout(); row.setSpacing(6)
        row.addWidget(widget, 1)
        if send is not None:
            btn = QtWidgets.QPushButton("Set"); btn.setFixedWidth(52)
            btn.clicked.connect(lambda: (self._call(send), self._clear_dirty(widget)))
            row.addWidget(btn)
            if isinstance(widget, QtWidgets.QAbstractSpinBox):
                widget.valueChanged.connect(lambda _v, w=widget: self._mark_dirty(w))
                widget.lineEdit().returnPressed.connect(
                    lambda: (self._call(send), self._clear_dirty(widget)))
        form.addRow(label, row)

    def _combo(self, items, send):
        """A combo that sends on a USER choice only (`activated`, gotcha #13)."""
        c = QtWidgets.QComboBox()
        c.addItems(list(items))
        c.activated.connect(lambda _i: self._call(send, c.currentText()))
        return c

    def _build_header(self):
        row = QtWidgets.QHBoxLayout(); row.setSpacing(12)
        title = QtWidgets.QLabel("OSCILLOSCOPE")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; "
                            f"letter-spacing:2px;")
        row.addWidget(title)
        self.conn = QtWidgets.QLabel("o  connecting"); row.addWidget(self.conn)
        self.idn = QtWidgets.QLabel(""); self.idn.setObjectName("hint"); row.addWidget(self.idn)
        row.addStretch(1)
        self.rate = QtWidgets.QLabel("trigger -- Hz"); self.rate.setObjectName("hint")
        row.addWidget(self.rate)
        settings = QtWidgets.QPushButton("Settings")
        settings.clicked.connect(self._open_settings)
        mark_always(settings)          # a viewer may LOOK; the service refuses the OK
        row.addWidget(settings)
        return row

    def _build_left(self):
        w = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(w); col.setContentsMargins(0, 0, 0, 0); col.setSpacing(10)
        self.inp = {}
        for ch in ("ch1", "ch2"):
            card, lay = _card(ch.upper())
            card.findChild(QtWidgets.QLabel).setStyleSheet(f"color:{_ch_color(ch)};")
            form = QtWidgets.QFormLayout(); form.setSpacing(6)
            on = QtWidgets.QCheckBox("trace on")
            on.clicked.connect(lambda v, c=ch: self._call(self.ctrl.set_channel_enabled, c, v))
            form.addRow("", on)
            vdiv = self._spin(0.001, 10.0, 3, "V/div", 0.1)
            self._row(form, "Scale", vdiv, lambda c=ch, s=vdiv: self.ctrl.set_vdiv(c, s.value()))
            off = self._spin(-40.0, 40.0, 3, "V", 0.01)
            self._row(form, "Offset", off, lambda c=ch, s=off: self.ctrl.set_offset(c, s.value()))
            cpl = self._combo(COUPLINGS, lambda v, c=ch: self.ctrl.set_coupling(c, v))
            form.addRow("Coupling", cpl)
            probe = self._spin(1, 1000, 0, "x", 1)
            self._row(form, "Probe", probe, lambda c=ch, s=probe: self.ctrl.set_probe(c, s.value()))
            lay.addLayout(form)
            sub = QtWidgets.QLabel("QUANTITY"); sub.setObjectName("hint"); lay.addWidget(sub)
            qf = QtWidgets.QFormLayout(); qf.setSpacing(6)
            label = QtWidgets.QLineEdit(); unit = QtWidgets.QLineEdit()
            scale = self._spin(-1e9, 1e9, 6, "per V", 1)
            qoff = self._spin(-1e9, 1e9, 6, "", 1)
            qf.addRow("Label", label); qf.addRow("Unit", unit)
            qf.addRow("Scale", scale); qf.addRow("At 0 V", qoff)
            send = QtWidgets.QPushButton("Set quantity")
            send.clicked.connect(lambda _=False, c=ch: self._send_quantity(c))
            qf.addRow("", send)
            lay.addLayout(qf)
            col.addWidget(card)
            self.inp[ch] = {"on": on, "vdiv": vdiv, "offset": off, "coupling": cpl,
                            "probe": probe, "label": label, "unit": unit, "scale": scale,
                            "qoff": qoff}
        card, lay = _card("Timebase")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        self.tdiv = self._spin(1e-6, 50e3, 6, "ms/div", 1)
        self._row(form, "Scale", self.tdiv, lambda: self.ctrl.set_tdiv(self.tdiv.value() / 1e3))
        self.delay = self._spin(-1e6, 1e6, 6, "ms", 1)
        self._row(form, "Delay", self.delay, lambda: self.ctrl.set_delay(self.delay.value() / 1e3))
        self.sara = QtWidgets.QLabel("--"); form.addRow("Sample rate", self.sara)
        lay.addLayout(form); col.addWidget(card)
        card, lay = _card("Trigger")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        self.tsrc = self._combo(TRIGGER_SOURCES, self.ctrl.set_trigger_source)
        form.addRow("Source", self.tsrc)
        self.tlevel = self._spin(-100, 100, 3, "V", 0.05)
        self._row(form, "Level", self.tlevel, lambda: self.ctrl.set_trigger_level(self.tlevel.value()))
        self.tslope = self._combo(TRIGGER_SLOPES, self.ctrl.set_trigger_slope)
        form.addRow("Slope", self.tslope)
        self.tmode = self._combo(TRIGGER_MODES, self.ctrl.set_trigger_mode)
        form.addRow("Mode", self.tmode)
        lay.addLayout(form); col.addWidget(card)
        col.addStretch(1)
        return w

    def _plot(self, xlabel, xunit, ylabel, yunit):
        import pyqtgraph as pg
        pg.setConfigOptions(antialias=True)
        p = pg.PlotWidget(background=COLORS["code_bg"])
        pen = pg.mkPen(COLORS["muted"])
        for axis in ("left", "bottom"):
            ax = p.getAxis(axis); ax.setPen(pen); ax.setTextPen(pen)
        p.setLabel("bottom", xlabel, units=xunit)
        p.setLabel("left", ylabel, units=yunit)
        p.showGrid(x=True, y=True, alpha=0.15)
        return p

    def _build_centre(self):
        import pyqtgraph as pg
        w = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(w); col.setContentsMargins(0, 0, 0, 0); col.setSpacing(10)
        self.tabs = QtWidgets.QTabWidget()
        mark_always(self.tabs)          # which view: this window only, fine for a viewer

        # -- tab 1: X(t), Y(t) --------------------------------------------------
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page); lay.setContentsMargins(6, 8, 6, 6)
        self.yt = self._plot("time", "s", "CH1", "")
        # CH2 on its OWN axis (right), as a scope scales each channel by its
        # own V/div: a 0.4 V signal next to a 10 V one would otherwise be a
        # flat line. A second ViewBox shares the time axis with the first.
        self.vb2 = pg.ViewBox()
        pi = self.yt.getPlotItem()
        pi.showAxis("right")
        pi.scene().addItem(self.vb2)
        pi.getAxis("right").linkToView(self.vb2)
        self.vb2.setXLink(pi)
        ax = pi.getAxis("right")
        ax.setPen(pg.mkPen(COLORS["muted"])); ax.setTextPen(pg.mkPen(_ch_color("ch2")))
        pi.getAxis("left").setTextPen(pg.mkPen(_ch_color("ch1")))
        pi.vb.sigResized.connect(lambda: self.vb2.setGeometry(pi.vb.sceneBoundingRect()))
        self.curves = {"ch1": self.yt.plot([], [], pen=pg.mkPen(_ch_color("ch1"), width=1.4)),
                       "ch2": pg.PlotCurveItem([], [], pen=pg.mkPen(_ch_color("ch2"), width=1.4))}
        self.vb2.addItem(self.curves["ch2"])
        self.trig_line = pg.InfiniteLine(pos=0, angle=90, movable=False,
                                         pen=pg.mkPen(COLORS["accent_dim"], style=QtCore.Qt.DashLine))
        self.yt.addItem(self.trig_line)
        lay.addWidget(self.yt, 1)
        self.yt_cursor = QtWidgets.QLabel(" "); self.yt_cursor.setObjectName("hint")
        lay.addWidget(self.yt_cursor)
        self.yt.scene().sigMouseMoved.connect(self._cursor_yt)
        self.tabs.addTab(page, "X(t), Y(t)")

        # -- tab 2: XY / YX -------------------------------------------------------
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page); lay.setContentsMargins(6, 8, 6, 6)
        bar = QtWidgets.QHBoxLayout()
        self.swap_xy = QtWidgets.QCheckBox("YX: CH2 horizontal, CH1 vertical")
        self.swap_xy.setToolTip("Which channel is horizontal (XY = CH1 horizontal).")
        mark_always(self.swap_xy)
        self.swap_xy.setChecked(self._settings.value("swap_xy", False, type=bool))
        self.swap_xy.toggled.connect(lambda on: (self._remember("swap_xy", bool(on)),
                                                 self._force_fetch()))
        bar.addWidget(self.swap_xy); bar.addStretch(1)
        lay.addLayout(bar)
        self.xy = self._plot("X", "", "Y", "")
        self.xy_curve = self.xy.plot([], [], pen=pg.mkPen(COLORS["accent"], width=1.6))
        lay.addWidget(self.xy, 1)
        self.xy_cursor = QtWidgets.QLabel(" "); self.xy_cursor.setObjectName("hint")
        lay.addWidget(self.xy_cursor)
        self.xy.scene().sigMouseMoved.connect(self._cursor_xy)
        self.tabs.addTab(page, "XY / YX")

        self.tabs.setCurrentIndex(self._settings.value("tab", 0, type=int))
        self.tabs.currentChanged.connect(lambda i: self._remember("tab", int(i)))
        col.addWidget(self.tabs, 1)
        card, lay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMaximumHeight(110)
        lay.addWidget(self.log)
        col.addWidget(card, 0)
        return w

    def _build_right(self):
        w = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(w); col.setContentsMargins(0, 0, 0, 0); col.setSpacing(10)
        card, lay = _card("Averaging")
        self.avg_label = QtWidgets.QLabel("0 / 0")
        self.avg_label.setObjectName("bigValue")
        lay.addWidget(self.avg_label)
        self.avg_bar = QtWidgets.QProgressBar(); self.avg_bar.setTextVisible(False)
        self.avg_bar.setFixedHeight(6); lay.addWidget(self.avg_bar)
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        self.avg = self._spin(1, 100000, 0, "traces", 1)
        self._row(form, "Average", self.avg, lambda: self.ctrl.set_averages(int(self.avg.value())))
        self.points = self._spin(16, 100000, 0, "points", 100)
        self._row(form, "Trace", self.points, lambda: self.ctrl.set_points(int(self.points.value())))
        lay.addLayout(form)
        btns = QtWidgets.QHBoxLayout()
        restart = QtWidgets.QPushButton("Restart average")
        restart.clicked.connect(lambda: self._call(self.ctrl.restart_average))
        btns.addWidget(restart)
        lay.addLayout(btns)
        col.addWidget(card)

        card, lay = _card("Filter (zero phase)")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        self.lp = self._spin(0, 1e9, 3, "Hz", 10)
        self._row(form, "Low-pass", self.lp, lambda: self.ctrl.set_filter(lowpass_Hz=self.lp.value()))
        self.hp = self._spin(0, 1e9, 3, "Hz", 1)
        self._row(form, "High-pass", self.hp, lambda: self.ctrl.set_filter(highpass_Hz=self.hp.value()))
        self.order = self._spin(1, 8, 0, "", 1)
        self._row(form, "Order", self.order, lambda: self.ctrl.set_filter(order=int(self.order.value())))
        hint = QtWidgets.QLabel("0 = off. The same filter on every channel, no phase "
                                "shift: the channels stay time-aligned.")
        hint.setObjectName("hint"); hint.setWordWrap(True)
        lay.addLayout(form); lay.addWidget(hint)
        col.addWidget(card)

        card, lay = _card("Measurement")
        abtn = QtWidgets.QHBoxLayout()
        self.acq_btn = QtWidgets.QPushButton("Acquire"); self.acq_btn.setObjectName("primary")
        self.acq_btn.clicked.connect(lambda: self._call(self.ctrl.acquire))
        self.abort_btn = QtWidgets.QPushButton("Abort"); self.abort_btn.setObjectName("danger")
        self.abort_btn.clicked.connect(lambda: self._call(self.ctrl.abort))
        mark_always(self.abort_btn)     # the safety verb: a viewer may stop it
        abtn.addWidget(self.acq_btn); abtn.addWidget(self.abort_btn)
        lay.addLayout(abtn)
        self.src = QtWidgets.QComboBox(); self.src.addItems(["live average", "last acquisition"])
        mark_always(self.src)           # which numbers to show: this window only
        self.src.currentIndexChanged.connect(lambda _i: self._force_fetch())
        lay.addWidget(self.src)
        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["", "CH1", "CH2"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        rows = [("mean", "mean"), ("rms", "rms"), ("pk2pk", "pk-pk"),
                ("amplitude", "amplitude"), ("frequency", "freq (Hz)")]
        self._value_rows = rows
        self.table.setRowCount(len(rows) + 1)
        for i, (_, name) in enumerate(rows):
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(name))
        self.table.setItem(len(rows), 0, QtWidgets.QTableWidgetItem("phase 2-1"))
        self.table.setMinimumHeight(230)
        lay.addWidget(self.table)
        col.addWidget(card)

        card, lay = _card("Generator")
        self.gen_note = QtWidgets.QLabel("")
        self.gen_note.setObjectName("hint"); self.gen_note.setWordWrap(True)
        lay.addWidget(self.gen_note)
        self.gen_card = card
        col.addWidget(card)
        col.addStretch(1)
        return w

    # ---- actions ----------------------------------------------------------------

    def _call(self, fn, *args):
        try:
            r = fn(*args)
        except (ControlRefused, ValueError) as exc:
            self._on_event("warn", f"refused: {exc}")
            return None
        if isinstance(r, dict) and r.get("ok") is False:
            self._on_event("warn", r.get("error", "refused"))
        return r

    def _send_quantity(self, ch):
        i = self.inp[ch]
        self._call(self.ctrl.set_physical, ch, i["scale"].value(), i["qoff"].value(),
                   i["unit"].text().strip() or "V", i["label"].text().strip())
        self._clear_dirty(i["scale"], i["qoff"])

    def _mark_dirty(self, spin):
        if self._syncing or spin in self._dirty:
            return
        self._dirty.add(spin)
        spin.setStyleSheet(f"border: 1px solid {COLORS['accent']};")
        spin.setToolTip("changed here, not sent yet -- press Set (or Enter)")

    def _clear_dirty(self, *spins):
        for spin in spins:
            self._dirty.discard(spin)
            spin.setStyleSheet("")
            spin.setToolTip("")

    def _open_settings(self):
        self.ctrl.get_config()
        SettingsDialog(self.ctrl, self.cfg, lambda: self._sync_inputs(force=True), self).exec()

    @staticmethod
    def _axis_text(axis, value: float) -> str:
        """`value` (a view coordinate, i.e. in the base unit) written the way
        the axis shows it: pyqtgraph puts an SI prefix on the axis label (mV,
        ms) and scales the ticks, so the readout must use the same scale --
        the first build printed raw volts under an axis in mV."""
        scale = getattr(axis, "autoSIPrefixScale", 1.0) or 1.0
        prefix = getattr(axis, "labelUnitPrefix", "") or ""
        units = getattr(axis, "labelUnits", "") or ""
        return f"{value * scale:.5g} {prefix}{units}".rstrip()

    def _cursor_yt(self, pos):
        pi = self.yt.getPlotItem()
        if not pi.vb.sceneBoundingRect().contains(pos):
            return
        p1 = pi.vb.mapSceneToView(pos)
        p2 = self.vb2.mapSceneToView(pos)
        self.yt_cursor.setText(
            f"cursor  t = {self._axis_text(pi.getAxis('bottom'), p1.x())}   "
            f"CH1 = {self._axis_text(pi.getAxis('left'), p1.y())}   "
            f"CH2 = {self._axis_text(pi.getAxis('right'), p2.y())}")

    def _cursor_xy(self, pos):
        pi = self.xy.getPlotItem()
        if not pi.vb.sceneBoundingRect().contains(pos):
            return
        p = pi.vb.mapSceneToView(pos)
        self.xy_cursor.setText(
            f"cursor  {pi.getAxis('bottom').labelText or 'x'} = "
            f"{self._axis_text(pi.getAxis('bottom'), p.x())}   "
            f"{pi.getAxis('left').labelText or 'y'} = "
            f"{self._axis_text(pi.getAxis('left'), p.y())}")

    def _force_fetch(self):
        self._last_trace_t = 0.0

    # ---- refresh -------------------------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = COLORS["danger"] if level == "error" else (
            COLORS["accent"] if level == "warn" else COLORS["muted"])
        self.log.appendHtml(f'<span style="color:{COLORS["accent_dim"]}">'
                            f'{time.strftime("%H:%M:%S")}</span> '
                            f'<span style="color:{color}">{msg}</span>')

    def _sync_inputs(self, force: bool = False):
        """Boxes follow the instrument, except the ones being edited."""
        s = self.ctrl.status()
        self._syncing = True
        try:
            def put(spin, value, k=1.0):
                f = _num(value)
                if f is not None and (force or spin not in self._dirty) and not spin.hasFocus():
                    spin.setValue(f * k)

            def put_combo(combo, value):
                if value and not combo.view().isVisible() and combo.currentText() != value:
                    i = combo.findText(str(value))
                    if i >= 0:
                        combo.setCurrentIndex(i)
            for ch, i in self.inp.items():
                put(i["vdiv"], s.get(f"{ch}_vdiv_V")); put(i["offset"], s.get(f"{ch}_offset_V"))
                put(i["probe"], s.get(f"{ch}_probe"))
                put(i["scale"], s.get(f"{ch}_phys_scale")); put(i["qoff"], s.get(f"{ch}_phys_offset"))
                put_combo(i["coupling"], s.get(f"{ch}_coupling"))
                i["on"].setChecked(bool(s.get(f"{ch}_enabled", True)))
                if force or not i["unit"].hasFocus():
                    i["unit"].setText(str(s.get(f"{ch}_unit") or "V"))
                if force or not i["label"].hasFocus():
                    i["label"].setText(str(s.get(f"{ch}_label") or ""))
            put(self.tdiv, s.get("tdiv_s"), 1e3); put(self.delay, s.get("delay_s"), 1e3)
            put(self.tlevel, s.get("trigger_level_V"))
            for combo, key in ((self.tsrc, "trigger_source"), (self.tslope, "trigger_slope"),
                               (self.tmode, "trigger_mode")):
                put_combo(combo, s.get(key))
            put(self.avg, s.get("averages")); put(self.points, s.get("points"))
            put(self.lp, s.get("lowpass_Hz")); put(self.hp, s.get("highpass_Hz"))
            put(self.order, s.get("filter_order"))
        finally:
            self._syncing = False
        if force:
            self._dirty.clear()
            for w in self.findChildren(QtWidgets.QDoubleSpinBox):
                w.setStyleSheet("")

    def _refresh(self):
        if self._control_bar is not None:
            self._control_bar.refresh()
        s = self.ctrl.status()
        if s.get("connected"):
            err = s.get("hw_error")
            self.conn.setText("o  scope error" if err else "o  connected")
            self.conn.setStyleSheet(f"color:{COLORS['danger'] if err else COLORS['ok']}; "
                                    f"font-weight:700;")
        else:
            self.conn.setText("o  offline")
            self.conn.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        self.idn.setText(str(s.get("idn") or ""))
        mode = s.get("trigger_mode")
        self.rate.setText(f"trigger {_fmt(s.get('trigger_rate_Hz'), '.1f')} Hz   mode {mode}"
                          + ("   STOPPED" if mode == "stop" else "")
                          + ("   ROLL (no triggered records)" if s.get("rolling") else ""))
        self.sara.setText(_si(s.get("sample_rate_Hz"), "Sa/s"))
        self._sync_inputs()
        n, want = int(s.get("running_n") or 0), max(1, int(s.get("averages") or 1))
        if s.get("acquiring"):
            pct = float(s.get("acq_progress") or 0)
            self.avg_label.setText(f"acquiring #{s.get('acq_id')}  {int(pct * want)} / {want}")
            self.avg_bar.setValue(int(100 * pct))
        else:
            self.avg_label.setText(f"{n} / {want}")
            self.avg_bar.setValue(int(100 * n / want))
        gens = int(s.get("generator_channels") or 0)
        self.gen_card.setEnabled(gens > 0)
        self.gen_note.setText(f"{gens} generator output(s)." if gens else
                              "This instrument has no generator. (The Analog Discovery "
                              "backend will have two; the AFG1062 is its own module.)")
        self._fill_table(s)
        now = time.monotonic()
        if now - self._last_trace_t >= _TRACE_PERIOD_MS / 1e3:
            self._last_trace_t = now
            self._fetch_and_draw(s)

    def _fill_table(self, s):
        smp = s.get("sample") or {}
        src = smp if (self.src.currentIndex() == 1 and smp and not smp.get("aborted")) \
            else (s.get("live") or {})
        rows = self._value_rows
        for j, ch in enumerate(("ch1", "ch2")):
            vals = src.get(ch) or {}
            for i, (key, _) in enumerate(rows):
                self.table.setItem(i, j + 1, QtWidgets.QTableWidgetItem(_fmt(vals.get(key), ".5g")))
        item = QtWidgets.QTableWidgetItem(_fmt(src.get("phase_21_deg"), ".2f") + " deg")
        # no phase: the reason on hover (flat / clipped channel, too short a record)
        item.setToolTip(src.get("phase_21_reason") or "")
        self.table.setItem(len(rows), 1, item)

    def _fetch_and_draw(self, s):
        which = "sample" if self.src.currentIndex() == 1 else "live"
        try:
            tr = self.ctrl.get_trace(which)
        except Exception:
            return                          # nothing yet: keep the last picture
        t = tr.get("time_s")
        if t is None or len(t) == 0:
            return
        units = {ch: s.get(f"{ch}_unit", "") for ch in ("ch1", "ch2")}
        for ch, curve in self.curves.items():
            y = tr.get(ch)
            if y is None:
                curve.setData([], [])
            else:
                curve.setData(t, y)
        for ch, side in (("ch1", "left"), ("ch2", "right")):
            self.yt.setLabel(side, s.get(f"{ch}_label") or ch.upper(), units=units[ch])
        x, y = tr.get("ch1"), tr.get("ch2")
        swap = self.swap_xy.isChecked()           # YX: CH2 horizontal
        if x is not None and y is not None:
            h, v = ("ch2", "ch1") if swap else ("ch1", "ch2")
            self.xy_curve.setData(*((y, x) if swap else (x, y)))
            self.xy.setLabel("bottom", s.get(f"{h}_label") or h.upper(), units=units[h])
            self.xy.setLabel("left", s.get(f"{v}_label") or v.upper(), units=units[v])

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Scope-like object. The theme is chosen ONCE
    here, from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))
    # '.' decimal point and no thousands separator whatever the Windows locale
    # (suite gotcha #18).
    loc = QtCore.QLocale.c()
    loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
    QtCore.QLocale.setDefault(loc)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from .theme import apply_window_icon
    apply_window_icon(app)
    app.setStyle("Fusion")
    apply_palette(app)
    app.setStyleSheet(build_stylesheet())
    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    return app.exec()


def main(theme: str | None = None) -> int:
    """Run against the built-in simulated bench, in-process."""
    from ..config import Config
    from ..sim_system import build_sim_system
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    scope, _ = build_sim_system(cfg)
    return run_app(scope, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
