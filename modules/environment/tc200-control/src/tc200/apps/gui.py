"""Control GUI for the Thorlabs TC200 heater controller.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a Heater-like object (a real
in-process Heater, or a Tc200Client facade for a remote service). It sends
commands and reads a status snapshot on a Qt timer to update the numbers, the
history chart and the indicator. Brain events arrive on a Qt signal so they
can safely cross into the GUI thread.

The signature widget is the HotPlateIndicator: a sample block on a heater
plate whose element glows when the output is on (brighter the further below
the setpoint it is -- the controller is then pushing hard), heat shimmer rising
from it, the PT100 clipped to the block (red on a sensor fault or a wrong
sensor setting), and a thermometer scale beside it with the setpoint marker
and the TMAX trip line.
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import (D_GAIN_RANGE, I_GAIN_RANGE, P_GAIN_RANGE, PMAX_MIN_W, TMAX_MAX_C,
                      TMAX_MIN_C, Config)
from ..sim_system import build_sim_system
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog
from .control_bar import ControlBar, mark_always

#: seconds of history in the strip chart
HISTORY_S = 900.0


# ------------------------------------------------------------- signal bridge

class Bridge(QtCore.QObject):
    """Carries brain events across the thread boundary into the GUI."""
    event = QtCore.Signal(str, str)


# ------------------------------------------------------------- small helpers

def _card(title: str | None = None):
    frame = QtWidgets.QFrame()
    frame.setObjectName("card")
    lay = QtWidgets.QVBoxLayout(frame)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(10)
    if title:
        lbl = QtWidgets.QLabel(title.upper())
        lbl.setObjectName("cardTitle")
        lay.addWidget(lbl)
    return frame, lay


def _fmt(v: float, fmt: str) -> str:
    return "--" if v is None or not math.isfinite(v) else format(v, fmt)


# ------------------------------------------------------------- the indicator

class HotPlateIndicator(QtWidgets.QWidget):
    """A sample block on a heater plate, with a thermometer scale beside it.

    * the heater element (a zig-zag in the plate) glows amber while the output
      is ON; how bright follows the heating demand, estimated from how far
      below the setpoint the block is (the TC200 has no power readback);
    * shimmer lines rise from the block while it heats (own ~33 ms timer),
      so "heating" and "holding" are told apart at a glance;
    * the PT100 is the little resistor clipped to the block -- red when the box
      reports a sensor alarm or is set to the wrong sensor type;
    * the scale runs from the lower limit to the TMAX trip (a red line); the
      mercury is the measured temperature, the triangle the setpoint. Green
      when the setpoint counts as reached.
    """

    def __init__(self):
        super().__init__()
        self.setFixedSize(250, 160)
        self._t = float("nan")
        self._sp = float("nan")
        self._lo, self._tmax = 20.0, 120.0
        self._enabled = False
        self._reached = False
        self._sensor_bad = False
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, t_C, sp_C, lo_C, tmax_C, enabled, reached, sensor_bad):
        self._t = t_C if t_C is not None else float("nan")
        self._sp = sp_C if sp_C is not None else float("nan")
        self._lo = lo_C if lo_C is not None and math.isfinite(lo_C) else 20.0
        tm = tmax_C if tmax_C is not None and math.isfinite(tmax_C) else self._lo + 100.0
        self._tmax = max(tm, self._lo + 1.0)
        self._enabled, self._reached, self._sensor_bad = enabled, reached, sensor_bad
        if enabled and not self._timer.isActive():
            self._timer.start()
        elif not enabled and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _demand(self) -> float:
        """0..1: how hard the controller is probably pushing."""
        if not self._enabled or not (math.isfinite(self._t) and math.isfinite(self._sp)):
            return 0.0
        return max(0.25, min(1.0, (self._sp - self._t) / 5.0 + 0.35))

    def _tick(self):
        self._phase = (self._phase + 0.02) % 1.0
        self.update()

    def paintEvent(self, ev):
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        accent = QColor(COLORS["accent"])
        hot = QColor(COLORS["accent_hi"])
        muted = QColor(COLORS["muted"])
        metal = QColor("#5a626e")
        demand = self._demand()

        # ---- heat shimmer above the block ---------------------------------------
        bx, bw, btop, bh = 22.0, 110.0, 64.0, 34.0
        if self._enabled:
            for k in range(3):
                x0 = bx + 22 + k * 33
                s = (self._phase + k / 3.0) % 1.0
                a = int(200 * (1.0 - s) * (0.4 + 0.6 * demand))
                col = QColor(hot.red(), hot.green(), hot.blue(), a)
                p.setPen(QPen(col, 2.0)); p.setBrush(Qt.NoBrush)
                path = QPainterPath()
                y0 = btop - 4 - s * 40
                path.moveTo(x0, y0)
                for j in range(1, 5):
                    path.lineTo(x0 + (5 if j % 2 else -5), y0 - j * 4)
                p.drawPath(path)

        # ---- the sample block -------------------------------------------------------
        block = QColor(COLORS["ok"]) if self._reached else QColor(COLORS["panel_hi"])
        p.setPen(QPen(metal, 2.0)); p.setBrush(block)
        p.drawRoundedRect(QRectF(bx + 15, btop, bw - 30, bh), 4, 4)

        # ---- the heater plate with its element ---------------------------------------
        py = btop + bh
        p.setPen(QPen(metal, 2.0)); p.setBrush(QColor(COLORS["panel"]))
        p.drawRoundedRect(QRectF(bx, py, bw, 24), 5, 5)
        if self._enabled:
            glow = QColor(accent.red(), accent.green(), accent.blue(),
                          int(90 + 165 * demand))
        else:
            glow = muted
        p.setPen(QPen(glow, 2.4)); p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        path.moveTo(bx + 10, py + 12)
        for j in range(1, 16):
            path.lineTo(bx + 10 + j * 6, py + (5 if j % 2 else 19))
        p.drawPath(path)

        # ---- the PT100 clipped to the block ------------------------------------------
        sx, sy = bx + bw - 12, btop + 10
        sens = QColor(COLORS["danger"]) if self._sensor_bad else QColor(COLORS["text"])
        p.setPen(QPen(sens, 1.6)); p.setBrush(QColor(COLORS["panel_hi"]))
        p.drawRect(QRectF(sx, sy, 20, 9))
        p.drawLine(QPointF(sx + 20, sy + 4.5), QPointF(sx + 30, sy + 4.5))
        p.drawLine(QPointF(sx + 30, sy + 4.5), QPointF(sx + 30, sy + 30))
        fnt = p.font(); fnt.setPointSize(7); p.setFont(fnt)
        p.drawText(QRectF(sx - 8, sy - 14, 44, 12), Qt.AlignHCenter, "PT100")

        # ---- thermometer scale (linear, limit .. TMAX) --------------------------------
        tx, ttop, tbot = 196.0, 12.0, h - 30.0

        def y_of(v):
            f = (v - self._lo) / (self._tmax - self._lo)
            return tbot - max(0.0, min(1.0, f)) * (tbot - ttop)

        p.setPen(QPen(metal, 2.0)); p.setBrush(QColor(COLORS["panel_hi"]))
        p.drawRoundedRect(QRectF(tx - 6, ttop, 12, tbot - ttop + 4), 6, 6)
        p.drawEllipse(QPointF(tx, tbot + 9), 10, 10)
        fill = QColor(COLORS["ok"]) if self._reached else accent
        p.setPen(Qt.NoPen); p.setBrush(fill)
        p.drawEllipse(QPointF(tx, tbot + 9), 7, 7)
        if math.isfinite(self._t):
            y = y_of(self._t)
            p.drawRoundedRect(QRectF(tx - 3, y, 6, tbot - y + 6), 3, 3)
        # TMAX trip line at the top of the scale
        p.setPen(QPen(QColor(COLORS["danger"]), 2.0))
        p.drawLine(QPointF(tx - 11, ttop), QPointF(tx + 11, ttop))
        # setpoint marker
        if math.isfinite(self._sp):
            y = y_of(self._sp)
            p.setPen(Qt.NoPen); p.setBrush(QColor(COLORS["text"]))
            p.drawPolygon(QPolygonF([QPointF(tx - 9, y), QPointF(tx - 16, y - 5),
                                     QPointF(tx - 16, y + 5)]))
        p.setPen(muted)
        fnt.setPointSize(7); p.setFont(fnt)
        p.drawText(QRectF(tx + 13, ttop - 6, 50, 12), Qt.AlignLeft, f"{self._tmax:g} C")
        p.drawText(QRectF(tx + 13, tbot - 6, 50, 12), Qt.AlignLeft, f"{self._lo:g} C")

        # ---- caption --------------------------------------------------------------------
        if self._sensor_bad:
            cap, col = "SENSOR!", QColor(COLORS["danger"])
        elif not self._enabled:
            cap, col = "OFF", muted
        elif self._reached:
            cap, col = "HOLDING", QColor(COLORS["ok"])
        else:
            cap, col = "HEATING", hot
        p.setPen(col)
        fnt.setBold(True); fnt.setPointSize(8); p.setFont(fnt)
        p.drawText(QRectF(bx, h - 18, bw, 14), Qt.AlignHCenter, cap)
        p.end()


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        title = "TC200  -  heater controller"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1180, 760)
        self._t0 = time.monotonic()
        self._hist_t: deque = deque()
        self._hist_T: deque = deque()
        self._hist_sp: deque = deque()
        self._last_hist = -1.0

        root = QtWidgets.QWidget(); root.setObjectName("root")
        # Control or viewer (control_bar.py): a bar above the panels, only for
        # a GUI on a service whose client knows about control -- a local GUI
        # owns its heater and has nobody to share it with. (Built before the
        # panels, which the log needs; its log callback is only used later.)
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
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)

        # brain events -> log
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

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
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(360)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(16)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("TC200")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        mark_always(settings_btn)    # a viewer may LOOK; the service refuses the OK
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        header.addWidget(settings_btn)
        col.addLayout(header)

        # connection / sensor card
        ccard, clay = _card()
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        self.conn_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        clay.addWidget(self.conn_dot)
        self.idn_label = QtWidgets.QLabel("—")
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        clay.addWidget(self.idn_label)
        self.sensor_label = QtWidgets.QLabel("Sensor: —")
        clay.addWidget(self.sensor_label)
        col.addWidget(ccard)

        # ---- temperature card -------------------------------------------------
        tcard, tlay = _card("Temperature")
        form = QtWidgets.QFormLayout(); form.setSpacing(8)
        self.temp_spin = QtWidgets.QDoubleSpinBox()
        self.temp_spin.setDecimals(1); self.temp_spin.setSingleStep(0.5)
        self.temp_spin.setSuffix("  °C")
        form.addRow("Setpoint", self.temp_spin)
        tlay.addLayout(form)
        set_t = QtWidgets.QPushButton("Set temperature"); set_t.setObjectName("primary")
        set_t.clicked.connect(self._set_temperature)
        tlay.addWidget(set_t)
        row = QtWidgets.QHBoxLayout()
        self.on_btn = QtWidgets.QPushButton("Heater ON"); self.on_btn.setObjectName("primary")
        self.on_btn.clicked.connect(lambda: self._call(self.ctrl.set_enabled, True))
        self.off_btn = QtWidgets.QPushButton("Heater OFF"); self.off_btn.setObjectName("danger")
        # the SAFETY verb (net/service.py): works for a viewer too
        self.off_btn.clicked.connect(lambda: self._call(self.ctrl.heater_off))
        mark_always(self.off_btn)
        row.addWidget(self.on_btn, 1); row.addWidget(self.off_btn, 1)
        tlay.addLayout(row)
        col.addWidget(tcard)

        # ---- controller card (settings stored in the box) ---------------------
        kcard, klay = _card("Controller (stored in the TC200)")
        form = QtWidgets.QFormLayout(); form.setSpacing(8)
        self.p_spin = QtWidgets.QSpinBox(); self.p_spin.setRange(*P_GAIN_RANGE)
        self.i_spin = QtWidgets.QSpinBox(); self.i_spin.setRange(*I_GAIN_RANGE)
        self.d_spin = QtWidgets.QSpinBox(); self.d_spin.setRange(*D_GAIN_RANGE)
        gains = QtWidgets.QHBoxLayout(); gains.setSpacing(6)
        for lbl, w in (("P", self.p_spin), ("I", self.i_spin), ("D", self.d_spin)):
            gains.addWidget(QtWidgets.QLabel(lbl)); gains.addWidget(w, 1)
        form.addRow("PID", gains)
        self.pmax_spin = QtWidgets.QDoubleSpinBox()
        self.pmax_spin.setDecimals(1); self.pmax_spin.setSingleStep(0.5)
        self.pmax_spin.setSuffix("  W")
        self.tmax_spin = QtWidgets.QDoubleSpinBox()
        self.tmax_spin.setDecimals(1); self.tmax_spin.setSingleStep(1.0)
        self.tmax_spin.setRange(TMAX_MIN_C, TMAX_MAX_C); self.tmax_spin.setSuffix("  °C")
        form.addRow("Power limit", self.pmax_spin)
        form.addRow("Trip (TMAX)", self.tmax_spin)
        klay.addLayout(form)
        apply_k = QtWidgets.QPushButton("Apply to controller")
        apply_k.clicked.connect(self._apply_controller)
        klay.addWidget(apply_k)
        col.addWidget(kcard)

        self._apply_limits_to_widgets()
        self._seeded = False        # seeded from the first status (adopted values)
        col.addStretch(1)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        ocard, olay = _card("Sample temperature")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(24)
        box = QtWidgets.QVBoxLayout(); box.setSpacing(2)
        cap = QtWidgets.QLabel("MEASURED")
        cap.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; font-weight:700; letter-spacing:1px;")
        line = QtWidgets.QHBoxLayout(); line.setSpacing(5)
        self.temp_value = QtWidgets.QLabel("—"); self.temp_value.setObjectName("bigValue")
        self.temp_value.setMinimumWidth(150)
        unit = QtWidgets.QLabel("°C"); unit.setObjectName("unit")
        line.addWidget(self.temp_value); line.addWidget(unit, 0, QtCore.Qt.AlignBottom)
        line.addStretch(1)
        self.temp_sub = QtWidgets.QLabel("—")
        self.temp_sub.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        self.lamp = QtWidgets.QLabel("●  not reached")
        self.alarm_label = QtWidgets.QLabel("")
        box.addWidget(cap); box.addLayout(line); box.addWidget(self.temp_sub)
        box.addWidget(self.lamp); box.addWidget(self.alarm_label); box.addStretch(1)
        row.addLayout(box, 1)
        self.indicator = HotPlateIndicator()
        row.addWidget(self.indicator, 0, QtCore.Qt.AlignVCenter)
        olay.addLayout(row)
        colw.addWidget(ocard)

        gcard, glay = _card("History")
        self.plot, self.curve, self.sp_curve = self._make_plot()
        glay.addWidget(self.plot, 1)
        colw.addWidget(gcard, 3)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(90)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 1)
        return panel

    def _make_plot(self):
        import pyqtgraph as pg
        pg.setConfigOptions(antialias=True)
        w = pg.PlotWidget(background=COLORS["code_bg"])
        w.setMinimumHeight(160)
        pen = pg.mkPen(COLORS["muted"])
        for axis in ("left", "bottom"):
            ax = w.getAxis(axis); ax.setPen(pen); ax.setTextPen(pen)
        w.setLabel("left", "temperature (C)")
        w.setLabel("bottom", "time (s)")
        w.showGrid(x=True, y=True, alpha=0.15)
        sp = w.plot([], [], pen=pg.mkPen(COLORS["muted"], width=1, style=QtCore.Qt.DashLine))
        curve = w.plot([], [], pen=pg.mkPen(COLORS["accent"], width=2))
        return w, curve, sp

    def _apply_limits_to_widgets(self):
        lim = self.cfg.limits
        self.temp_spin.setRange(lim.temperature_min_C, self.ctrl.temperature_max())
        self.pmax_spin.setRange(PMAX_MIN_W, max(PMAX_MIN_W, lim.pmax_max_W))

    def _seed_controller_widgets(self, s):
        self.p_spin.setValue(s.p_gain); self.i_spin.setValue(s.i_gain)
        self.d_spin.setValue(s.d_gain)
        if math.isfinite(s.pmax_W):
            self.pmax_spin.setValue(s.pmax_W)
        if math.isfinite(s.tmax_C):
            self.tmax_spin.setValue(s.tmax_C)

    # ---- actions ---------------------------------------------------------

    def _call(self, fn, *args):
        """Run a command; a refusal goes to the log instead of crashing the GUI."""
        try:
            fn(*args)
        except Exception as exc:
            self._on_event("error", str(exc))

    def _set_temperature(self):
        self._call(self.ctrl.set_temperature, self.temp_spin.value())

    def _apply_controller(self):
        """Push only what was edited, so an untouched field commands nothing."""
        s = self.ctrl.status()
        if (self.p_spin.value(), self.i_spin.value(), self.d_spin.value()) != (
                s.p_gain, s.i_gain, s.d_gain):
            self._call(self.ctrl.set_pid, self.p_spin.value(), self.i_spin.value(),
                       self.d_spin.value())
        if abs(self.pmax_spin.value() - s.pmax_W) > 0.01:
            self._call(self.ctrl.set_pmax, self.pmax_spin.value())
        if abs(self.tmax_spin.value() - s.tmax_C) > 0.01:
            self._call(self.ctrl.set_tmax, self.tmax_spin.value())
        self._apply_limits_to_widgets()

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        self._apply_limits_to_widgets()
        self._seed_controller_widgets(self.ctrl.status())

    # ---- refresh & events ------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = COLORS["danger"] if level == "error" else (
            COLORS["accent"] if level == "warn" else COLORS["muted"])
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(
            f'<span style="color:{COLORS["accent_dim"]}">{stamp}</span> '
            f'<span style="color:{color}">{msg}</span>')

    def _refresh(self):
        if self._control_bar is not None:
            self._control_bar.refresh()
        s = self.ctrl.status()

        # the boxes start at what the TC200 was ALREADY set to (adopted), so
        # pressing a button without editing never changes anything
        if not self._seeded and s.connected and math.isfinite(s.setpoint_C):
            self._apply_limits_to_widgets()
            self.temp_spin.setValue(s.setpoint_C)
            self._seed_controller_widgets(s)
            self._seeded = True
            # the brain announced this before the window existed; say it again
            self._on_event("info", f"adopted from the TC200: set {s.setpoint_C:.1f} C, heater "
                                   f"{'ON' if s.enabled else 'off'}, sensor {s.sensor}, PID "
                                   f"{s.p_gain}/{s.i_gain}/{s.d_gain}, PMAX {s.pmax_W:g} W, "
                                   f"TMAX {s.tmax_C:g} C (nothing commanded)")
        if math.isfinite(s.temperature_max_C) and abs(
                self.temp_spin.maximum() - s.temperature_max_C) > 1e-6:
            self.temp_spin.setMaximum(s.temperature_max_C)   # TMAX moved the ceiling

        self.temp_value.setText(_fmt(s.temperature_C, ".2f"))
        self.temp_sub.setText(f"set {_fmt(s.setpoint_C, '.1f')} °C  ·  heater "
                              f"{'ON' if s.enabled else 'off'}  ·  {s.mode or '—'} mode")
        if s.temperature_stable:
            self.lamp.setText("●  reached")
            self.lamp.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.lamp.setText("●  not reached")
            self.lamp.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        alarms = []
        if s.sensor_alarm:
            alarms.append("SENSOR ALARM")
        if s.tmax_alarm:
            alarms.append("TMAX ALARM")
        if s.connected and s.sensor and not s.sensor_ok:
            alarms.append("WRONG SENSOR SETTING")
        self.alarm_label.setText("  ·  ".join(alarms))
        self.alarm_label.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")

        sens = (s.sensor or "—").upper()
        self.sensor_label.setText(
            f"Sensor: {sens}" + ("" if s.sensor_ok or not s.sensor else "  (expected PTC100)"))
        self.sensor_label.setStyleSheet(
            "" if s.sensor_ok or not s.sensor else f"color:{COLORS['danger']};")

        if s.hw_error:
            self.conn_dot.setText("●  hardware error")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
            self.conn_dot.setToolTip(s.hw_error)
        elif s.connected:
            self.conn_dot.setText("●  connected" + ("  (simulated)" if s.simulated else ""))
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
            self.conn_dot.setToolTip("")
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        if s.idn:
            self.idn_label.setText(s.idn)

        self.indicator.set_state(s.temperature_C, s.setpoint_C, s.temperature_min_C,
                                 s.tmax_C, s.enabled, s.temperature_stable,
                                 s.sensor_alarm or (bool(s.sensor) and not s.sensor_ok))

        # history: one point every 0.25 s
        now = time.monotonic() - self._t0
        if now - self._last_hist >= 0.25 and math.isfinite(s.temperature_C):
            self._last_hist = now
            self._hist_t.append(now)
            self._hist_T.append(s.temperature_C)
            self._hist_sp.append(s.setpoint_C)
            while self._hist_t and now - self._hist_t[0] > HISTORY_S:
                for d in (self._hist_t, self._hist_T, self._hist_sp):
                    d.popleft()
            t = list(self._hist_t)
            self.curve.setData(t, list(self._hist_T))
            self.sp_curve.setData(t, list(self._hist_sp))

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        # local: stop the brain (it switches the heater off if configured to);
        # remote: just close the client -- the service keeps running
        self.ctrl.shutdown()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Heater-like object (an in-process Heater, or a
    Tc200Client facade for a remote service). The theme is chosen ONCE here,
    from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # swap the active palette first
    # Numbers in the C locale, not the Windows one (gotcha #18): under a
    # comma-decimal locale 35.5 C shows as "35,5".
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
    if not remote:
        ctrl.start()                  # the local simulator: open it and start polling
    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    return app.exec()


def main(theme: str | None = None) -> int:
    """Default: run against the built-in simulator, in-process. `theme` (if given)
    overrides cfg.ui.theme for this launch.

    The local simulator starts as if the box had been left heating a block
    towards 40 C (it is at 34 C) -- so the panel shows the loop at work. The
    brain still ADOPTS that state and commands nothing at start."""
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    cfg.hardware.poll_s = 0.2
    heater, _ = build_sim_system(cfg, temperature_C=34.0, setpoint_C=40.0, enabled=True)
    return run_app(heater, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
