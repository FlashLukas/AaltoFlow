"""Control GUI for the Kepco BOP bipolar power supply.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator (BOP + coil)
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a BipolarSupply-like object (the
real in-process brain, or a KepcoClient facade for a remote service). It sends
commands and reads a status snapshot on a Qt timer. Brain events arrive on a Qt
signal so they can safely cross into the GUI thread.

The signature widget is the QuadrantIndicator: the supply's V-I plane. A bipolar
OPERATIONAL supply works in all four quadrants -- it SOURCES power when V and I
have the same sign (I, III) and SINKS it when they differ (II, IV; e.g. while a
coil gives its energy back during a ramp-down). The dot is the measured
operating point, its fading trail the path it took (during a ramp into a
resistive coil that trail draws the load line), the dashed lines the limit
channel, the dotted line the target.
"""

from __future__ import annotations

import math
import os
import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import Config
from ..sim_system import build_sim_system
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog


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
    lbl = None
    if title:
        lbl = QtWidgets.QLabel(title.upper())
        lbl.setObjectName("cardTitle")
        lay.addWidget(lbl)
    return frame, lay, lbl


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _restyle(w: QtWidgets.QWidget, name: str) -> None:
    """Change a widget's objectName (the QSS selector) and re-apply the sheet."""
    if w.objectName() != name:
        w.setObjectName(name)
        w.style().unpolish(w)
        w.style().polish(w)


# ------------------------------------------------------------- the indicator

class QuadrantIndicator(QtWidgets.QWidget):
    """The BOP's four-quadrant V-I plane with the live operating point."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 260)
        self._trail: deque = deque(maxlen=120)
        self._st = None
        self._vmax = 20.0
        self._imax = 10.0
        self._pulse = 0.0
        self._timer = QtCore.QTimer(self)      # glow animation, ~30 fps
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, st, vmax: float, imax: float) -> None:
        self._st = st
        self._vmax = max(1e-6, float(vmax))
        self._imax = max(1e-6, float(imax))
        if st is not None and _finite(st.voltage_V) and _finite(st.current_A):
            self._trail.append((st.voltage_V, st.current_A))
        on = bool(st is not None and st.output)
        if on and not self._timer.isActive():
            self._timer.start()
        elif not on and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        self._pulse = (self._pulse + 0.04) % 1.0
        self.update()

    # -- drawing -----------------------------------------------------------

    def paintEvent(self, ev):
        from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QRadialGradient
        from PySide6.QtCore import QRectF, QPointF, Qt

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        # colours are read HERE, at paint time, so the theme is always current
        c_text, c_muted = QColor(COLORS["text"]), QColor(COLORS["muted"])
        c_grid, c_border = QColor(COLORS["grid"]), QColor(COLORS["border"])
        c_acc, c_hi = QColor(COLORS["accent"]), QColor(COLORS["accent_hi"])
        c_danger = QColor(COLORS["danger"])

        # plot area: leave room for tick labels and the caption
        left, right, top, bottom = 56, 16, 14, 34
        pw, ph = w - left - right, h - top - bottom
        if pw < 40 or ph < 40:
            p.end(); return
        cx, cy = left + pw / 2.0, top + ph / 2.0

        def X(v):  # volts -> pixels
            return cx + (v / self._vmax) * (pw / 2.0)

        def Y(i):  # amps -> pixels (up is positive)
            return cy - (i / self._imax) * (ph / 2.0)

        # quadrant shading: source (I, III) vs sink (II, IV)
        src = QColor(c_acc); src.setAlpha(22)
        snk = QColor(c_muted); snk.setAlpha(14)
        p.setPen(Qt.NoPen)
        p.fillRect(QRectF(cx, top, pw / 2, ph / 2), src)
        p.fillRect(QRectF(left, cy, pw / 2, ph / 2), src)
        p.fillRect(QRectF(left, top, pw / 2, ph / 2), snk)
        p.fillRect(QRectF(cx, cy, pw / 2, ph / 2), snk)

        f = p.font(); f.setPointSize(7); f.setBold(True); p.setFont(f)
        p.setPen(c_muted)
        for (qx, qy, lab) in ((1, -1, "SOURCE"), (-1, 1, "SOURCE"),
                              (-1, -1, "SINK"), (1, 1, "SINK")):
            rx = cx + (6 if qx > 0 else -pw / 2 + 6)
            ry = cy + (6 if qy > 0 else -ph / 2 + 4)
            p.drawText(QRectF(rx, ry, 70, 14), Qt.AlignLeft, lab)

        # grid at half scale + the axes
        p.setPen(QPen(c_grid, 1))
        for fr in (-0.5, 0.5):
            p.drawLine(QPointF(X(fr * self._vmax), top), QPointF(X(fr * self._vmax), top + ph))
            p.drawLine(QPointF(left, Y(fr * self._imax)), QPointF(left + pw, Y(fr * self._imax)))
        p.setPen(QPen(c_border, 1.4))
        p.drawRect(QRectF(left, top, pw, ph))
        p.drawLine(QPointF(left, cy), QPointF(left + pw, cy))
        p.drawLine(QPointF(cx, top), QPointF(cx, top + ph))

        # tick labels
        f.setBold(False); f.setPointSize(8); p.setFont(f)
        p.setPen(c_muted)
        p.drawText(QRectF(0, top - 2, left - 6, 14), Qt.AlignRight, f"{self._imax:+g} A")
        p.drawText(QRectF(0, top + ph - 12, left - 6, 14), Qt.AlignRight, f"{-self._imax:+g} A")
        p.drawText(QRectF(0, cy - 7, left - 6, 14), Qt.AlignRight, "I")
        p.drawText(QRectF(left, top + ph + 2, 60, 14), Qt.AlignLeft, f"{-self._vmax:+g} V")
        p.drawText(QRectF(left + pw - 60, top + ph + 2, 60, 14), Qt.AlignRight, f"{self._vmax:+g} V")
        p.drawText(QRectF(cx - 10, top + ph + 2, 20, 14), Qt.AlignHCenter, "V")

        st = self._st
        if st is None:
            p.end(); return
        on = bool(st.output)

        # the limit channel (dashed) and the target (dotted)
        lim_pen = QPen(c_hi if on else c_muted, 1.4, Qt.DashLine)
        tgt_pen = QPen(c_acc if on else c_muted, 1.4, Qt.DotLine)
        if st.mode == "current":
            vl = min(abs(st.voltage_limit_V or 0.0), self._vmax)
            p.setPen(lim_pen)
            for s in (-1, 1):
                p.drawLine(QPointF(X(s * vl), top), QPointF(X(s * vl), top + ph))
            p.setPen(tgt_pen)
            p.drawLine(QPointF(left, Y(st.current_set_A)), QPointF(left + pw, Y(st.current_set_A)))
            prog_pt = QPointF(cx, Y(st.programmed)) if _finite(st.programmed) else None
        else:
            il = min(abs(st.current_limit_A or 0.0), self._imax)
            p.setPen(lim_pen)
            for s in (-1, 1):
                p.drawLine(QPointF(left, Y(s * il)), QPointF(left + pw, Y(s * il)))
            p.setPen(tgt_pen)
            p.drawLine(QPointF(X(st.voltage_set_V), top), QPointF(X(st.voltage_set_V), top + ph))
            prog_pt = QPointF(X(st.programmed), cy) if _finite(st.programmed) else None

        # where the ramp is: a small diamond on the regulated axis
        if on and prog_pt is not None:
            p.setPen(Qt.NoPen); p.setBrush(c_acc)
            d = 5.0
            p.drawPolygon(QtGui.QPolygonF([QPointF(prog_pt.x(), prog_pt.y() - d),
                                           QPointF(prog_pt.x() + d, prog_pt.y()),
                                           QPointF(prog_pt.x(), prog_pt.y() + d),
                                           QPointF(prog_pt.x() - d, prog_pt.y())]))

        # the trail, oldest faintest
        n = len(self._trail)
        p.setPen(Qt.NoPen)
        for k, (v, i) in enumerate(self._trail):
            col = QColor(c_acc if on else c_muted)
            col.setAlpha(int(20 + 120 * (k + 1) / max(1, n)))
            p.setBrush(col)
            p.drawEllipse(QPointF(X(v), Y(i)), 1.8, 1.8)

        # the operating point
        if _finite(st.voltage_V) and _finite(st.current_A):
            pt = QPointF(X(st.voltage_V), Y(st.current_A))
            if on:
                glow_r = 16 + 5 * math.sin(2 * math.pi * self._pulse)
                g = QRadialGradient(pt, glow_r)
                c0 = QColor(c_danger if st.at_limit else c_acc); c0.setAlpha(170)
                c1 = QColor(c0); c1.setAlpha(0)
                g.setColorAt(0.0, c0); g.setColorAt(1.0, c1)
                p.setBrush(QBrush(g)); p.drawEllipse(pt, glow_r, glow_r)
                p.setBrush(c_hi if not st.at_limit else c_danger)
                p.setPen(QPen(c_text, 1.2))
                p.drawEllipse(pt, 5.5, 5.5)
            else:
                p.setBrush(Qt.NoBrush); p.setPen(QPen(c_muted, 1.6))
                p.drawEllipse(pt, 5.5, 5.5)

        # caption
        if not on:
            cap, col = "OUTPUT OFF", c_muted
        elif st.at_limit:
            cap = "AT " + ("VOLTAGE" if st.mode == "current" else "CURRENT") + " LIMIT"
            col = c_danger
        elif st.ramping:
            cap, col = "RAMPING", c_hi
        elif _finite(st.power_W) and st.power_W < -1e-3:
            cap, col = f"SINKING  {abs(st.power_W):.2f} W", c_hi
        else:
            pw_ = st.power_W if _finite(st.power_W) else 0.0
            cap, col = f"SOURCING  {pw_:.2f} W", c_hi
        f.setBold(True); f.setPointSize(9); p.setFont(f)
        p.setPen(col)
        p.drawText(QRectF(0, h - 16, w, 15), Qt.AlignHCenter, cap)
        p.end()


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self._mode_shown = None
        self._badge = None
        title = "Kepco BOP - Bipolar Power Supply"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1280, 860)

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

        # start the brain (opens the backend; a no-op fetch for a remote client)
        self.ctrl.start()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()
        self._refresh()

    # ---- layout ----------------------------------------------------------

    def _spin(self, dec=4, step=0.01):
        w = QtWidgets.QDoubleSpinBox()
        w.setDecimals(dec); w.setSingleStep(step)
        w.setKeyboardTracking(False)
        return w

    def _row(self, lay, spin, slot, text="Set"):
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton(text); b.setObjectName("primary")
        b.clicked.connect(slot)
        row.addWidget(spin, 1); row.addWidget(b)
        lay.addLayout(row)

    def _build_sidebar(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(360)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("KEPCO BOP")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; "
                            f"font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        header.addWidget(settings_btn)
        col.addLayout(header)

        # state card
        scard, slay, _ = _card()
        top = QtWidgets.QHBoxLayout()
        self.state_badge = QtWidgets.QLabel("OUTPUT OFF")
        self.state_badge.setObjectName("stateBadge")
        top.addWidget(self.state_badge); top.addStretch(1)
        self.conn_dot = QtWidgets.QLabel("connecting")
        top.addWidget(self.conn_dot)
        slay.addLayout(top)
        self.idn_label = QtWidgets.QLabel("-")
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        self.idn_label.setWordWrap(True)
        slay.addWidget(self.idn_label)
        col.addWidget(scard)

        # mode
        mcard, mlay, _ = _card("Mode")
        mrow = QtWidgets.QHBoxLayout()
        self.btn_cur = QtWidgets.QPushButton("Current")
        self.btn_volt = QtWidgets.QPushButton("Voltage")
        self.btn_cur.clicked.connect(lambda: self._safe(self.ctrl.set_mode, "current"))
        self.btn_volt.clicked.connect(lambda: self._safe(self.ctrl.set_mode, "voltage"))
        mrow.addWidget(self.btn_cur); mrow.addWidget(self.btn_volt)
        mlay.addLayout(mrow)
        self.mode_hint = QtWidgets.QLabel("Change the mode with the output off.")
        self.mode_hint.setObjectName("hint")
        mlay.addWidget(self.mode_hint)
        col.addWidget(mcard)

        # setpoint
        tcard, tlay, self.set_title = _card("Setpoint")
        self.set_spin = self._spin()
        self._row(tlay, self.set_spin, self._apply_setpoint)
        col.addWidget(tcard)

        # limit channel
        lcard, llay, self.lim_title = _card("Limit")
        self.lim_spin = self._spin()
        self._row(llay, self.lim_spin, self._apply_limit)
        col.addWidget(lcard)

        # ramp
        rcard, rlay, self.rate_title = _card("Ramp rate")
        self.rate_spin = self._spin(dec=3, step=0.1)
        self._row(rlay, self.rate_spin, self._apply_rate)
        col.addWidget(rcard)

        # acquisition: the same averaged, settled read a scan uses
        acard, alay, _ = _card("Acquisition")
        arow = QtWidgets.QHBoxLayout()
        self.acq_label = QtWidgets.QLabel("no sample yet")
        self.acq_label.setWordWrap(True)
        acq_btn = QtWidgets.QPushButton("Acquire")
        acq_btn.setToolTip("Wait the settle time, then average fresh readings "
                           "(what a scan records).")
        acq_btn.clicked.connect(lambda: self._safe(self.ctrl.acquire))
        arow.addWidget(self.acq_label, 1); arow.addWidget(acq_btn)
        alay.addLayout(arow)
        col.addWidget(acard)

        col.addStretch(1)
        self.out_btn = QtWidgets.QPushButton("Output ON")
        self.out_btn.setObjectName("primary")
        self.out_btn.setMinimumHeight(44)
        self.out_btn.clicked.connect(self._toggle_output)
        col.addWidget(self.out_btn)
        kill = QtWidgets.QPushButton("Output off NOW (no ramp)")
        kill.setObjectName("danger")
        kill.setToolTip("Emergency only. With a coil attached the supply has to "
                        "absorb its stored energy.")
        kill.clicked.connect(lambda: self._safe(self.ctrl.output_off_now))
        col.addWidget(kill)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        ocard, olay, _ = _card("Measured at the output")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(28)
        self.v_value = self._readout(row, "Voltage", "V")
        self.i_value = self._readout(row, "Current", "A")
        self.p_value = self._readout(row, "Power", "W")
        self.prog_value, self.prog_unit = self._readout(row, "Programmed", "A", unit_ref=True)
        row.addStretch(1)
        olay.addLayout(row)
        colw.addWidget(ocard)

        qcard, qlay, _ = _card("Operating point (V-I plane)")
        self.quad = QuadrantIndicator()
        qlay.addWidget(self.quad, 1)
        colw.addWidget(qcard, 3)

        lcard, llay, _ = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(110)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 1)
        return panel

    def _readout(self, row, label, unit, unit_ref=False):
        box = QtWidgets.QVBoxLayout(); box.setSpacing(2)
        cap = QtWidgets.QLabel(label.upper())
        cap.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; "
                          f"font-weight:700; letter-spacing:1px;")
        line = QtWidgets.QHBoxLayout(); line.setSpacing(5)
        val = QtWidgets.QLabel("-"); val.setObjectName("bigValue")
        val.setMinimumWidth(150)
        u = QtWidgets.QLabel(unit); u.setObjectName("unit")
        line.addWidget(val); line.addWidget(u, 0, QtCore.Qt.AlignBottom)
        box.addWidget(cap); box.addLayout(line)
        holder = QtWidgets.QWidget(); holder.setLayout(box)
        row.addWidget(holder)
        return (val, u) if unit_ref else val

    # ---- mode-dependent widgets -------------------------------------------

    def _apply_mode_widgets(self, s) -> None:
        """Retitle and re-range the three spin boxes for the active mode, and
        load them with the service's values. Only when the mode CHANGES, so
        the user's half-typed number is never overwritten by the timer."""
        cur = s.mode == "current"
        lo, hi = self.ctrl.current_range() if cur else self.ctrl.voltage_range()
        unit = "A" if cur else "V"
        self.set_title.setText(("Current setpoint" if cur else "Voltage setpoint").upper())
        self.set_spin.setRange(lo, hi); self.set_spin.setSuffix(f"  {unit}")
        self.set_spin.setValue(s.current_set_A if cur else s.voltage_set_V)

        lmax = self.ctrl.voltage_limit_max() if cur else self.ctrl.current_limit_max()
        self.lim_title.setText(("Voltage limit (compliance)" if cur else "Current limit").upper())
        self.lim_spin.setRange(0.0, lmax)
        self.lim_spin.setSuffix("  V" if cur else "  A")
        self.lim_spin.setValue(s.voltage_limit_V if cur else s.current_limit_A)

        lim = self.cfg.limits
        self.rate_spin.setRange(0.001, lim.rate_max_A_per_s if cur else lim.rate_max_V_per_s)
        self.rate_spin.setSuffix("  A/s" if cur else "  V/s")
        self.rate_spin.setValue(s.ramp_rate_A_per_s if cur else s.ramp_rate_V_per_s)

        self.prog_unit.setText(unit)
        _restyle(self.btn_cur, "primary" if cur else "")
        _restyle(self.btn_volt, "" if cur else "primary")
        self._mode_shown = s.mode

    # ---- actions ---------------------------------------------------------

    def _safe(self, fn, *args):
        """Run a command; show a refusal (ValueError) in the log instead of crashing."""
        try:
            fn(*args)
        except ValueError as exc:
            self._on_event("error", str(exc))

    def _apply_setpoint(self):
        if self._mode_shown == "voltage":
            self._safe(self.ctrl.set_voltage, self.set_spin.value())
        else:
            self._safe(self.ctrl.set_current, self.set_spin.value())

    def _apply_limit(self):
        if self._mode_shown == "voltage":
            self._safe(self.ctrl.set_current_limit, self.lim_spin.value())
        else:
            self._safe(self.ctrl.set_voltage_limit, self.lim_spin.value())

    def _apply_rate(self):
        if self._mode_shown == "voltage":
            self._safe(lambda v: self.ctrl.set_ramp(rate_V_per_s=v), self.rate_spin.value())
        else:
            self._safe(lambda v: self.ctrl.set_ramp(rate_A_per_s=v), self.rate_spin.value())

    def _toggle_output(self):
        s = self.ctrl.status()
        self._safe(self.ctrl.set_output, not bool(s.output_request))

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        self._mode_shown = None         # re-range everything on the next refresh

    # ---- refresh & events ------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = COLORS["danger"] if level == "error" else (
            COLORS["accent"] if level == "warn" else COLORS["muted"])
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(
            f'<span style="color:{COLORS["accent_dim"]}">{stamp}</span> '
            f'<span style="color:{color}">{msg}</span>')

    def _refresh(self):
        s = self.ctrl.status()
        if s.mode != self._mode_shown:
            self._apply_mode_widgets(s)

        def fmt(x, spec):
            return format(x, spec) if _finite(x) else "-"
        self.v_value.setText(fmt(s.voltage_V, "+.4f"))
        self.i_value.setText(fmt(s.current_A, "+.4f"))
        self.p_value.setText(fmt(s.power_W, "+.3f"))
        self.prog_value.setText(fmt(s.programmed, "+.4f"))

        # badge + output button (restyle only when the state flips)
        if not s.output and not s.output_request:
            badge = ("OUTPUT OFF", COLORS["muted"])
        elif s.at_limit:
            badge = ("AT LIMIT", COLORS["danger"])
        elif s.ramping:
            badge = ("RAMPING", COLORS["accent"])
        else:
            badge = ("OUTPUT ON", COLORS["ok"])
        if badge != self._badge:
            self._badge = badge
            self.state_badge.setText(badge[0])
            self.state_badge.setStyleSheet(
                f"QLabel#stateBadge {{ color:{badge[1]}; border-color:{badge[1]}; "
                f"background:{COLORS['panel_hi']}; border-radius:10px; padding:4px 12px; "
                f"font-weight:700; letter-spacing:1px; }}")
        if s.output_request:
            self.out_btn.setText("Ramp down + Output OFF")
            _restyle(self.out_btn, "danger")
        else:
            self.out_btn.setText("Output ON (ramp up)")
            _restyle(self.out_btn, "primary")
        live = bool(s.output or s.output_request)
        self.btn_cur.setEnabled(not live); self.btn_volt.setEnabled(not live)

        if s.connected:
            text, col = "connected", COLORS["ok"]
        else:
            text, col = "offline", COLORS["danger"]
        if s.hw_error:
            text, col = "hardware error", COLORS["danger"]
            self.conn_dot.setToolTip(s.hw_error)
        self.conn_dot.setText(text)
        self.conn_dot.setStyleSheet(f"color:{col}; font-weight:700;")
        if s.idn:
            self.idn_label.setText(s.idn)

        smp = s.sample or {}
        if s.acquiring:
            self.acq_label.setText(f"#{s.acq_id}: averaging {s.acq_readings} readings ...")
        elif smp.get("acq_id"):
            self.acq_label.setText(
                f"#{smp['acq_id']}:  {smp.get('voltage_V', 0):+.4f} V   "
                f"{smp.get('current_A', 0):+.5f} A   (n={smp.get('n')})")

        self.quad.set_state(s, self.ctrl.voltage_limit_max(), self.ctrl.current_limit_max())

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()      # local: ramp to zero, output off; remote: just disconnect
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False, after_show=None) -> int:
    """Start the Qt app with a BipolarSupply-like object (the in-process brain,
    or a KepcoClient facade). The theme is chosen ONCE here, from cfg.ui.theme,
    BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # swap the active palette first
    # Qt number widgets follow the Windows locale ("10,000" for 10 ms on the
    # lab PC, gotcha #18): force the C locale, no group separators.
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
    if after_show is not None:
        after_show()
    return app.exec()


def main(theme: str | None = None, demo: bool = False) -> int:
    """Default: run against the built-in simulator, in-process. `theme` (if
    given) overrides cfg.ui.theme for this launch. `demo` (or the environment
    variable KEPCO_DEMO=1, used when rendering the README screenshot) makes the
    simulated BOP be FOUND with its output on at 2.5 A (8 V compliance) -- the
    brain adopts that at start, exactly as it would a live real unit."""
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    demo = demo or os.environ.get("KEPCO_DEMO", "") not in ("", "0")
    if demo:
        cfg.sim.found_mode = "current"
        cfg.sim.found_output = True
        cfg.sim.found_current_A = 2.5
        cfg.sim.found_voltage_V = 8.0
        cfg.ramp.rate_A_per_s = 2.0
    supply, _ = build_sim_system(cfg)
    after = None
    if demo:
        def after():
            QtCore.QTimer.singleShot(600, supply.acquire)    # after the 0.35 s settle
    return run_app(supply, cfg, after_show=after)


if __name__ == "__main__":
    raise SystemExit(main())
