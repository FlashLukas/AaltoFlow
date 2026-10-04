"""Control GUI for the spectrum analyser -- the GW Instek GSP-818, or the simulator.

    uv run scripts/run_gui.py                  # local simulator
    uv run scripts/run_gui.py --real           # the GSP-818, in this process
    uv run scripts/run_gui.py --connect HOST   # a running service (either kind)

The window holds a brain-like object (an in-process SpectrumAnalyzer or a
Gsp818Client facade) and never cares which. A 60 ms timer reads status(); a
new trace is fetched only when `trace_id` moved. Events cross into the GUI
thread on a Qt signal.

Signature widget: SweepScope -- a miniature of the analyser's screen: the
graticule, the latest trace as a glowing silhouette, the sweep beam running
across it at the real sweep rate, the RBW filter drawn to scale as a bell at
the peak, and two lamps: TG (radiating while the tracking generator drives
GEN OUTPUT) and OVL (the mixer is overloaded).

The trace view has two modes, both against the BRAIN's reference (so the GUI,
the console and a scan all mean the same reference):
  Spectrum (dBm)               the trace as measured
  Normalised (trace - thru)    scalar network analysis: the DUT's |S21| in dB
"""

from __future__ import annotations

import math
import time

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from ..model import DETECTORS, DUTS
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog
from .control_bar import ALWAYS_PROPERTY, ControlBar, mark_always

VIEWS = ("Spectrum (dBm)", "Normalised: trace - thru reference (dB)")


class Bridge(QtCore.QObject):
    """Carries analyser events across the thread boundary into the GUI."""
    event = QtCore.Signal(str, str)


def _card(title: str | None = None):
    frame = QtWidgets.QFrame()
    frame.setObjectName("card")
    lay = QtWidgets.QVBoxLayout(frame)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(6)
    if title:
        lbl = QtWidgets.QLabel(title.upper())
        lbl.setObjectName("cardTitle")
        lay.addWidget(lbl)
    return frame, lay


def _fmt(v, fmt: str, none: str = "--") -> str:
    return format(v, fmt) if isinstance(v, (int, float)) and math.isfinite(v) else none


def _fmt_Hz(hz) -> str:
    if not (isinstance(hz, (int, float)) and math.isfinite(hz)):
        return "--"
    for unit, scale in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if abs(hz) >= scale:
            return f"{hz / scale:.4g} {unit}"
    return f"{hz:.4g} Hz"


def _age(seconds) -> str:
    if not (isinstance(seconds, (int, float)) and math.isfinite(seconds)):
        return "--"
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


# ------------------------------------------------------------- the indicator

class SweepScope(QtWidgets.QWidget):
    """A miniature analyser screen: trace silhouette, sweep beam, RBW bell,
    TG and OVL lamps. Colours are read from COLORS at paint time (theme)."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(280, 170)
        self._y = None                   # trace, dBm, downsampled
        self._ref = 0.0                  # reference level: top of the screen
        self._rbw_frac = 0.0             # RBW / span
        self._peak_frac = math.nan       # where the peak is, 0..1 across
        self._progress = 0.0
        self._sweeping = False
        self._tg = False
        self._overload = False
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_trace(self, y_dBm):
        y = np.asarray(y_dBm, dtype=float)
        if y.size > 200:                 # max per column keeps carriers visible
            n = y.size // 200
            y = y[: n * 200].reshape(200, n).max(axis=1)
        self._y = y

    def set_state(self, ref_dBm, rbw_Hz, span_Hz, peak_frac, progress, sweeping, tg, overload):
        self._ref = ref_dBm if math.isfinite(ref_dBm) else 0.0
        self._rbw_frac = (rbw_Hz / span_Hz) if (math.isfinite(rbw_Hz) and span_Hz > 0) else 0.0
        self._peak_frac = peak_frac
        self._progress = progress if math.isfinite(progress) else 0.0
        self._sweeping, self._tg, self._overload = bool(sweeping), bool(tg), bool(overload)

    def _tick(self):
        if not self.isVisible():
            return
        self._phase = (self._phase + 0.035) % 1.0
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        lamp_h = 22
        x0, y0, pw, ph = 6.0, 6.0, w - 12.0, h - 12.0 - lamp_h
        if pw < 40 or ph < 30:
            p.end()
            return
        screen = QtCore.QRectF(x0, y0, pw, ph)

        # the screen and its 10 x 8 graticule
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["border"]), 1.2))
        p.setBrush(QtGui.QColor(COLORS["code_bg"]))
        p.drawRoundedRect(screen, 5, 5)
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["grid"]), 1))
        for i in range(1, 10):
            x = x0 + pw * i / 10
            p.drawLine(QtCore.QPointF(x, y0 + 2), QtCore.QPointF(x, y0 + ph - 2))
        for j in range(1, 8):
            y = y0 + ph * j / 8
            p.drawLine(QtCore.QPointF(x0 + 2, y), QtCore.QPointF(x0 + pw - 2, y))

        def Y(dbm):                      # 10 dB/div: the screen spans ref .. ref-80 dB
            f = (self._ref - dbm) / 80.0
            return y0 + ph * min(max(f, 0.0), 1.0)

        # the trace silhouette: a filled glow under an amber line
        y = self._y
        if y is not None and y.size > 1:
            n = y.size
            line = QtGui.QPainterPath()
            for i, v in enumerate(y):
                pt = QtCore.QPointF(x0 + pw * i / (n - 1), Y(v if math.isfinite(v) else -1e9))
                line.lineTo(pt) if i else line.moveTo(pt)
            fill = QtGui.QPainterPath(line)
            fill.lineTo(x0 + pw, y0 + ph)
            fill.lineTo(x0, y0 + ph)
            fill.closeSubpath()
            grad = QtGui.QLinearGradient(0, y0, 0, y0 + ph)
            g0 = QtGui.QColor(COLORS["accent"]); g0.setAlpha(90)
            g1 = QtGui.QColor(COLORS["accent"]); g1.setAlpha(8)
            grad.setColorAt(0, g0); grad.setColorAt(1, g1)
            p.setPen(QtCore.Qt.NoPen); p.setBrush(grad)
            p.drawPath(fill)
            p.setPen(QtGui.QPen(QtGui.QColor(COLORS["accent"]), 1.4))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawPath(line)

        # the RBW filter to scale, as a bell at the peak (at least a few px wide,
        # so a 10 Hz RBW on a 1 GHz span is still visible as "very narrow")
        if math.isfinite(self._peak_frac) and self._rbw_frac > 0:
            cx = x0 + pw * min(max(self._peak_frac, 0.0), 1.0)
            half = max(3.0, pw * self._rbw_frac * 1.5)
            bell = QtGui.QPainterPath()
            base = y0 + ph - 2
            for i in range(41):
                t = -1 + 2 * i / 40
                yy = base - (ph * 0.28) * math.exp(-4 * math.log(2) * (1.5 * t) ** 2)
                pt = QtCore.QPointF(cx + t * half, yy)
                bell.lineTo(pt) if i else bell.moveTo(pt)
            p.setPen(QtGui.QPen(QtGui.QColor(COLORS["accent_hi"]), 1.2, QtCore.Qt.DashLine))
            p.drawPath(bell)

        # the sweep beam
        if self._sweeping:
            bx = x0 + pw * min(max(self._progress, 0.0), 1.0)
            beam = QtGui.QLinearGradient(bx - 16, 0, bx, 0)
            b0 = QtGui.QColor(COLORS["accent_hi"]); b0.setAlpha(0)
            b1 = QtGui.QColor(COLORS["accent_hi"]); b1.setAlpha(110)
            beam.setColorAt(0, b0); beam.setColorAt(1, b1)
            p.setPen(QtCore.Qt.NoPen); p.setBrush(beam)
            p.drawRect(QtCore.QRectF(max(x0, bx - 16), y0 + 1, min(16, bx - x0), ph - 2))
            p.setPen(QtGui.QPen(QtGui.QColor(COLORS["accent_hi"]), 1.5))
            p.drawLine(QtCore.QPointF(bx, y0 + 1), QtCore.QPointF(bx, y0 + ph - 1))

        # the lamps under the screen
        f = p.font(); f.setPointSize(7); f.setBold(True); p.setFont(f)
        ly = y0 + ph + 4 + lamp_h / 2 - 2
        self._lamp(p, x0 + 10, ly, "TG", self._tg, COLORS["accent"], radiate=True)
        self._lamp(p, x0 + 70, ly, "OVL", self._overload, COLORS["danger"])
        p.setPen(QtGui.QColor(COLORS["muted"]))
        p.drawText(QtCore.QRectF(x0 + 120, ly - 7, pw - 120, 14),
                   QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                   f"REF {self._ref:.0f} dBm  10 dB/div")
        p.end()

    def _lamp(self, p, x, y, text, on, color, radiate=False):
        col = QtGui.QColor(color if on else COLORS["border"])
        if on:
            glow = QtGui.QRadialGradient(x, y, 11)
            g0 = QtGui.QColor(col); g0.setAlpha(150)
            g1 = QtGui.QColor(col); g1.setAlpha(0)
            glow.setColorAt(0, g0); glow.setColorAt(1, g1)
            p.setPen(QtCore.Qt.NoPen); p.setBrush(glow)
            p.drawEllipse(QtCore.QPointF(x, y), 11, 11)
            if radiate:                  # arcs leaving the lamp: RF going out
                for k in range(2):
                    r = 6 + 10 * ((self._phase + k * 0.5) % 1.0)
                    a = QtGui.QColor(col); a.setAlpha(int(200 * (1 - (r - 6) / 10)))
                    p.setPen(QtGui.QPen(a, 1.3)); p.setBrush(QtCore.Qt.NoBrush)
                    p.drawArc(QtCore.QRectF(x - r, y - r, 2 * r, 2 * r), -45 * 16, 90 * 16)
        p.setPen(QtCore.Qt.NoPen); p.setBrush(col)
        p.drawEllipse(QtCore.QPointF(x, y), 4.5, 4.5)
        p.setPen(QtGui.QColor(COLORS["text"] if on else COLORS["muted"]))
        p.drawText(QtCore.QRectF(x + 9, y - 7, 40, 14),
                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, text)


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self._simulated = None           # learnt from the first status
        self.setWindowTitle("Spectrum analyser" + ("  (remote)" if remote else ""))
        self.resize(1280, 860)

        self._trace = None
        self._trace_id = -1
        self._last_fetch = 0.0
        self._last_acq = 0
        self._ref_id = None              # reference acq_id the trace was fetched against
        self._norm_error = ""            # why norm could not be shown, if it could not
        self._plot_mode = None

        root = QtWidgets.QWidget(); root.setObjectName("root")
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(16)
        # the sidebar scrolls: it holds every front-panel knob, more than a
        # laptop screen is tall
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self._build_sidebar()); scroll.setFixedWidth(372)
        scroll.widget().setObjectName("root")
        outer.addWidget(scroll, 0)
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
        panel = QtWidgets.QWidget(); panel.setFixedWidth(352)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(10)
        lim = self.cfg.limits

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("SPECTRUM")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
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

        # frequency
        fcard, flay = _card("Frequency")
        form = QtWidgets.QFormLayout(); form.setSpacing(5)
        fmin, fmax = lim.freq_min_Hz / 1e6, lim.freq_max_Hz / 1e6
        self.start_spin = self._dspin(fmin, fmax, 3, " MHz", 10.0)
        self.stop_spin = self._dspin(fmin, fmax, 3, " MHz", 10.0)
        self.center_spin = self._dspin(fmin, fmax, 3, " MHz", 10.0)
        # 3 decimals (1 kHz) to fit the card; a 100 Hz span is set from the console
        self.span_spin = self._dspin(max(lim.min_span_Hz / 1e6, 0.001), fmax, 3, " MHz", 10.0)
        self.points_spin = QtWidgets.QSpinBox(); self.points_spin.setRange(lim.points_min, lim.points_max)
        # two ways to say the same range; each pair has its own Set button below
        grid = QtWidgets.QGridLayout(); grid.setHorizontalSpacing(6); grid.setVerticalSpacing(5)
        for r, (l1, w1, l2, w2) in enumerate((("Start", self.start_spin, "Stop", self.stop_spin),
                                             ("Center", self.center_spin, "Span", self.span_spin))):
            grid.addWidget(QtWidgets.QLabel(l1), r, 0); grid.addWidget(w1, r, 1)
            grid.addWidget(QtWidgets.QLabel(l2), r, 2); grid.addWidget(w2, r, 3)
        grid.setColumnStretch(1, 1); grid.setColumnStretch(3, 1)
        flay.addLayout(grid)
        form.addRow("Points", self.points_spin)
        flay.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Set start/stop")
        b.clicked.connect(self._apply_start_stop)
        row.addWidget(b)
        b = QtWidgets.QPushButton("Set center/span"); b.setObjectName("primary")
        b.clicked.connect(self._apply_center_span)
        row.addWidget(b)
        flay.addLayout(row)
        col.addWidget(fcard)

        # bandwidth / amplitude / sweep
        bcard, blay = _card("Bandwidth · amplitude · sweep")
        grid = QtWidgets.QGridLayout(); grid.setHorizontalSpacing(6); grid.setVerticalSpacing(5)
        self.rbw_spin = self._dspin(lim.rbw_min_Hz / 1e3, lim.rbw_max_Hz / 1e3, 3, "  kHz", 1.0)
        self.vbw_spin = self._dspin(lim.vbw_min_Hz / 1e3, lim.vbw_max_Hz / 1e3, 3, "  kHz", 1.0)
        self.ref_spin = self._dspin(lim.ref_level_min_dBm, lim.ref_level_max_dBm, 1, "  dBm", 5.0)
        self.att_spin = self._dspin(0, lim.atten_max_dB, 0, "  dB", 1.0)
        self.swt_spin = self._dspin(lim.sweep_time_min_s, lim.sweep_time_max_s, 3, "  s", 0.1)
        self.avg_spin = QtWidgets.QSpinBox(); self.avg_spin.setRange(lim.averages_min, lim.averages_max)
        self.auto_chk = {}
        rows = (("RBW", self.rbw_spin, "rbw"), ("VBW", self.vbw_spin, "vbw"),
                ("Ref level", self.ref_spin, None), ("Atten", self.att_spin, "atten"),
                ("Sweep time", self.swt_spin, "sweep_time"), ("Averages", self.avg_spin, None))
        for r, (label, w, auto) in enumerate(rows):
            grid.addWidget(QtWidgets.QLabel(label), r, 0)
            grid.addWidget(w, r, 1)
            if auto:
                chk = QtWidgets.QCheckBox("auto")
                # .clicked = the USER only; a status refresh uses setChecked
                # under blockSignals and cannot echo back (gotcha #13)
                chk.clicked.connect(lambda on, a=auto: self._set_auto(a, on))
                grid.addWidget(chk, r, 2)
                self.auto_chk[auto] = chk
        r = len(rows)
        grid.addWidget(QtWidgets.QLabel("Detector"), r, 0)
        self.det_combo = QtWidgets.QComboBox(); self.det_combo.addItems(list(DETECTORS))
        self.det_combo.activated.connect(lambda _i: self._call(
            self.ctrl.set_detector, self.det_combo.currentText()))
        grid.addWidget(self.det_combo, r, 1)
        self.preamp_chk = QtWidgets.QCheckBox("preamp")
        self.preamp_chk.clicked.connect(lambda on: self._call(self.ctrl.set_preamp, on))
        grid.addWidget(self.preamp_chk, r, 2)
        grid.setColumnStretch(1, 1)
        blay.addLayout(grid)
        row = QtWidgets.QHBoxLayout()
        self.eff_label = QtWidgets.QLabel("—"); self.eff_label.setObjectName("hint")
        self.eff_label.setWordWrap(True)
        row.addWidget(self.eff_label, 1)
        apply = QtWidgets.QPushButton("Apply"); apply.setObjectName("primary")
        apply.clicked.connect(self._apply_bandwidth)
        row.addWidget(apply)
        blay.addLayout(row)
        col.addWidget(bcard)

        # tracking generator
        tcard, tlay = _card("Tracking generator")
        row = QtWidgets.QHBoxLayout()
        self.tg_btn = QtWidgets.QPushButton("TG OFF"); self.tg_btn.setCheckable(True)
        self.tg_btn.setToolTip("RF out of GEN OUTPUT, following the sweep. Left as the instrument has it at start; "
                                  "off when the service stops.")
        # Switching OFF goes through the SAFETY verb tg_off (net/service.py),
        # which a viewer may send too; switching ON is an ordinary change.
        self.tg_btn.clicked.connect(
            lambda on: self._call(self.ctrl.set_tg, True) if on else self._call(self.ctrl.tg_off))
        self.tg_level_spin = self._dspin(lim.tg_level_min_dBm, lim.tg_level_max_dBm, 1, "  dBm", 1.0)
        b = QtWidgets.QPushButton("Set level")
        b.clicked.connect(lambda: self._call(self.ctrl.set_tg_level, self.tg_level_spin.value()))
        row.addWidget(self.tg_btn, 1); row.addWidget(self.tg_level_spin, 1); row.addWidget(b)
        tlay.addLayout(row)
        self.dut_row = QtWidgets.QWidget()
        drow = QtWidgets.QHBoxLayout(self.dut_row); drow.setContentsMargins(0, 0, 0, 0)
        drow.addWidget(QtWidgets.QLabel("Simulated DUT"))
        self.dut_combo = QtWidgets.QComboBox(); self.dut_combo.addItems(list(DUTS))
        self.dut_combo.activated.connect(lambda _i: self._call(
            self.ctrl.set_dut, self.dut_combo.currentText()))
        drow.addWidget(self.dut_combo, 1)
        tlay.addWidget(self.dut_row)
        col.addWidget(tcard)

        # acquisition
        acard, alay = _card("Acquire (scan-safe trace)")
        self.cont_chk = QtWidgets.QCheckBox("Continuous sweep")
        self.cont_chk.clicked.connect(lambda on: self._call(self.ctrl.set_continuous, on))
        alay.addWidget(self.cont_chk)
        row = QtWidgets.QHBoxLayout()
        self.acq_btn = QtWidgets.QPushButton("Acquire"); self.acq_btn.setObjectName("primary")
        self.acq_btn.setMinimumHeight(32)
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

        # reference
        rcard, rlay = _card("Thru reference")
        row = QtWidgets.QHBoxLayout()
        self.ref_btn = QtWidgets.QPushButton("Take reference")
        self.ref_btn.setToolTip("Tracking generator on, a THRU where the device goes: acquire "
                                "(with the same averaging) and keep it as the reference.")
        self.ref_btn.clicked.connect(lambda: self._call(self.ctrl.take_reference))
        self.clear_ref_btn = QtWidgets.QPushButton("Clear")
        self.clear_ref_btn.clicked.connect(lambda: self._call(self.ctrl.clear_reference))
        row.addWidget(self.ref_btn, 1); row.addWidget(self.clear_ref_btn)
        rlay.addLayout(row)
        self.ref_label = QtWidgets.QLabel("none"); self.ref_label.setObjectName("hint")
        self.ref_label.setWordWrap(True)
        rlay.addWidget(self.ref_label)
        col.addWidget(rcard)
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

        mcard, mlay = _card("Marker")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(24)
        self.big, self.big_cap, self.big_unit = {}, {}, {}
        for key, label, unit in (("freq", "PEAK", "MHz"), ("level", "LEVEL", "dBm"),
                                 ("floor", "NOISE FLOOR", "dBm")):
            box = QtWidgets.QVBoxLayout(); box.setSpacing(0)
            box.addStretch(1)
            cap = QtWidgets.QLabel(label); cap.setObjectName("hint")
            box.addWidget(cap)
            line = QtWidgets.QHBoxLayout(); line.setSpacing(6)
            v = QtWidgets.QLabel("—"); v.setObjectName("bigValue")
            v.setMinimumWidth(140); v.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            u = QtWidgets.QLabel(unit); u.setObjectName("unit")
            line.addWidget(v); line.addWidget(u, 0, QtCore.Qt.AlignBottom)
            box.addLayout(line)
            box.addStretch(1)
            self.big[key], self.big_cap[key], self.big_unit[key] = v, cap, u
            row.addLayout(box)
        row.addStretch(1)
        self.indicator = SweepScope(); self.indicator.setFixedWidth(320)
        row.addWidget(self.indicator)
        mlay.addLayout(row)
        colw.addWidget(mcard)

        tcard, tlay = _card("Trace")
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Show"))
        self.view_combo = QtWidgets.QComboBox()
        self.view_combo.addItems(list(VIEWS))
        self.view_combo.setToolTip("Normalised uses the analyser's thru reference "
                                   "(THRU REFERENCE card): trace - reference, in dB")
        # norm is FETCHED from the brain (it owns the reference): new view = new fetch
        self.view_combo.currentIndexChanged.connect(lambda _i: self._force_fetch())
        bar.addWidget(self.view_combo)
        self.which_combo = QtWidgets.QComboBox()
        self.which_combo.addItems(["Latest sweep", "Last acquisition"])
        self.which_combo.currentIndexChanged.connect(lambda _i: self._force_fetch())
        bar.addWidget(self.which_combo)
        # which trace this window shows changes nothing on the analyser: fine
        # for a viewer
        mark_always(self.view_combo, self.which_combo)
        bar.addStretch(1)
        self.trace_label = QtWidgets.QLabel(""); self.trace_label.setObjectName("hint")
        bar.addWidget(self.trace_label)
        tlay.addLayout(bar)
        import pyqtgraph as pg
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget(background=COLORS["code_bg"])
        self.plot.setMinimumHeight(220)
        pen = pg.mkPen(COLORS["muted"])
        for axis in ("left", "bottom"):
            ax = self.plot.getAxis(axis); ax.setPen(pen); ax.setTextPen(pen)
            ax.enableAutoSIPrefix(False)
        self.plot.setLabel("bottom", "frequency", units="MHz")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.curve = self.plot.plot([], [], pen=pg.mkPen(COLORS["accent"], width=1.4))
        self.peak_dot = self.plot.plot([], [], pen=None, symbol="o", symbolSize=8,
                                       symbolBrush=COLORS["accent_hi"], symbolPen=None)
        self.ref_line = pg.InfiniteLine(angle=0, movable=False,
                                        pen=pg.mkPen(COLORS["muted"], width=1,
                                                     style=QtCore.Qt.DashLine))
        self.plot.addItem(self.ref_line)
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

    def _set_auto(self, which: str, on: bool):
        fn = {"rbw": self.ctrl.set_rbw_auto, "vbw": self.ctrl.set_vbw_auto,
              "atten": self.ctrl.set_atten_auto, "sweep_time": self.ctrl.set_sweep_time_auto}[which]
        self._call(fn, on)

    def _apply_start_stop(self):
        s = self.ctrl.status()
        start, stop = self.start_spin.value() * 1e6, self.stop_spin.value() * 1e6
        # order matters: each bounds the other, so move the edge that makes room first
        if start >= s.stop_Hz:
            self._call(self.ctrl.set_stop, stop); self._call(self.ctrl.set_start, start)
        else:
            self._call(self.ctrl.set_start, start); self._call(self.ctrl.set_stop, stop)
        self._apply_points()

    def _apply_center_span(self):
        self._call(self.ctrl.set_center, self.center_spin.value() * 1e6)
        self._call(self.ctrl.set_span, self.span_spin.value() * 1e6)
        self._apply_points()

    def _apply_points(self):
        if self.points_spin.value() != self.ctrl.status().points:
            self._call(self.ctrl.set_points, self.points_spin.value())

    def _apply_bandwidth(self):
        """Send only what changed: every change restarts an acquisition and logs
        an event, and six of them for one click would bury the log. A value is
        sent only where its auto box is off (typing a value = manual)."""
        s = self.ctrl.status()
        for spin, now, scale, setter, auto in (
                (self.rbw_spin, s.rbw_set_Hz, 1e3, self.ctrl.set_rbw, "rbw"),
                (self.vbw_spin, s.vbw_set_Hz, 1e3, self.ctrl.set_vbw, "vbw"),
                (self.ref_spin, s.ref_level_dBm, 1, self.ctrl.set_ref_level, None),
                (self.att_spin, s.atten_set_dB, 1, self.ctrl.set_atten, "atten"),
                (self.swt_spin, s.sweep_time_set_s, 1, self.ctrl.set_sweep_time, "sweep_time"),
                (self.avg_spin, s.averages, 1, self.ctrl.set_averages, None)):
            if auto and self.auto_chk[auto].isChecked():
                continue
            want = spin.value() * scale
            manual_now = not (auto and bool(getattr(s, f"{auto}_auto", False)))
            same = isinstance(now, (int, float)) and math.isclose(want, now, rel_tol=1e-9, abs_tol=1e-9)
            if not (same and manual_now):
                self._call(setter, want)

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
        never while the user is typing in them. A box whose auto is on shows
        the value IN USE, greyed out."""
        s = self.ctrl.status()
        auto = {k: bool(getattr(s, f"{k}_auto", False)) for k in self.auto_chk}
        for spin, val in (
                (self.start_spin, s.start_Hz / 1e6), (self.stop_spin, s.stop_Hz / 1e6),
                (self.center_spin, s.center_Hz / 1e6), (self.span_spin, s.span_Hz / 1e6),
                (self.rbw_spin, (s.rbw_Hz if auto["rbw"] else s.rbw_set_Hz) / 1e3),
                (self.vbw_spin, (s.vbw_Hz if auto["vbw"] else s.vbw_set_Hz) / 1e3),
                (self.ref_spin, s.ref_level_dBm),
                (self.att_spin, s.atten_dB if auto["atten"] else s.atten_set_dB),
                (self.swt_spin, s.sweep_time_s if auto["sweep_time"] else s.sweep_time_set_s),
                (self.tg_level_spin, s.tg_level_dBm)):
            if isinstance(val, (int, float)) and math.isfinite(val) and (force or not spin.hasFocus()):
                spin.setValue(val)
        for spin, val in ((self.points_spin, s.points), (self.avg_spin, s.averages)):
            if force or not spin.hasFocus():
                spin.setValue(int(val))
        for key, chk in self.auto_chk.items():
            chk.blockSignals(True); chk.setChecked(auto[key]); chk.blockSignals(False)
        self.rbw_spin.setEnabled(not auto["rbw"]); self.vbw_spin.setEnabled(not auto["vbw"])
        self.att_spin.setEnabled(not auto["atten"]); self.swt_spin.setEnabled(not auto["sweep_time"])
        for combo, text in ((self.det_combo, s.detector), (self.dut_combo, getattr(s, "dut", ""))):
            if force or not combo.view().isVisible():
                i = combo.findText(text)
                if i >= 0:
                    combo.setCurrentIndex(i)
        for chk, val in ((self.cont_chk, s.continuous), (self.preamp_chk, s.preamp)):
            chk.blockSignals(True); chk.setChecked(bool(val)); chk.blockSignals(False)
        self.tg_btn.blockSignals(True)
        self.tg_btn.setChecked(bool(s.tg_on))
        # While the TG is ON a click can only switch it OFF (tg_off, a safety
        # verb): a viewer may press it then. While it is off a click would
        # switch RF ON, so the viewer guard (control_bar.py) covers it.
        self.tg_btn.setProperty(ALWAYS_PROPERTY, bool(s.tg_on))
        self.tg_btn.setText("TG ON" if s.tg_on else "TG OFF")
        # objectName "danger" = red: RF is leaving the instrument
        name = "danger" if s.tg_on else ""
        if self.tg_btn.objectName() != name:
            self.tg_btn.setObjectName(name)
            self.tg_btn.style().unpolish(self.tg_btn); self.tg_btn.style().polish(self.tg_btn)
        self.tg_btn.blockSignals(False)

    def _force_fetch(self):
        self._trace_id = -1
        self._last_fetch = 0.0

    def _set_simulated(self, simulated: bool):
        """Things that depend on WHICH analyser this is, set once it is known."""
        if simulated == self._simulated:
            return
        self._simulated = simulated
        from ..backends import ANALYSER_NAME
        self.kind_label.setText("SIMULATED" if simulated else ANALYSER_NAME.split(" ", 2)[-1])
        self.setWindowTitle(("Spectrum analyser - simulated GSP-818" if simulated
                             else f"Spectrum analyser - {ANALYSER_NAME}")
                            + ("  (remote)" if self._remote else ""))
        # the pretend DUT exists only in the simulator
        self.dut_row.setVisible(simulated)

    def _refresh(self):
        if self._control_bar is not None:
            self._control_bar.refresh()
        s = self.ctrl.status()
        now = time.monotonic()
        self._set_simulated(bool(getattr(s, "simulated", True)))

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

        self.eff_label.setText(
            f"in use: RBW {_fmt_Hz(s.rbw_Hz)}, VBW {_fmt_Hz(s.vbw_Hz)}, att "
            f"{_fmt(s.atten_dB, '.0f')} dB, sweep {_fmt(s.sweep_time_s, '.3g')} s, "
            f"{s.detector_in_use or '--'} detector")
        self._sync_inputs()

        # the indicator
        peak_frac = ((s.peak_Hz - s.start_Hz) / s.span_Hz
                     if all(math.isfinite(v) for v in (s.peak_Hz, s.start_Hz, s.span_Hz))
                     and s.span_Hz > 0 else math.nan)
        self.indicator.set_state(s.ref_level_dBm, s.rbw_Hz, s.span_Hz, peak_frac,
                                 s.sweep_progress, s.sweeping, s.tg_on, s.overload)

        # acquisition
        self.acq_bar.setValue(int(100 * s.acq_progress) if s.acquiring else 0)
        self.acq_btn.setEnabled(bool(s.connected) and not s.acquiring)
        self.ref_btn.setEnabled(bool(s.connected) and not s.acquiring)
        self.abort_btn.setEnabled(bool(s.acquiring))
        smp = s.sample
        if smp and smp.get("acq_id") != self._last_acq:
            self._last_acq = smp.get("acq_id")
            what = "reference" if smp.get("reference") else "acquisition"
            if smp.get("aborted"):
                self.sample_label.setText(f"#{smp.get('acq_id')}: {what} aborted")
            else:
                self.sample_label.setText(
                    f"#{smp.get('acq_id')}: peak {_fmt(smp.get('peak_Hz', math.nan) / 1e6, '.4f')} MHz "
                    f"at {_fmt(smp.get('peak_dBm'), '.2f')} dBm, floor {_fmt(smp.get('floor_dBm'), '.1f')} "
                    f"dBm, {smp.get('averages')} avg" + ("  OVERLOAD" if smp.get("overload") else ""))
            if self.which_combo.currentIndex() == 1:
                self._force_fetch()

        # reference
        ref = s.reference or {}
        if s.acquiring and getattr(s, "acq_is_reference", False):
            self.ref_label.setText(f"taking reference #{s.acq_id} ...")
        elif ref.get("present"):
            tg = (f"TG {_fmt(ref.get('tg_level_dBm'), 'g')} dBm" if ref.get("tg_on")
                  else "TG OFF (cannot normalise)")
            self.ref_label.setText(
                f"#{ref.get('acq_id')}: {tg}, {_fmt_Hz(ref.get('start_Hz'))} - "
                f"{_fmt_Hz(ref.get('stop_Hz'))}, {ref.get('points')} pts, {_age(ref.get('age_s'))}")
        else:
            self.ref_label.setText("none")
        ref_id = ref.get("acq_id") if ref.get("present") else None
        if ref_id != self._ref_id:
            self._ref_id = ref_id
            if self.view_combo.currentIndex() != 0:
                self._force_fetch()

        # trace: fetch only when there is a new one, and not more than ~7 per second
        which = "last" if self.which_combo.currentIndex() == 0 else "sample"
        new = s.trace_id != self._trace_id if which == "last" else self._trace_id == -1
        if new and now - self._last_fetch > 0.14:
            self._last_fetch = now
            try:
                self._trace = self._fetch(which)
                self._trace_id = s.trace_id
                self._redraw(s)
            except Exception:
                pass                     # nothing measured yet

    def _fetch(self, which: str) -> dict:
        """The trace for the current view. If norm is refused (no reference, or
        it does not match), show the spectrum and say why instead of an empty plot."""
        self._norm_error = ""
        if self.view_combo.currentIndex() == 0:
            return self.ctrl.get_trace(which, "power")
        try:
            return self.ctrl.get_trace(which, "norm")
        except Exception as exc:
            self._norm_error = str(exc)
            return self.ctrl.get_trace(which, "power")

    def _redraw(self, s):
        t = self._trace
        if t is None:
            return
        fx = t["freqs_Hz"] / 1e6
        label = (f"{t['points']} pts, RBW {_fmt_Hz(t.get('rbw_Hz'))}, VBW {_fmt_Hz(t.get('vbw_Hz'))}, "
                 f"{t.get('detector', '')}")
        if "norm_dB" in t:
            y = t["norm_dB"]
            mode = "norm"
            label += f"  -  minus thru reference #{t.get('reference_acq_id')}"
        else:
            y = t["power_dBm"]
            mode = "power"
            if self.view_combo.currentIndex() != 0:
                label += "  -  SPECTRUM: " + (self._norm_error or "no reference")
        if mode != self._plot_mode:
            self._plot_mode = mode
            self.plot.setLabel("left", "transmission" if mode == "norm" else "power",
                               units="dB" if mode == "norm" else "dBm")
        self.curve.setData(fx, y)
        self.trace_label.setText(label)
        self.trace_label.setToolTip(label)
        if mode == "power":
            self.ref_line.setVisible(True)
            self.ref_line.setPos(t.get("ref_level_dBm", 0.0))
            self.indicator.set_trace(y)
        else:
            self.ref_line.setVisible(True)
            self.ref_line.setPos(0.0)    # 0 dB = as good as the thru
        pk = t.get("peak_Hz", math.nan)
        if isinstance(pk, (int, float)) and math.isfinite(pk) and len(fx):
            i = int(np.nanargmax(y)) if np.isfinite(y).any() else 0
            self.peak_dot.setData([fx[i]], [y[i]])
            self.big["freq"].setText(f"{fx[i]:.4f}")
            self.big["level"].setText(_fmt(y[i], ".2f"))
            self.big_cap["level"].setText("LEVEL" if mode == "power" else "TRANSMISSION")
            # the unit follows the view: dBm is absolute, dB relative to the thru
            self.big_unit["level"].setText("dBm" if mode == "power" else "dB")
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
    sa, _ = build_sim_system(cfg)
    return run_app(sa, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
