"""Control GUI for the HP 8648D RF signal generator (dark / light theme).

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a SignalSource-like object (a real
in-process SignalSource, or an Hp8648Client facade for a remote service). It
sends commands (set_rf / set_power / set_frequency) and reads a status snapshot
on a Qt timer to update the numbers and the spectrum glyph. Brain events come
from another thread, so they arrive on a Qt signal (the Bridge), which Qt
delivers safely inside the GUI thread.

The signature widget is the SpectrumIndicator: a miniature spectrum-analyser
screen across the whole 9 kHz - 4 GHz range. The carrier is a single spectral
line at the set frequency, as tall as the output level; behind it the
instrument's power CEILING is drawn as a staircase, so you can see at a glance
why +12 dBm is fine at 2 GHz but not at 3 GHz.
"""

from __future__ import annotations

import math
import os
import random
import time

from PySide6 import QtCore, QtGui, QtWidgets

from .. import spec
from ..config import Config
from ..sim_system import build_sim_system
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog
from .control_bar import ControlBar, mark_always
from ..control import ControlRefused


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


def _c_locale(widget):
    """Qt number widgets follow the WINDOWS locale (gotcha #18): on a PC set to
    Finnish, 2450.5 would display as '2 450,5' and typed '.' would be refused.
    The C locale, without group separators, is the same everywhere."""
    loc = QtCore.QLocale.c()
    loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
    widget.setLocale(loc)
    return widget


# ------------------------------------------------------------- the spectrum

class SpectrumIndicator(QtWidgets.QWidget):
    """A miniature spectrum-analyser screen: one CW line on a log axis.

    * x axis: frequency, LINEAR over 0 - 4 GHz. A log axis would give the
      kHz decades as much room as the GHz ones, and the one feature worth
      seeing -- the ceiling stepping down at 2500 MHz -- would shrink to a few
      pixels at the right edge. Below ~10 MHz the line simply sits at the left.
    * y axis: output level in dBm, -140 .. +25.
    * the STAIRCASE is the instrument's specified maximum level (spec.py) --
      it steps down above 2500 MHz; the shaded band above the effective ceiling
      is where the brain will not go.
    * the CARRIER is a vertical line with a peak marker. RF on: bright, with a
      flickering noise floor like a live analyser. RF off: a dim dashed outline
      of what WOULD come out when switched on.
    * reverse-power trip: a red banner across the screen.

    Colours are read from COLORS at paint time (never cached), so the theme
    chosen at start-up applies.
    """

    F_LO, F_HI = spec.FREQ_MIN_HZ, spec.FREQ_MAX_HZ
    P_LO, P_HI = -140.0, 25.0

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(220)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                           QtWidgets.QSizePolicy.Expanding)
        self._on = False
        self._freq = 1e9
        self._power = -30.0
        self._ceiling = 13.0
        self._envelope = 13.0
        self._option_1ea = False
        self._rpp = False
        self._rng = random.Random(8648)
        self._grass = [0.0] * 160
        self._t0 = time.monotonic()
        # the noise floor flickers on its own timer while RF is on, so the
        # screen looks alive however slowly status is polled
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._tick()

    def set_state(self, rf_on, freq_Hz, power_dBm, ceiling_dBm, envelope_dBm,
                  option_1ea=False, rpp=False):
        self._on = bool(rf_on)
        self._freq = float(freq_Hz)
        self._power = float(power_dBm)
        self._ceiling = float(ceiling_dBm)
        self._envelope = float(envelope_dBm)
        self._option_1ea = bool(option_1ea)
        self._rpp = bool(rpp)
        if self._on and not self._timer.isActive():
            self._timer.start()
        elif not self._on and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        # a random walk per bin, pulled back to zero: looks like a real noise
        # floor rather than white static
        for i in range(len(self._grass)):
            self._grass[i] = 0.6 * self._grass[i] + self._rng.gauss(0.0, 2.2)
        self.update()

    # -- coordinate helpers --------------------------------------------------

    def _x(self, f, r):
        u = min(max(f, 0.0), self.F_HI) / self.F_HI
        return r.left() + u * r.width()

    def _y(self, p, r):
        u = (min(max(p, self.P_LO), self.P_HI) - self.P_LO) / (self.P_HI - self.P_LO)
        return r.bottom() - u * r.height()

    # -- drawing -----------------------------------------------------------

    def paintEvent(self, ev):
        QColor, QPen, QPointF, QRectF = QtGui.QColor, QtGui.QPen, QtCore.QPointF, QtCore.QRectF
        Qt = QtCore.Qt
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # screen
        screen = QRectF(0.5, 0.5, w - 1, h - 1)
        p.setPen(QPen(QColor(COLORS["border"]), 1))
        p.setBrush(QColor(COLORS["code_bg"]))
        p.drawRoundedRect(screen, 8, 8)
        r = QRectF(44, 14, w - 58, h - 40)          # plot area inside the axes

        font = p.font()
        font.setPointSize(7)
        p.setFont(font)
        grid = QColor(COLORS["grid"])
        muted = QColor(COLORS["muted"])

        # frequency grid every 500 MHz, labelled every GHz
        for k in range(9):
            x = self._x(k * 500e6, r)
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            if k % 2 == 0:
                p.setPen(muted)
                lab = "0" if k == 0 else f"{k // 2} GHz"
                box = QRectF(x - 24, r.bottom() + 3, 48, 12)
                if k == 8:                       # keep the last label inside
                    box = QRectF(x - 48, r.bottom() + 3, 48, 12)
                p.drawText(box, Qt.AlignRight if k == 8 else Qt.AlignHCenter, lab)
        for lvl in (-120, -80, -40, 0, 20):
            y = self._y(lvl, r)
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(muted)
            p.drawText(QRectF(2, y - 6, 38, 12), Qt.AlignRight | Qt.AlignVCenter,
                       f"{lvl:+d}" if lvl else "0")

        # forbidden band above the EFFECTIVE ceiling (envelope and spec)
        danger = QColor(COLORS["danger"])
        # the step edges come from spec.py, the same table the brain clamps with
        edges = spec.band_edges(self._option_1ea)
        path = QtGui.QPainterPath()
        spec_path = QtGui.QPainterPath()
        first = True
        for a, b in zip(edges[:-1], edges[1:]):
            mid = math.sqrt(a * b)
            s_max = spec.spec_max_dBm(mid, self._option_1ea)
            eff = min(s_max, self._envelope)
            for f in (a, b):
                pt = QPointF(self._x(f, r), self._y(eff, r))
                sp = QPointF(self._x(f, r), self._y(s_max, r))
                if first:
                    path.moveTo(pt); spec_path.moveTo(sp); first = False
                else:
                    path.lineTo(pt); spec_path.lineTo(sp)
        band = QtGui.QPainterPath(path)
        band.lineTo(QPointF(r.right(), r.top()))
        band.lineTo(QPointF(r.left(), r.top()))
        band.closeSubpath()
        fill = QColor(danger); fill.setAlpha(34)
        p.setPen(Qt.NoPen); p.setBrush(fill)
        p.drawPath(band)
        pen = QPen(muted, 1.2, Qt.DashLine)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        p.drawPath(spec_path)
        edge = QColor(danger); edge.setAlpha(170)
        p.setPen(QPen(edge, 1.4))
        p.drawPath(path)

        # noise floor ("grass") -- only while RF is on, like a live analyser
        accent = QColor(COLORS["accent"])
        x0 = self._x(self._freq, r)
        if self._on:
            n = len(self._grass)
            floor = -128.0
            g = QtGui.QPainterPath()
            for i in range(n):
                x = r.left() + i * r.width() / (n - 1)
                y = self._y(floor + self._grass[i], r)
                if i == 0:
                    g.moveTo(x, y)
                else:
                    g.lineTo(x, y)
            gc = QColor(accent); gc.setAlpha(80)
            p.setPen(QPen(gc, 1)); p.drawPath(g)

        # the carrier
        top = self._y(self._power, r)
        if self._on:
            for width, alpha in ((9, 40), (5, 80), (2.2, 255)):
                c = QColor(accent); c.setAlpha(alpha)
                p.setPen(QPen(c, width, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(QPointF(x0, r.bottom()), QPointF(x0, top))
            head = QColor(COLORS["accent_hi"])
        else:
            p.setPen(QPen(muted, 1.4, Qt.DashLine))
            p.drawLine(QPointF(x0, r.bottom()), QPointF(x0, top))
            head = muted
        tri = QtGui.QPolygonF([QPointF(x0, top - 1), QPointF(x0 - 6, top - 11),
                               QPointF(x0 + 6, top - 11)])
        p.setPen(Qt.NoPen); p.setBrush(head); p.drawPolygon(tri)

        # marker readout next to the peak, kept inside the screen
        font.setPointSize(8); font.setBold(True); p.setFont(font)
        txt = f"{self._freq / 1e6:.5f} MHz   {self._power:+.1f} dBm"
        tw = p.fontMetrics().horizontalAdvance(txt) + 8
        tx = x0 + 10 if x0 + 10 + tw < r.right() else x0 - 10 - tw
        p.setPen(QColor(COLORS["text"] if self._on else COLORS["muted"]))
        p.drawText(QRectF(tx, top - 24, tw, 14), Qt.AlignLeft | Qt.AlignVCenter, txt)

        # caption
        if self._rpp:
            cap, col = "REVERSE POWER  -  RF OFF", QColor(COLORS["danger"])
        elif self._on:
            cap, col = "RF ON", QColor(COLORS["ok"])
        else:
            cap, col = "RF OFF", muted
        p.setPen(col)
        p.drawText(QRectF(r.left() + 6, r.top() + 2, 260, 14), Qt.AlignLeft, cap)
        p.setPen(muted)
        font.setBold(False); p.setFont(font)
        p.drawText(QRectF(r.right() - 220, r.top() + 2, 214, 14), Qt.AlignRight,
                   f"ceiling here {self._ceiling:+.1f} dBm")
        p.end()


# ------------------------------------------------------------- main window

_FREQ_UNITS = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9}

#: The Sweep card: shown name -> (knob, pace unit shown, wire units per shown
#: unit, decimals, first pace offered in the shown unit). No phase: the 8648D
#: has no phase control.
_SWEEP_UI = {"Frequency": ("frequency", "MHz/s", 1e6, 3, 10.0),
             "Level": ("power", "dB/s", 1.0, 2, 1.0)}
_SWEEP_RUNIT = {"frequency": "Hz_per_s", "power": "dB_per_s"}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self._freq_unit = "MHz"
        self._prev_scale = _FREQ_UNITS[self._freq_unit]
        self._ceiling_shown = None
        title = "HP 8648D - RF Signal Generator"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1180, 700)

        root = QtWidgets.QWidget(); root.setObjectName("root")
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)
        # Control or viewer (control_bar.py): a bar across the top, only for a
        # GUI on a service whose client knows about control -- a local GUI
        # owns its generator and has nobody to share it with.
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

        # brain events -> log (the Bridge hops threads)
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

        # start the brain (opens the backend, adopts the instrument's state)
        # and the refresh timer
        self.ctrl.start()
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
        title = QtWidgets.QLabel("HP 8648D")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        mark_always(settings_btn)    # a viewer may LOOK; the service refuses the OK
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        header.addWidget(settings_btn)
        col.addLayout(header)

        # RF state card
        rcard, rlay = _card()
        top = QtWidgets.QHBoxLayout()
        self.state_badge = QtWidgets.QLabel("RF OFF")
        self.state_badge.setObjectName("stateBadge")
        top.addWidget(self.state_badge)
        top.addStretch(1)
        self.conn_dot = QtWidgets.QLabel("connecting")
        self.conn_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        top.addWidget(self.conn_dot)
        rlay.addLayout(top)
        self.idn_label = QtWidgets.QLabel("-")
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        self.idn_label.setWordWrap(True)
        rlay.addWidget(self.idn_label)
        self.alarm_label = QtWidgets.QLabel("")
        self.alarm_label.setWordWrap(True)
        self.alarm_label.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        self.alarm_label.hide()
        rlay.addWidget(self.alarm_label)
        col.addWidget(rcard)

        self.rf_btn = QtWidgets.QPushButton("Turn RF On")
        self.rf_btn.setObjectName("primary")
        self.rf_btn.setMinimumHeight(44)
        self.rf_btn.clicked.connect(self._toggle_rf)
        col.addWidget(self.rf_btn)
        self._rf_on = False
        self._spins_seeded = False

        # frequency (spin + unit + set)
        fcard, flay = _card("Frequency")
        frow = QtWidgets.QHBoxLayout()
        self.freq_spin = _c_locale(QtWidgets.QDoubleSpinBox())
        self.freq_spin.setDecimals(5)
        self.freq_spin.setKeyboardTracking(False)
        self.unit_combo = QtWidgets.QComboBox()
        self.unit_combo.addItems(list(_FREQ_UNITS.keys()))
        self.unit_combo.setCurrentText(self._freq_unit)
        self.unit_combo.currentTextChanged.connect(self._change_freq_unit)
        set_freq = QtWidgets.QPushButton("Set"); set_freq.setObjectName("primary")
        set_freq.clicked.connect(self._set_frequency)
        frow.addWidget(self.freq_spin, 1); frow.addWidget(self.unit_combo); frow.addWidget(set_freq)
        flay.addLayout(frow)
        col.addWidget(fcard)
        self._apply_freq_unit_range(initial_hz=self.cfg.signal.frequency_Hz)

        # power
        pcard, play = _card("Output level")
        prow = QtWidgets.QHBoxLayout()
        self.power_spin = _c_locale(QtWidgets.QDoubleSpinBox())
        self.power_spin.setRange(self.cfg.limits.power_min_dBm, self.cfg.limits.power_max_dBm)
        self.power_spin.setDecimals(1); self.power_spin.setSingleStep(1.0)
        self.power_spin.setValue(self.cfg.signal.power_dBm); self.power_spin.setSuffix("  dBm")
        set_pow = QtWidgets.QPushButton("Set"); set_pow.setObjectName("primary")
        set_pow.clicked.connect(self._set_power)
        prow.addWidget(self.power_spin, 1); prow.addWidget(set_pow)
        play.addLayout(prow)
        self.ceiling_hint = QtWidgets.QLabel("")
        self.ceiling_hint.setObjectName("hint")
        self.ceiling_hint.setWordWrap(True)
        play.addWidget(self.ceiling_hint)
        col.addWidget(pcard)

        # protection & state lamps: what the instrument itself reports
        scard, slay = _card("Instrument state")
        self.lamps = {}
        for key, text in (("rpp", "Reverse-power protection"),
                          ("spec", "Level within specification"),
                          ("mod", "All modulation off (pure CW)")):
            row = QtWidgets.QHBoxLayout(); row.setSpacing(8)
            dot = QtWidgets.QLabel("●")
            lab = QtWidgets.QLabel(text)
            state = QtWidgets.QLabel("-")
            state.setStyleSheet(f"color:{COLORS['muted']}; font-weight:700;")
            row.addWidget(dot); row.addWidget(lab, 1); row.addWidget(state)
            slay.addLayout(row)
            self.lamps[key] = (dot, state)
        col.addWidget(scard)
        self._set_from = {"f": None, "p": None}

        col.addStretch(1)
        off_btn = QtWidgets.QPushButton("RF Off"); off_btn.setObjectName("danger")
        off_btn.setMinimumHeight(38)
        # the SAFETY verb (net/service.py): works for a viewer too
        off_btn.clicked.connect(lambda: self._safe(self.ctrl.rf_off))
        mark_always(off_btn)
        col.addWidget(off_btn)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        ocard, olay = _card("Output (read back from the instrument)")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(28)
        self.freq_value = self._readout(row, "Frequency", "MHz", minw=190)
        self.power_value = self._readout(row, "Level", "dBm", minw=110)
        self.ceiling_value = self._readout(row, "Ceiling here", "dBm", minw=100)
        row.addStretch(1)
        olay.addLayout(row)
        self.spectrum = SpectrumIndicator()
        olay.addWidget(self.spectrum, 1)
        colw.addWidget(ocard, 3)
        colw.addWidget(self._build_sweep())

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(110)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 1)
        return panel

    def _build_sweep(self) -> QtWidgets.QWidget:
        """SWEEP (2026-10-10): walk one knob CONTINUOUSLY to the value in its
        box on the left, at a set pace -- what a fly scan does row by row, by
        hand. The RF output is not touched; Stop ends the sweep where it is."""
        card, lay = _card("Sweep")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(8)
        self.sweep_knob = QtWidgets.QComboBox()
        self.sweep_knob.addItems(list(_SWEEP_UI))
        self.sweep_knob.setToolTip("Which knob to sweep; the target is the value "
                                   "in that knob's box on the left")
        self.sweep_rate = _c_locale(QtWidgets.QDoubleSpinBox())
        self.sweep_rate.setMinimumWidth(150)
        self.sweep_rate.setToolTip("Sweep pace: the service steps the knob every "
                                   f"{self.cfg.hardware.ramp_dt_s * 1e3:g} ms")
        # each knob remembers its own pace while you switch between them
        self._sweep_rates = {name: spec_[4] for name, spec_ in _SWEEP_UI.items()}
        self._sweep_shown = None
        self.sweep_knob.currentTextChanged.connect(self._sweep_knob_changed)
        go = QtWidgets.QPushButton("Sweep to"); go.setObjectName("primary")
        go.setToolTip("Sweep the chosen knob continuously to the value in its box")
        go.clicked.connect(self._sweep)
        stop = QtWidgets.QPushButton("Stop")
        stop.setToolTip("End the sweep where it is (allowed also while viewing)")
        stop.clicked.connect(lambda: self._safe(self.ctrl.ramp_stop))
        mark_always(stop)            # ramp_stop is a safety verb (net/service.py)
        self.sweep_state = QtWidgets.QLabel("idle")
        self.sweep_state.setStyleSheet(f"color:{COLORS['muted']};")
        row.addWidget(self.sweep_knob); row.addWidget(self.sweep_rate, 1)
        row.addWidget(go); row.addWidget(stop)
        lay.addLayout(row)
        lay.addWidget(self.sweep_state)
        self._sweep_knob_changed(self.sweep_knob.currentText())
        return card

    def _sweep_knob_changed(self, name: str):
        if self._sweep_shown is not None:
            self._sweep_rates[self._sweep_shown] = self.sweep_rate.value()
        knob, unit, scale, decimals, _default = _SWEEP_UI[name]
        runit = _SWEEP_RUNIT[knob]
        lim = self.cfg.limits
        self.sweep_rate.setDecimals(decimals)
        self.sweep_rate.setRange(getattr(lim, f"ramp_rate_min_{runit}") / scale,
                                 getattr(lim, f"ramp_rate_max_{runit}") / scale)
        self.sweep_rate.setSuffix(f"  {unit}")
        self.sweep_rate.setValue(self._sweep_rates[name])
        self._sweep_shown = name

    def _sweep(self):
        name = self.sweep_knob.currentText()
        knob, _unit, scale, _d, _default = _SWEEP_UI[name]
        target = {"frequency": self._current_freq_hz,
                  "power": self.power_spin.value}[knob]()
        fn = getattr(self.ctrl, f"ramp_{knob}")
        try:
            self._safe(fn, target, self.sweep_rate.value() * scale)
        except Exception as exc:      # a refused sweep goes to the log, not a crash
            self._on_event("error", f"sweep refused: {exc}")

    def _readout(self, row, label, unit, minw=120):
        box = QtWidgets.QVBoxLayout(); box.setSpacing(2)
        cap = QtWidgets.QLabel(label.upper())
        cap.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; font-weight:700; letter-spacing:1px;")
        line = QtWidgets.QHBoxLayout(); line.setSpacing(5)
        val = QtWidgets.QLabel("-"); val.setObjectName("bigValue")
        val.setMinimumWidth(minw)
        u = QtWidgets.QLabel(unit); u.setObjectName("unit")
        line.addWidget(val); line.addWidget(u, 0, QtCore.Qt.AlignBottom)
        box.addWidget(cap); box.addLayout(line)
        holder = QtWidgets.QWidget(); holder.setLayout(box)
        row.addWidget(holder)
        return val

    # ---- frequency unit handling ----------------------------------------

    def _apply_freq_unit_range(self, initial_hz=None):
        """Set the freq spin's range/step for the current unit, keeping the Hz."""
        scale = _FREQ_UNITS[self._freq_unit]
        cur_hz = initial_hz if initial_hz is not None else self.freq_spin.value() * self._prev_scale
        lo = self.cfg.limits.freq_min_Hz / scale
        hi = self.cfg.limits.freq_max_Hz / scale
        step = {"Hz": 1000.0, "kHz": 1.0, "MHz": 1.0, "GHz": 0.001}[self._freq_unit]
        self.freq_spin.blockSignals(True)
        self.freq_spin.setRange(lo, hi)
        self.freq_spin.setSingleStep(step)
        self.freq_spin.setValue(cur_hz / scale)
        self.freq_spin.setSuffix(f"  {self._freq_unit}")
        self.freq_spin.blockSignals(False)
        self._prev_scale = scale

    def _change_freq_unit(self, unit: str):
        self._prev_scale = _FREQ_UNITS[self._freq_unit]
        self._freq_unit = unit
        self._apply_freq_unit_range()

    def _current_freq_hz(self) -> float:
        return self.freq_spin.value() * _FREQ_UNITS[self._freq_unit]

    # ---- actions ---------------------------------------------------------

    def _safe(self, fn, *args):
        """Run a command; a refusal because another PC holds control goes to
        the log (the service emits no event for that; normally the viewer
        guard of the control bar stops the click before it gets here)."""
        try:
            fn(*args)
        except ControlRefused as exc:
            self._on_event("error", str(exc))

    def _toggle_rf(self):
        self._safe(self.ctrl.set_rf, not self._rf_on)

    def _set_frequency(self):
        self._safe(self.ctrl.set_frequency, self._current_freq_hz())

    def _set_power(self):
        self._safe(self.ctrl.set_power, self.power_spin.value())

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        self._ceiling_shown = None      # force the power range to be redone
        self._apply_freq_unit_range(initial_hz=self._current_freq_hz())

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
        self._rf_on = bool(s.rf_on)

        # The entry boxes start at the ADOPTED setpoints (what the instrument
        # was doing when we connected), not at the config defaults -- once,
        # at the first frame from a connected brain/service, so typing is
        # never overwritten afterwards.
        if not self._spins_seeded and getattr(s, "connected", False):
            self._spins_seeded = True
            self._apply_freq_unit_range(initial_hz=s.frequency_set_Hz)
            self.power_spin.setValue(s.power_set_dBm)

        self.freq_value.setText(f"{s.frequency_Hz / 1e6:.5f}")
        self.power_value.setText(f"{s.power_dBm:+.1f}")
        self.ceiling_value.setText(f"{s.power_ceiling_dBm:+.1f}")

        # the power spin's top follows the live ceiling -- only touched when it
        # changes, so typing into the box is not disturbed every 60 ms
        if s.power_ceiling_dBm != self._ceiling_shown:
            self._ceiling_shown = s.power_ceiling_dBm
            self.power_spin.setRange(self.cfg.limits.power_min_dBm, s.power_ceiling_dBm)
            self.ceiling_hint.setText(
                f"Max {s.power_ceiling_dBm:+.1f} dBm at {s.frequency_set_Hz / 1e6:g} MHz "
                f"(spec {s.spec_max_dBm:+.0f} dBm, envelope "
                f"{self.cfg.limits.power_max_dBm:+.0f} dBm).")

        if s.rf_on != getattr(self, "_btn_state", None):
            self._btn_state = s.rf_on
            if s.rf_on:
                self.state_badge.setText("RF ON")
                self._badge_color(COLORS["ok"])
                self.rf_btn.setText("Turn RF Off")
                self.rf_btn.setObjectName("danger")
            else:
                self.state_badge.setText("RF OFF")
                self._badge_color(COLORS["muted"])
                self.rf_btn.setText("Turn RF On")
                self.rf_btn.setObjectName("primary")
            # re-apply QSS after the objectName (selector) changed
            self.rf_btn.style().unpolish(self.rf_btn)
            self.rf_btn.style().polish(self.rf_btn)

        # the sweep line: which knob walks where (an older service: no keys)
        sw = getattr(s, "sweep", None) or {}
        moving = [k for k in ("frequency", "power") if sw.get(f"{k}_ramping")]
        if moving:
            parts = []
            for k in moving:
                if k == "frequency":
                    tgt = sw.get("frequency_ramp_target_Hz")
                    if tgt is not None:
                        parts.append(f"frequency -> {tgt / 1e6:,.3f} MHz")
                else:
                    tgt = sw.get("power_ramp_target_dBm")
                    if tgt is not None:
                        parts.append(f"level -> {tgt:+.1f} dBm")
            text = "sweeping " + ", ".join(parts)
        else:
            text = "idle"
        if text != self.sweep_state.text():
            self.sweep_state.setText(text)
            self.sweep_state.setStyleSheet(
                f"color:{COLORS['accent'] if moving else COLORS['muted']};")

        # The input boxes follow the SETPOINT when it changes from elsewhere (a
        # scan, a console, another GUI) -- but never while you are typing in one,
        # and not while that knob SWEEPS: the box holds the sweep's target
        # ("Sweep to"), and the moving setpoint would overwrite it every frame.
        # When the sweep ends the box catches up with where it stopped.
        if s.frequency_set_Hz != self._set_from["f"] and "frequency" not in moving:
            self._set_from["f"] = s.frequency_set_Hz
            if not self.freq_spin.hasFocus():
                self.freq_spin.blockSignals(True)
                self.freq_spin.setValue(s.frequency_set_Hz / _FREQ_UNITS[self._freq_unit])
                self.freq_spin.blockSignals(False)
        if s.power_set_dBm != self._set_from["p"] and "power" not in moving:
            self._set_from["p"] = s.power_set_dBm
            if not self.power_spin.hasFocus():
                self.power_spin.setValue(s.power_set_dBm)

        self._lamp("rpp", not s.rpp_tripped, "armed", "TRIPPED")
        self._lamp("spec", not s.level_unspecified, "yes", "NO")
        self._lamp("mod", s.modulation_off, "yes", "NO")

        alarms = []
        if s.rpp_tripped:
            alarms.append("Reverse power protection tripped. Remove the source, "
                          "then turn RF on to re-arm.")
        if s.hw_error:
            alarms.append(f"Hardware: {s.hw_error}")
        self.alarm_label.setText("\n".join(alarms))
        self.alarm_label.setVisible(bool(alarms))

        if s.connected:
            self.conn_dot.setText("connected")
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        if s.idn:
            self.idn_label.setText(s.idn)

        self.spectrum.set_state(s.rf_on, s.frequency_Hz, s.power_dBm,
                                s.power_ceiling_dBm, self.cfg.limits.power_max_dBm,
                                self.cfg.hardware.option_1ea, s.rpp_tripped)

    def _lamp(self, key, good, good_text, bad_text):
        dot, state = self.lamps[key]
        if state.property("good") == good:      # restyle only on a change
            return
        state.setProperty("good", good)
        color = COLORS["ok"] if good else COLORS["danger"]
        dot.setStyleSheet(f"color:{color};")
        state.setText(good_text if good else bad_text)
        state.setStyleSheet(f"color:{color}; font-weight:700;")

    def _badge_color(self, color):
        self.state_badge.setStyleSheet(
            f"QLabel#stateBadge {{ color:{color}; border: 1px solid {color}; "
            f"background:{COLORS['panel_hi']}; border-radius:10px; padding:4px 12px; "
            f"font-weight:700; letter-spacing:1px; }}")

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()          # local: RF off + disconnect; remote: just close
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a SignalSource-like object. The theme is chosen
    ONCE here, from cfg.ui.theme, BEFORE any widget is built (gotcha #6)."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # swap the active palette first
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from .theme import apply_window_icon
    apply_window_icon(app)
    app.setStyle("Fusion")
    apply_palette(app)
    app.setStyleSheet(build_stylesheet())
    win = MainWindow(ctrl, cfg, remote=remote)
    if os.environ.get("HP8648_DEMO") and not remote:
        # For the README screenshot (tools/render_all.py has no warm-up for this
        # module): a carrier above 2500 MHz, where the ceiling has stepped down.
        # NEVER when connected to a service: a stray environment variable must
        # not switch the RF of a real instrument on (reviewer fix).
        ctrl.set_frequency(3.2e9)
        ctrl.set_power(4.0)
        ctrl.set_rf(True)
    win.show()
    return app.exec()


def main(theme: str | None = None) -> int:
    """Default: run against the built-in simulator, in-process. `theme` (if given)
    overrides cfg.ui.theme for this launch."""
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    src, _ = build_sim_system(cfg)
    return run_app(src, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
