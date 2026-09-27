"""Control GUI for the Keithley 2450 SourceMeter.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a SourceMeter-like object (the
in-process brain, or a K2450Client facade for a remote service). It sends
commands (set_output / set_voltage / set_current_limit / acquire ...) and reads
a status snapshot on a Qt timer. Events arrive on a Qt signal so they can
safely cross into the GUI thread.

The signature widget is the IVPlaneIndicator: the SMU's four-quadrant V-I
plane. It draws the 2450's output envelope (the two boxes 21 V x 1.05 A and
210 V x 105 mA), the compliance limit as a pair of lines, and the live
operating point with a fading trail -- so an IV sweep draws its own curve, and
running into compliance is something you SEE (the point slides along the limit
line, which turns red).
"""

from __future__ import annotations

import math
import os
import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets

from ..backends.base import range_table
from ..config import Config
from ..sim_system import build_sim_system
from ..smu import fmt_si
from .settings_dialog import SettingsDialog
from .theme import COLORS, apply_palette, build_stylesheet, set_theme

_NAN = float("nan")


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


def _eng(value: float, unit: str) -> tuple[str, str]:
    """(number, prefixed unit) for a big readout: 0.0012345 A -> ('1.2345', 'mA')."""
    if value is None or not math.isfinite(value):
        return "--", unit
    if value == 0:
        return "0.0000", unit
    for scale, prefix in ((1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"),
                          (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= scale * 0.99995:
            return f"{value / scale:.4f}", prefix + unit
    return f"{value / 1e-15:.4f}", "f" + unit


def _dspin(lo, hi, dec, step, suffix=""):
    w = QtWidgets.QDoubleSpinBox()
    # Qt number widgets follow the Windows locale ("10,000"); force the C
    # locale so the decimal point is a point (gotcha #18).
    w.setLocale(QtCore.QLocale.c())
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setSingleStep(step)
    if suffix:
        w.setSuffix("  " + suffix)
    return w


# ------------------------------------------------------------- the V-I plane

def _slog(x: float, x0: float, xmax: float) -> float:
    """Signed logarithmic compression to -1..+1.

    An SMU works over nine decades of current: on a linear axis a 1 uA point
    and a 0 A point are the same pixel. log10(1 + |x|/x0) is linear near zero
    (so the sign change is continuous) and logarithmic far from it."""
    if not math.isfinite(x):
        return 0.0
    return math.copysign(math.log10(1 + abs(x) / x0) / math.log10(1 + xmax / x0), x)


class IVPlaneIndicator(QtWidgets.QWidget):
    """The four-quadrant V-I plane with the envelope, compliance and the point.

    Quadrants I and III are SOURCE (the SMU delivers power: V and I have the
    same sign); II and IV are SINK (the sample pushes power back -- a charged
    capacitor, a solar cell). Both axes are signed-log, so microamps and amps
    share one picture.
    """

    V0, VMAX = 0.05, 250.0            # axis compression: linear below ~50 mV
    I0, IMAX = 1e-7, 1.5              # ... below ~100 nA

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(250)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._output = False
        self._fn = "voltage"
        self._v = _NAN
        self._i = _NAN
        self._tripped = False
        self._limit = _NAN
        self._box = (21.0, 1.05, 210.0, 0.105)     # replaced from cfg.limits
        self._trail: deque = deque(maxlen=160)
        self._pulse = 0.0
        # the glow pulses on its own timer so the point looks alive while the
        # output is on, independent of how often status is polled
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_box(self, v_lo_box: float, i_hi: float, v_hi: float, i_lo_box: float):
        self._box = (v_lo_box, i_hi, v_hi, i_lo_box)

    def set_state(self, output: bool, fn: str, v: float, i: float,
                  tripped: bool, limit: float):
        self._output = bool(output)
        self._fn = fn
        self._v, self._i = v, i
        self._tripped = bool(tripped)
        self._limit = limit
        if self._output and math.isfinite(v) and math.isfinite(i):
            if not self._trail or self._trail[-1] != (v, i):
                self._trail.append((v, i))
        if self._output and not self._timer.isActive():
            self._timer.start()
        elif not self._output and self._timer.isActive():
            self._timer.stop()
        self.update()

    def clear_trail(self):
        self._trail.clear()
        self.update()

    def _tick(self):
        self._pulse = (self._pulse + 0.04) % 1.0
        self.update()

    # -- drawing --------------------------------------------------------------

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        # plot area: leave room for axis labels
        left, right, top, bottom = 46, 16, 14, 34
        pw, ph = max(10, w - left - right), max(10, h - top - bottom)
        cx, cy = left + pw / 2, top + ph / 2

        def X(v):
            return cx + _slog(v, self.V0, self.VMAX) * pw / 2

        def Y(i):
            return cy - _slog(i, self.I0, self.IMAX) * ph / 2

        C = lambda k, a=255: _qc(COLORS[k], a)     # colours read at PAINT time

        # background of the plot
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(C("code_bg"))
        p.drawRoundedRect(QtCore.QRectF(left, top, pw, ph), 6, 6)

        # ---- the output envelope: union of the two boxes ------------------------
        vb, ib_hi, vh, ib_lo = self._box
        pts = [(vb, ib_hi), (vb, ib_lo), (vh, ib_lo), (vh, -ib_lo), (vb, -ib_lo),
               (vb, -ib_hi), (-vb, -ib_hi), (-vb, -ib_lo), (-vh, -ib_lo), (-vh, ib_lo),
               (-vb, ib_lo), (-vb, ib_hi)]
        poly = QtGui.QPolygonF([QtCore.QPointF(X(v), Y(i)) for v, i in pts])
        p.setBrush(C("panel_hi"))
        p.setPen(QtGui.QPen(C("border"), 1.4))
        p.drawPolygon(poly)

        # decade grid (faint), so the log axes can be read
        pen = QtGui.QPen(C("grid"), 1)
        p.setPen(pen)
        for v in (0.1, 1.0, 10.0, 100.0):
            for s in (-1, 1):
                p.drawLine(QtCore.QPointF(X(s * v), top), QtCore.QPointF(X(s * v), top + ph))
        for i in (1e-6, 1e-4, 1e-2, 1.0):
            for s in (-1, 1):
                p.drawLine(QtCore.QPointF(left, Y(s * i)), QtCore.QPointF(left + pw, Y(s * i)))

        # axes
        p.setPen(QtGui.QPen(C("muted", 170), 1.2))
        p.drawLine(QtCore.QPointF(left, cy), QtCore.QPointF(left + pw, cy))
        p.drawLine(QtCore.QPointF(cx, top), QtCore.QPointF(cx, top + ph))

        # quadrant names: SOURCE where V and I have the same sign
        f = p.font()
        f.setPointSize(7)
        f.setBold(True)
        p.setFont(f)
        p.setPen(C("muted", 150))
        for qx, qy, name in ((1, -1, "SOURCE"), (-1, 1, "SOURCE"),
                             (-1, -1, "SINK"), (1, 1, "SINK")):
            rx = cx + qx * pw * 0.36 - 30
            ry = cy + qy * ph * 0.40 - 7
            p.drawText(QtCore.QRectF(rx, ry, 60, 14), QtCore.Qt.AlignCenter, name)

        # axis tick labels at the box corners (the numbers that matter on a 2450)
        f.setBold(False)
        p.setFont(f)
        p.setPen(C("muted"))
        for v in (vb, vh):
            for s in (-1, 1):
                p.drawText(QtCore.QRectF(X(s * v) - 30, top + ph + 2, 60, 12),
                           QtCore.Qt.AlignHCenter, f"{s * v:+g} V")
        for i in (ib_lo, ib_hi):
            for s in (-1, 1):
                p.drawText(QtCore.QRectF(0, Y(s * i) - 6, left - 4, 12),
                           QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                           fmt_si(s * i, "A").replace(" ", ""))

        # ---- compliance lines -----------------------------------------------------
        if math.isfinite(self._limit):
            col = C("danger") if self._tripped else C("accent_dim")
            pen = QtGui.QPen(col, 2.2 if self._tripped else 1.4)
            pen.setStyle(QtCore.Qt.SolidLine if self._tripped else QtCore.Qt.DashLine)
            p.setPen(pen)
            if self._fn == "voltage":            # a current limit: horizontal lines
                for s in (-1, 1):
                    y = Y(s * self._limit)
                    p.drawLine(QtCore.QPointF(left, y), QtCore.QPointF(left + pw, y))
            else:                                # a voltage limit: vertical lines
                for s in (-1, 1):
                    x = X(s * self._limit)
                    p.drawLine(QtCore.QPointF(x, top), QtCore.QPointF(x, top + ph))

        # ---- the trail: where the operating point has been ----------------------------
        n = len(self._trail)
        if n > 1:
            prev = None
            for k, (v, i) in enumerate(self._trail):
                pt = QtCore.QPointF(X(v), Y(i))
                if prev is not None:
                    a = int(40 + 180 * k / n)
                    p.setPen(QtGui.QPen(C("accent", a), 2.0))
                    p.drawLine(prev, pt)
                prev = pt

        # ---- the operating point ---------------------------------------------------------
        if self._output and math.isfinite(self._v) and math.isfinite(self._i):
            pt = QtCore.QPointF(X(self._v), Y(self._i))
            key = "danger" if self._tripped else "accent"
            glow = 9 + 5 * math.sin(self._pulse * 2 * math.pi)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(C(key, 60))
            p.drawEllipse(pt, glow + 4, glow + 4)
            p.setBrush(C(key, 120))
            p.drawEllipse(pt, glow * 0.6 + 2, glow * 0.6 + 2)
            p.setBrush(C("accent_hi") if not self._tripped else C("danger"))
            p.drawEllipse(pt, 4.5, 4.5)
        else:
            p.setPen(QtGui.QPen(C("muted"), 1.6))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawEllipse(QtCore.QPointF(cx, cy), 5, 5)

        # ---- caption --------------------------------------------------------------------
        if not self._output:
            cap, key = "OUTPUT OFF", "muted"
        elif self._tripped:
            cap, key = "IN COMPLIANCE", "danger"
        else:
            cap, key = f"SOURCING {self._fn.upper()}", "accent_hi"
        f.setBold(True)
        f.setPointSize(8)
        p.setFont(f)
        p.setPen(C(key))
        p.drawText(QtCore.QRectF(left, h - 16, pw, 14), QtCore.Qt.AlignHCenter, cap)
        p.end()


def _qc(hex_color: str, alpha: int = 255) -> QtGui.QColor:
    c = QtGui.QColor(hex_color)
    c.setAlpha(alpha)
    return c


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self._shown_fn = None            # which function the source widgets show
        self._shown_ranges = None        # (fn) the range combos were built for
        self._last_sample_id = None
        title = "Keithley 2450 - SourceMeter"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1280, 860)

        root = QtWidgets.QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)

        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

        # start the brain (opens the backend and ADOPTS its state -- nothing is
        # written, an output that is on stays on) and the refresh timer
        self.ctrl.start()
        lim = self.cfg.limits
        self.iv.set_box(lim.box_voltage_V, lim.current_max_A,
                        lim.voltage_max_V, lim.box_current_A)
        self._sync_source_widgets(force=True)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

    # ---- layout ----------------------------------------------------------

    def _build_sidebar(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(360)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("KEITHLEY 2450")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; "
                            "font-weight:800; letter-spacing:2px;")
        header.addWidget(title)
        header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        header.addWidget(settings_btn)
        col.addLayout(header)

        # output state card
        rcard, rlay = _card()
        top = QtWidgets.QHBoxLayout()
        self.state_badge = QtWidgets.QLabel("OUTPUT OFF")
        self.state_badge.setObjectName("stateBadge")
        top.addWidget(self.state_badge)
        top.addStretch(1)
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        self.conn_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        top.addWidget(self.conn_dot)
        rlay.addLayout(top)
        self.idn_label = QtWidgets.QLabel("—")
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        rlay.addWidget(self.idn_label)
        col.addWidget(rcard)

        self.out_btn = QtWidgets.QPushButton("Output ON")
        self.out_btn.setObjectName("primary")
        self.out_btn.setMinimumHeight(44)
        self.out_btn.clicked.connect(self._toggle_output)
        col.addWidget(self.out_btn)
        self._out_on = False

        # source card
        scard, slay = _card("Source")
        form = QtWidgets.QFormLayout()
        form.setSpacing(8)
        self.fn_combo = QtWidgets.QComboBox()
        self.fn_combo.addItems(["voltage", "current"])
        self.fn_combo.activated.connect(self._set_function)      # user-only signal
        form.addRow("Function", self.fn_combo)

        self.level_spin = _dspin(-210, 210, 6, 0.1)
        lrow = QtWidgets.QHBoxLayout()
        lrow.addWidget(self.level_spin, 1)
        b = QtWidgets.QPushButton("Set")
        b.setObjectName("primary")
        b.clicked.connect(self._set_level)
        lrow.addWidget(b)
        self.level_label = QtWidgets.QLabel("Level")
        form.addRow(self.level_label, lrow)

        self.limit_spin = _dspin(0, 1050, 6, 0.1)
        mrow = QtWidgets.QHBoxLayout()
        mrow.addWidget(self.limit_spin, 1)
        b = QtWidgets.QPushButton("Set")
        b.setObjectName("primary")
        b.clicked.connect(self._set_limit)
        mrow.addWidget(b)
        self.limit_label = QtWidgets.QLabel("Limit")
        form.addRow(self.limit_label, mrow)

        self.srange_combo = QtWidgets.QComboBox()
        self.srange_combo.activated.connect(self._set_source_range)
        form.addRow("Range", self.srange_combo)
        slay.addLayout(form)
        self.env_label = QtWidgets.QLabel("")
        self.env_label.setObjectName("hint")
        self.env_label.setWordWrap(True)
        slay.addWidget(self.env_label)
        col.addWidget(scard)

        # measure card
        mcard, mlay = _card("Measure")
        mform = QtWidgets.QFormLayout()
        mform.setSpacing(8)
        self.mrange_combo = QtWidgets.QComboBox()
        self.mrange_combo.activated.connect(self._set_measure_range)
        self.mrange_label = QtWidgets.QLabel("Range")
        mform.addRow(self.mrange_label, self.mrange_combo)
        self.nplc_spin = _dspin(self.cfg.limits.nplc_min, self.cfg.limits.nplc_max, 2, 0.1)
        self.nplc_spin.setValue(self.cfg.measure.nplc)
        self.nplc_spin.editingFinished.connect(self._set_nplc)
        mform.addRow("NPLC", self.nplc_spin)
        self.wire_check = QtWidgets.QCheckBox("4-wire (remote sense)")
        self.wire_check.setChecked(self.cfg.measure.four_wire)
        self.wire_check.clicked.connect(self._set_four_wire)     # user-only (gotcha #13)
        mform.addRow("Sense", self.wire_check)
        mlay.addLayout(mform)
        col.addWidget(mcard)

        col.addStretch(1)
        off_btn = QtWidgets.QPushButton("Output OFF")
        off_btn.setObjectName("danger")
        off_btn.setMinimumHeight(38)
        off_btn.clicked.connect(self._output_off)
        col.addWidget(off_btn)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0)
        colw.setSpacing(14)

        ocard, olay = _card("Measured")
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(24)
        self.v_value, self.v_unit = self._readout(row, "Voltage", "V", minw=150)
        self.i_value, self.i_unit = self._readout(row, "Current", "A", minw=150)
        self.r_value, self.r_unit = self._readout(row, "Resistance", "ohm", minw=150)
        row.addStretch(1)
        lamps = QtWidgets.QVBoxLayout()
        self.comp_lamp = QtWidgets.QLabel("COMPLIANCE")
        self.settle_lamp = QtWidgets.QLabel("SETTLED")
        for lab in (self.comp_lamp, self.settle_lamp):
            lab.setObjectName("stateBadge")
            lab.setAlignment(QtCore.Qt.AlignCenter)
            lamps.addWidget(lab)
        row.addLayout(lamps)
        olay.addLayout(row)
        self.read_info = QtWidgets.QLabel("")
        self.read_info.setObjectName("hint")
        olay.addWidget(self.read_info)
        colw.addWidget(ocard)

        icard, ilay = _card("V-I plane")
        self.iv = IVPlaneIndicator()
        ilay.addWidget(self.iv, 1)
        colw.addWidget(icard, 3)

        acard, alay = _card("Acquisition (scan-safe sample)")
        arow = QtWidgets.QHBoxLayout()
        arow.addWidget(QtWidgets.QLabel("Readings"))
        self.readings_spin = QtWidgets.QSpinBox()
        self.readings_spin.setLocale(QtCore.QLocale.c())
        self.readings_spin.setRange(self.cfg.limits.readings_min, self.cfg.limits.readings_max)
        self.readings_spin.setValue(self.cfg.acquisition.readings)
        self.readings_spin.editingFinished.connect(
            lambda: self.ctrl.set_acquisition(self.readings_spin.value()))
        arow.addWidget(self.readings_spin)
        acq_btn = QtWidgets.QPushButton("Acquire")
        acq_btn.setObjectName("primary")
        acq_btn.clicked.connect(self._acquire)
        arow.addWidget(acq_btn)
        self.sample_label = QtWidgets.QLabel("no sample yet")
        self.sample_label.setStyleSheet(f"color:{COLORS['text']};")
        arow.addWidget(self.sample_label, 1)
        alay.addLayout(arow)
        colw.addWidget(acard)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(110)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 1)
        return panel

    def _readout(self, row, label, unit, minw=120):
        box = QtWidgets.QVBoxLayout()
        box.setSpacing(2)
        cap = QtWidgets.QLabel(label.upper())
        cap.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; "
                          "font-weight:700; letter-spacing:1px;")
        line = QtWidgets.QHBoxLayout()
        line.setSpacing(5)
        val = QtWidgets.QLabel("--")
        val.setObjectName("bigValue")
        val.setMinimumWidth(minw)
        u = QtWidgets.QLabel(unit)
        u.setObjectName("unit")
        u.setMinimumWidth(36)
        line.addWidget(val)
        line.addWidget(u, 0, QtCore.Qt.AlignBottom)
        box.addWidget(cap)
        box.addLayout(line)
        holder = QtWidgets.QWidget()
        holder.setLayout(box)
        row.addWidget(holder)
        return val, u

    # ---- source widgets follow the source function -------------------------

    def _sync_source_widgets(self, force: bool = False, st=None):
        """Re-label and re-range the level/limit spins for the active function.
        Level in V or mA, limit in mA or V -- the units a bench user thinks in."""
        src = self.cfg.source
        fn = st.source_function if st is not None else src.function
        if fn == self._shown_fn and not force:
            return
        self._shown_fn = fn
        self.fn_combo.setCurrentText(fn)
        lim = self.cfg.limits
        for w in (self.level_spin, self.limit_spin):
            w.blockSignals(True)
        if fn == "voltage":
            self.level_label.setText("Voltage")
            self.level_spin.setDecimals(6)
            self.level_spin.setSuffix("  V")
            self.level_spin.setRange(-lim.voltage_max_V, lim.voltage_max_V)
            self.level_spin.setValue(src.voltage_V)
            self.limit_label.setText("I limit")
            self.limit_spin.setDecimals(6)
            self.limit_spin.setSuffix("  mA")
            self.limit_spin.setRange(lim.current_limit_min_A * 1e3, lim.current_max_A * 1e3)
            self.limit_spin.setValue(src.current_limit_A * 1e3)
            self.mrange_label.setText("I range")
        else:
            self.level_label.setText("Current")
            self.level_spin.setDecimals(6)
            self.level_spin.setSuffix("  mA")
            self.level_spin.setRange(-lim.current_max_A * 1e3, lim.current_max_A * 1e3)
            self.level_spin.setValue(src.current_A * 1e3)
            self.limit_label.setText("V limit")
            self.limit_spin.setDecimals(4)
            self.limit_spin.setSuffix("  V")
            self.limit_spin.setRange(lim.voltage_limit_min_V, lim.voltage_max_V)
            self.limit_spin.setValue(src.voltage_limit_V)
            self.mrange_label.setText("V range")
        for w in (self.level_spin, self.limit_spin):
            w.blockSignals(False)
        self._build_range_combos(fn)

    def _build_range_combos(self, fn: str):
        mfn = "current" if fn == "voltage" else "voltage"
        for combo, f in ((self.srange_combo, fn), (self.mrange_combo, mfn)):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Auto", None)
            unit = "V" if f == "voltage" else "A"
            for r in range_table(f):
                combo.addItem(fmt_si(r, unit), r)
            combo.blockSignals(False)
        self._shown_ranges = fn

    def _select_range(self, combo, auto: bool, value: float):
        if combo.view().isVisible():
            return                       # the user is choosing; do not fight them
        idx = 0
        if not auto and value is not None and math.isfinite(value):
            for k in range(1, combo.count()):
                if abs(combo.itemData(k) - value) <= 1e-12 + 1e-6 * value:
                    idx = k
                    break
        if combo.currentIndex() != idx:
            combo.blockSignals(True)
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)

    # ---- actions ---------------------------------------------------------

    def _call(self, fn, *args):
        """Run a command; a refusal becomes a red log line, not a crash."""
        try:
            fn(*args)
        except (ValueError, RuntimeError) as exc:
            self._on_event("error", str(exc))

    def _toggle_output(self):
        self._call(self.ctrl.set_output, not self._out_on)

    def _output_off(self):
        self._call(self.ctrl.set_output, False)

    def _set_function(self, _idx=None):
        self._call(self.ctrl.set_source_function, self.fn_combo.currentText())

    def _set_level(self):
        if self._shown_fn == "voltage":
            self._call(self.ctrl.set_voltage, self.level_spin.value())
        else:
            self._call(self.ctrl.set_current, self.level_spin.value() * 1e-3)

    def _set_limit(self):
        if self._shown_fn == "voltage":
            self._call(self.ctrl.set_current_limit, self.limit_spin.value() * 1e-3)
        else:
            self._call(self.ctrl.set_voltage_limit, self.limit_spin.value())

    def _set_source_range(self, _idx=None):
        r = self.srange_combo.currentData()
        if r is None:
            self._call(self.ctrl.set_source_auto_range, True)
        else:
            self._call(self.ctrl.set_source_range, r)

    def _set_measure_range(self, _idx=None):
        r = self.mrange_combo.currentData()
        if r is None:
            self._call(self.ctrl.set_measure_auto_range, True)
        else:
            self._call(self.ctrl.set_measure_range, r)

    def _set_nplc(self):
        self._call(self.ctrl.set_nplc, self.nplc_spin.value())

    def _set_four_wire(self, checked: bool):
        self._call(self.ctrl.set_four_wire, bool(checked))

    def _acquire(self):
        self._call(self.ctrl.acquire)

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        lim = self.cfg.limits
        self.iv.set_box(lim.box_voltage_V, lim.current_max_A,
                        lim.voltage_max_V, lim.box_current_A)
        self.nplc_spin.setRange(lim.nplc_min, lim.nplc_max)
        self.readings_spin.setRange(lim.readings_min, lim.readings_max)
        self._sync_source_widgets(force=True)

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
        self._out_on = bool(s.output)

        # a remote client learns a function change from status; pull the config
        # then, so the spins show the stored level/limit of the new function
        if s.source_function != self._shown_fn:
            if self._remote:
                self.ctrl.get_config()
            self._sync_source_widgets(st=s)

        for (val, unit_lbl), x, u in (((self.v_value, self.v_unit), s.voltage_V, "V"),
                                      ((self.i_value, self.i_unit), s.current_A, "A"),
                                      ((self.r_value, self.r_unit), s.resistance_ohm, "ohm")):
            num, uu = _eng(x, u)
            val.setText(num)
            unit_lbl.setText(uu)

        self._lamp(self.comp_lamp, s.output and s.tripped, "danger")
        self._lamp(self.settle_lamp, s.output and s.settled, "ok")

        if s.output:
            self.read_info.setText(
                f"reading {s.readings}  -  {s.read_ms:.1f} ms  -  "
                f"{'4-wire' if s.four_wire else '2-wire'}  -  NPLC {s.nplc:g}"
                + (f"  -  {s.flag}" if s.flag else ""))
        else:
            self.read_info.setText("output off: no readings")

        # output badge + toggle button (restyle only when the state flips)
        if s.output != getattr(self, "_btn_state", None):
            self._btn_state = s.output
            if s.output:
                self.state_badge.setText("OUTPUT ON")
                self._badge_color(self.state_badge, COLORS["accent"])
                self.out_btn.setText("Output OFF")
                self.out_btn.setObjectName("danger")
            else:
                self.state_badge.setText("OUTPUT OFF")
                self._badge_color(self.state_badge, COLORS["muted"])
                self.out_btn.setText("Output ON")
                self.out_btn.setObjectName("primary")
            self.out_btn.style().unpolish(self.out_btn)
            self.out_btn.style().polish(self.out_btn)

        if s.connected:
            self.conn_dot.setText("●  connected")
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        if s.hw_error:
            self.idn_label.setText("hardware error: " + s.hw_error)
        elif s.idn:
            self.idn_label.setText(s.idn)

        self._follow_setpoints(s)

        fn = s.source_function
        uv, ui = ("V", "A") if fn == "voltage" else ("A", "V")
        self.env_label.setText(
            f"level up to +-{fmt_si(s.level_max, uv)}, limit "
            f"{fmt_si(s.limit_min, ui)} .. {fmt_si(s.limit_max, ui)}  "
            f"(2450 boxes: 21 V x 1.05 A, 210 V x 105 mA)")
        self._select_range(self.srange_combo, s.source_auto_range, s.source_range)
        self._select_range(self.mrange_combo, s.measure_auto_range, s.measure_range)
        if s.source_auto_range and math.isfinite(s.source_range):
            self.srange_combo.setItemText(0, f"Auto ({fmt_si(s.source_range, uv)})")
        if s.measure_auto_range and math.isfinite(s.measure_range):
            self.mrange_combo.setItemText(0, f"Auto ({fmt_si(s.measure_range, ui)})")

        limit = s.current_limit_A if fn == "voltage" else s.voltage_limit_V
        self.iv.set_state(s.output, fn, s.voltage_V, s.current_A, s.tripped, limit)

        smp = s.sample or {}
        if s.acquiring:
            self.sample_label.setText(f"acquiring #{s.acq_id} ... "
                                      f"{100 * s.acq_progress:.0f} %")
        elif smp.get("acq_id") is not None and smp.get("acq_id") != self._last_sample_id:
            self._last_sample_id = smp.get("acq_id")
            if smp.get("aborted"):
                self.sample_label.setText(f"#{smp['acq_id']} aborted ({smp.get('why', '')})")
            else:
                self.sample_label.setText(
                    f"#{smp['acq_id']}:  V {fmt_si(smp.get('voltage_V', _NAN), 'V')}"
                    f"   I {fmt_si(smp.get('current_A', _NAN), 'A')}"
                    f" +- {fmt_si(smp.get('current_std_A', _NAN), 'A')}"
                    f"   R {fmt_si(smp.get('resistance_ohm', _NAN), 'ohm')}"
                    f"   n={smp.get('n')}" + ("   COMPLIANCE" if smp.get("tripped") else ""))

    def _follow_setpoints(self, s):
        """Keep the entry widgets showing the instrument's ACTUAL setpoints --
        a clamp, a scan or another client may have changed them -- except the
        one the user is typing in (hasFocus), which we must not overwrite."""
        if s.source_function == "voltage":
            level, limit = s.source_voltage_set_V, s.current_limit_A * 1e3
        else:
            level, limit = s.source_current_set_A * 1e3, s.voltage_limit_V
        for w, v in ((self.level_spin, level), (self.limit_spin, limit),
                     (self.nplc_spin, s.nplc), (self.readings_spin, s.acq_readings)):
            if v is None or (isinstance(v, float) and not math.isfinite(v)):
                continue
            if w.hasFocus():
                continue
            if isinstance(w, QtWidgets.QDoubleSpinBox):
                differs = abs(w.value() - v) > 0.5 * 10 ** -w.decimals()
            else:
                v = int(v)
                differs = w.value() != v
            if differs:
                w.blockSignals(True)
                w.setValue(v)
                w.blockSignals(False)
        if self.wire_check.isChecked() != bool(s.four_wire):
            self.wire_check.blockSignals(True)          # gotcha #13
            self.wire_check.setChecked(bool(s.four_wire))
            self.wire_check.blockSignals(False)

    def _lamp(self, lab: QtWidgets.QLabel, on: bool, key: str):
        state = (bool(on), key)
        if getattr(lab, "_state", None) == state:
            return
        lab._state = state
        self._badge_color(lab, COLORS[key] if on else COLORS["border"],
                          COLORS[key] if on else COLORS["muted"])

    def _badge_color(self, lab, border, text=None):
        text = text or border
        lab.setStyleSheet(
            f"QLabel#stateBadge {{ color:{text}; border:1px solid {border}; "
            f"background:{COLORS['panel_hi']}; border-radius:10px; padding:4px 12px; "
            f"font-weight:700; letter-spacing:1px; }}")

    # ---- demo pose (README render only) --------------------------------------

    def start_demo(self):
        """Put the SIMULATOR through a diode IV sweep into compliance, so a
        rendered front panel shows a curve instead of an idle plane. Used only by
        main() under Qt's offscreen platform (tools/render_all.py); never with a
        real instrument or a remote service."""
        c = self.ctrl
        c.cfg.sim.load = "diode"
        c.set_nplc(0.1)
        c.set_current_limit(0.02)
        c.set_voltage(-0.5)
        c.set_output(True)
        self._demo_v = -0.5

        def step():
            self._demo_v += 0.025
            if self._demo_v > 0.95:
                self._demo_timer.stop()
                self._call(c.acquire)
                return
            c.set_voltage(round(self._demo_v, 4))

        self._demo_timer = QtCore.QTimer(self)
        self._demo_timer.setInterval(40)
        self._demo_timer.timeout.connect(step)
        self._demo_timer.start()

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()          # local: output OFF + disconnect; remote: close client
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False, demo: bool = False) -> int:
    """Start the Qt app with a SourceMeter-like object. The theme is chosen ONCE
    here, from cfg.ui.theme, BEFORE any widget is built (never rebind COLORS)."""
    set_theme(getattr(cfg.ui, "theme", "dark"))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from .theme import apply_window_icon
    apply_window_icon(app)
    app.setStyle("Fusion")
    apply_palette(app)
    app.setStyleSheet(build_stylesheet())
    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    if demo and not remote:
        win.start_demo()
    return app.exec()


def main(theme: str | None = None) -> int:
    """Default: run against the built-in simulator, in-process. `theme` (if given)
    overrides cfg.ui.theme for this launch.

    Under Qt's offscreen platform (only the front-panel renderer uses it) the
    simulator is posed with a diode IV sweep, so the README picture shows the
    V-I plane doing its job rather than an idle instrument."""
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    smu, _ = build_sim_system(cfg)
    demo = os.environ.get("QT_QPA_PLATFORM", "") == "offscreen"
    return run_app(smu, cfg, demo=demo)


if __name__ == "__main__":
    raise SystemExit(main())
