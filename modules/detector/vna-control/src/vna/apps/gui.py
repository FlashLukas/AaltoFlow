"""Control GUI for the VNA -- a Keysight PNA-X or Copper Mountain C1209, or the simulator.

    uv run scripts/run_gui.py                  # local simulator (follows mag2d if running)
    uv run scripts/run_gui.py --real           # the PNA-X, in this process
    uv run scripts/run_gui.py --connect HOST   # a running service (either kind)

The window holds an Analyzer-like object (an in-process Analyzer or a VnaClient
facade) and never cares which. A 60 ms timer reads status(); a new trace is
fetched only when `trace_id` moved. Events cross into the GUI thread on a Qt
signal.

Signature widget: KittelIndicator -- the resonance curve f(H) of the SIMULATED
film, the band the VNA is sweeping, and a glowing dot where the sample sits
right now. It is shown only when the backend is the simulator: on the real
analyser the model's curve would be a claim about a sample it knows nothing of.

The trace view has four modes, all against the BRAIN's reference (so the GUI,
the console and a scan all mean the same reference):
  Raw |S|                the S-parameter as measured: loss slope, ripple, phase winding
  Divided by reference   S / S_ref = 1 + u: the cables cancel, the sample remains
  u (real + imag)        u = (S - S_ref)/S_ref, Re u on top, Im u below
  ln(S/S_ref)            the logarithm: proportional to the susceptibility at any
                         line depth, where u is its small-signal limit
"""

from __future__ import annotations

import math
import time

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from .. import model
from ..field import FIELD_SOURCES
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog

VIEWS = ("Raw |S|", "Divided by reference", "u (real + imag)", "ln(S/S_ref) (real + imag)")


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


def _fmt(v: float, fmt: str, none: str = "--") -> str:
    return format(v, fmt) if isinstance(v, (int, float)) and math.isfinite(v) else none


def _age(seconds: float) -> str:
    if not (isinstance(seconds, (int, float)) and math.isfinite(seconds)):
        return "--"
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


# ------------------------------------------------------------- the indicator

class KittelIndicator(QtWidgets.QWidget):
    """f(H) of the simulated film at the current field angle, the swept band,
    and where the sample is now."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(260, 170)
        self._sample = model.Sample() if hasattr(model, "Sample") else None
        self._field = float("nan")
        self._angle = 0.0
        self._band = (float("nan"), float("nan"))
        self._ok = False
        self._sweeping = False
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, sample, field_mT, angle_deg, start_Hz, stop_Hz, field_ok, sweeping):
        self._sample = sample
        self._field, self._band = field_mT, (start_Hz, stop_Hz)
        self._angle = angle_deg if math.isfinite(angle_deg) else 0.0
        self._ok, self._sweeping = bool(field_ok), bool(sweeping)

    def _tick(self):
        if not self.isVisible():
            return
        self._phase = (self._phase + 0.04) % 1.0
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        left, right, top, bottom = 34, 10, 10, 24
        pw, ph = w - left - right, h - top - bottom
        s = self._sample
        if s is None or pw < 20 or ph < 20:
            p.end()
            return
        ang = self._angle

        # the window: fields up to max(100 mT, 1.3 |H|, saturation + 60 mT)
        sat = model.saturation_mT(s)
        h_max = max(100.0, 1.3 * abs(self._field) if math.isfinite(self._field) else 0.0,
                    sat + 60.0)
        f_top = max(model.kittel_Hz(h_max, s, ang) or 0.0,
                    self._band[1] if math.isfinite(self._band[1]) else 0.0) * 1.08
        if not math.isfinite(f_top) or f_top <= 0:
            f_top = 10e9

        def X(hm):
            return left + (hm + h_max) / (2 * h_max) * pw

        def Y(f):
            return top + ph - f / f_top * ph

        # frame and zero line
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["border"]), 1))
        p.setBrush(QtGui.QColor(COLORS["code_bg"]))
        p.drawRoundedRect(QtCore.QRectF(left, top, pw, ph), 4, 4)
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["grid"]), 1))
        p.drawLine(QtCore.QPointF(X(0), top), QtCore.QPointF(X(0), top + ph))

        # the swept band
        lo, hi = self._band
        if math.isfinite(lo) and math.isfinite(hi):
            band = QtGui.QColor(COLORS["accent"]); band.setAlpha(38)
            p.setPen(QtCore.Qt.NoPen); p.setBrush(band)
            y0, y1 = max(top, Y(min(hi, f_top))), min(top + ph, Y(lo))
            p.drawRect(QtCore.QRectF(left, y0, pw, max(0.0, y1 - y0)))

        # the Kittel curve, both field polarities
        pen = QtGui.QPen(QtGui.QColor(COLORS["muted"]), 2)
        p.setPen(pen); p.setBrush(QtCore.Qt.NoBrush)
        for sign in (-1, 1):
            path = QtGui.QPainterPath()
            started = False
            for i in range(121):
                hm = sign * h_max * i / 120
                f = model.kittel_Hz(hm, s, ang)
                if not math.isfinite(f) or f > f_top:
                    started = False
                    continue
                pt = QtCore.QPointF(X(hm), Y(f))
                path.lineTo(pt) if started else path.moveTo(pt)
                started = True
            p.drawPath(path)

        # the operating point
        f_now = model.kittel_Hz(self._field, s, ang) if math.isfinite(self._field) else math.nan
        if math.isfinite(self._field):
            x = X(max(-h_max, min(h_max, self._field)))
            col = QtGui.QColor(COLORS["accent"] if self._ok else COLORS["danger"])
            dash = QtGui.QPen(col, 1, QtCore.Qt.DashLine)
            p.setPen(dash)
            p.drawLine(QtCore.QPointF(x, top), QtCore.QPointF(x, top + ph))
            if math.isfinite(f_now) and f_now <= f_top:
                y = Y(f_now)
                in_band = math.isfinite(lo) and lo <= f_now <= hi
                r = 5.0 + (2.5 * math.sin(2 * math.pi * self._phase) if self._sweeping else 0.0)
                glow = QtGui.QRadialGradient(x, y, 18)
                g0 = QtGui.QColor(col); g0.setAlpha(170 if in_band else 70)
                g1 = QtGui.QColor(col); g1.setAlpha(0)
                glow.setColorAt(0, g0); glow.setColorAt(1, g1)
                p.setPen(QtCore.Qt.NoPen); p.setBrush(glow)
                p.drawEllipse(QtCore.QPointF(x, y), 18, 18)
                p.setBrush(QtGui.QColor(COLORS["accent_hi"] if in_band else COLORS["muted"]))
                p.drawEllipse(QtCore.QPointF(x, y), r, r)

        # labels
        p.setPen(QtGui.QColor(COLORS["muted"]))
        f = p.font(); f.setPointSize(7); p.setFont(f)
        p.drawText(QtCore.QRectF(0, top - 2, left - 4, 12), QtCore.Qt.AlignRight,
                   f"{f_top / 1e9:.0f}")
        p.drawText(QtCore.QRectF(0, top + ph - 10, left - 4, 12), QtCore.Qt.AlignRight, "0")
        p.drawText(QtCore.QRectF(2, top + ph / 2 - 6, left - 6, 12), QtCore.Qt.AlignRight, "GHz")
        p.drawText(QtCore.QRectF(left, top + ph + 2, 60, 12), QtCore.Qt.AlignLeft,
                   f"-{h_max:.0f}")
        p.drawText(QtCore.QRectF(left + pw - 60, top + ph + 2, 60, 12), QtCore.Qt.AlignRight,
                   f"+{h_max:.0f} mT")
        f.setBold(True); f.setPointSize(8); p.setFont(f)
        if math.isfinite(f_now):
            cap = f"f_r {f_now / 1e9:.3f} GHz"
        elif math.isfinite(self._field):
            cap = "no resonance (not saturated)"
        else:
            cap = "no field"
        p.drawText(QtCore.QRectF(left, top + ph + 2, pw, 12), QtCore.Qt.AlignHCenter, cap)
        p.end()


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self._simulated = None           # learnt from the first status
        self.setWindowTitle("VNA" + ("  (remote)" if remote else ""))
        self.resize(1240, 800)

        self._trace = None
        self._trace_id = -1
        self._last_fetch = 0.0
        self._last_acq = 0
        self._ref_id = None              # reference acq_id the trace was fetched against
        self._u_error = ""               # why u could not be shown, if it could not
        self._plot_mode = None

        root = QtWidgets.QWidget(); root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)

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

    # ---- layout ----------------------------------------------------------

    def _build_sidebar(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget(); panel.setFixedWidth(340)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("VNA")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title)
        self.kind_label = QtWidgets.QLabel(""); self.kind_label.setObjectName("hint")
        header.addWidget(self.kind_label); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        header.addWidget(settings_btn)
        col.addLayout(header)

        ccard, clay = _card()
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        clay.addWidget(self.conn_dot)
        col.addWidget(ccard)

        # field
        fcard, flay = _card("Field (from the magnet)")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Source"))
        self.source_combo = QtWidgets.QComboBox(); self.source_combo.addItems(list(FIELD_SOURCES))
        # .activated fires for USER choices only, so a status update cannot echo back
        self.source_combo.activated.connect(lambda _i: self._call(
            self.ctrl.set_field_source, self.source_combo.currentText()))
        row.addWidget(self.source_combo, 1)
        flay.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        self.manual_spin = self._dspin(-self.cfg.limits.manual_field_max_mT,
                                       self.cfg.limits.manual_field_max_mT, 2, "  mT", 1.0)
        self.manual_angle_spin = self._dspin(-360.0, 360.0, 1, "  deg", 5.0)
        b = QtWidgets.QPushButton("Set")
        b.setToolTip("The manual field and angle (used when the source is manual, "
                     "and until a magnet is heard)")
        b.clicked.connect(lambda: self._call(self.ctrl.set_manual_field, self.manual_spin.value(),
                                             self.manual_angle_spin.value()))
        row.addWidget(self.manual_spin, 3); row.addWidget(self.manual_angle_spin, 2)
        row.addWidget(b)
        flay.addLayout(row)
        self.field_label = QtWidgets.QLabel("—"); self.field_label.setObjectName("hint")
        self.field_label.setWordWrap(True)
        flay.addWidget(self.field_label)
        col.addWidget(fcard)

        # sweep
        scard, slay = _card("Sweep")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        lim = self.cfg.limits
        self.sparam_combo = QtWidgets.QComboBox(); self.sparam_combo.addItems(list(model.SPARAMS))
        # applies at once (like the source): it is a choice, not a number being typed
        self.sparam_combo.activated.connect(lambda _i: self._call(
            self.ctrl.set_sparam, self.sparam_combo.currentText()))
        self.start_spin = self._dspin(lim.freq_min_Hz / 1e9, lim.freq_max_Hz / 1e9, 4, "  GHz", 0.1)
        self.stop_spin = self._dspin(lim.freq_min_Hz / 1e9, lim.freq_max_Hz / 1e9, 4, "  GHz", 0.1)
        self.points_spin = QtWidgets.QSpinBox(); self.points_spin.setRange(lim.points_min, lim.points_max)
        self.ifbw_spin = self._dspin(lim.ifbw_min_Hz / 1e3, lim.ifbw_max_Hz / 1e3, 3, "  kHz", 1.0)
        self.power_spin = self._dspin(lim.power_min_dBm, lim.power_max_dBm, 1, "  dBm", 1.0)
        self.avg_spin = QtWidgets.QSpinBox(); self.avg_spin.setRange(lim.averages_min, lim.averages_max)
        for label, w in (("S-parameter", self.sparam_combo),
                         ("Start", self.start_spin), ("Stop", self.stop_spin),
                         ("Points", self.points_spin), ("IF bandwidth", self.ifbw_spin),
                         ("Power", self.power_spin), ("Averages", self.avg_spin)):
            form.addRow(label, w)
        slay.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        self.sweep_time_label = QtWidgets.QLabel("—"); self.sweep_time_label.setObjectName("hint")
        row.addWidget(self.sweep_time_label, 1)
        apply = QtWidgets.QPushButton("Apply"); apply.setObjectName("primary")
        apply.clicked.connect(self._apply_sweep)
        row.addWidget(apply)
        slay.addLayout(row)
        col.addWidget(scard)

        # acquisition
        acard, alay = _card("Acquire (scan-safe trace)")
        self.cont_chk = QtWidgets.QCheckBox("Continuous sweep")
        self.cont_chk.clicked.connect(lambda on: self._call(self.ctrl.set_continuous, on))  # user only
        alay.addWidget(self.cont_chk)
        row = QtWidgets.QHBoxLayout()
        self.acq_btn = QtWidgets.QPushButton("Acquire"); self.acq_btn.setObjectName("primary")
        self.acq_btn.setMinimumHeight(34)
        self.acq_btn.clicked.connect(lambda: self._call(self.ctrl.acquire))
        self.abort_btn = QtWidgets.QPushButton("Abort")
        self.abort_btn.clicked.connect(lambda: self._call(self.ctrl.abort))
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
        rcard, rlay = _card("Reference")
        row = QtWidgets.QHBoxLayout()
        self.ref_btn = QtWidgets.QPushButton("Take reference")
        self.ref_btn.setToolTip("Acquire (with the same averaging) and keep the result as the "
                                "reference. Take it where the sample does not resonate in the band.")
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

        rcard, rlay = _card("Resonance")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(24)
        self.big, self.big_cap = {}, {}
        for key, label, unit in (("field", "FIELD", "mT"), ("dip", "DIP", "GHz"),
                                 ("depth", "DEPTH", "dB")):
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
        self.model_label = QtWidgets.QLabel(""); self.model_label.setObjectName("hint")
        row.addStretch(1)
        self.indicator = KittelIndicator(); self.indicator.setFixedWidth(300)
        row.addWidget(self.indicator)
        rlay.addLayout(row)
        rlay.addWidget(self.model_label)
        colw.addWidget(rcard)

        tcard, tlay = _card("Trace")
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Show"))
        self.view_combo = QtWidgets.QComboBox()
        self.view_combo.addItems(list(VIEWS))
        self.view_combo.setToolTip("Divided by reference and u use the analyser's reference "
                                   "(REFERENCE card): u = (S - S_ref)/S_ref")
        # u is FETCHED from the brain (it owns the reference), so a new view = a new fetch
        self.view_combo.currentIndexChanged.connect(lambda _i: self._force_fetch())
        bar.addWidget(self.view_combo)
        self.which_combo = QtWidgets.QComboBox()
        self.which_combo.addItems(["Latest sweep", "Last acquisition"])
        self.which_combo.currentIndexChanged.connect(lambda _i: self._force_fetch())
        bar.addWidget(self.which_combo)
        bar.addStretch(1)
        self.trace_label = QtWidgets.QLabel(""); self.trace_label.setObjectName("hint")
        bar.addWidget(self.trace_label)
        tlay.addLayout(bar)
        self.mag_plot, self.mag_curve = self._make_plot("|S21|", "dB")
        self.phase_plot, self.phase_curve = self._make_plot("phase", "deg")
        self.phase_plot.setXLink(self.mag_plot)
        import pyqtgraph as pg
        self.dip_line = pg.InfiniteLine(angle=90, movable=False,
                                        pen=pg.mkPen(COLORS["accent_hi"], width=1))
        self.model_line = pg.InfiniteLine(angle=90, movable=False,
                                          pen=pg.mkPen(COLORS["muted"], width=1,
                                                       style=QtCore.Qt.DashLine))
        self.mag_plot.addItem(self.model_line); self.mag_plot.addItem(self.dip_line)
        tlay.addWidget(self.mag_plot, 3)
        tlay.addWidget(self.phase_plot, 2)
        colw.addWidget(tcard, 1)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMaximumHeight(120)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 0)
        return panel

    def _make_plot(self, name, unit):
        import pyqtgraph as pg
        pg.setConfigOptions(antialias=True)
        w = pg.PlotWidget(background=COLORS["code_bg"])
        w.setMinimumHeight(120)
        pen = pg.mkPen(COLORS["muted"])
        for axis in ("left", "bottom"):
            ax = w.getAxis(axis); ax.setPen(pen); ax.setTextPen(pen)
            ax.enableAutoSIPrefix(False)
        w.setLabel("left", name, units=unit)
        w.setLabel("bottom", "frequency", units="GHz")
        w.showGrid(x=True, y=True, alpha=0.15)
        curve = w.plot([], [], pen=pg.mkPen(COLORS["accent"], width=1.6))
        return w, curve

    # ---- actions ---------------------------------------------------------

    def _call(self, fn, *args):
        try:
            r = fn(*args)
            if isinstance(r, dict) and r.get("ok") is False:
                self._on_event("warn", r.get("error", "refused"))
        except Exception as exc:
            self._on_event("warn", f"refused: {exc}")

    def _apply_sweep(self):
        """Send only what changed: every sweep change restarts an acquisition
        and logs an event, and six of them for one click would bury the log."""
        s = self.ctrl.status()
        for spin, now, scale, setter in (
                (self.stop_spin, s.stop_Hz, 1e9, self.ctrl.set_stop),
                (self.start_spin, s.start_Hz, 1e9, self.ctrl.set_start),
                (self.points_spin, s.points, 1, self.ctrl.set_points),
                (self.ifbw_spin, s.ifbw_Hz, 1e3, self.ctrl.set_ifbw),
                (self.power_spin, s.power_dBm, 1, self.ctrl.set_power),
                (self.avg_spin, s.averages, 1, self.ctrl.set_averages)):
            want = spin.value() * scale
            if not (isinstance(now, (int, float)) and math.isclose(want, now, rel_tol=1e-9, abs_tol=1e-9)):
                self._call(setter, want)
        # a new start may be refused until the new stop is in; send start again
        if not math.isclose(self.start_spin.value() * 1e9, self.ctrl.status().start_Hz, abs_tol=1.0):
            self._call(self.ctrl.set_start, self.start_spin.value() * 1e9)

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
        never while the user is typing in them."""
        s = self.ctrl.status()
        for spin, val in ((self.start_spin, s.start_Hz / 1e9), (self.stop_spin, s.stop_Hz / 1e9),
                          (self.ifbw_spin, s.ifbw_Hz / 1e3), (self.power_spin, s.power_dBm),
                          (self.manual_spin, s.manual_field_mT),
                          (self.manual_angle_spin, s.manual_angle_deg)):
            if math.isfinite(val) and (force or not spin.hasFocus()):
                spin.setValue(val)
        for spin, val in ((self.points_spin, s.points), (self.avg_spin, s.averages)):
            if force or not spin.hasFocus():
                spin.setValue(int(val))
        for combo, text in ((self.source_combo, s.field_source_set),
                            (self.sparam_combo, s.sparam)):
            if force or not combo.view().isVisible():
                i = combo.findText(text)
                if i >= 0:
                    combo.setCurrentIndex(i)
        self.cont_chk.blockSignals(True)
        self.cont_chk.setChecked(bool(s.continuous))
        self.cont_chk.blockSignals(False)

    def _force_fetch(self):
        self._trace_id = -1
        self._last_fetch = 0.0

    def _set_simulated(self, simulated: bool):
        """Things that depend on WHICH analyser this is, set once it is known."""
        if simulated == self._simulated:
            return
        self._simulated = simulated
        from ..backends import analyser_name
        name = analyser_name(self.cfg)
        self.kind_label.setText("SIMULATED" if simulated else name.split(" ", 1)[-1])
        self.setWindowTitle(("VNA - simulated (S-parameters of a YIG film)" if simulated
                             else f"VNA - {name}")
                            + ("  (remote)" if self._remote else ""))
        # the model's Kittel curve describes the SIMULATED film only
        self.indicator.setVisible(simulated)
        self.model_label.setVisible(simulated)

    def _refresh(self):
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

        # field
        age = f", {s.field_age_s:.1f} s old" if math.isfinite(s.field_age_s) and s.field_age_s > 0.5 else ""
        self.field_label.setText(f"in use: {_fmt(s.field_mT, '.3f')} mT at {_fmt(s.angle_deg, '.1f')} deg "
                                 f"from {s.field_source or '--'}{age}")
        self.field_label.setStyleSheet(
            "" if s.field_ok else f"color:{COLORS['danger']}; font-weight:700;")
        self.big["field"].setText(_fmt(s.field_mT, ".2f"))
        self.big["field"].setStyleSheet("" if s.field_ok else f"color:{COLORS['danger']};")
        self.big_cap["field"].setText(f"FIELD  at {_fmt(s.angle_deg, '.1f')} deg")
        self.sweep_time_label.setText(f"sweep time {_fmt(s.sweep_time_s, '.3g')} s")

        if self._simulated:
            # the sample as a model, for the indicator (rebuilt from status, so a
            # remote simulator draws its own film, not this GUI's defaults)
            smp = model.Sample(ms_mT=_num(s.ms_mT, 176.0), gamma_GHz_per_T=_num(s.gamma_GHz_per_T, 28.0),
                               alpha=_num(s.alpha, 5e-4), dh0_mT=_num(s.dh0_mT, 0.3),
                               h_anis_mT=_num(s.h_anis_mT, 0.0), hk_mT=_num(s.hk_mT, 0.0),
                               easy_axis_deg=_num(s.easy_axis_deg, 0.0), geometry=s.geometry)
            self.indicator.set_state(smp, s.field_mT, s.angle_deg, s.start_Hz, s.stop_Hz,
                                     s.field_ok, s.sweeping)
            ang = s.angle_deg if math.isfinite(s.angle_deg) else 0.0
            lw = model.linewidth_Hz(s.field_mT, smp, ang) if math.isfinite(s.field_mT) else math.nan
            aniso = f", Hk {s.hk_mT:g} mT along {s.easy_axis_deg:g} deg" if _num(s.hk_mT, 0.0) else ""
            self.model_label.setText(
                f"model: Kittel {_fmt(s.f_res_model_Hz / 1e9, '.4f')} GHz, linewidth "
                f"{_fmt(lw / 1e6, '.1f')} MHz  ({s.geometry.replace('_', '-')}, mu0Ms {s.ms_mT:g} mT, "
                f"alpha {s.alpha:g}, mu0dH0 {s.dh0_mT:g} mT{aniso})")

        self._sync_inputs()

        # acquisition
        self.acq_bar.setValue(int(100 * s.acq_progress) if s.acquiring else 0)
        self.acq_btn.setEnabled(bool(s.connected) and not s.acquiring)
        self.ref_btn.setEnabled(bool(s.connected) and not s.acquiring)
        self.abort_btn.setEnabled(bool(s.acquiring))
        smp_d = s.sample
        if smp_d and smp_d.get("acq_id") != self._last_acq:
            self._last_acq = smp_d.get("acq_id")
            what = "reference" if smp_d.get("reference") else "acquisition"
            if smp_d.get("aborted"):
                self.sample_label.setText(f"#{smp_d.get('acq_id')}: {what} aborted")
            else:
                self.sample_label.setText(
                    f"#{smp_d.get('acq_id')} ({smp_d.get('sparam', '')}): dip "
                    f"{_fmt(smp_d.get('dip_Hz', math.nan) / 1e9, '.4f')} GHz "
                    f"({_fmt(smp_d.get('dip_dB'), '.2f')} dB) at {_fmt(smp_d.get('field_mT'), '.2f')} mT, "
                    f"{_fmt(smp_d.get('angle_deg'), '.1f')} deg, {smp_d.get('averages')} avg"
                    + ("" if smp_d.get("field_ok") else "  FIELD NOT LIVE"))
            if self.which_combo.currentIndex() == 1:
                self._force_fetch()

        # reference
        ref = s.reference or {}
        if s.acquiring and getattr(s, "acq_is_reference", False):
            self.ref_label.setText(f"taking reference #{s.acq_id} ...")
        elif ref.get("present"):
            self.ref_label.setText(
                f"#{ref.get('acq_id')}: {_fmt(ref.get('field_mT'), '.2f')} mT at "
                f"{_fmt(ref.get('angle_deg'), '.1f')} deg, {ref.get('sparam')}, "
                f"{_fmt(ref.get('start_Hz', math.nan) / 1e9, 'g')}-"
                f"{_fmt(ref.get('stop_Hz', math.nan) / 1e9, 'g')} GHz, {ref.get('points')} pts, "
                f"{_age(ref.get('age_s'))}")
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
                self._redraw()
            except Exception:
                pass                     # nothing measured yet

    def _fetch(self, which: str) -> dict:
        """The trace for the current view: `s` for raw, `u` from the BRAIN for
        the other two. If u is refused (no reference, or it does not match),
        show the raw trace and say why instead of an empty plot."""
        self._u_error = ""
        if self.view_combo.currentIndex() == 0:
            return self.ctrl.get_trace(which, "s")
        # The last view wants the logarithm; the middle two are built from u.
        quantity = "ln" if self.view_combo.currentIndex() == 3 else "u"
        try:
            return self.ctrl.get_trace(which, quantity)
        except Exception as exc:
            self._u_error = str(exc)
            return self.ctrl.get_trace(which, "s")

    def _set_plot_mode(self, mode: str, sparam: str):
        if (mode, sparam) == self._plot_mode:
            return
        self._plot_mode = (mode, sparam)
        if mode == "u":
            self.mag_plot.setLabel("left", "Re u", units="")
            self.phase_plot.setLabel("left", "Im u", units="")
        elif mode == "ln":
            self.mag_plot.setLabel("left", f"Re ln({sparam}/{sparam}_ref)", units="")
            self.phase_plot.setLabel("left", f"Im ln({sparam}/{sparam}_ref)", units="")
        elif mode == "divided":
            self.mag_plot.setLabel("left", f"|{sparam} / {sparam}_ref|", units="dB")
            self.phase_plot.setLabel("left", "phase", units="deg")
        else:
            self.mag_plot.setLabel("left", f"|{sparam}|", units="dB")
            self.phase_plot.setLabel("left", "phase", units="deg")

    def _redraw(self):
        t = self._trace
        if t is None:
            return
        f = t["freqs_Hz"]
        fx = f / 1e9
        sp = t.get("sparam", "S21")
        view = self.view_combo.currentIndex()
        label = f"{sp}, {t['points']} pts, IFBW {t['ifbw_Hz'] / 1e3:g} kHz, {t['power_dBm']:g} dBm"
        if "ln" in t:
            # Re = attenuation through the film, Im = the phase it adds; both
            # are proportional to the susceptibility (up to the geometry factor
            # this module deliberately does not invent).
            ln = t["ln"]
            self._set_plot_mode("ln", sp)
            self.mag_curve.setData(fx, ln.real)
            self.phase_curve.setData(fx, ln.imag)
            label += (f"  -  ln against reference #{t.get('reference_acq_id')} at "
                      f"{_fmt(t.get('reference_field_mT'), '.1f')} mT, "
                      f"{_fmt(t.get('reference_angle_deg'), '.1f')} deg")
        elif "u" in t:
            u = t["u"]
            ref = (f"reference #{t.get('reference_acq_id')} at "
                   f"{_fmt(t.get('reference_field_mT'), '.1f')} mT, "
                   f"{_fmt(t.get('reference_angle_deg'), '.1f')} deg")
            if view == 2:
                self._set_plot_mode("u", sp)
                self.mag_curve.setData(fx, u.real)
                self.phase_curve.setData(fx, u.imag)
                label += f"  -  u against {ref}"
            else:
                z = 1 + u                              # S / S_ref
                self._set_plot_mode("divided", sp)
                self.mag_curve.setData(fx, 20 * np.log10(np.clip(np.abs(z), 1e-15, None)))
                self.phase_curve.setData(fx, np.degrees(np.angle(z)))
                label += f"  -  divided by {ref}"
        else:
            z = t["s"]
            self._set_plot_mode("raw", sp)
            self.mag_curve.setData(fx, 20 * np.log10(np.clip(np.abs(z), 1e-15, None)))
            self.phase_curve.setData(fx, np.degrees(np.angle(z)))
            if view != 0:
                label += "  -  RAW: " + (self._u_error or "no reference")
        self.trace_label.setText(label)
        self.trace_label.setToolTip(label)
        dip = t.get("dip_Hz", math.nan)
        self.dip_line.setVisible(math.isfinite(dip))
        if math.isfinite(dip):
            self.dip_line.setPos(dip / 1e9)
            self.big["dip"].setText(f"{dip / 1e9:.4f}")
            self.big["depth"].setText(_fmt(t.get("dip_dB"), ".2f"))
        fr = t.get("f_res_model_Hz", math.nan)
        fr = fr if isinstance(fr, (int, float)) else math.nan
        self.model_line.setVisible(math.isfinite(fr))
        if math.isfinite(fr):
            self.model_line.setPos(fr / 1e9)

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()
        super().closeEvent(ev)


def _num(v, default):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else default


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with an Analyzer-like object. The theme is chosen ONCE
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
    vna, _ = build_sim_system(cfg)
    return run_app(vna, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
