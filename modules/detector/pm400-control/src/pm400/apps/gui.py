"""Control GUI for the Thorlabs PM400 power / energy meter console.

    uv run scripts/run_gui.py                  # local simulator
    uv run scripts/run_gui.py --real           # the real console, in this process
    uv run scripts/run_gui.py --connect HOST   # a running service

The window holds a Pm400Meter-like object (an in-process Pm400Meter or a
Pm400Client facade) and never cares which. A 60 ms timer reads status();
events cross into the GUI thread on a Qt signal.

The panel follows the HEAD plugged into the console: a power head shows W,
auto range and the averaging time; a pyroelectric head shows J per pulse, the
energy range and the pulse rate; with no head the controls grey out.

Signature widget: SensorHeadIndicator -- the head seen from the front, drawn
as what is actually plugged in (a photodiode's small chip, a thermopile's
black absorber with heat rings that lag the light like the real one, a pyro
crystal that flashes on every pulse, or an empty connector), with the beam
arriving from the left (continuous for CW, discrete pulses for a pyro head)
and a bar showing how full the current range is.
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets

from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog
from .control_bar import ControlBar, mark_always


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


def split_value(value: float, unit: str = "W") -> tuple[str, str]:
    """3.3e-6, 'W' -> ('3.3000', 'µW'): 5 significant figures and a prefixed unit."""
    if not math.isfinite(value):
        return "--", unit or "W"
    for scale, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(value) >= scale:
            break
    else:
        scale, prefix = 1e-12, "p"
    v = value / scale
    decimals = max(0, 4 - int(math.floor(math.log10(abs(v)))) if v else 4)
    return f"{v:.{decimals}f}", f"{prefix}{unit or 'W'}"


def fmt_value(value: float, unit: str = "W") -> str:
    v, u = split_value(value, unit)
    return f"{v} {u}"


# ------------------------------------------------------------- the indicator

class SensorHeadIndicator(QtWidgets.QWidget):
    """The plugged-in head seen from the front, with the light arriving on it."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(160)
        self.setMinimumWidth(220)
        self._head = "none"
        self._value = float("nan")
        self._range = float("nan")
        self._unit = "W"
        self._flag = ""
        self._rate = float("nan")
        self._readings = -1
        self._phase = 0.0
        self._heat = 0.0          # displayed thermopile glow: LAGS the level
        self._flash = 0.0         # pyro crystal flash, decays after each pulse
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, head: str, value: float, range_: float, unit: str,
                  flag: str, readings: int, rate_Hz: float = float("nan")):
        # a new pyro reading = a new pulse: flash the crystal
        if head == "pyro" and readings != self._readings and math.isfinite(value):
            self._flash = 1.0
        self._head, self._value, self._range = head, value, range_
        self._unit, self._flag, self._readings, self._rate = unit, flag, readings, rate_Hz

    def level(self) -> float:
        """0..1 on a LOG scale over nine decades (1 nW..1 W, or 1 nJ..1 J): a
        linear glow would be black for everything but the top decade."""
        if not math.isfinite(self._value) or self._value <= 0:
            return 0.0
        return max(0.0, min(1.0, (math.log10(self._value) + 9.0) / 9.0))

    def _tick(self):
        self._phase = (self._phase + 0.03) % 1.0
        # the absorber warms up and cools down with a ~1 s time constant, like
        # the real thermopile (33 ms ticks -> factor 1 - exp(-0.033))
        self._heat += (self.level() - self._heat) * 0.033
        self._flash *= 0.85
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w * 0.60, h * 0.44
        r = min(w * 0.30, h * 0.34)
        accent = QtGui.QColor(COLORS["accent"])
        hi = QtGui.QColor(COLORS["accent_hi"])
        metal = QtGui.QColor("#5a626e")
        lvl = self.level()

        # -- the beam, arriving from the left
        if self._head != "none" and lvl > 0:
            bw = 4 + 8 * lvl
            if self._head == "pyro":
                # discrete pulses travelling towards the head
                p.setPen(QtCore.Qt.NoPen)
                for k in range(4):
                    x = ((k / 4 + self._phase) % 1.0) * (cx - r)
                    c = QtGui.QColor(hi); c.setAlpha(int(70 + 170 * lvl))
                    p.setBrush(c)
                    p.drawRoundedRect(QtCore.QRectF(x - 6, cy - bw / 2, 12, bw), 3, 3)
            else:
                grad = QtGui.QLinearGradient(0, cy, cx, cy)
                c0 = QtGui.QColor(accent); c0.setAlpha(0)
                c1 = QtGui.QColor(accent); c1.setAlpha(int(80 + 160 * lvl))
                grad.setColorAt(0.0, c0); grad.setColorAt(1.0, c1)
                p.setPen(QtCore.Qt.NoPen); p.setBrush(grad)
                p.drawRect(QtCore.QRectF(0, cy - bw / 2, cx - r * 0.3, bw))

        # -- the head itself
        if self._head == "none":
            self._paint_socket(p, cx, cy, r, metal)
        else:
            p.setPen(QtGui.QPen(metal, 2.6))
            p.setBrush(QtGui.QColor(COLORS["panel_hi"]))
            p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
            if self._head == "thermal":
                self._paint_thermal(p, cx, cy, r, accent)
            elif self._head == "pyro":
                self._paint_pyro(p, cx, cy, r, accent, hi)
            else:
                self._paint_photodiode(p, cx, cy, r, accent, lvl)

        # -- range fill bar under the head
        bar = QtCore.QRectF(cx - r, cy + r + 10, 2 * r, 6)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(COLORS["border"]))
        p.drawRoundedRect(bar, 3, 3)
        if math.isfinite(self._value) and math.isfinite(self._range) and self._range > 0:
            fill = max(0.0, min(1.0, self._value / self._range))
            p.setBrush(QtGui.QColor(COLORS["danger"] if self._flag == "overrange" or fill > 0.95
                                    else COLORS["ok"]))
            p.drawRoundedRect(QtCore.QRectF(bar.x(), bar.y(), bar.width() * fill, 6), 3, 3)

        # -- caption
        if self._flag and self._flag != "no_sensor":
            cap, col = self._flag.upper(), QtGui.QColor(COLORS["danger"])
        elif self._head == "none":
            cap, col = "NO HEAD", QtGui.QColor(COLORS["muted"])
        else:
            cap = self._head.upper()
            if math.isfinite(self._range):
                cap += f"  ·  range {fmt_value(self._range, self._unit)}"
            col = QtGui.QColor(COLORS["muted"])
        p.setPen(col)
        f = p.font(); f.setBold(True); f.setPointSize(8); p.setFont(f)
        p.drawText(QtCore.QRectF(0, h - 16, w, 14), QtCore.Qt.AlignHCenter, cap)
        p.end()

    # the four heads ------------------------------------------------------------
    def _paint_photodiode(self, p, cx, cy, r, accent, lvl):
        """A small Si chip behind a window: glows with the log level."""
        glow = QtGui.QRadialGradient(cx, cy, r * 0.7)
        g0 = QtGui.QColor(accent); g0.setAlpha(int(30 + 225 * lvl))
        g1 = QtGui.QColor(accent); g1.setAlpha(0)
        glow.setColorAt(0.0, g0); glow.setColorAt(1.0, g1)
        p.setPen(QtCore.Qt.NoPen); p.setBrush(glow)
        p.drawEllipse(QtCore.QPointF(cx, cy), r * 0.7, r * 0.7)
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["muted"]), 1.2))
        p.setBrush(QtCore.Qt.NoBrush)
        s = r * 0.32
        p.drawRect(QtCore.QRectF(cx - s, cy - s, 2 * s, 2 * s))

    def _paint_thermal(self, p, cx, cy, r, accent):
        """A black absorber whose heat rings spread outward; the glow follows
        the light with a lag, as a thermopile does."""
        heat = self._heat
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(COLORS["code_bg"]))
        p.drawEllipse(QtCore.QPointF(cx, cy), r * 0.82, r * 0.82)
        core = QtGui.QRadialGradient(cx, cy, r * 0.82)
        c0 = QtGui.QColor(accent); c0.setAlpha(int(20 + 200 * heat))
        c1 = QtGui.QColor(accent); c1.setAlpha(0)
        core.setColorAt(0.0, c0); core.setColorAt(1.0, c1)
        p.setBrush(core)
        p.drawEllipse(QtCore.QPointF(cx, cy), r * 0.82, r * 0.82)
        p.setBrush(QtCore.Qt.NoBrush)
        for k in range(3):
            frac = (k / 3 + self._phase * 0.5) % 1.0
            c = QtGui.QColor(accent); c.setAlpha(int((1 - frac) * 160 * heat))
            p.setPen(QtGui.QPen(c, 1.6))
            rr = r * (0.15 + 0.65 * frac)
            p.drawEllipse(QtCore.QPointF(cx, cy), rr, rr)

    def _paint_pyro(self, p, cx, cy, r, accent, hi):
        """A square pyroelectric crystal that flashes on every pulse."""
        s = r * 0.5
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["muted"]), 1.2))
        p.setBrush(QtGui.QColor(COLORS["code_bg"]))
        p.drawRect(QtCore.QRectF(cx - s, cy - s, 2 * s, 2 * s))
        if self._flash > 0.02:
            c = QtGui.QColor(hi); c.setAlpha(int(230 * self._flash))
            p.setPen(QtCore.Qt.NoPen); p.setBrush(c)
            p.drawRect(QtCore.QRectF(cx - s + 2, cy - s + 2, 2 * s - 4, 2 * s - 4))
            halo = QtGui.QColor(accent); halo.setAlpha(int(120 * self._flash))
            p.setPen(QtGui.QPen(halo, 3)); p.setBrush(QtCore.Qt.NoBrush)
            p.drawEllipse(QtCore.QPointF(cx, cy), r * 0.95, r * 0.95)

    def _paint_socket(self, p, cx, cy, r, metal):
        """Nothing plugged in: the console's empty D-sub connector."""
        p.setPen(QtGui.QPen(metal, 2.4))
        p.setBrush(QtGui.QColor(COLORS["panel_hi"]))
        path = QtGui.QPainterPath()
        top, bot = r * 1.0, r * 0.8          # a trapezoid, wider on top
        path.moveTo(cx - top, cy - r * 0.45)
        path.lineTo(cx + top, cy - r * 0.45)
        path.lineTo(cx + bot, cy + r * 0.45)
        path.lineTo(cx - bot, cy + r * 0.45)
        path.closeSubpath()
        p.drawPath(path)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(COLORS["muted"]))
        for row, n in ((-0.15, 5), (0.18, 4)):
            for i in range(n):
                x = cx + (i - (n - 1) / 2) * r * 0.36
                p.drawEllipse(QtCore.QPointF(x, cy + row * r), 2.2, 2.2)


# ------------------------------------------------------------- main window

_WINDOWS_S = {"10 s": 10, "30 s": 30, "1 min": 60, "5 min": 300}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self.setWindowTitle("PM400 - Optical Power / Energy Meter" + ("  (remote)" if remote else ""))
        self.resize(1140, 740)

        # one history for the plot: (monotonic time, value); cleared on a head change
        self._hist: deque = deque(maxlen=6000)
        self._last_reading = -1
        self._last_acq = 0
        self._last_head = None
        self._paused = False

        root = QtWidgets.QWidget(); root.setObjectName("root")
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)
        # Control or viewer (control_bar.py): a bar across the top, only for a
        # GUI on a service whose client knows about control -- a local GUI
        # owns its meter and has nobody to share it with.
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
        except Exception as exc:          # e.g. no console plugged in: show it, don't crash
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
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("PM400")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        mark_always(settings_btn)    # a viewer may LOOK; the service refuses the OK
        header.addWidget(settings_btn)
        col.addLayout(header)

        ccard, clay = _card("Console & head")
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        clay.addWidget(self.conn_dot)
        self.head_label = QtWidgets.QLabel("—")
        self.head_label.setStyleSheet("font-weight:700;")
        clay.addWidget(self.head_label)
        self.idn_label = QtWidgets.QLabel("—"); self.idn_label.setObjectName("hint")
        self.idn_label.setWordWrap(True)
        clay.addWidget(self.idn_label)
        col.addWidget(ccard)

        # wavelength
        wcard, wlay = _card("Correction wavelength")
        row = QtWidgets.QHBoxLayout()
        self.wl_spin = QtWidgets.QDoubleSpinBox()
        self.wl_spin.setDecimals(1); self.wl_spin.setSingleStep(1.0); self.wl_spin.setSuffix("  nm")
        self.wl_spin.setRange(self.cfg.limits.wavelength_min_nm, self.cfg.limits.wavelength_max_nm)
        self.wl_set = QtWidgets.QPushButton("Set"); self.wl_set.setObjectName("primary")
        self.wl_set.clicked.connect(lambda: self._call(self.ctrl.set_wavelength, self.wl_spin.value()))
        row.addWidget(self.wl_spin, 1); row.addWidget(self.wl_set)
        wlay.addLayout(row)
        self.wl_hint = QtWidgets.QLabel("—"); self.wl_hint.setObjectName("hint")
        wlay.addWidget(self.wl_hint)
        col.addWidget(wcard)

        # range + averaging
        rcard, rlay = _card("Range & averaging")
        self.auto_chk = QtWidgets.QCheckBox("Auto range")
        self.auto_chk.clicked.connect(lambda on: self._call(self.ctrl.set_auto_range, on))  # user only
        rlay.addWidget(self.auto_chk)
        row = QtWidgets.QHBoxLayout()
        self.range_spin = QtWidgets.QDoubleSpinBox()
        self.range_spin.setDecimals(4); self.range_spin.setSuffix("  mW")
        self.range_spin.setRange(0.0, self.cfg.limits.range_max_W * 1e3)
        self.range_set = QtWidgets.QPushButton("Set"); self.range_set.setObjectName("primary")
        self.range_set.clicked.connect(
            lambda: self._call(self.ctrl.set_range, self.range_spin.value() * 1e-3))
        row.addWidget(self.range_spin, 1); row.addWidget(self.range_set)
        rlay.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        self.avg_spin = QtWidgets.QDoubleSpinBox()
        self.avg_spin.setDecimals(1); self.avg_spin.setSuffix("  ms avg")
        self.avg_spin.setRange(self.cfg.limits.avg_time_min_s * 1e3, self.cfg.limits.avg_time_max_s * 1e3)
        self.avg_set = QtWidgets.QPushButton("Set")
        self.avg_set.clicked.connect(
            lambda: self._call(self.ctrl.set_avg_time, self.avg_spin.value() * 1e-3))
        row.addWidget(self.avg_spin, 1); row.addWidget(self.avg_set)
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
        self.settle_spin = QtWidgets.QDoubleSpinBox()
        self.settle_spin.setDecimals(1); self.settle_spin.setSuffix("  s settle")
        self.settle_spin.setRange(0.0, self.cfg.limits.settle_max_s)
        b = QtWidgets.QPushButton("Set")
        b.clicked.connect(self._set_acquisition)
        row.addWidget(self.readings_spin, 1); row.addWidget(self.settle_spin, 1); row.addWidget(b)
        alay.addLayout(row)
        self.acq_btn = QtWidgets.QPushButton("Acquire"); self.acq_btn.setObjectName("primary")
        self.acq_btn.setMinimumHeight(34)
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
        self.zero_btn = QtWidgets.QPushButton("Zero (cover the head)")
        self.zero_btn.setMinimumHeight(34)
        self.zero_btn.clicked.connect(self._zero)
        col.addWidget(self.zero_btn)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        pcard, play = _card(None)
        self.value_title = QtWidgets.QLabel("POWER"); self.value_title.setObjectName("cardTitle")
        play.addWidget(self.value_title)
        row = QtWidgets.QHBoxLayout(); row.setSpacing(18)
        box = QtWidgets.QVBoxLayout(); box.setSpacing(2)
        line = QtWidgets.QHBoxLayout(); line.setSpacing(8)
        self.value_label = QtWidgets.QLabel("—"); self.value_label.setObjectName("bigValue")
        self.value_label.setStyleSheet("font-size: 54px;")
        # fixed width + right-aligned: the unit stays next to the digits and the
        # layout does not jump when the number of digits changes
        self.value_label.setMinimumWidth(260)
        self.value_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.unit_label = QtWidgets.QLabel("W"); self.unit_label.setObjectName("unit")
        self.unit_label.setStyleSheet("font-size: 22px;")
        line.addWidget(self.value_label); line.addWidget(self.unit_label, 0, QtCore.Qt.AlignBottom)
        line.addStretch(1)
        box.addLayout(line)
        self.value_sub = QtWidgets.QLabel("—"); self.value_sub.setObjectName("hint")
        box.addWidget(self.value_sub)
        box.addStretch(1)
        row.addLayout(box, 1)
        self.indicator = SensorHeadIndicator(); self.indicator.setFixedWidth(240)
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
        # the plot's own view (window, pause, clear) changes nothing on the
        # meter: fine for a viewer
        mark_always(self.window_combo, self.pause_btn, clear)
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

    def _call(self, fn, *args):
        """Run a setter; a refusal (e.g. no head) goes to the log, not a crash."""
        try:
            r = fn(*args)
        except Exception as exc:
            self._on_event("warn", f"refused: {exc}")
            return
        if isinstance(r, dict) and not r.get("ok", True):
            self._on_event("warn", f"refused: {r.get('error')}")

    def _set_acquisition(self):
        self._call(self.ctrl.set_acquisition, self.readings_spin.value())
        self._call(self.ctrl.set_settle, self.settle_spin.value())

    def _acquire(self):
        try:
            self.ctrl.acquire()
        except Exception as exc:
            self._on_event("warn", f"acquire refused: {exc}")

    def _zero(self):
        ok = QtWidgets.QMessageBox.question(
            self, "Zero the head",
            "Cover the sensor head completely. Whatever light reaches it now becomes "
            "the new zero.\n\nStart the zero adjustment?")
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
        prefix = "mJ" if s.quantity == "energy" else "mW"
        self.range_spin.setSuffix(f"  {prefix}")
        for spin, lo, hi, k in ((self.wl_spin, s.wavelength_min_nm, s.wavelength_max_nm, 1.0),
                                (self.range_spin, s.range_min, s.range_max, 1e3),
                                (self.avg_spin, s.avg_time_min_s, s.avg_time_max_s, 1e3)):
            if math.isfinite(lo) and math.isfinite(hi):
                spin.setRange(lo * k, hi * k)
        for spin, val in ((self.wl_spin, s.wavelength_set_nm),
                          (self.range_spin, s.range_set * 1e3),
                          (self.avg_spin, s.avg_time_set_s * 1e3)):
            if math.isfinite(val) and (force or not spin.hasFocus()):
                spin.setValue(val)
        if force or not self.readings_spin.hasFocus():
            self.readings_spin.setValue(int(s.acq_readings))
        if force or not self.settle_spin.hasFocus():
            self.settle_spin.setValue(float(s.acq_settle_s))
        self.auto_chk.blockSignals(True)
        self.auto_chk.setChecked(bool(s.auto_range))
        self.auto_chk.blockSignals(False)

    def _refresh(self):
        if self._control_bar is not None:
            self._control_bar.refresh()
        s = self.ctrl.status()
        now = time.monotonic()
        unit = s.unit or "W"
        energy = s.quantity == "energy"
        has_head = s.quantity != "none"

        # a different head measures something else: start a fresh history
        if s.head != self._last_head:
            self._last_head = s.head
            self._hist.clear()
            self.plot.setLabel("left", "energy" if energy else "power", units=unit)
            self._sync_inputs(force=True)

        if s.readings != self._last_reading and math.isfinite(s.value):
            self._last_reading = s.readings
            self._hist.append((now, s.value))

        v, u = split_value(s.value, unit)
        self.value_label.setText(v); self.unit_label.setText(u)
        self.value_title.setText("ENERGY PER PULSE" if energy else "POWER")
        if energy:
            rate = s.rep_rate_Hz
            avg_p = s.value * rate if math.isfinite(rate) and math.isfinite(s.value) else float("nan")
            sub = (f"{rate:.2f} Hz   " if math.isfinite(rate) else "") + (
                f"avg power {fmt_value(avg_p, 'W')}   " if math.isfinite(avg_p) else "")
        else:
            dbm = 10 * math.log10(s.value / 1e-3) if s.value > 0 else float("nan")
            sub = f"{dbm:.2f} dBm   " if math.isfinite(dbm) else ""
        if has_head:
            sub += f"at {s.wavelength_nm:g} nm   " + (
                f"{s.read_ms:.0f} ms/reading" if math.isfinite(s.read_ms) else "")
        else:
            sub = "plug a sensor head into the console"
        self.value_sub.setText(sub)
        bad = s.flag and s.flag != "no_sensor"
        self.value_label.setStyleSheet(
            f"font-size: 54px; color: {COLORS['danger'] if bad else COLORS['text']};")
        self.indicator.set_state(s.head, s.value, s.range, unit, s.flag, s.readings,
                                 s.rep_rate_Hz)

        # connection + head
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
        self.head_label.setText(f"{s.head} head: {s.sensor}" if has_head else "no sensor head")
        self.idn_label.setText(s.idn or "—")

        # sensor controls follow the head
        self.wl_spin.setEnabled(has_head and s.wavelength_settable)
        self.wl_set.setEnabled(has_head and s.wavelength_settable)
        self.wl_hint.setText(
            f"head calibrated {s.wavelength_min_nm:g}..{s.wavelength_max_nm:g} nm"
            if has_head and math.isfinite(s.wavelength_min_nm) else "—")
        self.auto_chk.setVisible(not energy)
        self.auto_chk.setEnabled(has_head)
        manual = has_head and (energy or not s.auto_range)
        self.range_spin.setEnabled(manual)
        self.range_set.setEnabled(manual)
        self.avg_spin.setVisible(not energy); self.avg_set.setVisible(not energy)
        self.avg_spin.setEnabled(has_head); self.avg_set.setEnabled(has_head)
        if has_head:
            avg = (f", averaging {s.avg_time_s * 1e3:.4g} ms"
                   if math.isfinite(s.avg_time_s) else "")
            self.range_label.setText(f"in use: {fmt_value(s.range, unit)}{avg}")
        else:
            self.range_label.setText("—")
        self._sync_inputs()

        # acquisition + zero
        self.acq_bar.setValue(int(100 * s.acq_progress) if s.acquiring else 0)
        self.acq_btn.setEnabled(bool(s.connected) and has_head
                                and not s.acquiring and not s.zeroing)
        self.zero_btn.setEnabled(bool(s.connected) and s.zero_supported
                                 and not s.acquiring and not s.zeroing)
        self.zero_btn.setToolTip("" if s.zero_supported else
                                 "this head has no zero adjustment")
        smp = s.sample
        if smp and smp.get("acq_id") != self._last_acq:
            self._last_acq = smp.get("acq_id")
            su = smp.get("unit") or unit
            self.sample_label.setText(
                f"#{smp.get('acq_id')}: {fmt_value(smp.get('value', float('nan')), su)} "
                f"± {fmt_value(smp.get('std', float('nan')), su)}  (n={smp.get('n')})"
                + (f"  {smp['flag'].upper()}" if smp.get("flag") else ""))

        if not self._paused:
            self._redraw_plot(now, unit)

    def _redraw_plot(self, now: float, unit: str):
        span = _WINDOWS_S[self.window_combo.currentText()]
        pts = [(t - now, p) for t, p in self._hist if now - t <= span]
        if pts:
            xs, ys = zip(*pts)
            self.curve.setData(list(xs), list(ys))
            mean = sum(ys) / len(ys)
            sd = math.sqrt(sum((y - mean) ** 2 for y in ys) / len(ys))
            self.stats_label.setText(
                f"mean {fmt_value(mean, unit)}   sd {fmt_value(sd, unit)}   "
                f"min {fmt_value(min(ys), unit)}   max {fmt_value(max(ys), unit)}")
        else:
            self.curve.setData([], [])
            self.stats_label.setText("")
        self.plot.setXRange(-span, 0, padding=0)

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Pm400Meter-like object. The theme is chosen ONCE
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
