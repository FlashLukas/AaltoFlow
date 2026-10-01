"""Control GUI for the spectrum analyser -- a Signal Hound SA44B / SA124B
(+ USB-TG44A), or the simulator.

    uv run scripts/run_gui.py                  # local simulator
    uv run scripts/run_gui.py --real           # the real analyser, in this process
    uv run scripts/run_gui.py --connect HOST   # a running service (either kind)

The window holds a brain-like object (an in-process SpectrumAnalyzer or a
SignalhoundClient facade) and never cares which. A 60 ms timer reads status();
a new trace is fetched only when `trace_id` moved. Events cross into the GUI
thread on a Qt signal.

Signature widget: HoundScope -- a miniature analyser screen. On top, a ruler
of everything the connected model can reach, with the swept window lit (and
the tracking generator's range under it, a diamond where its CW sits); below,
the latest trace drawn like phosphor on a 10 dB/div graticule hung from the
reference level, with the sweep beam running across as the analyser sweeps.

THE TRACKING GENERATOR is SHOWN here, never driven (Lukas, 2026-09-28): the
shsg module uses it as a CW source and shsna for TG sweeps, both through this
service. The TRACKING GENERATOR card says what it is doing -- unknown, parked,
a CW, or a TG sweep for shsna (spectrum sweeping then pauses).
"""

from __future__ import annotations

import math
import time

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from ..instruments import DETECTORS, TG_RANGE_HZ, model_range
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog
from .control_bar import ControlBar, mark_always


class Bridge(QtCore.QObject):
    """Carries analyser events across the thread boundary into the GUI."""
    event = QtCore.Signal(str, str)


def _card(title: str | None = None):
    frame = QtWidgets.QFrame()
    frame.setObjectName("card")
    lay = QtWidgets.QVBoxLayout(frame)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(8)
    if title:
        lbl = QtWidgets.QLabel(title.upper())
        lbl.setObjectName("cardTitle")
        lay.addWidget(lbl)
    return frame, lay


def _fmt(v, fmt: str, none: str = "--") -> str:
    return format(v, fmt) if isinstance(v, (int, float)) and math.isfinite(v) else none


def _hz(v: float) -> str:
    """A frequency in the unit a person would say it in."""
    if not (isinstance(v, (int, float)) and math.isfinite(v)):
        return "--"
    for unit, scale in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if abs(v) >= scale:
            return f"{v / scale:.6g} {unit}"
    return f"{v:.3g} Hz"


def tg_line(s) -> str:
    """One line saying what the tracking generator is doing (it is driven by
    the shsg / shsna modules, not from this panel)."""
    if not getattr(s, "tg_attached", False):
        return "not attached"
    mode = getattr(s, "tg_mode", "unknown")
    if mode == "sweep":
        return f"TG sweep #{s.tg_acq_id} for SNA running -- spectrum paused"
    if mode == "cw":
        return f"CW {_hz(s.tg_cw_freq_hz)} at {_fmt(s.tg_cw_level_dbm, 'g')} dBm (SG)"
    if mode == "parked":
        return (f"parked: {_hz(s.tg_park_hz)} at {_fmt(s.tg_park_level_dbm, 'g')} dBm "
                "(it has no off)")
    return "unknown (may be emitting what another program left on)"


# ------------------------------------------------------------- the indicator

class HoundScope(QtWidgets.QWidget):
    """A miniature analyser screen: model range ruler + phosphor trace + sweep beam."""

    DIVS_X, DIVS_Y, DB_PER_DIV = 10, 8, 10.0

    def __init__(self):
        super().__init__()
        self.setMinimumSize(300, 190)
        self._range = model_range("SA44B")[:2]
        self._tg_attached = False
        self._window = (math.nan, math.nan)
        self._trace = None                 # (freqs, dB) of the latest sweep
        self._ref = -20.0
        self._progress = 0.0
        self._sweeping = False
        self._tg_cw = math.nan              # CW frequency when the TG emits one
        self._overload = False
        self._caption = ""
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, model, tg_attached, start_Hz, stop_Hz, ref_dBm, progress, sweeping,
                  tg_cw_Hz, overload, caption):
        """tg_cw_Hz: the frequency of the TG's CW, NaN when there is none."""
        self._range = model_range(model or "SA44B")[:2]
        self._tg_attached = bool(tg_attached)
        self._window = (start_Hz, stop_Hz)
        if isinstance(ref_dBm, (int, float)) and math.isfinite(ref_dBm):
            self._ref = float(ref_dBm)
        self._progress = float(progress) if math.isfinite(progress) else 0.0
        self._sweeping, self._overload = bool(sweeping), bool(overload)
        self._tg_cw = float(tg_cw_Hz) if isinstance(tg_cw_Hz, (int, float)) else math.nan
        self._caption = caption

    def set_trace(self, freqs, db):
        self._trace = (np.asarray(freqs, dtype=float), np.asarray(db, dtype=float))

    def _tick(self):
        if not self.isVisible():
            return
        self._phase = (self._phase + 0.05) % 1.0
        self.update()

    def _columns(self, x0, width):
        """The trace squeezed into one value per pixel column -- the MAXIMUM,
        like an analyser's display, so a narrow tone survives the squeeze."""
        f, db = self._trace
        lo, hi = self._window
        if f.size < 2 or not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
            return []
        n = max(2, int(width))
        col = np.clip(((f - lo) / (hi - lo) * (n - 1)).round().astype(int), 0, n - 1)
        out = np.full(n, -np.inf)
        good = np.isfinite(db)
        np.maximum.at(out, col[good], db[good])
        return [(x0 + i * width / (n - 1), v) for i, v in enumerate(out) if np.isfinite(v)]

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        m = 6
        f = p.font()
        f.setPointSize(7)
        p.setFont(f)

        # ---- the ruler: everything this model can reach, the window lit ----
        fmin, fmax = self._range
        rx, ry, rw, rh = m, m, w - 2 * m, 8
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(COLORS["panel_hi"]))
        p.drawRoundedRect(QtCore.QRectF(rx, ry, rw, rh), 3, 3)

        def X(hz):
            return rx + max(0.0, min(1.0, hz / fmax)) * rw

        if self._tg_attached:                 # the TG44A's range, as a thin stripe
            tg = QtGui.QColor(COLORS["accent_dim"])
            p.setBrush(tg)
            p.drawRect(QtCore.QRectF(X(TG_RANGE_HZ[0]), ry + rh + 1,
                                     X(TG_RANGE_HZ[1]) - X(TG_RANGE_HZ[0]), 2))
        lo, hi = self._window
        if math.isfinite(lo) and math.isfinite(hi):
            x0, x1 = X(lo), X(hi)
            if x1 - x0 < 3:                   # a narrow span still shows as a notch
                c = (x0 + x1) / 2
                x0, x1 = c - 1.5, c + 1.5
            p.setBrush(QtGui.QColor(COLORS["accent"]))
            p.drawRoundedRect(QtCore.QRectF(x0, ry - 1, x1 - x0, rh + 2), 2, 2)
        if self._tg_attached and math.isfinite(self._tg_cw):
            # where the TG's CW sits: a small diamond on the ruler, pulsing
            cx, cy = X(self._tg_cw), ry + rh / 2
            r = 3.5 + 1.0 * math.sin(2 * math.pi * self._phase)
            p.setBrush(QtGui.QColor(COLORS["accent_hi"]))
            p.setPen(QtGui.QPen(QtGui.QColor(COLORS["bg"]), 1))
            p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(cx, cy - r), QtCore.QPointF(cx + r, cy),
                                           QtCore.QPointF(cx, cy + r), QtCore.QPointF(cx - r, cy)]))
            p.setPen(QtCore.Qt.NoPen)
        p.setPen(QtGui.QColor(COLORS["muted"]))
        p.drawText(QtCore.QRectF(rx, ry + rh + 3, 80, 11), QtCore.Qt.AlignLeft, "0")
        p.drawText(QtCore.QRectF(rx + rw - 80, ry + rh + 3, 80, 11), QtCore.Qt.AlignRight,
                   f"{fmax / 1e9:.1f} GHz")

        # ---- the screen ----
        sx, sy = m, ry + rh + 16
        sw, sh = w - 2 * m, h - sy - 16
        if sw < 40 or sh < 40:
            p.end()
            return
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["border"]), 1))
        p.setBrush(QtGui.QColor(COLORS["code_bg"]))
        p.drawRoundedRect(QtCore.QRectF(sx, sy, sw, sh), 4, 4)
        grid = QtGui.QPen(QtGui.QColor(COLORS["grid"]), 1)
        p.setPen(grid)
        for i in range(1, self.DIVS_X):
            x = sx + i * sw / self.DIVS_X
            p.drawLine(QtCore.QPointF(x, sy + 2), QtCore.QPointF(x, sy + sh - 2))
        for j in range(1, self.DIVS_Y):
            y = sy + j * sh / self.DIVS_Y
            p.drawLine(QtCore.QPointF(sx + 2, y), QtCore.QPointF(sx + sw - 2, y))

        bottom = self._ref - self.DIVS_Y * self.DB_PER_DIV

        def Y(db):
            return sy + sh * (self._ref - max(bottom, min(self._ref, db))) / (self._ref - bottom)

        pts = self._columns(sx + 2, sw - 4) if self._trace is not None else []
        if len(pts) >= 2:
            path = QtGui.QPainterPath(QtCore.QPointF(pts[0][0], Y(pts[0][1])))
            for x, v in pts[1:]:
                path.lineTo(QtCore.QPointF(x, Y(v)))
            glow = QtGui.QColor(COLORS["accent"])
            glow.setAlpha(55)
            p.setBrush(QtCore.Qt.NoBrush)
            p.setPen(QtGui.QPen(glow, 4))               # phosphor bloom under the line
            p.drawPath(path)
            p.setPen(QtGui.QPen(QtGui.QColor(COLORS["accent"]), 1.3))
            p.drawPath(path)

        # the sweep beam, with a fading trail behind it
        if self._sweeping:
            bx = sx + 2 + self._progress * (sw - 4)
            trail = QtGui.QLinearGradient(bx - 40, 0, bx, 0)
            c0 = QtGui.QColor(COLORS["accent_hi"]); c0.setAlpha(0)
            c1 = QtGui.QColor(COLORS["accent_hi"]); c1.setAlpha(60)
            trail.setColorAt(0, c0)
            trail.setColorAt(1, c1)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(trail)
            p.drawRect(QtCore.QRectF(max(sx + 2, bx - 40), sy + 2, min(40, bx - sx - 2), sh - 4))
            p.setPen(QtGui.QPen(QtGui.QColor(COLORS["accent_hi"]), 1.5))
            p.drawLine(QtCore.QPointF(bx, sy + 2), QtCore.QPointF(bx, sy + sh - 2))

        # overload badge
        if self._overload:
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QColor(COLORS["danger"]))
            p.drawRoundedRect(QtCore.QRectF(sx + sw - 38, sy + 5, 32, 13), 3, 3)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QtGui.QColor(COLORS["bg"]))
            p.drawText(QtCore.QRectF(sx + sw - 38, sy + 5, 32, 13), QtCore.Qt.AlignCenter, "OVL")
            f.setBold(False)
            p.setFont(f)

        p.setPen(QtGui.QColor(COLORS["muted"]))
        p.drawText(QtCore.QRectF(sx + 4, sy + 3, sw / 2, 11), QtCore.Qt.AlignLeft,
                   f"REF {self._ref:g} dBm")
        p.drawText(QtCore.QRectF(sx, sy + sh + 2, sw, 12), QtCore.Qt.AlignHCenter,
                   self._caption)
        p.end()


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self._kind = None                 # (simulated, model), learnt from status
        self.setWindowTitle("Spectrum analyser" + ("  (remote)" if remote else ""))
        self.resize(1240, 820)

        self._trace = None
        self._trace_id = -1
        self._last_fetch = 0.0
        self._last_acq = 0

        root = QtWidgets.QWidget(); root.setObjectName("root")
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)
        # Control or viewer (control_bar.py): a bar across the top, only for a
        # GUI on a service whose client knows about control -- a local GUI
        # owns its analyser and has nobody to share it with.
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

        try:
            self.ctrl.start()
        except Exception as exc:          # show it, don't crash
            self._on_event("error", f"start failed: {exc}")
        self._sync_inputs(force=True)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

        # The first GUI to connect gets control; a later one opens as a viewer
        # (control_bar.py). Only once the log exists, so the bar can say so.
        if self._control_bar is not None:
            self._control_bar.claim_if_free()

    # ---- layout ----------------------------------------------------------

    def _build_sidebar(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget(); panel.setFixedWidth(340)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("SPECTRUM")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; "
                            "letter-spacing:2px;")
        header.addWidget(title)
        self.kind_label = QtWidgets.QLabel(""); self.kind_label.setObjectName("hint")
        header.addWidget(self.kind_label); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        mark_always(settings_btn)    # a viewer may LOOK; the service refuses the OK
        header.addWidget(settings_btn)
        col.addLayout(header)

        ccard, clay = _card()
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        clay.addWidget(self.conn_dot)
        col.addWidget(ccard)

        # sweep
        scard, slay = _card("Sweep")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        lim = self.cfg.limits
        self.center_spin = self._dspin(lim.freq_min_Hz / 1e9, lim.freq_max_Hz / 1e9, 6, "  GHz", 0.01)
        self.span_spin = self._dspin(lim.min_span_Hz / 1e6, lim.freq_max_Hz / 1e6, 4, "  MHz", 1.0)
        self.ref_spin = self._dspin(lim.ref_min_dBm, lim.ref_max_dBm, 1, "  dBm", 1.0)
        self.rbw_spin = self._dspin(lim.rbw_min_Hz / 1e3, lim.rbw_max_Hz / 1e3, 4, "  kHz", 1.0)
        self.vbw_spin = self._dspin(lim.rbw_min_Hz / 1e3, lim.rbw_max_Hz / 1e3, 4, "  kHz", 1.0)
        self.avg_spin = QtWidgets.QSpinBox(); self.avg_spin.setRange(lim.averages_min, lim.averages_max)
        self.det_combo = QtWidgets.QComboBox(); self.det_combo.addItems(list(DETECTORS))
        # applies at once: it is a choice, not a number being typed
        self.det_combo.activated.connect(lambda _i: self._call(
            self.ctrl.set_detector, self.det_combo.currentText()))
        for label, wdg in (("Centre", self.center_spin), ("Span", self.span_spin),
                           ("Ref level", self.ref_spin), ("RBW", self.rbw_spin),
                           ("VBW", self.vbw_spin), ("Detector", self.det_combo),
                           ("Averages", self.avg_spin)):
            form.addRow(label, wdg)
        slay.addLayout(form)
        self.reject_chk = QtWidgets.QCheckBox("Image rejection")
        self.reject_chk.clicked.connect(lambda on: self._call(self.ctrl.set_reject, on))  # user only
        slay.addWidget(self.reject_chk)
        row = QtWidgets.QHBoxLayout()
        self.sweep_time_label = QtWidgets.QLabel("—"); self.sweep_time_label.setObjectName("hint")
        self.sweep_time_label.setWordWrap(True)
        row.addWidget(self.sweep_time_label, 1)
        apply = QtWidgets.QPushButton("Apply"); apply.setObjectName("primary")
        apply.clicked.connect(self._apply_sweep)
        row.addWidget(apply)
        slay.addLayout(row)
        col.addWidget(scard)
        # EDITED BUT NOT APPLIED. The status poll keeps the boxes following
        # changes made elsewhere (console, scan) -- but it used to skip only
        # the box with keyboard focus, so a value typed into Centre was put
        # back the moment you clicked into Span (Lukas, 2026-10-01: "whenever
        # I change any settings it comes back to the original ones"). A box
        # the user changed is now "dirty": the poll leaves it alone, it gets
        # an amber outline, and Apply (or Enter in it) sends it and clears it.
        self._dirty: set = set()
        self._syncing = False            # True while the POLL sets values
        self._sweep_spins = (self.center_spin, self.span_spin, self.ref_spin,
                             self.rbw_spin, self.vbw_spin, self.avg_spin)
        for spin in self._sweep_spins:
            spin.valueChanged.connect(lambda _v, s=spin: self._mark_dirty(s))
            spin.lineEdit().returnPressed.connect(self._apply_sweep)

        # acquisition
        acard, alay = _card("Acquire (scan-safe trace)")
        self.cont_chk = QtWidgets.QCheckBox("Continuous sweep")
        self.cont_chk.clicked.connect(lambda on: self._call(self.ctrl.set_continuous, on))
        alay.addWidget(self.cont_chk)
        row = QtWidgets.QHBoxLayout()
        self.acq_btn = QtWidgets.QPushButton("Acquire"); self.acq_btn.setObjectName("primary")
        self.acq_btn.setMinimumHeight(34)
        self.acq_btn.clicked.connect(lambda: self._call(self.ctrl.acquire))
        self.abort_btn = QtWidgets.QPushButton("Abort")
        self.abort_btn.clicked.connect(lambda: self._call(self.ctrl.abort))
        mark_always(self.abort_btn)  # the SAFETY verb: works for a viewer too
        row.addWidget(self.acq_btn, 1); row.addWidget(self.abort_btn)
        alay.addLayout(row)
        self.acq_bar = QtWidgets.QProgressBar(); self.acq_bar.setRange(0, 100)
        self.acq_bar.setTextVisible(False); self.acq_bar.setFixedHeight(6)
        alay.addWidget(self.acq_bar)
        self.sample_label = QtWidgets.QLabel("no acquisition yet"); self.sample_label.setObjectName("hint")
        self.sample_label.setWordWrap(True)
        alay.addWidget(self.sample_label)
        col.addWidget(acard)

        # tracking generator: SHOWN, not driven -- shsg (CW) and shsna (TG
        # sweeps) drive it through this service (Lukas, 2026-09-28)
        gcard, glay = _card("Tracking generator")
        self.tg_label = QtWidgets.QLabel("--"); self.tg_label.setObjectName("hint")
        self.tg_label.setWordWrap(True)
        self.tg_label.setToolTip("Driven by the shsg (signal generator) and shsna (scalar "
                                 "network analyser) modules; this panel only shows it.")
        glay.addWidget(self.tg_label)
        col.addWidget(gcard)

        col.addStretch(1)
        return panel

    @staticmethod
    def _dspin(lo, hi, decimals, suffix, step):
        s = QtWidgets.QDoubleSpinBox()
        s.setDecimals(decimals); s.setRange(lo, hi); s.setSuffix(suffix); s.setSingleStep(step)
        return s

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        rcard, rlay = _card("Marker")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(24)
        self.big, self.big_cap = {}, {}
        for key, label, unit in (("peak", "PEAK", "GHz"), ("level", "LEVEL", "dBm"),
                                 ("floor", "FLOOR", "dBm")):
            box = QtWidgets.QVBoxLayout(); box.setSpacing(0)
            box.addStretch(1)                 # keep caption and number together, centred
            cap = QtWidgets.QLabel(label); cap.setObjectName("hint")
            box.addWidget(cap)
            line = QtWidgets.QHBoxLayout(); line.setSpacing(6)
            v = QtWidgets.QLabel("—"); v.setObjectName("bigValue")
            v.setMinimumWidth(150); v.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            u = QtWidgets.QLabel(unit); u.setObjectName("unit")
            line.addWidget(v); line.addWidget(u, 0, QtCore.Qt.AlignBottom)
            box.addLayout(line)
            box.addStretch(1)
            self.big[key], self.big_cap[key] = v, cap
            row.addLayout(box)
        row.addStretch(1)
        self.indicator = HoundScope(); self.indicator.setFixedWidth(330)
        row.addWidget(self.indicator)
        rlay.addLayout(row)
        colw.addWidget(rcard)

        tcard, tlay = _card("Trace")
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Show"))
        self.which_combo = QtWidgets.QComboBox()
        self.which_combo.addItems(["Latest sweep", "Last acquisition"])
        self.which_combo.currentIndexChanged.connect(lambda _i: self._force_fetch())
        bar.addWidget(self.which_combo)
        bar.addStretch(1)
        self.trace_label = QtWidgets.QLabel(""); self.trace_label.setObjectName("hint")
        bar.addWidget(self.trace_label)
        tlay.addLayout(bar)
        import pyqtgraph as pg
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget(background=COLORS["code_bg"])
        self.plot.setMinimumHeight(200)
        pen = pg.mkPen(COLORS["muted"])
        for axis in ("left", "bottom"):
            ax = self.plot.getAxis(axis); ax.setPen(pen); ax.setTextPen(pen)
            ax.enableAutoSIPrefix(False)
        self.plot.setLabel("bottom", "frequency", units="GHz")
        self.plot.setLabel("left", "power", units="dBm")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.curve = self.plot.plot([], [], pen=pg.mkPen(COLORS["accent"], width=1.4))
        self.peak_line = pg.InfiniteLine(angle=90, movable=False,
                                         pen=pg.mkPen(COLORS["accent_hi"], width=1,
                                                      style=QtCore.Qt.DashLine))
        self.ref_line = pg.InfiniteLine(angle=0, movable=False,
                                        pen=pg.mkPen(COLORS["muted"], width=1,
                                                     style=QtCore.Qt.DashLine))
        self.plot.addItem(self.peak_line); self.plot.addItem(self.ref_line)
        tlay.addWidget(self.plot, 1)
        colw.addWidget(tcard, 1)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMaximumHeight(120)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 0)
        return panel

    # ---- actions ---------------------------------------------------------

    def _call(self, fn, *args):
        try:
            r = fn(*args)
            if isinstance(r, dict) and r.get("ok") is False:
                self._on_event("warn", r.get("error", "refused"))
        except Exception as exc:
            self._on_event("warn", f"refused: {exc}")

    def _apply_sweep(self):
        """Send only what changed: every change restarts an acquisition and
        logs an event, and six of them for one click would bury the log.
        RBW goes before VBW, because VBW may not exceed it."""
        s = self.ctrl.status()
        for spin, now, scale, setter in (
                (self.center_spin, s.center_Hz, 1e9, self.ctrl.set_center),
                (self.span_spin, s.span_Hz, 1e6, self.ctrl.set_span),
                (self.ref_spin, s.ref_level_dBm, 1, self.ctrl.set_ref_level),
                (self.rbw_spin, s.rbw_Hz, 1e3, self.ctrl.set_rbw),
                (self.vbw_spin, s.vbw_Hz, 1e3, self.ctrl.set_vbw),
                (self.avg_spin, s.averages, 1, self.ctrl.set_averages)):
            want = spin.value() * scale
            if not (isinstance(now, (int, float)) and math.isclose(want, now, rel_tol=1e-9,
                                                                     abs_tol=1e-9)):
                self._call(setter, want)
        # a new span may have been refused until the new centre was in; send it again
        if not math.isclose(self.span_spin.value() * 1e6, self.ctrl.status().span_Hz, abs_tol=1.0):
            self._call(self.ctrl.set_span, self.span_spin.value() * 1e6)
        # sent: the boxes follow the service again (a refused value is put back
        # by the next poll, and the log says why)
        for spin in list(self._dirty):
            self._clear_dirty(spin)

    def _mark_dirty(self, spin):
        if self._syncing or spin in self._dirty:
            return                       # the poll set it, or already marked
        self._dirty.add(spin)
        spin.setStyleSheet(f"border: 1px solid {COLORS['accent']};")
        spin.setToolTip("changed here, not sent yet -- press Apply (or Enter)")

    def _clear_dirty(self, spin):
        self._dirty.discard(spin)
        spin.setStyleSheet("")
        spin.setToolTip("")

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        SettingsDialog(self.ctrl, self.cfg, lambda: self._sync_inputs(force=True), self).exec()

    # ---- refresh & events ------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = COLORS["danger"] if level == "error" else (
            COLORS["accent"] if level == "warn" else COLORS["muted"])
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(
            f'<span style="color:{COLORS["accent_dim"]}">{stamp}</span> '
            f'<span style="color:{color}">{msg}</span>')

    def _sync_inputs(self, force: bool = False):
        """Input boxes follow settings changed ELSEWHERE (console, scan), but
        never while the user is typing in them, and never a box the user
        changed and has not applied yet (``_dirty``). ``force`` (after the
        Settings dialog) puts every box back to the service's value."""
        s = self.ctrl.status()
        if force:
            for spin in list(self._dirty):
                self._clear_dirty(spin)
        self._syncing = True             # these setValue calls are not user edits
        try:
            for spin, val in ((self.center_spin, s.center_Hz / 1e9),
                              (self.span_spin, s.span_Hz / 1e6),
                              (self.ref_spin, s.ref_level_dBm), (self.rbw_spin, s.rbw_Hz / 1e3),
                              (self.vbw_spin, s.vbw_Hz / 1e3)):
                if math.isfinite(val) and (force or (not spin.hasFocus()
                                                     and spin not in self._dirty)):
                    spin.setValue(val)
            if force or (not self.avg_spin.hasFocus() and self.avg_spin not in self._dirty):
                self.avg_spin.setValue(int(s.averages))
        finally:
            self._syncing = False
        if force or not self.det_combo.view().isVisible():
            i = self.det_combo.findText(s.detector)
            if i >= 0:
                self.det_combo.setCurrentIndex(i)
        # programmatic setChecked with signals blocked: no echo back (gotcha #13)
        for chk, val in ((self.cont_chk, s.continuous), (self.reject_chk, s.reject)):
            chk.blockSignals(True)
            chk.setChecked(bool(val))
            chk.blockSignals(False)

    def _force_fetch(self):
        self._trace_id = -1
        self._last_fetch = 0.0

    def _set_kind(self, simulated: bool, model: str):
        """Things that depend on WHICH analyser this is, set once it is known."""
        if (simulated, model) == self._kind:
            return
        self._kind = (simulated, model)
        name = f"Signal Hound {model}" if model else "Signal Hound"
        self.kind_label.setText(f"SIMULATED {model}" if simulated else model)
        title = (f"Spectrum analyser - simulated {model}" if simulated
                 else f"Spectrum analyser - {name}")
        self.setWindowTitle(title + ("  (remote)" if self._remote else ""))

    def _refresh(self):
        if self._control_bar is not None:
            self._control_bar.refresh()
        s = self.ctrl.status()
        now = time.monotonic()
        self._set_kind(bool(getattr(s, "simulated", True)), getattr(s, "device_model", ""))

        # connection
        if s.hw_error:
            self.conn_dot.setText("●  error"); self.conn_dot.setToolTip(s.hw_error)
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        elif s.connected:
            self.conn_dot.setText("●  sweeping" if s.sweeping else "●  idle")
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
            self.conn_dot.setToolTip(s.idn)
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")

        paused = getattr(s, "spectrum_paused", "")
        if paused:
            self.sweep_time_label.setText("paused: " + paused)
        elif getattr(s, "configured", True):
            self.sweep_time_label.setText(
                f"{s.points} bins of {_hz(s.bin_Hz)}, sweep {_fmt(s.sweep_time_s, '.3g')} s")
        else:
            # start-up rule: nothing has been sent to the analyser yet
            self.sweep_time_label.setText("not configured yet: Continuous or Acquire sweeps")
        self.tg_label.setText(tg_line(s))
        self._sync_inputs()

        # acquisition
        self.acq_bar.setValue(int(100 * s.acq_progress) if s.acquiring else 0)
        self.acq_btn.setEnabled(bool(s.connected) and not s.acquiring)
        self.abort_btn.setEnabled(bool(s.acquiring))
        smp = s.sample
        if smp and smp.get("acq_id") != self._last_acq:
            self._last_acq = smp.get("acq_id")
            if smp.get("aborted"):
                self.sample_label.setText(f"#{smp.get('acq_id')}: acquisition aborted")
            else:
                self.sample_label.setText(
                    f"#{smp.get('acq_id')}: peak {_hz(smp.get('peak_Hz', math.nan))} at "
                    f"{_fmt(smp.get('peak_dBm'), '.2f')} dBm, floor {_fmt(smp.get('floor_dBm'), '.1f')} "
                    f"dBm, {smp.get('averages')} avg"
                    + ("  OVERLOAD" if smp.get("overload") else ""))
            if self.which_combo.currentIndex() == 1:
                self._force_fetch()

        # trace: fetch only when there is a new one, and not more than ~7 per second
        which = "last" if self.which_combo.currentIndex() == 0 else "sample"
        new = s.trace_id != self._trace_id if which == "last" else self._trace_id == -1
        if new and now - self._last_fetch > 0.14:
            self._last_fetch = now
            try:
                self._trace = self._fetch(which)
                self._trace_id = s.trace_id
                self._redraw()
            except Exception:
                pass                     # nothing measured yet

        tg_mode = getattr(s, "tg_mode", "unknown")
        cw = s.tg_cw_freq_hz if tg_mode == "cw" else math.nan
        mode = {"cw": "SA + TG CW", "sweep": "TG SWEEP (SNA)"}.get(tg_mode, "SA")
        self.indicator.set_state(
            getattr(s, "device_model", ""), s.tg_attached, s.start_Hz, s.stop_Hz,
            s.ref_level_dBm, s.sweep_progress, s.sweeping, cw, s.overload,
            f"{mode}  10 dB/div  RBW {_hz(s.rbw_Hz)}")

    def _fetch(self, which: str) -> dict:
        raw = self.ctrl.get_trace(which, "trace")
        self.indicator.set_trace(raw["freqs_Hz"], raw["trace"])
        return raw

    def _redraw(self):
        t = self._trace
        if t is None:
            return
        fx = t["freqs_Hz"] / 1e9
        label = (f"{t['points']} bins, RBW {_hz(t['rbw_Hz'])}, VBW {_hz(t['vbw_Hz'])}, "
                 f"{t['detector']}")
        self.curve.setData(fx, t["trace"])
        self.ref_line.setPos(t["ref_level_dBm"])
        self.trace_label.setText(label)
        self.trace_label.setToolTip(label)
        pk = t.get("peak_Hz", math.nan)
        self.peak_line.setVisible(math.isfinite(pk))
        if math.isfinite(pk):
            self.peak_line.setPos(pk / 1e9)
            self.big["peak"].setText(f"{pk / 1e9:.6f}")
            self.big["level"].setText(_fmt(t.get("peak_dBm"), ".2f"))
            self.big["floor"].setText(_fmt(t.get("floor_dBm"), ".1f"))

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a brain-like object. The theme is chosen ONCE
    here, from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))
    # '.' decimal point and no thousands separator whatever the Windows locale
    # (suite gotcha #18).
    loc = QtCore.QLocale.c()
    loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
    QtCore.QLocale.setDefault(loc)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
    from .theme import apply_window_icon
    apply_window_icon(app)
    app.setStyle("Fusion")
    apply_palette(app)
    app.setStyleSheet(build_stylesheet())
    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    return app.exec()


def main(theme: str | None = None) -> int:
    """Run against the built-in simulator, in-process."""
    from ..config import Config
    from ..sim_system import build_sim_system
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    # In-process SIMULATOR: no instrument to disturb, so sweep at once (a real
    # analyser is left untouched at start -- acquisition.sweep_on_start).
    cfg.acquisition.sweep_on_start = True
    signalhound, _ = build_sim_system(cfg)
    return run_app(signalhound, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
