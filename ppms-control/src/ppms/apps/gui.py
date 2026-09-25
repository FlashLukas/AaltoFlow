"""Control GUI for the Quantum Design DynaCool (field, temperature, chamber).

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a Cryostat-like object (a real
in-process Cryostat, or a PpmsClient facade for a remote service). It sends
setpoints and reads a status snapshot on a Qt timer to update the numbers, the
two strip charts and the indicator. Brain events arrive on a Qt signal so they
can safely cross into the GUI thread.

The successor of the old LabVIEW front panel `QDInstrument_ControlField.vi`
(set field / set temperature with rate and approach, a chart of each, status
words, chamber) -- plus the one thing that panel only showed as an LED on the
other VI: whether each setpoint is REACHED, which is what a scan waits on.

The signature widget is the CryostatIndicator: the sample space of a cryostat
with field lines through it (count and brightness follow |B|, direction follows
its sign) and a thermometer on a log scale (1.8 K ... 400 K spans a factor of 200).
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import FIELD_APPROACHES, TEMPERATURE_APPROACHES, Config
from ..sim_system import build_sim_system
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog

#: seconds of history in the strip charts
HISTORY_S = 600.0


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

class CryostatIndicator(QtWidgets.QWidget):
    """The sample space: field lines through it, a thermometer beside it.

    Field lines are vertical arrows through the sample; how many are drawn and
    how bright they are follow |B| / B_max, their direction the sign of B. While
    the magnet is ramping they drift along their direction (own ~33 ms timer),
    so "moving" and "holding" are told apart at a glance. The thermometer fills
    on a LOG scale between the configured temperature limits.
    """

    def __init__(self):
        super().__init__()
        self.setFixedSize(230, 150)
        self._b = 0.0
        self._b_max = 9000.0
        self._t = float("nan")
        self._t_lo, self._t_hi = 1.8, 400.0
        self._ramping = False
        self._field_ok = False
        self._temp_ok = False
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, b_mT, b_max, t_K, t_lo, t_hi, ramping, field_ok, temp_ok):
        self._b = b_mT if b_mT is not None and math.isfinite(b_mT) else 0.0
        self._b_max = max(b_max, 1e-9)
        self._t = t_K if t_K is not None else float("nan")
        self._t_lo, self._t_hi = max(t_lo, 1e-3), max(t_hi, t_lo * 1.01)
        self._ramping, self._field_ok, self._temp_ok = ramping, field_ok, temp_ok
        if ramping and not self._timer.isActive():
            self._timer.start()
        elif not ramping and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        self._phase = (self._phase + 0.03) % 1.0
        self.update()

    def paintEvent(self, ev):
        from PySide6.QtCore import QRectF, QPointF, Qt
        from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        accent = QColor(COLORS["accent"])
        muted = QColor(COLORS["muted"])
        metal = QColor("#5b6470")

        # ---- the sample chamber (a rounded column) -----------------------------
        cx, top, bot = 78.0, 12.0, h - 22.0
        chamber = QRectF(cx - 38, top, 76, bot - top)
        p.setPen(QPen(metal, 2.2)); p.setBrush(QColor(COLORS["panel_hi"]))
        p.drawRoundedRect(chamber, 16, 16)

        # ---- field lines ------------------------------------------------------------
        frac = min(1.0, abs(self._b) / self._b_max)
        n = 0 if abs(self._b) < 1e-3 * self._b_max else 1 + int(round(4 * frac))
        up = self._b >= 0
        span = bot - top - 16
        for i in range(n):
            x = cx - 26 + (52 * (i + 0.5) / n)
            a = int(90 + 165 * (0.3 + 0.7 * frac))
            col = QColor(accent.red(), accent.green(), accent.blue(), a)
            p.setPen(QPen(col, 2.0)); p.setBrush(col)
            p.drawLine(QPointF(x, top + 8), QPointF(x, bot - 8))
            # arrow heads, drifting along the line while the magnet ramps
            for k in range(2):
                s = ((k / 2.0) + (self._phase if self._ramping else 0.25)) % 1.0
                y = (top + 8 + s * span) if up is False else (bot - 8 - s * span)
                d = -7 if up else 7
                p.drawPolygon(QPolygonF([QPointF(x, y + d), QPointF(x - 4, y),
                                         QPointF(x + 4, y)]))

        # the sample, in the middle
        sample = QColor(COLORS["ok"]) if self._field_ok else QColor(COLORS["text"])
        p.setPen(Qt.NoPen); p.setBrush(sample)
        p.drawRoundedRect(QRectF(cx - 16, (top + bot) / 2 - 3, 32, 6), 2, 2)

        # ---- thermometer (log scale) ------------------------------------------------
        tx, ttop, tbot = 170.0, top + 4, bot - 14
        p.setPen(QPen(metal, 2.0)); p.setBrush(QColor(COLORS["panel_hi"]))
        p.drawRoundedRect(QRectF(tx - 7, ttop, 14, tbot - ttop), 7, 7)
        p.drawEllipse(QPointF(tx, tbot + 6), 11, 11)
        if math.isfinite(self._t):
            lo, hi = math.log(self._t_lo), math.log(self._t_hi)
            f = (math.log(max(self._t, self._t_lo)) - lo) / (hi - lo)
            f = max(0.0, min(1.0, f))
            fill = QColor(COLORS["ok"]) if self._temp_ok else accent
            p.setPen(Qt.NoPen); p.setBrush(fill)
            y = tbot - f * (tbot - ttop - 6)
            p.drawRoundedRect(QRectF(tx - 4, y, 8, tbot - y + 4), 4, 4)
            p.drawEllipse(QPointF(tx, tbot + 6), 8, 8)
        # scale labels
        p.setPen(muted)
        fnt = p.font(); fnt.setPointSize(7); p.setFont(fnt)
        p.drawText(QRectF(tx + 12, ttop - 4, 50, 12), Qt.AlignLeft, f"{self._t_hi:g} K")
        p.drawText(QRectF(tx + 12, tbot - 8, 50, 12), Qt.AlignLeft, f"{self._t_lo:g} K")

        # ---- caption ------------------------------------------------------------------
        if self._ramping:
            cap, col = "RAMPING", QColor(COLORS["accent_hi"])
        elif self._field_ok:
            cap, col = "HOLDING", QColor(COLORS["ok"])
        else:
            cap, col = "SETTLING", muted
        p.setPen(col)
        fnt.setBold(True); fnt.setPointSize(8); p.setFont(fnt)
        p.drawText(QRectF(cx - 60, h - 16, 120, 14), Qt.AlignHCenter, cap)
        p.end()


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        title = "PPMS DynaCool  -  field & temperature"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1180, 740)
        self._t0 = time.monotonic()
        self._hist_t: deque = deque()
        self._hist_b: deque = deque()
        self._hist_bsp: deque = deque()
        self._hist_T: deque = deque()
        self._hist_Tsp: deque = deque()
        self._last_hist = 0.0

        root = QtWidgets.QWidget(); root.setObjectName("root")
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

    # ---- layout ----------------------------------------------------------

    def _build_sidebar(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(360)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(16)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("DYNACOOL")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        header.addWidget(settings_btn)
        col.addLayout(header)

        # connection / chamber card
        ccard, clay = _card()
        top = QtWidgets.QHBoxLayout()
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        self.conn_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        top.addWidget(self.conn_dot); top.addStretch(1)
        clay.addLayout(top)
        self.idn_label = QtWidgets.QLabel("—")
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        clay.addWidget(self.idn_label)
        self.chamber_label = QtWidgets.QLabel("Chamber: —")
        clay.addWidget(self.chamber_label)
        col.addWidget(ccard)

        lim, f, t = self.cfg.limits, self.cfg.field, self.cfg.temperature

        # ---- field card ------------------------------------------------------
        fcard, flay = _card("Magnetic field")
        form = QtWidgets.QFormLayout(); form.setSpacing(8)
        self.field_spin = QtWidgets.QDoubleSpinBox()
        self.field_spin.setDecimals(2); self.field_spin.setSingleStep(10.0)
        self.field_spin.setSuffix("  mT")
        self.frate_spin = QtWidgets.QDoubleSpinBox()
        self.frate_spin.setDecimals(2); self.frate_spin.setSuffix("  mT/s")
        self.fapproach = QtWidgets.QComboBox(); self.fapproach.addItems(FIELD_APPROACHES)
        form.addRow("Setpoint", self.field_spin)
        form.addRow("Rate", self.frate_spin)
        form.addRow("Approach", self.fapproach)
        flay.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        set_f = QtWidgets.QPushButton("Set field"); set_f.setObjectName("primary")
        set_f.clicked.connect(self._set_field)
        zero_f = QtWidgets.QPushButton("Go to zero")
        zero_f.clicked.connect(self._zero_field)
        row.addWidget(set_f, 1); row.addWidget(zero_f)
        flay.addLayout(row)
        col.addWidget(fcard)

        # ---- temperature card ------------------------------------------------
        tcard, tlay = _card("Temperature")
        form = QtWidgets.QFormLayout(); form.setSpacing(8)
        self.temp_spin = QtWidgets.QDoubleSpinBox()
        self.temp_spin.setDecimals(3); self.temp_spin.setSingleStep(1.0)
        self.temp_spin.setSuffix("  K")
        self.trate_spin = QtWidgets.QDoubleSpinBox()
        self.trate_spin.setDecimals(2); self.trate_spin.setSuffix("  K/min")
        self.tapproach = QtWidgets.QComboBox(); self.tapproach.addItems(TEMPERATURE_APPROACHES)
        form.addRow("Setpoint", self.temp_spin)
        form.addRow("Rate", self.trate_spin)
        form.addRow("Approach", self.tapproach)
        tlay.addLayout(form)
        set_t = QtWidgets.QPushButton("Set temperature"); set_t.setObjectName("primary")
        set_t.clicked.connect(self._set_temperature)
        tlay.addWidget(set_t)
        col.addWidget(tcard)

        self._apply_limits_to_widgets()
        self.frate_spin.setValue(f.rate_mT_per_s)
        self.fapproach.setCurrentText(f.approach)
        self.trate_spin.setValue(t.rate_K_per_min)
        self.tapproach.setCurrentText(t.approach)
        self._spins_seeded = False        # seeded from the first status (adopted setpoints)

        col.addStretch(1)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        ocard, olay = _card("Sample environment")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(24)
        fbox, self.field_value, self.field_sub, self.field_lamp = self._readout("Field", "mT")
        tbox, self.temp_value, self.temp_sub, self.temp_lamp = self._readout("Temperature", "K")
        row.addWidget(fbox); row.addWidget(tbox)
        row.addStretch(1)
        self.indicator = CryostatIndicator()
        row.addWidget(self.indicator, 0, QtCore.Qt.AlignVCenter)
        olay.addLayout(row)
        colw.addWidget(ocard)

        gcard, glay = _card("History")
        self.field_plot, self.field_curve, self.field_sp_curve = self._make_plot("field", "mT")
        self.temp_plot, self.temp_curve, self.temp_sp_curve = self._make_plot("temperature", "K")
        glay.addWidget(self.field_plot, 1)
        glay.addWidget(self.temp_plot, 1)
        colw.addWidget(gcard, 3)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(90)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 1)
        return panel

    def _readout(self, label, unit):
        holder = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(holder); box.setSpacing(2); box.setContentsMargins(0, 0, 0, 0)
        cap = QtWidgets.QLabel(label.upper())
        cap.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; font-weight:700; letter-spacing:1px;")
        line = QtWidgets.QHBoxLayout(); line.setSpacing(5)
        val = QtWidgets.QLabel("—"); val.setObjectName("bigValue"); val.setMinimumWidth(150)
        u = QtWidgets.QLabel(unit); u.setObjectName("unit")
        line.addWidget(val); line.addWidget(u, 0, QtCore.Qt.AlignBottom)
        sub = QtWidgets.QLabel("—")
        sub.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        lamp = QtWidgets.QLabel("●  not reached")
        box.addWidget(cap); box.addLayout(line); box.addWidget(sub); box.addWidget(lamp)
        return holder, val, sub, lamp

    def _make_plot(self, name, unit):
        import pyqtgraph as pg
        pg.setConfigOptions(antialias=True)
        w = pg.PlotWidget(background=COLORS["code_bg"])
        w.setMinimumHeight(120)
        pen = pg.mkPen(COLORS["muted"])
        for axis in ("left", "bottom"):
            ax = w.getAxis(axis); ax.setPen(pen); ax.setTextPen(pen)
        w.setLabel("left", f"{name} ({unit})")
        w.setLabel("bottom", "time (s)")
        w.showGrid(x=True, y=True, alpha=0.15)
        sp = w.plot([], [], pen=pg.mkPen(COLORS["muted"], width=1, style=QtCore.Qt.DashLine))
        curve = w.plot([], [], pen=pg.mkPen(COLORS["accent"], width=2))
        return w, curve, sp

    def _apply_limits_to_widgets(self):
        lim = self.cfg.limits
        self.field_spin.setRange(-lim.field_max_mT, lim.field_max_mT)
        self.frate_spin.setRange(lim.field_rate_min_mT_per_s, lim.field_rate_max_mT_per_s)
        self.temp_spin.setRange(lim.temperature_min_K, lim.temperature_max_K)
        self.trate_spin.setRange(lim.temperature_rate_min_K_per_min,
                                 lim.temperature_rate_max_K_per_min)

    # ---- actions ---------------------------------------------------------

    def _call(self, fn, *args):
        """Run a command; a refusal goes to the log instead of crashing the GUI."""
        try:
            fn(*args)
        except Exception as exc:
            self._on_event("error", str(exc))

    def _set_field(self):
        # rate and approach first: they are what the setpoint is sent with
        self._call(self.ctrl.set_field_rate, self.frate_spin.value())
        self._call(self.ctrl.set_field_approach, self.fapproach.currentText())
        self._call(self.ctrl.set_field, self.field_spin.value())

    def _zero_field(self):
        self.field_spin.setValue(0.0)
        self._set_field()

    def _set_temperature(self):
        self._call(self.ctrl.set_temperature_rate, self.trate_spin.value())
        self._call(self.ctrl.set_temperature_approach, self.tapproach.currentText())
        self._call(self.ctrl.set_temperature, self.temp_spin.value())

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        self._apply_limits_to_widgets()
        self.frate_spin.setValue(self.cfg.field.rate_mT_per_s)
        self.fapproach.setCurrentText(self.cfg.field.approach)
        self.trate_spin.setValue(self.cfg.temperature.rate_K_per_min)
        self.tapproach.setCurrentText(self.cfg.temperature.approach)

    # ---- refresh & events ------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = COLORS["danger"] if level == "error" else (
            COLORS["accent"] if level == "warn" else COLORS["muted"])
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(
            f'<span style="color:{COLORS["accent_dim"]}">{stamp}</span> '
            f'<span style="color:{color}">{msg}</span>')

    def _lamp(self, lamp, ok: bool):
        if ok:
            lamp.setText("●  reached")
            lamp.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            lamp.setText("●  not reached")
            lamp.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")

    def _refresh(self):
        s = self.ctrl.status()

        # the spin boxes start at what MultiVu was ALREADY set to (adopted),
        # so pressing "Set" without editing never moves anything
        if not self._spins_seeded and s.connected and math.isfinite(s.setpoint_field_mT):
            self.field_spin.setValue(s.setpoint_field_mT)
            self.temp_spin.setValue(s.setpoint_temperature_K)
            self._spins_seeded = True

        self.field_value.setText(_fmt(s.measured_field_mT, ",.2f"))
        self.field_sub.setText(f"set {_fmt(s.setpoint_field_mT, ',.2f')} mT  ·  {s.field_status or '—'}")
        self.temp_value.setText(_fmt(s.temperature_K, ".3f"))
        self.temp_sub.setText(f"set {_fmt(s.setpoint_temperature_K, '.3f')} K  ·  "
                              f"{s.temperature_status or '—'}")
        self._lamp(self.field_lamp, s.field_stable)
        self._lamp(self.temp_lamp, s.temperature_stable)
        self.chamber_label.setText(f"Chamber: {s.chamber or '—'}")

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

        lim = self.cfg.limits
        ramping = s.field_status not in ("", "Holding (driven)", "Stable")
        self.indicator.set_state(s.measured_field_mT, lim.field_max_mT, s.temperature_K,
                                 lim.temperature_min_K, lim.temperature_max_K,
                                 ramping, s.field_stable, s.temperature_stable)

        # history: one point every 0.5 s is plenty for a cryostat
        now = time.monotonic() - self._t0
        if now - self._last_hist >= 0.5 and math.isfinite(s.measured_field_mT):
            self._last_hist = now
            self._hist_t.append(now)
            self._hist_b.append(s.measured_field_mT)
            self._hist_bsp.append(s.setpoint_field_mT)
            self._hist_T.append(s.temperature_K)
            self._hist_Tsp.append(s.setpoint_temperature_K)
            while self._hist_t and now - self._hist_t[0] > HISTORY_S:
                for d in (self._hist_t, self._hist_b, self._hist_bsp, self._hist_T, self._hist_Tsp):
                    d.popleft()
            t = list(self._hist_t)
            self.field_curve.setData(t, list(self._hist_b))
            self.field_sp_curve.setData(t, list(self._hist_bsp))
            self.temp_curve.setData(t, list(self._hist_T))
            self.temp_sp_curve.setData(t, list(self._hist_Tsp))

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()          # disconnect; field and temperature are left alone
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Cryostat-like object (an in-process Cryostat, or
    a PpmsClient facade for a remote service). The theme is chosen ONCE here,
    from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # swap the active palette first
    # Numbers in the C locale, not the Windows one (gotcha #18): under a
    # comma-decimal locale a 10 K setpoint shows as "10,000", which any
    # English reader takes for ten thousand.
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
    overrides cfg.ui.theme for this launch."""
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    cryo, _ = build_sim_system(cfg)
    return run_app(cryo, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
