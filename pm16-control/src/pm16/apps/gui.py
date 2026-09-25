"""Control GUI for the Thorlabs PM16 power meter.

    uv run scripts/run_gui.py                  # local simulator
    uv run scripts/run_gui.py --real           # the real meter, in this process
    uv run scripts/run_gui.py --connect HOST   # a running service

The window holds a PowerMeter-like object (an in-process PowerMeter or a
Pm16Client facade) and never cares which. A 60 ms timer reads status();
events cross into the GUI thread on a Qt signal.

Signature widget: PhotodiodeIndicator -- a beam landing on the sensor disc,
glowing brighter on a LOG scale (a power meter spans decades, a linear glow
would be black for everything but the top one), with an arc showing how full
the current range is.
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets

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


def split_W(watts: float) -> tuple[str, str]:
    """3.3e-6 -> ('3.3000', 'uW'): a value with 5 significant figures and its unit."""
    if not math.isfinite(watts):
        return "--", "W"
    for scale, unit in ((1.0, "W"), (1e-3, "mW"), (1e-6, "µW"), (1e-9, "nW")):
        if abs(watts) >= scale:
            break
    else:
        scale, unit = 1e-12, "pW"
    v = watts / scale
    decimals = max(0, 4 - int(math.floor(math.log10(abs(v)))) if v else 4)
    return f"{v:.{decimals}f}", unit


def fmt_W(watts: float) -> str:
    v, u = split_W(watts)
    return f"{v} {u}"


# ------------------------------------------------------------- the indicator

class PhotodiodeIndicator(QtWidgets.QWidget):
    """A beam hitting the photodiode; glow on a log scale, arc = range fill."""

    LOG_MIN, LOG_MAX = -9.0, 0.0            # 1 nW ... 1 W maps to 0 ... 1

    def __init__(self):
        super().__init__()
        self.setFixedHeight(150)
        self.setMinimumWidth(170)
        self._power = float("nan")
        self._range = float("nan")
        self._flag = ""
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, power_W: float, range_W: float, flag: str):
        self._power, self._range, self._flag = power_W, range_W, flag

    def level(self) -> float:
        if not math.isfinite(self._power) or self._power <= 0:
            return 0.0
        x = (math.log10(self._power) - self.LOG_MIN) / (self.LOG_MAX - self.LOG_MIN)
        return max(0.0, min(1.0, x))

    def _tick(self):
        self._phase = (self._phase + 0.03) % 1.0
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h * 0.56
        r = min(w, h) * 0.26
        accent = QtGui.QColor(COLORS["accent"])
        lvl = self.level()

        # -- the beam, coming in from the top, with travelling pulses
        if lvl > 0:
            beam_w = 6 + 6 * lvl
            grad = QtGui.QLinearGradient(cx, 0, cx, cy)
            c0 = QtGui.QColor(accent); c0.setAlpha(0)
            c1 = QtGui.QColor(accent); c1.setAlpha(int(90 + 150 * lvl))
            grad.setColorAt(0.0, c0); grad.setColorAt(1.0, c1)
            p.setPen(QtCore.Qt.NoPen); p.setBrush(grad)
            p.drawRect(QtCore.QRectF(cx - beam_w / 2, 0, beam_w, cy - r * 0.2))
            hi = QtGui.QColor(COLORS["accent_hi"])
            for k in range(3):
                y = ((k / 3 + self._phase) % 1.0) * (cy - r)
                hi.setAlpha(int(60 + 140 * lvl))
                p.setBrush(hi)
                p.drawEllipse(QtCore.QPointF(cx, y), beam_w * 0.35, beam_w * 0.6)

        # -- range fill arc (how close to the top of the range we are)
        def round_pen(colour):
            pen = QtGui.QPen(QtGui.QColor(colour), 5)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            return pen

        p.setBrush(QtCore.Qt.NoBrush)
        p.setPen(round_pen(COLORS["border"]))
        arc = QtCore.QRectF(cx - r - 14, cy - r - 14, 2 * (r + 14), 2 * (r + 14))
        p.drawArc(arc, 225 * 16, -270 * 16)
        if math.isfinite(self._power) and math.isfinite(self._range) and self._range > 0:
            fill = max(0.0, min(1.0, self._power / self._range))
            p.setPen(round_pen(COLORS["danger"] if self._flag == "overrange" or fill > 0.95
                               else COLORS["ok"]))
            p.drawArc(arc, 225 * 16, int(-270 * 16 * fill))

        # -- the sensor: a metal can with the active area glowing
        p.setPen(QtGui.QPen(QtGui.QColor("#5a626e"), 2.4))
        p.setBrush(QtGui.QColor(COLORS["panel_hi"]))
        p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
        glow = QtGui.QRadialGradient(cx, cy, r * 0.8)
        g0 = QtGui.QColor(accent); g0.setAlpha(int(40 + 215 * lvl))
        g1 = QtGui.QColor(accent); g1.setAlpha(0)
        glow.setColorAt(0.0, g0); glow.setColorAt(1.0, g1)
        p.setPen(QtCore.Qt.NoPen); p.setBrush(glow)
        p.drawEllipse(QtCore.QPointF(cx, cy), r * 0.8, r * 0.8)
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["muted"]), 1.2))
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawRect(QtCore.QRectF(cx - r * 0.35, cy - r * 0.35, r * 0.7, r * 0.7))

        # -- caption
        if self._flag:
            cap, col = self._flag.upper(), QtGui.QColor(COLORS["danger"])
        elif math.isfinite(self._range):
            cap, col = f"range {fmt_W(self._range)}", QtGui.QColor(COLORS["muted"])
        else:
            cap, col = "no reading", QtGui.QColor(COLORS["muted"])
        p.setPen(col)
        f = p.font(); f.setBold(True); f.setPointSize(8); p.setFont(f)
        p.drawText(QtCore.QRectF(0, h - 15, w, 13), QtCore.Qt.AlignHCenter, cap)
        p.end()


# ------------------------------------------------------------- main window

_WINDOWS_S = {"10 s": 10, "30 s": 30, "1 min": 60, "5 min": 300}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self.setWindowTitle("PM16 - Optical Power Meter" + ("  (remote)" if remote else ""))
        self.resize(1120, 700)

        # one history for the plot: (monotonic time, W), ~5 min at 17 Hz
        self._hist: deque = deque(maxlen=6000)
        self._last_reading = -1
        self._last_acq = 0
        self._paused = False

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
        title = QtWidgets.QLabel("PM16")
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

        # wavelength
        wcard, wlay = _card("Wavelength")
        row = QtWidgets.QHBoxLayout()
        self.wl_spin = QtWidgets.QDoubleSpinBox()
        self.wl_spin.setDecimals(1); self.wl_spin.setSingleStep(1.0); self.wl_spin.setSuffix("  nm")
        self.wl_spin.setRange(self.cfg.limits.wavelength_min_nm, self.cfg.limits.wavelength_max_nm)
        b = QtWidgets.QPushButton("Set"); b.setObjectName("primary")
        b.clicked.connect(lambda: self.ctrl.set_wavelength(self.wl_spin.value()))
        row.addWidget(self.wl_spin, 1); row.addWidget(b)
        wlay.addLayout(row)
        col.addWidget(wcard)

        # range
        rcard, rlay = _card("Range")
        self.auto_chk = QtWidgets.QCheckBox("Auto range")
        self.auto_chk.clicked.connect(lambda on: self.ctrl.set_auto_range(on))  # .clicked: user only
        rlay.addWidget(self.auto_chk)
        row = QtWidgets.QHBoxLayout()
        self.range_spin = QtWidgets.QDoubleSpinBox()
        self.range_spin.setDecimals(4); self.range_spin.setSuffix("  mW")
        self.range_spin.setRange(0.0, self.cfg.limits.range_max_W * 1e3)
        self.range_set = QtWidgets.QPushButton("Set"); self.range_set.setObjectName("primary")
        self.range_set.clicked.connect(lambda: self.ctrl.set_range(self.range_spin.value() * 1e-3))
        row.addWidget(self.range_spin, 1); row.addWidget(self.range_set)
        rlay.addLayout(row)
        self.range_label = QtWidgets.QLabel("—"); self.range_label.setObjectName("hint")
        rlay.addWidget(self.range_label)
        col.addWidget(rcard)

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
        self.zero_btn = QtWidgets.QPushButton("Zero (cover sensor)")
        self.zero_btn.setObjectName("danger"); self.zero_btn.setMinimumHeight(36)
        self.zero_btn.clicked.connect(self._zero)
        col.addWidget(self.zero_btn)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        pcard, play = _card("Power")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(18)
        box = QtWidgets.QVBoxLayout(); box.setSpacing(2)
        line = QtWidgets.QHBoxLayout(); line.setSpacing(8)
        self.power_value = QtWidgets.QLabel("—"); self.power_value.setObjectName("bigValue")
        self.power_value.setStyleSheet("font-size: 54px;")
        # fixed width + right-aligned: the unit stays next to the digits and the
        # layout does not jump when the number of digits changes
        self.power_value.setMinimumWidth(260)
        self.power_value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.power_unit = QtWidgets.QLabel("W"); self.power_unit.setObjectName("unit")
        self.power_unit.setStyleSheet("font-size: 22px;")
        line.addWidget(self.power_value); line.addWidget(self.power_unit, 0, QtCore.Qt.AlignBottom)
        line.addStretch(1)
        box.addLayout(line)
        self.power_sub = QtWidgets.QLabel("—"); self.power_sub.setObjectName("hint")
        box.addWidget(self.power_sub)
        box.addStretch(1)
        row.addLayout(box, 1)
        self.indicator = PhotodiodeIndicator(); self.indicator.setFixedWidth(200)
        row.addWidget(self.indicator)
        play.addLayout(row)
        colw.addWidget(pcard)

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
        w.setLabel("left", "power", units="W")      # pyqtgraph adds the SI prefix itself
        w.setLabel("bottom", "time", units="s")
        w.showGrid(x=True, y=True, alpha=0.15)
        curve = w.plot([], [], pen=pg.mkPen(COLORS["accent"], width=2))
        return w, curve

    # ---- actions ---------------------------------------------------------

    def _acquire(self):
        try:
            self.ctrl.acquire()
        except Exception as exc:
            self._on_event("warn", f"acquire refused: {exc}")

    def _zero(self):
        ok = QtWidgets.QMessageBox.question(
            self, "Zero the sensor",
            "Cover the sensor completely. Whatever light reaches it now becomes the "
            "new zero.\n\nStart the dark adjustment?")
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
        """Input boxes follow setpoints changed ELSEWHERE (console, scan), but
        never while the user is typing in them."""
        s = self.ctrl.status()
        lo, hi = s.wavelength_min_nm, s.wavelength_max_nm
        if math.isfinite(lo) and math.isfinite(hi):
            self.wl_spin.setRange(lo, hi)
        if math.isfinite(s.range_min_W) and math.isfinite(s.range_max_W):
            self.range_spin.setRange(s.range_min_W * 1e3, s.range_max_W * 1e3)
        for spin, val in ((self.wl_spin, s.wavelength_set_nm),
                          (self.range_spin, s.range_set_W * 1e3)):
            if math.isfinite(val) and (force or not spin.hasFocus()):
                spin.setValue(val)
        if force or not self.readings_spin.hasFocus():
            self.readings_spin.setValue(int(s.acq_readings))
        self.auto_chk.blockSignals(True)
        self.auto_chk.setChecked(bool(s.auto_range))
        self.auto_chk.blockSignals(False)

    def _refresh(self):
        s = self.ctrl.status()
        now = time.monotonic()

        if s.readings != self._last_reading and math.isfinite(s.power_W):
            self._last_reading = s.readings
            self._hist.append((now, s.power_W))

        v, u = split_W(s.power_W)
        self.power_value.setText(v); self.power_unit.setText(u)
        dbm = 10 * math.log10(s.power_W / 1e-3) if s.power_W > 0 else float("nan")
        self.power_sub.setText(
            (f"{dbm:.2f} dBm   " if math.isfinite(dbm) else "")
            + f"at {s.wavelength_nm:g} nm   "
            + (f"{s.read_ms:.0f} ms/reading" if math.isfinite(s.read_ms) else ""))
        self.power_value.setStyleSheet(
            f"font-size: 54px; color: {COLORS['danger'] if s.flag else COLORS['text']};")
        self.indicator.set_state(s.power_W, s.range_W, s.flag)

        # connection
        if s.hw_error:
            self.conn_dot.setText("●  hardware error")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
            self.conn_dot.setToolTip(s.hw_error)
        elif s.connected:
            self.conn_dot.setText("●  zeroing ..." if s.zeroing else "●  connected")
            self.conn_dot.setStyleSheet(
                f"color:{COLORS['accent'] if s.zeroing else COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        self.idn_label.setText(s.idn + (f"\nsensor {s.sensor}" if s.sensor else "") if s.idn else "—")

        # range
        self.range_spin.setEnabled(not s.auto_range)
        self.range_set.setEnabled(not s.auto_range)
        avg = f", averages {s.average_time_s * 1e3:.0f} ms" if math.isfinite(s.average_time_s) else ""
        self.range_label.setText(f"in use: {fmt_W(s.range_W)}{avg}")
        self._sync_inputs()

        # acquisition
        self.acq_bar.setValue(int(100 * s.acq_progress) if s.acquiring else 0)
        self.acq_btn.setEnabled(bool(s.connected) and not s.acquiring and not s.zeroing)
        self.zero_btn.setEnabled(bool(s.connected) and not s.acquiring and not s.zeroing)
        smp = s.sample
        if smp and smp.get("acq_id") != self._last_acq:
            self._last_acq = smp.get("acq_id")
            self.sample_label.setText(
                f"#{smp.get('acq_id')}: {fmt_W(smp.get('power_W', float('nan')))} "
                f"± {fmt_W(smp.get('std_W', float('nan')))}  (n={smp.get('n')})"
                + (f"  {smp['flag'].upper()}" if smp.get("flag") else ""))

        if not self._paused:
            self._redraw_plot(now)

    def _redraw_plot(self, now: float):
        span = _WINDOWS_S[self.window_combo.currentText()]
        pts = [(t - now, p) for t, p in self._hist if now - t <= span]
        if pts:
            xs, ys = zip(*pts)
            self.curve.setData(list(xs), list(ys))
            mean = sum(ys) / len(ys)
            sd = math.sqrt(sum((y - mean) ** 2 for y in ys) / len(ys))
            self.stats_label.setText(f"mean {fmt_W(mean)}   sd {fmt_W(sd)}   min {fmt_W(min(ys))}   max {fmt_W(max(ys))}")
        else:
            self.curve.setData([], [])
            self.stats_label.setText("")
        self.plot.setXRange(-span, 0, padding=0)

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a PowerMeter-like object. The theme is chosen ONCE
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
    """Run against the built-in simulator, in-process."""
    from ..config import Config
    from ..sim_system import build_sim_system
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    meter, _ = build_sim_system(cfg)
    return run_app(meter, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
