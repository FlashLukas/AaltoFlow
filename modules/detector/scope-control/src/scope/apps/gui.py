"""Front panel for the oscilloscope (real scope or the simulated MOKE bench).

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
  centre  Y-t (all channels against time) and XY (the hysteresis loop:
          loop Y against loop X, background removed when that is on, Hc+ and
          Hc- marked). Both in the channels' physical units.
  right   averaging (with "312 / 500" and Restart), the filter, the loop
          settings, the numbers (per channel + loop), Acquire, and the
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
_LOOP_ROWS = (("hc", "Hc"), ("hc_plus", "Hc+"), ("hc_minus", "Hc-"), ("bias", "bias"),
              ("ms", "Ms"), ("mr", "Mr"), ("squareness", "Mr/Ms"), ("slope", "slope"),
              ("area", "area"))


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
        body = QtWidgets.QHBoxLayout(); body.setSpacing(12)
        body.addWidget(self._scroll(self._build_left(), 330))
        body.addWidget(self._build_centre(), 1)
        body.addWidget(self._scroll(self._build_right(), 320))
        outer.addLayout(body, 1)

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
    def _scroll(widget, width):
        sc = QtWidgets.QScrollArea()
        sc.setWidget(widget); sc.setWidgetResizable(True)
        sc.setFrameShape(QtWidgets.QFrame.NoFrame)
        sc.setFixedWidth(width + 14)
        sc.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        return sc

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
        card, lay = _card("Y-t")
        self.yt = self._plot("time", "s", "CH1", "")
        # CH2 on its OWN axis (right), as a scope scales each channel by its
        # own V/div: a 0.4 V intensity next to a 100 mT field would otherwise
        # be a flat line. A second ViewBox shares the time axis with the first.
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
        col.addWidget(card, 1)
        card, lay = _card("XY  -  hysteresis loop")
        self.xy = self._plot("X", "", "Y", "")
        self.loop_curve = self.xy.plot([], [], pen=pg.mkPen(COLORS["accent"], width=1.6))
        self.hc_lines = []
        for _ in range(2):
            ln = pg.InfiniteLine(angle=90, movable=False,
                                 pen=pg.mkPen(COLORS["accent_hi"], style=QtCore.Qt.DashLine))
            self.xy.addItem(ln); self.hc_lines.append(ln)
        self.cursor = QtWidgets.QLabel(""); self.cursor.setObjectName("hint")
        self.xy.scene().sigMouseMoved.connect(lambda pos: self._cursor(self.xy, pos))
        self.yt.scene().sigMouseMoved.connect(lambda pos: self._cursor(self.yt, pos))
        lay.addWidget(self.xy, 1)
        lay.addWidget(self.cursor)
        col.addWidget(card, 1)
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
                                "shift: the loop is not tilted by it.")
        hint.setObjectName("hint"); hint.setWordWrap(True)
        lay.addLayout(form); lay.addWidget(hint)
        col.addWidget(card)

        card, lay = _card("Loop")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        self.lx = self._combo(("ch1", "ch2"), lambda v: self.ctrl.set_loop(x=v))
        self.ly = self._combo(("ch1", "ch2"), lambda v: self.ctrl.set_loop(y=v))
        form.addRow("X", self.lx); form.addRow("Y", self.ly)
        self.bg = QtWidgets.QCheckBox("subtract linear background")
        self.bg.clicked.connect(lambda v: self._call(self.ctrl.set_analysis, None, bool(v)))
        form.addRow("", self.bg)
        lay.addLayout(form)
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
        self.table.setRowCount(len(rows) + 1 + len(_LOOP_ROWS))
        for i, (_, name) in enumerate(rows):
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(name))
        self.table.setItem(len(rows), 0, QtWidgets.QTableWidgetItem("phase 2-1"))
        for j, (_, name) in enumerate(_LOOP_ROWS):
            self.table.setItem(len(rows) + 1 + j, 0, QtWidgets.QTableWidgetItem("loop " + name))
        self.table.setMinimumHeight(420)
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

    def _cursor(self, plot, pos):
        vb = plot.getPlotItem().vb
        if plot.sceneBoundingRect().contains(pos):
            p = vb.mapSceneToView(pos)
            self.cursor.setText(f"cursor  x = {p.x():.5g}   y = {p.y():.5g}")

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
                               (self.tmode, "trigger_mode"), (self.lx, "loop_x"),
                               (self.ly, "loop_y")):
                put_combo(combo, s.get(key))
            put(self.avg, s.get("averages")); put(self.points, s.get("points"))
            put(self.lp, s.get("lowpass_Hz")); put(self.hp, s.get("highpass_Hz"))
            put(self.order, s.get("filter_order"))
            self.bg.setChecked(bool(s.get("subtract_background", True)))
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
                          + ("   STOPPED" if mode == "stop" else ""))
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
        self.table.setItem(len(rows), 1, QtWidgets.QTableWidgetItem(
            _fmt(src.get("phase_21_deg"), ".2f") + " deg"))
        loop = src.get("loop") or {}
        ux = s.get(f"{s.get('loop_x', 'ch1')}_unit", "")
        uy = s.get(f"{s.get('loop_y', 'ch2')}_unit", "")
        units = {"hc": ux, "hc_plus": ux, "hc_minus": ux, "bias": ux, "ms": uy, "mr": uy,
                 "squareness": "", "slope": f"{uy}/{ux}", "area": f"{ux}*{uy}"}
        for k, (key, _) in enumerate(_LOOP_ROWS):
            self.table.setItem(len(rows) + 1 + k, 1, QtWidgets.QTableWidgetItem(
                f"{_fmt(loop.get(key), '.5g')} {units[key]}"))

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
        lx, ly = s.get("loop_x", "ch1"), s.get("loop_y", "ch2")
        x = tr.get(lx)
        y = tr.get("loop_y") if tr.get("loop_y") is not None else tr.get(ly)
        if x is not None and y is not None and lx != ly:
            self.loop_curve.setData(x, y)
            self.xy.setLabel("bottom", f"{s.get(f'{lx}_label', lx.upper())}", units=units[lx])
            self.xy.setLabel("left", f"{s.get(f'{ly}_label', ly.upper())}", units=units[ly])
        loop = tr.get("loop") or {}
        for ln, key in zip(self.hc_lines, ("hc_plus", "hc_minus")):
            v = _num(loop.get(key))
            ln.setVisible(v is not None)
            if v is not None:
                ln.setValue(v)

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
