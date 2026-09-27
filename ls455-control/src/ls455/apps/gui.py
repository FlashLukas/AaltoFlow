"""Control GUI for the Lake Shore 455 gaussmeter.

    uv run scripts/run_gui.py                  # local simulator
    uv run scripts/run_gui.py --real           # the real meter, in this process
    uv run scripts/run_gui.py --connect HOST   # a running service

The window holds a Gaussmeter-like object (an in-process Gaussmeter or a
Ls455Client facade) and never cares which. A 60 ms timer reads status();
events cross into the GUI thread on a Qt signal.

Signature widget: AxialProbeIndicator -- the axial Hall probe seen from the
side, with flux lines running along its axis through the Hall element at the
tip. Their direction is the SIGN of the field (an axial probe measures the
component along its own axis), their number and speed grow with the field on a
LOG scale (a gaussmeter spans decades), and a bipolar bar underneath shows
where the reading sits in the present range -- red when it overloads.
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets

from ..backends.base import DC_DIGITS, MODES, RMS_BANDS
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog


class Bridge(QtCore.QObject):
    """Carries meter events across the thread boundary into the GUI."""
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


def split_mT(value_mT: float) -> tuple[str, str]:
    """42.0123 -> ('42.012', 'mT'); 0.00321 -> ('3.2100', 'µT'); 1234 -> ('1.2340', 'T').

    Five significant figures and a unit that keeps the number between 1 and 1000."""
    if not math.isfinite(value_mT):
        return "--", "mT"
    a = abs(value_mT)
    if a >= 1000:
        v, unit = value_mT / 1000, "T"
    elif a >= 1 or a == 0:
        v, unit = value_mT, "mT"
    else:
        v, unit = value_mT * 1000, "µT"
    decimals = max(0, 4 - int(math.floor(math.log10(abs(v)))) if v else 4)
    return f"{v:.{decimals}f}", unit


def fmt_mT(value_mT: float) -> str:
    v, u = split_mT(value_mT)
    return f"{v} {u}"


# ------------------------------------------------------------- the indicator

class AxialProbeIndicator(QtWidgets.QWidget):
    """An axial Hall probe with flux lines along its axis (see module docstring)."""

    LOG_MIN, LOG_MAX = -3.0, 4.0            # 1 uT ... 10 T maps to 0 ... 1

    def __init__(self):
        super().__init__()
        self.setFixedHeight(160)
        self.setMinimumWidth(220)
        self._field = float("nan")
        self._range = float("nan")
        self._flag = ""
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, field_mT: float, range_mT: float, flag: str):
        self._field, self._range, self._flag = field_mT, range_mT, flag

    def level(self) -> float:
        """|B| on a log scale, 0..1."""
        if not math.isfinite(self._field) or self._field == 0:
            return 0.0
        x = (math.log10(abs(self._field)) - self.LOG_MIN) / (self.LOG_MAX - self.LOG_MIN)
        return max(0.0, min(1.0, x))

    def _tick(self):
        # the flux lines drift in the field's direction, faster for a stronger field
        sign = 1.0 if not math.isfinite(self._field) or self._field >= 0 else -1.0
        self._phase = (self._phase + sign * (0.004 + 0.02 * self.level())) % 1.0
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cy = h * 0.40
        tip_x = w * 0.62
        accent = QtGui.QColor(COLORS["accent"])
        lvl = self.level()
        bad = self._flag != ""
        positive = not math.isfinite(self._field) or self._field >= 0

        # -- flux lines along the probe axis: count and brightness follow |B|
        n_lines = 1 + int(round(4 * lvl)) if lvl > 0 else 0
        spacing = 11.0
        for k in range(n_lines):
            off = (k - (n_lines - 1) / 2) * spacing
            c = QtGui.QColor(COLORS["danger"] if bad else accent)
            c.setAlpha(int(70 + 150 * lvl * (1 - abs(off) / (3 * spacing + 1))))
            pen = QtGui.QPen(c, 1.6)
            p.setPen(pen)
            y = cy + off
            p.drawLine(QtCore.QPointF(8, y), QtCore.QPointF(w - 8, y))
            # arrowheads riding along the line show the direction of B
            for j in range(3):
                x = 8 + ((j / 3 + self._phase) % 1.0) * (w - 16)
                d = 5.0 if positive else -5.0
                path = QtGui.QPainterPath()
                path.moveTo(x + d, y)
                path.lineTo(x - d, y - 3.5)
                path.lineTo(x - d, y + 3.5)
                path.closeSubpath()
                p.fillPath(path, c)

        # -- the probe: a steel stem from the left with a handle, tip at tip_x
        metal = QtGui.QColor("#5a626e")
        p.setPen(QtGui.QPen(metal, 1.4))
        p.setBrush(QtGui.QColor(COLORS["panel_hi"]))
        p.drawRoundedRect(QtCore.QRectF(4, cy - 13, w * 0.22, 26), 6, 6)     # handle
        p.drawRect(QtCore.QRectF(4 + w * 0.22, cy - 5, tip_x - 4 - w * 0.22, 10))  # stem
        # the Hall element at the tip, glowing with the field
        glow = QtGui.QRadialGradient(tip_x, cy, 16)
        g0 = QtGui.QColor(COLORS["danger"] if bad else COLORS["accent_hi"])
        g0.setAlpha(int(60 + 195 * lvl))
        g1 = QtGui.QColor(g0); g1.setAlpha(0)
        glow.setColorAt(0.0, g0); glow.setColorAt(1.0, g1)
        p.setPen(QtCore.Qt.NoPen); p.setBrush(glow)
        p.drawEllipse(QtCore.QPointF(tip_x, cy), 16, 16)
        p.setPen(QtGui.QPen(metal, 1.4))
        p.setBrush(QtGui.QColor(COLORS["danger"] if bad else COLORS["accent"]))
        p.drawRect(QtCore.QRectF(tip_x - 4, cy - 5, 5, 10))

        # -- bipolar bar: where the reading sits in -range .. +range
        bar = QtCore.QRectF(12, h - 42, w - 24, 8)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(COLORS["border"]))
        p.drawRoundedRect(bar, 4, 4)
        mid = bar.center().x()
        if math.isfinite(self._field) and math.isfinite(self._range) and self._range > 0:
            frac = max(-1.0, min(1.0, self._field / self._range))
            fill_col = QtGui.QColor(COLORS["danger"] if bad or abs(frac) > 0.95 else COLORS["ok"])
            p.setBrush(fill_col)
            x1 = mid + frac * bar.width() / 2
            p.drawRoundedRect(QtCore.QRectF(min(mid, x1), bar.top(), abs(x1 - mid), bar.height()), 4, 4)
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["muted"]), 1.2))
        p.drawLine(QtCore.QPointF(mid, bar.top() - 3), QtCore.QPointF(mid, bar.bottom() + 3))

        # -- caption
        f = p.font(); f.setBold(True); f.setPointSize(8); p.setFont(f)
        if self._flag:
            cap, col = self._flag.upper(), QtGui.QColor(COLORS["danger"])
        elif math.isfinite(self._range):
            cap, col = f"-{fmt_mT(self._range)}    range    +{fmt_mT(self._range)}", \
                QtGui.QColor(COLORS["muted"])
        else:
            cap, col = "no reading", QtGui.QColor(COLORS["muted"])
        p.setPen(col)
        p.drawText(QtCore.QRectF(0, h - 28, w, 14), QtCore.Qt.AlignHCenter, cap)
        p.end()


# ------------------------------------------------------------- main window

_WINDOWS_S = {"10 s": 10, "30 s": 30, "1 min": 60, "5 min": 300}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self.setWindowTitle("LS455 - Gaussmeter" + ("  (remote)" if remote else ""))
        self.resize(1160, 760)

        # one history for the plot: (monotonic time, mT), ~5 min at 20 Hz
        self._hist: deque = deque(maxlen=6000)
        self._last_reading = -1
        self._last_acq = 0
        self._paused = False
        self._ranges: list = []

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
        except Exception as exc:          # e.g. no meter plugged in: show it, don't crash
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
        title = QtWidgets.QLabel("LS455")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        header.addWidget(settings_btn)
        col.addLayout(header)

        ccard, clay = _card()
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        clay.addWidget(self.conn_dot)
        self.idn_label = QtWidgets.QLabel("—"); self.idn_label.setObjectName("hint")
        self.idn_label.setWordWrap(True)
        clay.addWidget(self.idn_label)
        col.addWidget(ccard)

        # mode: DC (with resolution) or RMS (with band)
        mcard, mlay = _card("Mode")
        row = QtWidgets.QHBoxLayout()
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems([m.upper() for m in MODES])
        # `activated` fires only on a USER choice, never on setCurrentIndex
        self.mode_combo.activated.connect(lambda i: self.ctrl.set_mode(MODES[i]))
        self.digits_combo = QtWidgets.QComboBox()
        self.digits_combo.addItems([f"{d} digits" for d in DC_DIGITS])
        self.digits_combo.activated.connect(lambda i: self.ctrl.set_dc_digits(DC_DIGITS[i]))
        self.band_combo = QtWidgets.QComboBox()
        self.band_combo.addItems([f"{b} band" for b in RMS_BANDS])
        self.band_combo.activated.connect(lambda i: self.ctrl.set_rms_band(RMS_BANDS[i]))
        row.addWidget(self.mode_combo); row.addWidget(self.digits_combo, 1)
        row.addWidget(self.band_combo, 1)
        mlay.addLayout(row)
        self.mode_label = QtWidgets.QLabel("—"); self.mode_label.setObjectName("hint")
        mlay.addWidget(self.mode_label)
        col.addWidget(mcard)

        # range
        rcard, rlay = _card("Range")
        self.auto_chk = QtWidgets.QCheckBox("Auto range")
        self.auto_chk.clicked.connect(lambda on: self.ctrl.set_auto_range(on))  # .clicked: user only
        rlay.addWidget(self.auto_chk)
        row = QtWidgets.QHBoxLayout()
        self.range_combo = QtWidgets.QComboBox()
        self.range_set = QtWidgets.QPushButton("Set"); self.range_set.setObjectName("primary")
        self.range_set.clicked.connect(self._set_range)
        row.addWidget(self.range_combo, 1); row.addWidget(self.range_set)
        rlay.addLayout(row)
        col.addWidget(rcard)

        # relative
        lcard, llay = _card("Relative")
        self.rel_chk = QtWidgets.QCheckBox("Relative mode")
        self.rel_chk.clicked.connect(lambda on: self.ctrl.set_relative(on))
        llay.addWidget(self.rel_chk)
        row = QtWidgets.QHBoxLayout()
        self.rel_spin = QtWidgets.QDoubleSpinBox()
        self.rel_spin.setDecimals(4); self.rel_spin.setSuffix("  mT")
        lim = self.cfg.limits.rel_setpoint_max_mT
        self.rel_spin.setRange(-lim, lim)
        b = QtWidgets.QPushButton("Set")
        b.clicked.connect(lambda: self.ctrl.set_relative(True, self.rel_spin.value()))
        here = QtWidgets.QPushButton("Here")
        here.setToolTip("Relative to the field measured now")
        here.clicked.connect(self._relative_here)
        row.addWidget(self.rel_spin, 1); row.addWidget(b); row.addWidget(here)
        llay.addLayout(row)
        col.addWidget(lcard)

        # acquisition
        acard, alay = _card("Acquire (scan-safe sample)")
        row = QtWidgets.QHBoxLayout()
        self.readings_spin = QtWidgets.QSpinBox()
        self.readings_spin.setRange(self.cfg.limits.readings_min, self.cfg.limits.readings_max)
        self.readings_spin.setSuffix("  readings")
        b = QtWidgets.QPushButton("Set")
        b.clicked.connect(lambda: self.ctrl.set_acquisition(self.readings_spin.value()))
        row.addWidget(self.readings_spin, 1); row.addWidget(b)
        alay.addLayout(row)
        self.acq_btn = QtWidgets.QPushButton("Acquire"); self.acq_btn.setObjectName("primary")
        self.acq_btn.setMinimumHeight(36)
        self.acq_btn.clicked.connect(self._acquire)
        alay.addWidget(self.acq_btn)
        self.acq_bar = QtWidgets.QProgressBar(); self.acq_bar.setRange(0, 100)
        self.acq_bar.setTextVisible(False); self.acq_bar.setFixedHeight(6)
        alay.addWidget(self.acq_bar)
        self.sample_label = QtWidgets.QLabel("no sample yet"); self.sample_label.setObjectName("hint")
        self.sample_label.setWordWrap(True)
        alay.addWidget(self.sample_label)
        col.addWidget(acard)

        col.addStretch(1)
        self.zero_btn = QtWidgets.QPushButton("Zero probe (zero-gauss chamber)")
        self.zero_btn.setObjectName("danger"); self.zero_btn.setMinimumHeight(36)
        self.zero_btn.clicked.connect(self._zero)
        col.addWidget(self.zero_btn)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        fcard, flay = _card("Field")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(18)
        box = QtWidgets.QVBoxLayout(); box.setSpacing(2)
        line = QtWidgets.QHBoxLayout(); line.setSpacing(8)
        self.field_value = QtWidgets.QLabel("—"); self.field_value.setObjectName("bigValue")
        self.field_value.setStyleSheet("font-size: 54px;")
        # fixed width + right-aligned: the unit stays next to the digits and the
        # layout does not jump when the number of digits changes
        self.field_value.setMinimumWidth(280)
        self.field_value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.field_unit = QtWidgets.QLabel("mT"); self.field_unit.setObjectName("unit")
        self.field_unit.setStyleSheet("font-size: 22px;")
        line.addWidget(self.field_value); line.addWidget(self.field_unit, 0, QtCore.Qt.AlignBottom)
        line.addStretch(1)
        box.addLayout(line)
        self.field_sub = QtWidgets.QLabel("—"); self.field_sub.setObjectName("hint")
        box.addWidget(self.field_sub)
        self.rel_value = QtWidgets.QLabel(""); self.rel_value.setObjectName("hint")
        box.addWidget(self.rel_value)
        box.addStretch(1)
        row.addLayout(box, 1)
        self.indicator = AxialProbeIndicator(); self.indicator.setFixedWidth(260)
        row.addWidget(self.indicator)
        flay.addLayout(row)
        colw.addWidget(fcard)

        gcard, glay = _card("History")
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Window"))
        self.window_combo = QtWidgets.QComboBox(); self.window_combo.addItems(list(_WINDOWS_S))
        self.window_combo.setCurrentText("30 s")
        bar.addWidget(self.window_combo)
        self.pause_btn = QtWidgets.QPushButton("Pause"); self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(lambda on: setattr(self, "_paused", on))
        bar.addWidget(self.pause_btn)
        clear = QtWidgets.QPushButton("Clear"); clear.clicked.connect(self._hist.clear)
        bar.addWidget(clear)
        bar.addStretch(1)
        self.stats_label = QtWidgets.QLabel(""); self.stats_label.setObjectName("hint")
        bar.addWidget(self.stats_label)
        glay.addLayout(bar)
        self.plot, self.curve = self._make_plot()
        glay.addWidget(self.plot, 1)
        colw.addWidget(gcard, 3)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 1)
        return panel

    def _make_plot(self):
        import pyqtgraph as pg
        pg.setConfigOptions(antialias=True)
        w = pg.PlotWidget(background=COLORS["code_bg"])
        w.setMinimumHeight(180)
        pen = pg.mkPen(COLORS["muted"])
        for name in ("left", "bottom"):
            ax = w.getAxis(name); ax.setPen(pen); ax.setTextPen(pen)
        # The history is stored in tesla so pyqtgraph's own SI prefixing gives
        # "mT" / "uT" axis labels that follow the scale automatically.
        w.setLabel("left", "field", units="T")
        w.setLabel("bottom", "time", units="s")
        w.showGrid(x=True, y=True, alpha=0.15)
        curve = w.plot([], [], pen=pg.mkPen(COLORS["accent"], width=2))
        return w, curve

    # ---- actions ---------------------------------------------------------

    def _set_range(self):
        i = self.range_combo.currentIndex()
        if 0 <= i < len(self._ranges):
            self.ctrl.set_range(self._ranges[i])

    def _relative_here(self):
        try:
            self.ctrl.relative_here()
        except Exception as exc:
            self._on_event("warn", f"relative refused: {exc}")

    def _acquire(self):
        try:
            self.ctrl.acquire()
        except Exception as exc:
            self._on_event("warn", f"acquire refused: {exc}")

    def _zero(self):
        ok = QtWidgets.QMessageBox.question(
            self, "Zero the probe",
            "Put the probe tip in the ZERO-GAUSS CHAMBER and let it reach the "
            "chamber's temperature. Whatever field it sees now becomes the new "
            "zero.\n\nStart the zero?")
        if ok != QtWidgets.QMessageBox.Yes:
            return
        try:
            self.ctrl.zero()
        except Exception as exc:
            self._on_event("warn", f"zero refused: {exc}")

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
        """Input widgets follow settings changed ELSEWHERE (console, scan), but
        never while the user is typing in them. setCurrentIndex does not fire
        `activated`, so no feedback loop (gotcha #13)."""
        s = self.ctrl.status()
        if s.ranges_mT and list(s.ranges_mT) != self._ranges:
            self._ranges = list(s.ranges_mT)
            self.range_combo.clear()
            self.range_combo.addItems([fmt_mT(r) for r in self._ranges])
        if s.mode in MODES:
            self.mode_combo.setCurrentIndex(MODES.index(s.mode))
        if s.dc_digits in DC_DIGITS:
            self.digits_combo.setCurrentIndex(DC_DIGITS.index(s.dc_digits))
        if s.rms_band in RMS_BANDS:
            self.band_combo.setCurrentIndex(RMS_BANDS.index(s.rms_band))
        self.digits_combo.setVisible(s.mode == "dc")
        self.band_combo.setVisible(s.mode == "rms")
        if self._ranges and math.isfinite(s.range_mT) and (force or not self.range_combo.hasFocus()):
            k = min(range(len(self._ranges)), key=lambda i: abs(self._ranges[i] - s.range_mT))
            self.range_combo.setCurrentIndex(k)
        if math.isfinite(s.rel_setpoint_mT) and (force or not self.rel_spin.hasFocus()):
            self.rel_spin.setValue(s.rel_setpoint_mT)
        if force or not self.readings_spin.hasFocus():
            self.readings_spin.setValue(int(s.acq_readings))
        for chk, val in ((self.auto_chk, s.auto_range), (self.rel_chk, s.relative)):
            chk.blockSignals(True)
            chk.setChecked(bool(val))
            chk.blockSignals(False)

    def _refresh(self):
        s = self.ctrl.status()
        now = time.monotonic()

        if s.readings != self._last_reading and math.isfinite(s.field_mT):
            self._last_reading = s.readings
            self._hist.append((now, s.field_mT * 1e-3))       # stored in T, see _make_plot

        v, u = split_mT(s.field_mT)
        self.field_value.setText(v); self.field_unit.setText(u)
        gauss = s.field_mT * 10.0
        kind = "RMS" if s.mode == "rms" else "DC"
        self.field_sub.setText(
            f"{kind}   "
            + (f"= {gauss:.5g} G   " if math.isfinite(gauss) else "")
            + (f"{s.read_ms:.0f} ms/reading" if math.isfinite(s.read_ms) else ""))
        self.rel_value.setText(f"relative: {fmt_mT(s.field_rel_mT)} from {fmt_mT(s.rel_setpoint_mT)}"
                               if s.relative else "")
        self.field_value.setStyleSheet(
            f"font-size: 54px; color: {COLORS['danger'] if s.flag else COLORS['text']};")
        self.indicator.set_state(s.field_mT, s.range_mT, s.flag)

        # connection
        if s.hw_error:
            self.conn_dot.setText("●  hardware error")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
            self.conn_dot.setToolTip(s.hw_error)
        elif s.connected:
            self.conn_dot.setText("●  zeroing probe ..." if s.zeroing else "●  connected")
            self.conn_dot.setStyleSheet(
                f"color:{COLORS['accent'] if s.zeroing else COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        self.idn_label.setText((s.idn + (f"\nprobe {s.probe}" if s.probe else "")) if s.idn else "—")

        self.mode_label.setText(f"front panel in {s.display_unit}; an acquisition "
                                f"waits {s.settle_s:.3g} s for the filter")
        self.range_combo.setEnabled(not s.auto_range)
        self.range_set.setEnabled(not s.auto_range)
        self._sync_inputs()

        # acquisition
        self.acq_bar.setValue(int(100 * s.acq_progress) if s.acquiring else 0)
        self.acq_btn.setEnabled(bool(s.connected) and not s.acquiring and not s.zeroing)
        self.zero_btn.setEnabled(bool(s.connected) and not s.acquiring and not s.zeroing)
        smp = s.sample
        if smp and smp.get("acq_id") != self._last_acq:
            self._last_acq = smp.get("acq_id")
            self.sample_label.setText(
                f"#{smp.get('acq_id')}: {fmt_mT(smp.get('field_mT', float('nan')))} "
                f"± {fmt_mT(smp.get('std_mT', float('nan')))}  (n={smp.get('n')})"
                + (f"  {smp['flag'].upper()}" if smp.get("flag") else ""))

        if not self._paused:
            self._redraw_plot(now)

    def _redraw_plot(self, now: float):
        span = _WINDOWS_S[self.window_combo.currentText()]
        pts = [(t - now, b) for t, b in self._hist if now - t <= span]
        if pts:
            xs, ys = zip(*pts)
            self.curve.setData(list(xs), list(ys))
            mean = sum(ys) / len(ys)
            sd = math.sqrt(sum((y - mean) ** 2 for y in ys) / len(ys))
            self.stats_label.setText(f"mean {fmt_mT(mean * 1e3)}   sd {fmt_mT(sd * 1e3)}   "
                                     f"p-p {fmt_mT((max(ys) - min(ys)) * 1e3)}")
        else:
            self.curve.setData([], [])
            self.stats_label.setText("")
        self.plot.setXRange(-span, 0, padding=0)

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Gaussmeter-like object. The theme is chosen ONCE
    here, from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))
    # '.' decimal point and no thousands separator whatever the Windows locale
    # (suite gotcha #18: on the lab PC 10 ms showed as "10,000").
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
    """Run against the built-in simulator, in-process. Relative mode is switched
    on around the sim's 42 mT so a fresh window shows every readout working."""
    from ..config import Config
    from ..sim_system import build_sim_system
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    cfg.meter.relative = True
    cfg.meter.rel_setpoint_mT = 42.0
    cfg.hardware.push_on_start = True
    meter, _ = build_sim_system(cfg)
    # one acquisition shortly after start, so the sample line is not empty
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def first_sample():
        try:
            meter.acquire()
        except ValueError:
            pass
    QtCore.QTimer.singleShot(600, first_sample)
    del app
    return run_app(meter, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
