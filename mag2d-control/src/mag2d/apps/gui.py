"""Control GUI for the 2-axis vector magnet.

    uv run scripts/run_gui.py                     # local simulator, in this process
    uv run scripts/run_gui.py --connect HOST      # a running service

The window holds a Controller-like object (an in-process Controller or a
Mag2dClient) and never cares which. A 50 ms timer reads status(); events cross
from the control thread into the GUI thread on a Qt signal (Bridge), because Qt
widgets may only be touched from the GUI thread.

Signature widget: VectorDial -- the field as an arrow in the XY plane. The big
ring is field_max, the dim arrow is where you asked the field to be, the glowing
amber arrow is where the Hall probes say it is, and the small ring at the
setpoint tip is the tolerance band (green once the field has been inside it for
stable_time). A glance tells you magnitude, angle, and whether it has arrived.
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from ..config import Config
from ..controller import Refused
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog


class Bridge(QtCore.QObject):
    """Carries controller events across the thread boundary into the GUI."""
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


def _fmt(v, spec: str = ".2f", dash: str = "--") -> str:
    try:
        return format(v, spec) if math.isfinite(v) else dash
    except (TypeError, ValueError):
        return dash


def _qcolor(key: str, alpha: int | None = None) -> QtGui.QColor:
    c = QtGui.QColor(COLORS[key])
    if alpha is not None:
        c.setAlpha(alpha)
    return c


# ------------------------------------------------------------ the indicator

class VectorDial(QtWidgets.QWidget):
    """The field vector on a polar dial (see the module docstring)."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(300, 300)
        self._sp = (0.0, 0.0)
        self._meas = (float("nan"), float("nan"))
        self._fmax = 180.0
        self._tol = 0.5
        self._energized = False
        self._stable = False
        self._phase = 0.0
        # Own animation timer, independent of the status poll, so the glow
        # pulses smoothly; it only runs while the output is energized.
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, sp_bx, sp_by, bx, by, field_max, tol, energized, stable):
        self._sp = (sp_bx, sp_by)
        self._meas = (bx, by)
        self._fmax = max(1e-6, abs(field_max))
        self._tol = abs(tol)
        self._energized = bool(energized)
        self._stable = bool(stable)
        if self._energized and not self._timer.isActive():
            self._timer.start()
        elif not self._energized and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        self._phase = (self._phase + 0.04) % 1.0
        self.update()

    def _arrow(self, p, cx, cy, x, y, width, color, head=10.0):
        p.setPen(QtGui.QPen(color, width, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
        p.drawLine(QtCore.QPointF(cx, cy), QtCore.QPointF(x, y))
        length = math.hypot(x - cx, y - cy)
        if length < 4:
            return
        ux, uy = (x - cx) / length, (y - cy) / length
        h = min(head, length * 0.6)
        left = QtCore.QPointF(x - h * ux + 0.5 * h * uy, y - h * uy - 0.5 * h * ux)
        right = QtCore.QPointF(x - h * ux - 0.5 * h * uy, y - h * uy + 0.5 * h * ux)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(color)
        p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(x, y), left, right]))

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        R = max(20.0, min(w, h) / 2.0 - 24.0)
        s = R / self._fmax

        # dial face: full-scale ring, a half-scale ring, crosshair
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(_qcolor("panel_hi"))
        p.drawEllipse(QtCore.QPointF(cx, cy), R, R)
        p.setBrush(QtCore.Qt.NoBrush)
        p.setPen(QtGui.QPen(_qcolor("grid"), 1))
        p.drawLine(QtCore.QPointF(cx - R, cy), QtCore.QPointF(cx + R, cy))
        p.drawLine(QtCore.QPointF(cx, cy - R), QtCore.QPointF(cx, cy + R))
        p.setPen(QtGui.QPen(_qcolor("border"), 1, QtCore.Qt.DashLine))
        p.drawEllipse(QtCore.QPointF(cx, cy), R / 2, R / 2)
        p.setPen(QtGui.QPen(_qcolor("border"), 2))
        p.drawEllipse(QtCore.QPointF(cx, cy), R, R)

        f = p.font(); f.setPointSize(8); f.setBold(True); p.setFont(f)
        p.setPen(_qcolor("muted"))
        p.drawText(QtCore.QRectF(cx + R + 4, cy - 8, 26, 16), QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, "+X")
        p.drawText(QtCore.QRectF(cx - 13, cy - R - 20, 26, 16), QtCore.Qt.AlignCenter, "+Y")
        p.drawText(QtCore.QRectF(cx + R * 0.71 + 2, cy - R * 0.71 - 16, 90, 16),
                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, f"{self._fmax:g} mT")

        def to_px(bx, by):
            x, y = bx * s, -by * s                 # screen y grows DOWN, +By is up
            r = math.hypot(x, y)
            if r > R * 1.06:                       # keep an off-scale arrow on the dial
                x, y = x * R * 1.06 / r, y * R * 1.06 / r
            return cx + x, cy + y

        # setpoint: dim arrow + tolerance ring at its tip
        sx, sy = to_px(*self._sp)
        self._arrow(p, cx, cy, sx, sy, 2.0, _qcolor("muted", 170), head=9.0)
        ring = max(6.0, self._tol * s)
        p.setBrush(QtCore.Qt.NoBrush)
        p.setPen(QtGui.QPen(_qcolor("ok" if self._stable else "muted"),
                            2 if self._stable else 1, QtCore.Qt.DashLine))
        p.drawEllipse(QtCore.QPointF(sx, sy), ring, ring)

        # measured: glowing amber arrow while energized, grey otherwise
        bx, by = self._meas
        if math.isfinite(bx) and math.isfinite(by):
            mx, my = to_px(bx, by)
            if self._energized:
                pulse = 0.5 + 0.5 * math.sin(2 * math.pi * self._phase)
                glow = _qcolor("accent", int(40 + 50 * pulse))
                p.setPen(QtGui.QPen(glow, 12, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
                p.drawLine(QtCore.QPointF(cx, cy), QtCore.QPointF(mx, my))
                self._arrow(p, cx, cy, mx, my, 3.0, _qcolor("accent"), head=13.0)
            else:
                self._arrow(p, cx, cy, mx, my, 2.0, _qcolor("muted"), head=10.0)

        # hub
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(_qcolor("accent" if self._energized else "border"))
        p.drawEllipse(QtCore.QPointF(cx, cy), 5, 5)

        # caption
        f.setPointSize(8); p.setFont(f)
        if self._energized:
            cap, col = ("STABLE" if self._stable else "REGULATING"), _qcolor("ok" if self._stable else "accent_hi")
        else:
            cap, col = "de-energized", _qcolor("muted")
        p.setPen(col)
        p.drawText(QtCore.QRectF(0, h - 18, w, 16), QtCore.Qt.AlignHCenter, cap)
        p.end()


# state -> palette KEY (resolved live so it follows the active theme)
STATE_COLOR_KEY = {
    "OFF": "muted",
    "REGULATING": "accent",
    "STABLE": "ok",
    "RAMP_DOWN": "accent_hi",
    "FAULT": "danger",
}


# ------------------------------------------------------------ the window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self.setWindowTitle("mag2d - 2D vector magnet" + ("  (remote)" if remote else ""))
        self.resize(1320, 900)

        self._t0 = time.monotonic()
        self._window_s = 30.0
        n = 3000
        self._t = deque(maxlen=n)
        self._hist = {k: deque(maxlen=n) for k in ("bx", "by", "sbx", "sby")}
        self._out_mode = None

        root = QtWidgets.QWidget(); root.setObjectName("root")
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)
        self.setCentralWidget(root)

        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

    # ---- layout ----------------------------------------------------------

    def _dspin(self, lo, hi, dec, step, suffix, value=0.0):
        w = QtWidgets.QDoubleSpinBox()
        w.setRange(lo, hi); w.setDecimals(dec); w.setSingleStep(step)
        w.setSuffix("  " + suffix); w.setValue(value)
        w.setKeyboardTracking(False)
        return w

    def _build_sidebar(self) -> QtWidgets.QWidget:
        inner = QtWidgets.QWidget(); inner.setObjectName("root")
        col = QtWidgets.QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 8, 0); col.setSpacing(12)
        lim = self.cfg.limits
        fmax = abs(lim.field_max_mT)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("mag2d")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        header.addWidget(settings_btn)
        col.addLayout(header)

        # --- polar setpoint
        fcard, flay = _card("Field  (magnitude + angle)")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        self.field_spin = self._dspin(-fmax, fmax, 2, 1.0, "mT", 0.0)
        self.angle_spin = self._dspin(lim.angle_min_deg, lim.angle_max_deg, 2, 5.0, "deg", 0.0)
        form.addRow("Magnitude", self.field_spin)
        form.addRow("Angle", self.angle_spin)
        flay.addLayout(form)
        go = QtWidgets.QPushButton("Go"); go.setObjectName("primary")
        go.clicked.connect(self._go_polar)
        flay.addWidget(go)
        col.addWidget(fcard)

        # --- cartesian setpoint
        vcard, vlay = _card("Vector  (Bx, By)")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        self.bx_spin = self._dspin(-fmax, fmax, 2, 1.0, "mT", 0.0)
        self.by_spin = self._dspin(-fmax, fmax, 2, 1.0, "mT", 0.0)
        form.addRow("Bx", self.bx_spin)
        form.addRow("By", self.by_spin)
        vlay.addLayout(form)
        go2 = QtWidgets.QPushButton("Go"); go2.setObjectName("primary")
        go2.clicked.connect(self._go_vector)
        vlay.addWidget(go2)
        col.addWidget(vcard)

        # --- output
        ocard, olay = _card("Output")
        row = QtWidgets.QHBoxLayout()
        self.output_btn = QtWidgets.QPushButton("Energize")
        self.output_btn.clicked.connect(self._toggle_output)
        zero_btn = QtWidgets.QPushButton("Zero field")
        zero_btn.clicked.connect(lambda: self._call(self.ctrl.zero))
        row.addWidget(self.output_btn, 1); row.addWidget(zero_btn)
        olay.addLayout(row)
        self.drive_label = QtWidgets.QLabel("drive  X --   Y --")
        self.drive_label.setStyleSheet(f"color:{COLORS['muted']};")
        olay.addWidget(self.drive_label)
        col.addWidget(ocard)

        # --- interlock
        icard, ilay = _card("Interlock")
        self.water_lamp = QtWidgets.QLabel("●  water ?")
        ilay.addWidget(self.water_lamp)
        self.bypass_chk = QtWidgets.QCheckBox("Bypass water interlock")
        # .clicked (user only), not .toggled: a programmatic setChecked from the
        # status poll must not fire the command back (gotcha #13).
        self.bypass_chk.clicked.connect(self._bypass_clicked)
        ilay.addWidget(self.bypass_chk)
        self.temp_label = QtWidgets.QLabel("T1 --   T2 --")
        ilay.addWidget(self.temp_label)
        self.monitor_label = QtWidgets.QLabel("")
        self.monitor_label.setObjectName("hint")
        ilay.addWidget(self.monitor_label)
        col.addWidget(icard)

        # --- fault
        xcard, xlay = _card("Fault")
        self.fault_label = QtWidgets.QLabel("no fault")
        self.fault_label.setWordWrap(True)
        xlay.addWidget(self.fault_label)
        clear_btn = QtWidgets.QPushButton("Clear fault"); clear_btn.setObjectName("danger")
        clear_btn.clicked.connect(lambda: self._call(self.ctrl.clear_fault))
        xlay.addWidget(clear_btn)
        col.addWidget(xcard)
        col.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(350)
        return scroll

    def _big(self, text, unit):
        box = QtWidgets.QVBoxLayout(); box.setSpacing(0)
        row = QtWidgets.QHBoxLayout(); row.setSpacing(6)
        value = QtWidgets.QLabel("--"); value.setObjectName("bigValue")
        u = QtWidgets.QLabel(unit); u.setObjectName("unit")
        row.addWidget(value); row.addWidget(u, 0, QtCore.Qt.AlignBottom); row.addStretch(1)
        cap = QtWidgets.QLabel(text.upper()); cap.setObjectName("cardTitle")
        box.addWidget(cap); box.addLayout(row)
        return box, value

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(16)

        # --- readouts
        rcard, rlay = _card()
        top = QtWidgets.QHBoxLayout(); top.setSpacing(28)
        box, self.mag_value = self._big("|B| measured", "mT"); top.addLayout(box)
        box, self.angle_value = self._big("angle", "deg"); top.addLayout(box)
        grid = QtWidgets.QGridLayout(); grid.setHorizontalSpacing(14); grid.setVerticalSpacing(2)
        self.small = {}
        for r, (key, label) in enumerate((("bx", "Bx"), ("by", "By"), ("err", "error"))):
            name = QtWidgets.QLabel(label); name.setObjectName("cardTitle")
            val = QtWidgets.QLabel("--")
            val.setStyleSheet("font-size:17px; font-weight:700;")
            sp = QtWidgets.QLabel("")
            sp.setStyleSheet(f"color:{COLORS['muted']};")
            grid.addWidget(name, r, 0); grid.addWidget(val, r, 1); grid.addWidget(sp, r, 2)
            self.small[key] = (val, sp)
        top.addLayout(grid)
        top.addStretch(1)
        right = QtWidgets.QVBoxLayout(); right.setSpacing(8)
        self.state_badge = QtWidgets.QLabel("OFF"); self.state_badge.setObjectName("stateBadge")
        self.state_badge.setAlignment(QtCore.Qt.AlignCenter)
        self.stable_dot = QtWidgets.QLabel("●  not stable")
        self.setpoint_label = QtWidgets.QLabel("setpoint --")
        self.setpoint_label.setStyleSheet(f"color:{COLORS['muted']};")
        right.addWidget(self.state_badge); right.addWidget(self.stable_dot)
        right.addWidget(self.setpoint_label)
        top.addLayout(right)
        rlay.addLayout(top)
        col.addWidget(rcard)

        # --- dial + chart
        mid = QtWidgets.QHBoxLayout(); mid.setSpacing(16)
        dcard, dlay = _card("Field vector")
        self.dial = VectorDial()
        dlay.addWidget(self.dial, 1)
        dcard.setMinimumWidth(440)
        mid.addWidget(dcard, 0)

        pcard, play = _card("Bx, By  (mT)  vs  time  (s)   -   solid measured, dashed setpoint")
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setBackground(COLORS["panel"])
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        for ax in ("left", "bottom"):
            axis = self.plot.getAxis(ax)
            axis.setPen(pg.mkPen(COLORS["muted"]))
            axis.setTextPen(pg.mkPen(COLORS["muted"]))
        legend = self.plot.addLegend(offset=(10, 10))
        legend.setLabelTextColor(COLORS["muted"])
        self.curve_bx = self.plot.plot([], [], pen=pg.mkPen(COLORS["accent"], width=2), name="Bx")
        self.curve_by = self.plot.plot([], [], pen=pg.mkPen(COLORS["text"], width=2), name="By")
        self.curve_sbx = self.plot.plot([], [], pen=pg.mkPen(COLORS["accent"], width=1, style=QtCore.Qt.DashLine))
        self.curve_sby = self.plot.plot([], [], pen=pg.mkPen(COLORS["text"], width=1, style=QtCore.Qt.DashLine))
        play.addWidget(self.plot)
        mid.addWidget(pcard, 1)
        col.addLayout(mid, 1)

        # --- log
        lcard, llay = _card("Event log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setFixedHeight(110)
        llay.addWidget(self.log)
        col.addWidget(lcard)
        return panel

    # ---- actions ---------------------------------------------------------

    def _call(self, fn, *args):
        """Run a command; a refusal goes to the log instead of a traceback."""
        try:
            fn(*args)
            return True
        except (Refused, ValueError) as exc:
            self._on_event("error", str(exc))
            return False

    def _go_polar(self):
        self._call(self.ctrl.set_field, self.field_spin.value(), self.angle_spin.value())

    def _go_vector(self):
        self._call(self.ctrl.set_vector, self.bx_spin.value(), self.by_spin.value())

    def _toggle_output(self):
        self._call(self.ctrl.set_output, self._out_mode != "off")

    def _bypass_clicked(self, checked: bool):
        if checked:
            answer = QtWidgets.QMessageBox.warning(
                self, "Bypass the water interlock?",
                "Without the cooling-water interlock nothing stops the coils from "
                "overheating if the water is off.\n\nBypass it anyway?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if answer != QtWidgets.QMessageBox.Yes:
                self.bypass_chk.blockSignals(True)
                self.bypass_chk.setChecked(False)
                self.bypass_chk.blockSignals(False)
                return
        self._call(self.ctrl.set_water_bypass, checked)

    def _open_settings(self):
        self.ctrl.get_config()           # refresh from the source of truth first
        SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self).exec()

    def _on_settings_applied(self):
        lim = self.cfg.limits
        fmax = abs(lim.field_max_mT)
        for w in (self.field_spin, self.bx_spin, self.by_spin):
            w.setRange(-fmax, fmax)
        self.angle_spin.setRange(lim.angle_min_deg, lim.angle_max_deg)

    # ---- refresh & events ------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = {"error": COLORS["danger"], "warn": COLORS["accent_hi"]}.get(level, COLORS["muted"])
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(
            f'<span style="color:{COLORS["accent_dim"]}">{stamp}</span> '
            f'<span style="color:{color}">{msg}</span>')

    def _set_output_mode(self, mode: str):
        """'on' = the button energizes, 'off' = it switches off. Re-polish only on change."""
        if mode == self._out_mode:
            return
        self._out_mode = mode
        if mode == "off":
            self.output_btn.setText("Ramp down + off")
            self.output_btn.setObjectName("danger")
        else:
            self.output_btn.setText("Energize")
            self.output_btn.setObjectName("primary")
        self.output_btn.style().unpolish(self.output_btn)
        self.output_btn.style().polish(self.output_btn)

    def _refresh(self):
        s = self.ctrl.status()
        cfg = self.cfg
        t = time.monotonic() - self._t0

        self.mag_value.setText(_fmt(s.measured_magnitude_mT))
        self.angle_value.setText(_fmt(s.measured_angle_deg, ".1f"))
        for key, meas, sp in (("bx", s.measured_bx_mT, s.setpoint_bx_mT),
                              ("by", s.measured_by_mT, s.setpoint_by_mT)):
            val, spl = self.small[key]
            val.setText(f"{_fmt(meas, '+.2f')} mT")
            spl.setText(f"set {_fmt(sp, '+.2f')}")
        val, spl = self.small["err"]
        val.setText(f"{_fmt(s.error_mT, '.3f')} mT")
        spl.setText(f"tol {cfg.control.tolerance_mT:g}")
        self.setpoint_label.setText(
            f"setpoint {_fmt(s.setpoint_field_mT)} mT @ {_fmt(s.setpoint_angle_deg, '.1f')}°")

        color = COLORS[STATE_COLOR_KEY.get(s.state, "text")]
        self.state_badge.setText(s.state)
        self.state_badge.setStyleSheet(
            f"QLabel#stateBadge {{ color:{color}; border:1px solid {color}; "
            f"background:{COLORS['panel_hi']}; border-radius:10px; padding:4px 12px; "
            f"font-weight:700; letter-spacing:1px; }}")
        if s.field_stable:
            self.stable_dot.setText("●  field stable")
            self.stable_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.stable_dot.setText("●  not stable")
            self.stable_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")

        self._set_output_mode("off" if s.state in ("REGULATING", "STABLE") else "on")
        ox, oy = (s.output_V + [float("nan")] * 2)[:2]
        self.drive_label.setText(f"drive  X {_fmt(ox, '+.3f')} V   Y {_fmt(oy, '+.3f')} V"
                                 + ("   (enabled)" if s.energized else ""))

        # interlock card
        if s.water_ok:
            txt, key = "●  water OK", "ok"
        elif s.water_bypass:
            txt, key = "●  NO WATER  (bypassed)", "accent_hi"
        else:
            txt, key = "●  NO WATER", "danger"
        self.water_lamp.setText(txt)
        self.water_lamp.setStyleSheet(f"color:{COLORS[key]}; font-weight:700;")
        self.bypass_chk.blockSignals(True)
        self.bypass_chk.setChecked(bool(s.water_bypass))
        self.bypass_chk.blockSignals(False)
        t1, t2 = (s.temp_C + [float("nan")] * 2)[:2]
        limit = cfg.interlock.max_temp_C
        hot = s.temp_monitor and any(math.isfinite(x) and x > limit for x in (t1, t2))
        self.temp_label.setText(f"T1 {_fmt(t1, '.1f')} °C     T2 {_fmt(t2, '.1f')} °C")
        self.temp_label.setStyleSheet(f"color:{COLORS['danger' if hot else 'text']};")
        self.monitor_label.setText(
            f"temperature monitor ON, limit {limit:g} °C" if s.temp_monitor
            else "temperature monitor off (Settings > interlock)")

        if s.fault:
            self.fault_label.setText(s.fault)
            self.fault_label.setStyleSheet(f"color:{COLORS['danger']}; font-weight:600;")
        else:
            self.fault_label.setText("no fault" + (f"  (hw: {s.hw_error})" if s.hw_error else ""))
            self.fault_label.setStyleSheet(f"color:{COLORS['muted']};")

        self.dial.set_state(s.setpoint_bx_mT, s.setpoint_by_mT, s.measured_bx_mT,
                            s.measured_by_mT, cfg.limits.field_max_mT,
                            cfg.control.tolerance_mT, s.energized, s.field_stable)

        # strip chart
        self._t.append(t)
        for key, v in (("bx", s.measured_bx_mT), ("by", s.measured_by_mT),
                       ("sbx", s.setpoint_bx_mT), ("sby", s.setpoint_by_mT)):
            self._hist[key].append(v if math.isfinite(v) else float("nan"))
        ts = list(self._t)
        self.curve_bx.setData(ts, list(self._hist["bx"]), connect="finite")
        self.curve_by.setData(ts, list(self._hist["by"]), connect="finite")
        self.curve_sbx.setData(ts, list(self._hist["sbx"]), connect="finite")
        self.curve_sby.setData(ts, list(self._hist["sby"]), connect="finite")
        if t > self._window_s:
            self.plot.setXRange(t - self._window_s, t, padding=0)

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()      # local: ramp to 0 V + disable; remote: close the client
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Controller-like object (already started)."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # palette BEFORE building widgets
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
    from ..controller import WaterInterlockError
    from ..sim_system import build_sim_system
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    ctrl, _ = build_sim_system(cfg)
    try:
        ctrl.start()
    except WaterInterlockError as exc:
        print(f"mag2d: {exc}")
        return 3
    return run_app(ctrl, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
