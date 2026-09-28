"""Dark-theme control GUI for the magnet controller.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py

Architecture in one breath: the Controller runs its own threads (acquisition +
control loop). This window NEVER touches the hardware directly -- it sends
commands (set_field, set_current, demag, calibrate) which the control loop
picks up from its queue, and it reads back a thread-safe status snapshot on a
30 ms Qt timer to update the plot and the numbers. Controller events (info /
error) arrive on a Qt signal so they can safely cross from the control thread
into the GUI thread.
"""

from __future__ import annotations

import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from ..config import Config
from ..calibration import FieldCalibration
from ..sim_system import build_sim_system
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog
from .calibration_viewer import CalibrationViewer, can_view
from .aux_panel import AuxPanel


# ------------------------------------------------------------- signal bridge

class Bridge(QtCore.QObject):
    """Carries controller events across the thread boundary into the GUI."""
    event = QtCore.Signal(str, str)
    cal_done = QtCore.Signal(object)     # a fresh FieldCalibration from a run


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


class MagnetIndicator(QtWidgets.QWidget):
    """A small dipole-magnet glyph (the GMW 3470) that glows amber when current
    flows. Two pole pieces face each other across a gap; the gap lights up with
    intensity proportional to |current|, and the N/S labels flip with polarity."""

    def __init__(self, name: str = "GMW 3470"):
        super().__init__()
        self.setFixedHeight(96)
        self._current = 0.0
        self._max = 3.0
        self._name = name

    def set_state(self, current_A: float, max_A: float):
        self._current = current_A
        self._max = max(1e-6, max_A)
        self.update()

    def paintEvent(self, ev):
        from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QRadialGradient
        from PySide6.QtCore import QRectF, Qt

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2 - 4

        intensity = min(1.0, abs(self._current) / self._max)
        energized = abs(self._current) > 0.01

        gap, poleW, poleH = 44.0, 66.0, 46.0
        top = cy - poleH / 2
        left_x = cx - gap / 2 - poleW
        right_x = cx + gap / 2

        # glow + field lines in the gap
        if energized:
            grad = QRadialGradient(cx, cy, gap)
            a = int(35 + 190 * intensity)
            grad.setColorAt(0.0, QColor(255, 158, 44, a))
            grad.setColorAt(1.0, QColor(255, 158, 44, 0))
            p.setPen(Qt.NoPen); p.setBrush(QBrush(grad))
            p.drawRect(QRectF(cx - gap, top - 8, 2 * gap, poleH + 16))
            p.setPen(QPen(QColor(255, 180, 84, int(150 * intensity)), 2))
            forward = self._current >= 0
            for frac in (0.3, 0.5, 0.7):
                ly = top + poleH * frac
                x0, x1 = (left_x + poleW, right_x) if forward else (right_x, left_x + poleW)
                p.drawLine(int(x0), int(ly), int(x1), int(ly))
                # little arrowhead at the receiving pole
                dx = 5 if forward else -5
                p.drawLine(int(x1), int(ly), int(x1 - dx), int(ly - 3))
                p.drawLine(int(x1), int(ly), int(x1 - dx), int(ly + 3))

        # pole pieces (metal)
        p.setPen(QPen(QColor("#4a525e"), 1)); p.setBrush(QBrush(QColor("#39404a")))
        p.drawRoundedRect(QRectF(left_x, top, poleW, poleH), 8, 8)
        p.drawRoundedRect(QRectF(right_x, top, poleW, poleH), 8, 8)

        # coil hints, brighter when energized
        coil = QColor("#ffb454") if energized else QColor("#7c5327")
        p.setPen(QPen(coil, 3))
        for i in range(3):
            xl = left_x + 16 + i * 16
            xr = right_x + 16 + i * 16
            p.drawLine(int(xl), int(top - 5), int(xl), int(top + poleH + 5))
            p.drawLine(int(xr), int(top - 5), int(xr), int(top + poleH + 5))

        # N / S labels on the inner faces
        p.setPen(QColor("#e8eaed"))
        f = p.font(); f.setBold(True); f.setPointSize(12); p.setFont(f)
        ln, rn = ("N", "S") if self._current >= 0 else ("S", "N")
        p.drawText(QRectF(left_x, top, poleW, poleH), Qt.AlignRight | Qt.AlignVCenter, ln + " ")
        p.drawText(QRectF(right_x, top, poleW, poleH), Qt.AlignLeft | Qt.AlignVCenter, " " + rn)

        # caption
        if energized:
            cap, col = f"ENERGIZED · {abs(self._current):.3f} A", QColor("#ffb454")
        else:
            cap, col = "de-energized", QColor("#8b929c")
        p.setPen(col)
        f2 = p.font(); f2.setBold(True); f2.setPointSize(8); p.setFont(f2)
        p.drawText(QRectF(0, h - 16, w, 14), Qt.AlignHCenter, cap)
        p.end()


# state -> palette KEY (resolved live so it follows the active theme)
STATE_COLOR_KEY = {
    "IDLE": "muted",
    "RAMPING": "accent",
    "SEEK": "accent",
    "STABLE": "ok",
    "HOLD": "text",
    "DEMAG": "accent_hi",
    "CALIBRATE": "accent_hi",
}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, cal, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        # remote = we drive the magnet through a ClMagClient over the network.
        # Settings still work (they go over the socket), but a couple of flows
        # differ (calibration run has no completion callback across the wire).
        self._remote = remote
        title = "clMag · Magnet Field Controller"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1120, 680)

        # rolling history for the plot
        self._t0 = time.monotonic()
        self._t = deque(maxlen=6000)
        self._field = deque(maxlen=6000)
        self._window_s = 30.0

        tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(tabs)

        control = QtWidgets.QWidget(); control.setObjectName("root")
        outer = QtWidgets.QHBoxLayout(control)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(cal), 0)
        outer.addWidget(self._build_main(), 1)
        tabs.addTab(control, "Control")

        # AUX I/O tab (the DAQ's other BNCs). It works over the network too.
        self.aux_panel = AuxPanel(self.ctrl, self.cfg)
        tabs.addTab(self.aux_panel, "AUX I/O")

        # controller events -> log
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.bridge.cal_done.connect(self._on_cal_done)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

        # start the controller and the refresh timer
        self.ctrl.start()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

    # ---- layout ----------------------------------------------------------

    def _build_sidebar(self, cal: FieldCalibration) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(340)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(16)

        # --- header row: title + settings
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("clMag")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("⚙  Settings")
        settings_btn.clicked.connect(self._open_settings)
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        header.addWidget(settings_btn)
        col.addLayout(header)

        # --- readout card
        rcard, rlay = _card()
        top = QtWidgets.QHBoxLayout()
        self.state_badge = QtWidgets.QLabel("IDLE")
        self.state_badge.setObjectName("stateBadge")
        top.addWidget(self.state_badge)
        top.addStretch(1)
        self.stable_dot = QtWidgets.QLabel("●  not stable")
        self.stable_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        top.addWidget(self.stable_dot)
        rlay.addLayout(top)

        fld = QtWidgets.QHBoxLayout(); fld.setSpacing(6)
        self.field_value = QtWidgets.QLabel("0.00"); self.field_value.setObjectName("bigValue")
        unit = QtWidgets.QLabel("mT"); unit.setObjectName("unit")
        fld.addWidget(self.field_value); fld.addWidget(unit, 0, QtCore.Qt.AlignBottom); fld.addStretch(1)
        rlay.addLayout(fld)

        self.current_value = QtWidgets.QLabel("current  0.000 A")
        self.current_value.setStyleSheet(f"color:{COLORS['muted']};")
        self.setpoint_value = QtWidgets.QLabel("setpoint  —")
        self.setpoint_value.setStyleSheet(f"color:{COLORS['muted']};")
        rlay.addWidget(self.current_value)
        rlay.addWidget(self.setpoint_value)
        col.addWidget(rcard)

        # --- magnet energized indicator
        mcard, mlay = _card("Magnet · GMW 3470")
        self.magnet = MagnetIndicator()
        mlay.addWidget(self.magnet)
        col.addWidget(mcard)

        # --- field control
        fcard, flay = _card("Field")
        self.field_spin = QtWidgets.QDoubleSpinBox()
        lo, hi = cal.range_mT
        self.field_spin.setRange(round(lo, 1), round(hi, 1))
        self.field_spin.setDecimals(2); self.field_spin.setSingleStep(1.0)
        self.field_spin.setValue(50.0); self.field_spin.setSuffix("  mT")
        flay.addWidget(self.field_spin)
        self.pid_check = QtWidgets.QCheckBox("Use PID fine-tuning")
        self.pid_check.setChecked(True)
        flay.addWidget(self.pid_check)
        set_field_btn = QtWidgets.QPushButton("Set Field"); set_field_btn.setObjectName("primary")
        set_field_btn.clicked.connect(self._set_field)
        flay.addWidget(set_field_btn)
        col.addWidget(fcard)

        # --- current control
        ccard, clay = _card("Current")
        row = QtWidgets.QHBoxLayout()
        self.current_spin = QtWidgets.QDoubleSpinBox()
        self.current_spin.setRange(-self.cfg.limits.current_max_A, self.cfg.limits.current_max_A)
        self.current_spin.setDecimals(3); self.current_spin.setSingleStep(0.05)
        self.current_spin.setSuffix("  A")
        set_cur_btn = QtWidgets.QPushButton("Set Current")
        set_cur_btn.clicked.connect(self._set_current)
        row.addWidget(self.current_spin, 1); row.addWidget(set_cur_btn)
        clay.addLayout(row)
        col.addWidget(ccard)

        # --- demag + calibrate + stabilizer
        acard, alay = _card("Routines")
        drow = QtWidgets.QHBoxLayout()
        self.demag_spin = QtWidgets.QDoubleSpinBox()
        self.demag_spin.setRange(0.0, self.cfg.limits.current_max_A)
        self.demag_spin.setDecimals(2); self.demag_spin.setSingleStep(0.1)
        self.demag_spin.setValue(1.5); self.demag_spin.setSuffix("  A")
        demag_btn = QtWidgets.QPushButton("Demagnetize")
        demag_btn.clicked.connect(self._demag)
        drow.addWidget(self.demag_spin, 1); drow.addWidget(demag_btn)
        alay.addLayout(drow)

        crow = QtWidgets.QHBoxLayout()
        self.cal_spin = QtWidgets.QSpinBox()
        self.cal_spin.setRange(10, 100); self.cal_spin.setValue(30); self.cal_spin.setSuffix("  pts/leg")
        cal_btn = QtWidgets.QPushButton("Run Calibration")
        cal_btn.clicked.connect(self._calibrate)
        crow.addWidget(self.cal_spin, 1); crow.addWidget(cal_btn)
        alay.addLayout(crow)

        vrow = QtWidgets.QHBoxLayout()
        view_btn = QtWidgets.QPushButton("View curve…")
        view_btn.clicked.connect(self._view_calibration)
        vrow.addStretch(1); vrow.addWidget(view_btn)
        alay.addLayout(vrow)

        self.stab_check = QtWidgets.QCheckBox("Long-term stabilizer")
        self.stab_check.setChecked(True)
        self.stab_check.toggled.connect(lambda on: setattr(self.ctrl, "stabilizer_enabled", on))
        alay.addWidget(self.stab_check)
        col.addWidget(acard)

        col.addStretch(1)
        stop_btn = QtWidgets.QPushButton("Ramp to Zero  &  Stop"); stop_btn.setObjectName("danger")
        stop_btn.clicked.connect(lambda: self.ctrl.set_current(0.0))
        col.addWidget(stop_btn)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(16)

        # plot card
        pcard, play = _card("Field  (mT)  vs  time  (s)")
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setBackground(COLORS["panel"])
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        for ax in ("left", "bottom"):
            axis = self.plot.getAxis(ax)
            axis.setPen(pg.mkPen(COLORS["muted"]))
            axis.setTextPen(pg.mkPen(COLORS["muted"]))
        self.curve = self.plot.plot([], [], pen=pg.mkPen(COLORS["accent"], width=2))
        self.setpoint_line = pg.InfiniteLine(
            angle=0, pen=pg.mkPen(COLORS["muted"], width=1, style=QtCore.Qt.DashLine))
        self.setpoint_line.setVisible(False)
        self.plot.addItem(self.setpoint_line)
        self.tol_band = pg.LinearRegionItem(
            orientation="horizontal", movable=False,
            brush=pg.mkBrush(255, 158, 44, 28), pen=pg.mkPen(None))
        self.tol_band.setVisible(False)
        self.plot.addItem(self.tol_band)
        play.addWidget(self.plot)
        col.addWidget(pcard, 1)

        # status log card
        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setFixedHeight(150)
        llay.addWidget(self.log)
        col.addWidget(lcard)
        return panel

    # ---- actions ---------------------------------------------------------

    def _set_field(self):
        self.ctrl.set_field(self.field_spin.value(), use_pid=self.pid_check.isChecked())

    def _set_current(self):
        self.ctrl.set_current(self.current_spin.value())

    def _demag(self):
        self.ctrl.demag(self.demag_spin.value())

    def _calibrate(self):
        # in-process controller supports an on_done callback; the remote client
        # (no apply_config) does not, so it just fires the command.
        if self._remote:
            self.ctrl.calibrate(n_per_leg=self.cal_spin.value(), dwell_s=0.15)
        else:
            self.ctrl.calibrate(n_per_leg=self.cal_spin.value(), dwell_s=0.15,
                                 on_done=lambda cal: self.bridge.cal_done.emit(cal))

    def _on_cal_done(self, cal):
        """A fresh calibration finished measuring -> resync ranges and show it."""
        self._on_settings_applied()
        if can_view(cal):
            CalibrationViewer(cal, parent=self,
                              title="Calibration curve (just measured)").exec()

    def _view_calibration(self):
        cal = self.ctrl.get_calibration()      # local: the live curve; remote: fetched
        if not can_view(cal):
            QtWidgets.QMessageBox.information(
                self, "No curve to show",
                "There is no calibration curve to view yet. Run a calibration, or "
                "load one from Settings › Calibration.")
            return
        CalibrationViewer(cal, parent=self).exec()

    def _open_settings(self):
        # refresh from the source of truth first (a no-op locally; a fetch over
        # the socket in remote mode) so the dialog shows current values.
        self.ctrl.get_config()
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        """Re-sync widget ranges after settings or a calibration file change."""
        cal = self.ctrl.get_calibration()
        if cal and getattr(cal, "currents_A", None):
            lo, hi = cal.range_mT
            self.field_spin.setRange(round(lo, 1), round(hi, 1))
        lim = self.cfg.limits.current_max_A
        self.current_spin.setRange(-lim, lim)
        self.demag_spin.setRange(0.0, lim)

    # ---- refresh & events ------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = COLORS["danger"] if level == "error" else COLORS["muted"]
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(
            f'<span style="color:{COLORS["accent_dim"]}">{stamp}</span> '
            f'<span style="color:{color}">{msg}</span>')

    def _refresh(self):
        s = self.ctrl.status()
        t = time.monotonic() - self._t0
        self._t.append(t)
        self._field.append(s.measured_field_mT)

        self.field_value.setText(f"{s.measured_field_mT:.2f}")
        self.current_value.setText(f"current  {s.current_A:.3f} A")
        self.magnet.set_state(s.current_A, self.cfg.limits.current_max_A)
        if s.setpoint_field_mT is None:
            self.setpoint_value.setText("setpoint  —")
            self.setpoint_line.setVisible(False)
            self.tol_band.setVisible(False)
        else:
            self.setpoint_value.setText(f"setpoint  {s.setpoint_field_mT:.2f} mT")
            self.setpoint_line.setPos(s.setpoint_field_mT)
            self.setpoint_line.setVisible(True)
            tol = self.cfg.limits.field_tolerance_mT
            self.tol_band.setRegion((s.setpoint_field_mT - tol, s.setpoint_field_mT + tol))
            self.tol_band.setVisible(True)

        color = COLORS[STATE_COLOR_KEY.get(s.state, "text")]
        self.state_badge.setText(s.state)
        self.state_badge.setStyleSheet(
            f"QLabel#stateBadge {{ color:{color}; border-color:{color}; "
            f"background:{COLORS['panel_hi']}; border-radius:10px; padding:4px 12px; "
            f"font-weight:700; letter-spacing:1px; }}")
        if s.field_stable:
            self.stable_dot.setText("●  STABLE")
            self.stable_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.stable_dot.setText("●  seeking")
            self.stable_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")

        self.curve.setData(list(self._t), list(self._field))
        if t > self._window_s:
            self.plot.setXRange(t - self._window_s, t, padding=0)

        self.aux_panel.update_from_status(getattr(s, "aux", None))

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()      # ramp to zero + output off
        super().closeEvent(ev)


def run_app(ctrl, cfg, cal, remote: bool = False) -> int:
    """Start the Qt app with whatever controller-like object is given (a real
    in-process Controller, or a ClMagClient facade for a remote service)."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # choose palette BEFORE building widgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
    from .theme import apply_window_icon
    apply_window_icon(app)
    app.setStyle("Fusion")
    apply_palette(app)
    app.setStyleSheet(build_stylesheet())
    win = MainWindow(ctrl, cfg, cal, remote=remote)
    win.show()
    return app.exec()


def main(theme: str | None = None) -> int:
    """Default: run against the built-in simulator, in-process."""
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    ctrl, kepco, probe, acq, cal = build_sim_system(cfg)
    return run_app(ctrl, cfg, cal)


if __name__ == "__main__":
    raise SystemExit(main())
