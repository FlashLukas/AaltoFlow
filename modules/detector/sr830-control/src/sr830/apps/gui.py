"""Control GUI for the SR830 lock-in.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

The window holds a brain-like object: a real in-process DspLockIn, or an
Sr830Client facade for a remote service. It sends settings on user actions and
reads a status snapshot on a 60 ms timer.

Layout: a strip at the top that every tab shares -- connection, Acquire + the
last settled sample, the latest message -- and three tabs:

    Lock-in     reference, gain / filter, input and auto functions down the
                left; live R, theta, X, Y; the range meter; a live plot
    Aux I/O     AUX IN 1..4 readouts and plot, AUX OUT 1..4 setpoints
    Instrument  connection, every setting (generated from config), the log

The signature widget is the RangeMeter: an analogue needle showing R as a
fraction of the SENSITIVITY (full scale), with the SR830's two bipolar bar
graphs for X and Y underneath and its overload lamps. It answers the question
an SR830 user asks every few minutes -- "is my range right?" -- at a glance:
a needle loitering near zero wastes resolution, one in the red overloads.

Widget conventions (gotcha #13): user actions are wired to `clicked` and
`activated`, which Qt emits only for the USER, so copying a new status value
into a widget never fires a command back to the instrument.
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import Config
from .. import filters, tables
from ..sim_system import build_sim_system
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsPanel


# ------------------------------------------------------------- helpers

class Bridge(QtCore.QObject):
    """Carries lock-in events across the thread boundary into the GUI."""
    event = QtCore.Signal(str, str)


def _card(title: str | None = None):
    frame = QtWidgets.QFrame()
    frame.setObjectName("card")
    lay = QtWidgets.QVBoxLayout(frame)
    lay.setContentsMargins(12, 10, 12, 10)
    lay.setSpacing(6)
    if title:
        lbl = QtWidgets.QLabel(title.upper())
        lbl.setObjectName("cardTitle")
        lay.addWidget(lbl)
    return frame, lay


def _cap(text: str) -> QtWidgets.QLabel:
    lbl = QtWidgets.QLabel(text.upper())
    lbl.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; font-weight:700; "
                      f"letter-spacing:1px;")
    lbl.setMinimumWidth(52)
    return lbl


def si(v, unit: str = "V") -> tuple[str, str]:
    """0.00123 -> ('1.230', 'mV'). NaN/None -> ('--', unit)."""
    if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
        return "--", unit
    a = abs(v)
    for scale, p in ((1.0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p"),
                     (1e-15, "f")):
        if a >= scale or scale == 1e-15:
            return f"{v / scale:.3f}", p + unit
    return f"{v:.3f}", unit


def fmt_seconds(s) -> str:
    if s is None or not isinstance(s, (int, float)) or not math.isfinite(s):
        return "--"
    if s >= 1:
        return f"{s:.3g} s"
    if s >= 1e-3:
        return f"{s * 1e3:.3g} ms"
    return f"{s * 1e6:.3g} us"


def fmt_hz(f) -> str:
    if f is None or not isinstance(f, (int, float)) or not math.isfinite(f):
        return "--"
    for scale, unit in ((1e3, "kHz"), (1.0, "Hz")):
        if abs(f) >= scale or scale == 1.0:
            return f"{f / scale:,.6g} {unit}"


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


_FREQ_UNITS = {"Hz": 1.0, "kHz": 1e3}


# ------------------------------------------------------------- the range meter

class RangeMeter(QtWidgets.QWidget):
    """An analogue meter of R against the sensitivity, plus X / Y bar graphs.

    The needle shows R / full scale on a 0..10 dial with a red band from 100 %
    to 110 % -- the region where the SR830 flags an output overload. Beneath
    it, two bipolar bars (centre = zero, ends = +-full scale) show X and Y the
    way the SR830's front-panel bar graphs do, and a row of lamps shows the
    status byte: INPUT, FILTER, OUTPUT overload and reference UNLOCK.

    The needle EASES towards the measurement on the widget's own ~33 ms timer
    (as a damped meter movement would), so it moves smoothly whatever the
    status poll rate; a lamp stays lit for 0.6 s after the flag, or a one-poll
    overload would be invisible.
    """

    HOLD_S = 0.6

    def __init__(self):
        super().__init__()
        self.setMinimumSize(330, 290)
        self._frac = 0.0            # R / full scale, the target
        self._shown = 0.0           # what the needle shows now
        self._xf = self._yf = 0.0   # X / FS and Y / FS
        self._fs_label = "--"
        self._pct = "--"
        self._lamp_t = {"INPUT": -1e9, "FILTER": -1e9, "OUTPUT": -1e9, "UNLOCK": -1e9}
        self._acquiring = False
        self._pulse = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, x, y, full_scale, fs_label, overload: dict, unlocked: bool,
                  acquiring: bool):
        self._acquiring = bool(acquiring)
        self._fs_label = fs_label or "--"
        if _finite(x) and _finite(y) and _finite(full_scale) and full_scale > 0:
            self._frac = math.hypot(x, y) / full_scale
            self._xf, self._yf = x / full_scale, y / full_scale
            self._pct = f"{100.0 * self._frac:.0f} %"
        now = time.monotonic()
        for key, on in (("INPUT", overload.get("input")), ("FILTER", overload.get("filter")),
                        ("OUTPUT", overload.get("output")), ("UNLOCK", unlocked)):
            if on:
                self._lamp_t[key] = now

    def _tick(self):
        self._shown += 0.3 * (self._frac - self._shown)
        if self._acquiring:
            self._pulse = (self._pulse + 0.12) % (2 * math.pi)
        self.update()

    # the dial runs from 210 deg (0) to -30 deg (110 %): a 240 deg sweep
    @staticmethod
    def _angle(frac: float) -> float:
        f = max(0.0, min(1.1, frac))
        return math.radians(210.0 - 240.0 * f / 1.1)

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        # vertical budget: 14 above the dial, 1.55 rad for dial + readout, then
        # ~100 px for the two bars and the lamp row
        rad = max(40.0, min(w / 2.0 - 30, (h - 100) / 1.55))
        cx, cy = w / 2.0, 14 + rad

        border = QtGui.QColor(COLORS["border"])
        muted = QtGui.QColor(COLORS["muted"])
        text = QtGui.QColor(COLORS["text"])
        accent = QtGui.QColor(COLORS["accent"])
        danger = QtGui.QColor(COLORS["danger"])

        # dial face: the arc, a red band from 100 % to 110 %
        rect = QtCore.QRectF(cx - rad, cy - rad, 2 * rad, 2 * rad)
        p.setBrush(QtCore.Qt.NoBrush)
        p.setPen(QtGui.QPen(border, 2.0))
        p.drawArc(rect, int(-30 * 16), int(240 * 16))
        red = QtGui.QColor(danger); red.setAlpha(170)
        pen = QtGui.QPen(red, 7.0); pen.setCapStyle(QtCore.Qt.FlatCap)
        p.setPen(pen)
        a0 = math.degrees(self._angle(1.0))
        p.drawArc(rect.adjusted(4, 4, -4, -4), int(-30 * 16), int((a0 + 30) * 16))

        # acquisition: a pulsing glow along the scale while a sample is taken
        if self._acquiring:
            glow = QtGui.QColor(accent); glow.setAlpha(int(50 + 60 * (0.5 + 0.5 * math.sin(self._pulse))))
            p.setPen(QtGui.QPen(glow, 10.0))
            p.drawArc(rect.adjusted(-8, -8, 8, 8), int(-30 * 16), int(240 * 16))

        # ticks and labels 0..10 (tenths of full scale), minor ticks between
        f = p.font(); f.setPointSize(8); f.setBold(True); p.setFont(f)
        for k in range(0, 23):
            frac = k / 20.0
            ang = self._angle(frac)
            major = k % 2 == 0
            r0 = rad - (14 if major else 8)
            c, s = math.cos(ang), -math.sin(ang)
            p.setPen(QtGui.QPen(danger if frac > 1.0 else muted, 1.6 if major else 1.0))
            p.drawLine(QtCore.QPointF(cx + r0 * c, cy + r0 * s),
                       QtCore.QPointF(cx + rad * c, cy + rad * s))
            if major and k <= 20:
                rl = rad - 28
                p.setPen(muted)
                p.drawText(QtCore.QRectF(cx + rl * c - 12, cy + rl * s - 8, 24, 16),
                           QtCore.Qt.AlignCenter, str(k // 2))

        # readout inside the dial
        f.setPointSize(9); p.setFont(f)
        p.setPen(muted)
        p.drawText(QtCore.QRectF(cx - 90, cy + rad * 0.12, 180, 16), QtCore.Qt.AlignCenter,
                   f"FULL SCALE {self._fs_label}")
        f.setPointSize(15); p.setFont(f)
        p.setPen(danger if self._frac > 1.0 else text)
        p.drawText(QtCore.QRectF(cx - 90, cy + rad * 0.12 + 16, 180, 26),
                   QtCore.Qt.AlignCenter, self._pct)

        # the needle, with a soft shadow line so it reads on both themes
        ang = self._angle(self._shown)
        tip = QtCore.QPointF(cx + (rad - 6) * math.cos(ang), cy - (rad - 6) * math.sin(ang))
        pen = QtGui.QPen(danger if self._shown > 1.0 else accent, 3.0)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(QtCore.QPointF(cx, cy), tip)
        p.setPen(QtCore.Qt.NoPen); p.setBrush(text)
        p.drawEllipse(QtCore.QPointF(cx, cy), 5, 5)

        # X and Y bipolar bar graphs, like the SR830's own displays
        top = cy + rad * 0.55 + 22
        bw = min(w - 70, 2 * rad + 20)
        bx = cx - bw / 2
        for row, (name, frac, key) in enumerate((("X", self._xf, "accent"),
                                                 ("Y", self._yf, "quad"))):
            y0 = top + row * 18
            p.setPen(muted); f.setPointSize(8); p.setFont(f)
            p.drawText(QtCore.QRectF(bx - 22, y0 - 2, 18, 14), QtCore.Qt.AlignRight, name)
            p.setPen(QtGui.QPen(border, 1.0)); p.setBrush(QtGui.QColor(COLORS["panel_hi"]))
            p.drawRoundedRect(QtCore.QRectF(bx, y0, bw, 10), 3, 3)
            fr = max(-1.1, min(1.1, frac))
            col = QtGui.QColor(COLORS[key]) if abs(frac) <= 1.0 else danger
            p.setPen(QtCore.Qt.NoPen); p.setBrush(col)
            half = bw / 2.0 / 1.1
            x0, x1 = sorted((cx, cx + half * fr))
            p.drawRoundedRect(QtCore.QRectF(x0, y0 + 1, max(1.0, x1 - x0), 8), 2, 2)
            p.setPen(QtGui.QPen(muted, 1.0))
            p.drawLine(QtCore.QPointF(cx, y0 - 1), QtCore.QPointF(cx, y0 + 11))

        # status lamps
        now = time.monotonic()
        names = ("INPUT", "FILTER", "OUTPUT", "UNLOCK")
        lw = min(78.0, (w - 20) / 4.0)
        lx = cx - 2 * lw
        ly = top + 42
        f.setPointSize(7); p.setFont(f)
        for k, name in enumerate(names):
            on = now - self._lamp_t[name] < self.HOLD_S
            c = QtGui.QColor(danger if on else COLORS["panel_hi"])
            p.setPen(QtGui.QPen(border, 1.0)); p.setBrush(c)
            p.drawEllipse(QtCore.QPointF(lx + k * lw + 9, ly + 6), 5, 5)
            p.setPen(danger if on else muted)
            p.drawText(QtCore.QRectF(lx + k * lw + 18, ly, lw - 18, 13),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, name)
        p.end()


# ------------------------------------------------------------- the settings sidebar

class _Combo(QtWidgets.QComboBox):
    """A drop-down that sends its choice on USER activation only, and follows
    the instrument otherwise (never while its list is open)."""

    def __init__(self, items, on_pick):
        super().__init__()
        self.addItems(list(items))
        self.activated.connect(lambda _i: on_pick(self.currentText()))

    def follow(self, value: str):
        if value and value != self.currentText() and not self.view().isVisible():
            self.blockSignals(True)
            idx = self.findText(value)
            if idx >= 0:
                self.setCurrentIndex(idx)
            self.blockSignals(False)

    def set_items(self, items):
        items = list(items)
        if [self.itemText(i) for i in range(self.count())] != items:
            cur = self.currentIndex()
            self.blockSignals(True)
            self.clear(); self.addItems(items)
            self.setCurrentIndex(min(max(cur, 0), len(items) - 1))
            self.blockSignals(False)


def _row(label, *widgets, stretch_first=True):
    lay = QtWidgets.QHBoxLayout(); lay.setSpacing(6)
    lay.addWidget(_cap(label))
    for k, wdg in enumerate(widgets):
        lay.addWidget(wdg, 1 if (k == 0 and stretch_first) else 0)
    return lay


class Controls(QtWidgets.QWidget):
    """The left column: reference, gain / filter (with the auto functions), input."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        cfg = win.cfg
        call = win.call
        ctrl = win.ctrl
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)

        # -- reference ---------------------------------------------------------
        card, lay = _card("Reference")
        self.btn_int = QtWidgets.QPushButton("Internal")
        self.btn_ext = QtWidgets.QPushButton("External")
        for b, mode in ((self.btn_int, "internal"), (self.btn_ext, "external")):
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, m=mode: call(ctrl.set_reference_source, m))
        self.lock_badge = QtWidgets.QLabel("")
        self.lock_badge.setMinimumWidth(66); self.lock_badge.setAlignment(QtCore.Qt.AlignCenter)
        lay.addLayout(_row("Source", self.btn_int, self.btn_ext, self.lock_badge,
                           stretch_first=False))

        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setDecimals(4); self.freq_spin.setRange(0.0, 1e6)
        self.freq_unit = QtWidgets.QComboBox(); self.freq_unit.addItems(list(_FREQ_UNITS))
        self.freq_unit.setCurrentText("kHz" if cfg.reference.frequency_Hz >= 1e3 else "Hz")
        self._freq_scale = _FREQ_UNITS[self.freq_unit.currentText()]
        self.freq_spin.setValue(cfg.reference.frequency_Hz / self._freq_scale)
        self.freq_unit.currentTextChanged.connect(self._freq_unit_changed)
        self.freq_set = QtWidgets.QPushButton("Set"); self.freq_set.setObjectName("primary")
        self.freq_set.clicked.connect(lambda: call(
            ctrl.set_frequency, self.freq_spin.value() * self._freq_scale))
        lay.addLayout(_row("Freq", self.freq_spin, self.freq_unit, self.freq_set))

        self.harm_spin = QtWidgets.QSpinBox(); self.harm_spin.setRange(1, 19999)
        self.harm_spin.setValue(cfg.reference.harmonic)
        harm_set = QtWidgets.QPushButton("Set"); harm_set.setObjectName("primary")
        harm_set.clicked.connect(lambda: call(ctrl.set_harmonic, self.harm_spin.value()))
        self.phase_spin = QtWidgets.QDoubleSpinBox(); self.phase_spin.setRange(-180, 180)
        self.phase_spin.setDecimals(2); self.phase_spin.setSuffix(" deg")
        self.phase_spin.setValue(cfg.reference.phase_deg)
        phase_set = QtWidgets.QPushButton("Set"); phase_set.setObjectName("primary")
        phase_set.clicked.connect(lambda: call(ctrl.set_phase, self.phase_spin.value()))
        lay.addLayout(_row("Harm", self.harm_spin, harm_set))
        lay.addLayout(_row("Phase", self.phase_spin, phase_set))

        self.sine_spin = QtWidgets.QDoubleSpinBox()
        self.sine_spin.setRange(cfg.limits.sine_min_V, cfg.limits.sine_max_V)
        self.sine_spin.setDecimals(3); self.sine_spin.setSingleStep(0.002)
        self.sine_spin.setSuffix(" Vrms"); self.sine_spin.setValue(cfg.reference.sine_out_V)
        sine_set = QtWidgets.QPushButton("Set"); sine_set.setObjectName("primary")
        sine_set.clicked.connect(lambda: call(ctrl.set_sine_out, self.sine_spin.value()))
        lay.addLayout(_row("Sine out", self.sine_spin, sine_set))
        self.trigger = _Combo(tables.TRIGGERS, lambda t: call(ctrl.set_trigger, t))
        lay.addLayout(_row("Trigger", self.trigger))
        v.addWidget(card)

        # -- gain and filter --------------------------------------------------------
        card, lay = _card("Gain and filter")
        self.sens = _Combo(tables.SENS_LABELS_V, lambda s: call(ctrl.set_sensitivity, s))
        self.reserve = _Combo(tables.RESERVES, lambda s: call(ctrl.set_reserve, s))
        self.tc = _Combo(tables.TC_LABELS, lambda s: call(ctrl.set_time_constant, s))
        self.slope = _Combo(tables.SLOPES, lambda s: call(ctrl.set_slope, s))
        self.sync = QtWidgets.QCheckBox("Sync")
        self.sync.setToolTip("Synchronous filter: removes 2f ripple below 200 Hz")
        self.sync.clicked.connect(lambda on: call(ctrl.set_sync_filter, on))
        lay.addLayout(_row("Sens", self.sens))
        lay.addLayout(_row("Reserve", self.reserve))
        lay.addLayout(_row("TC", self.tc))
        lay.addLayout(_row("Slope", self.slope, self.sync))
        # the auto functions belong with the settings they change
        row = QtWidgets.QHBoxLayout(); row.setSpacing(6)
        self.auto_btns = []
        for text, fn in (("gain", ctrl.auto_gain), ("phase", ctrl.auto_phase),
                         ("reserve", ctrl.auto_reserve)):
            b = QtWidgets.QPushButton(f"Auto {text}")
            b.clicked.connect(lambda _=False, f=fn: call(f))
            row.addWidget(b)
            self.auto_btns.append(b)
        lay.addLayout(row)
        self.auto_note = QtWidgets.QLabel(""); self.auto_note.setObjectName("hint")
        self.auto_note.setWordWrap(True)
        lay.addWidget(self.auto_note)
        self.applied = QtWidgets.QLabel("--")
        self.applied.setObjectName("hint"); self.applied.setWordWrap(True)
        lay.addWidget(self.applied)
        v.addWidget(card)

        # -- input ----------------------------------------------------------------------
        card, lay = _card("Input")
        self.src = _Combo(tables.INPUT_SOURCES, lambda s: call(ctrl.set_input_source, s))
        self.cpl = _Combo(tables.COUPLINGS, lambda s: call(ctrl.set_input_coupling, s))
        self.gnd = _Combo(tables.GROUNDS, lambda s: call(ctrl.set_input_ground, s))
        self.line = _Combo(tables.LINE_FILTERS, lambda s: call(ctrl.set_line_filter, s))
        lay.addLayout(_row("Source", self.src, _cap("Cpl"), self.cpl))
        lay.addLayout(_row("Shield", self.gnd, _cap("Notch"), self.line))
        v.addWidget(card)

        v.addStretch(1)

        self._last_mode = None
        self._last_unit = None
        self._synced = {}

    def _freq_unit_changed(self, unit):
        hz = self.freq_spin.value() * self._freq_scale
        self._freq_scale = _FREQ_UNITS[unit]
        self.freq_spin.setValue(hz / self._freq_scale)

    def _follow_spin(self, key, spin, value, scale=1.0):
        """Copy a setpoint changed ELSEWHERE into a box -- only when it really
        moved since we last copied it, and never into a box being typed in."""
        if value is None or not _finite(value):
            return
        if self._synced.get(key) != value and not spin.hasFocus():
            self._synced[key] = value
            spin.setValue(value / scale)

    def refresh(self, s):
        mode = s.reference_source
        if mode != self._last_mode:
            self._last_mode = mode
            self.btn_int.setChecked(mode == "internal")
            self.btn_ext.setChecked(mode == "external")
            for b in (self.btn_int, self.btn_ext):
                b.setObjectName("primary" if b.isChecked() else "")
                b.style().unpolish(b); b.style().polish(b)
            ext = mode == "external"
            for wdg in (self.freq_spin, self.freq_unit, self.freq_set):
                wdg.setEnabled(not ext)
        if mode == "external":
            ok = not s.unlocked
            self.lock_badge.setText("LOCKED" if ok else "UNLOCKED")
            self.lock_badge.setStyleSheet(
                f"color:{COLORS['ok'] if ok else COLORS['danger']}; font-weight:800; font-size:11px;")
        else:
            self.lock_badge.setText("")
        f = s.ref_freq_Hz if mode == "external" else s.freq_set_Hz
        self._follow_spin("freq", self.freq_spin, f, self._freq_scale)
        self._follow_spin("harm", self.harm_spin, s.harmonic)
        self._follow_spin("phase", self.phase_spin, s.phase_deg)
        self._follow_spin("sine", self.sine_spin, s.sine_out_V)
        self.trigger.follow(s.trigger)

        if s.unit != self._last_unit:          # current input: the ranges read in A
            self._last_unit = s.unit
            self.sens.set_items(tables.SENS_LABELS_A if s.unit == "A" else tables.SENS_LABELS_V)
        self.sens.follow(s.sensitivity)
        self.reserve.follow(s.reserve)
        self.tc.follow(s.time_constant)
        self.slope.follow(s.slope)
        if self.sync.isChecked() != bool(s.sync_filter):
            self.sync.blockSignals(True); self.sync.setChecked(bool(s.sync_filter))
            self.sync.blockSignals(False)
        self.src.follow(s.input_source)
        self.cpl.follow(s.input_coupling)
        self.gnd.follow(s.input_ground)
        self.line.follow(s.line_filter)

        tc, order = s.tc_s, s.order
        bw = filters.enbw_Hz(tc, order) if _finite(tc) and tc > 0 else float("nan")
        pct = self.win.cfg.acquisition.settle_percent
        self.applied.setText(
            f"settles {pct:g} % in {fmt_seconds(s.settle_s)}  -  ENBW {bw:.3g} Hz  -  "
            f"detecting {fmt_hz(s.detect_freq_Hz)}")
        for b in self.auto_btns:
            b.setEnabled(bool(s.connected) and not s.auto_busy)
        if s.auto_busy:
            self.auto_note.setText(f"auto {s.auto_name} running ...")
        elif s.auto_note:
            self.auto_note.setText(f"last: {s.auto_note}")


# ------------------------------------------------------------- live history + plots

_WINDOWS = {"10 s": 10.0, "30 s": 30.0, "1 min": 60.0, "5 min": 300.0}


class History:
    """A rolling record of every live value, shared by all plots.

    Filled once per refresh (~60 ms), so 6000 points hold a bit over 5 minutes.
    One store instead of one per plot: a tab opened later already has its past.
    """

    MAX = 6000
    KEYS = ("x", "y", "r", "theta", "aux1", "aux2", "aux3", "aux4")

    def __init__(self):
        self.t = deque(maxlen=self.MAX)
        self.v = {k: deque(maxlen=self.MAX) for k in self.KEYS}

    def add(self, t: float, live: dict) -> None:
        def val(x):
            return float(x) if _finite(x) else float("nan")
        self.t.append(t)
        self.v["x"].append(val(live.get("x")))
        self.v["y"].append(val(live.get("y")))
        self.v["r"].append(val(live.get("r")))
        self.v["theta"].append(val(live.get("theta_deg")))
        aux = live.get("aux_in") or [None] * 4
        for k in range(4):
            self.v[f"aux{k + 1}"].append(val(aux[k] if k < len(aux) else None))

    def clear(self) -> None:
        self.t.clear()
        for d in self.v.values():
            d.clear()

    def window(self, keys, seconds: float):
        """(t relative to now, {key: values}) for the last `seconds`, as numpy arrays."""
        import numpy as np
        if not self.t:
            return np.array([]), {k: np.array([]) for k in keys}
        t = np.fromiter(self.t, float, len(self.t))
        start = int(np.searchsorted(t, t[-1] - seconds))
        out = {k: np.fromiter(self.v[k], float, len(self.v[k]))[start:] for k in keys}
        return t[start:] - t[-1], out


def _make_plot(y_label: str, units: str):
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=True)
    w = pg.PlotWidget(background=COLORS["code_bg"])
    w.setMinimumHeight(140)
    ax_pen = pg.mkPen(COLORS["muted"])
    for name in ("left", "bottom"):
        ax = w.getAxis(name)
        ax.setPen(ax_pen)
        ax.setTextPen(ax_pen)
    w.setLabel("left", y_label, units=units)
    w.setLabel("bottom", "time", units="s")
    w.showGrid(x=True, y=True, alpha=0.15)
    return w


class PlotControls:
    """Window length + Pause, shared shape for every live plot card."""

    def __init__(self):
        self.window = QtWidgets.QComboBox()
        self.window.addItems(list(_WINDOWS))
        self.window.setCurrentText("30 s")
        self.pause = QtWidgets.QCheckBox("Pause")
        self.pause.setToolTip("Freeze the plot. Data keeps being recorded.")

    def add_to(self, lay):
        lay.addWidget(_cap("Window"))
        lay.addWidget(self.window)
        lay.addWidget(self.pause)

    @property
    def seconds(self) -> float:
        return _WINDOWS[self.window.currentText()]


# ------------------------------------------------------------- the tabs

class LockInTab(QtWidgets.QWidget):
    """Settings on the left; readouts, range meter and live plot on the right."""

    QUANTITIES = {"R": ("r",), "X and Y": ("x", "y"), "Theta": ("theta",)}

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 8, 0, 0); grid.setSpacing(10)

        self.controls = Controls(win)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self.controls)
        scroll.setFixedWidth(380)
        grid.addWidget(scroll, 0, 0, 2, 1)

        # live readouts
        card, lay = _card("Live")
        big = QtWidgets.QHBoxLayout(); big.setSpacing(20)
        r_box = QtWidgets.QVBoxLayout(); r_box.setSpacing(0)
        r_box.addWidget(_cap("R"))
        r_line = QtWidgets.QHBoxLayout()
        self.r_val = QtWidgets.QLabel("--"); self.r_val.setObjectName("bigValue")
        # fixed width, right-aligned: the unit stays put while the digits change
        self.r_val.setMinimumWidth(150)
        self.r_val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom)
        self.r_unit = QtWidgets.QLabel("V"); self.r_unit.setObjectName("unit")
        r_line.addWidget(self.r_val); r_line.addWidget(self.r_unit, 0, QtCore.Qt.AlignBottom)
        r_line.addStretch(1)
        r_box.addLayout(r_line)
        big.addLayout(r_box, 1)
        th_box = QtWidgets.QVBoxLayout(); th_box.setSpacing(0)
        th_box.addWidget(_cap("Theta"))
        self.th_val = QtWidgets.QLabel("--"); self.th_val.setObjectName("midValue")
        th_box.addWidget(self.th_val)
        big.addLayout(th_box)
        lay.addLayout(big)
        self.xy = QtWidgets.QLabel("--"); self.xy.setObjectName("mono")
        lay.addWidget(self.xy)
        self.freq = QtWidgets.QLabel("--"); self.freq.setObjectName("mono")
        lay.addWidget(self.freq)
        self.aux = QtWidgets.QLabel("--"); self.aux.setObjectName("mono")
        lay.addWidget(self.aux)
        self.sample = QtWidgets.QLabel("no settled sample yet"); self.sample.setObjectName("hint")
        self.sample.setWordWrap(True)
        lay.addWidget(self.sample)
        lay.addStretch(1)
        grid.addWidget(card, 0, 1)

        mcard, mlay = _card("Range")
        self.meter = RangeMeter()
        mlay.addWidget(self.meter, 1)
        grid.addWidget(mcard, 0, 2)

        # live plot
        plot_card, plot_lay = _card()
        head = QtWidgets.QHBoxLayout(); head.setSpacing(8)
        title = QtWidgets.QLabel("LIVE PLOT"); title.setObjectName("cardTitle")
        head.addWidget(title); head.addStretch(1)
        head.addWidget(_cap("Show"))
        self.quantity = QtWidgets.QComboBox(); self.quantity.addItems(list(self.QUANTITIES))
        self.quantity.currentTextChanged.connect(self._quantity_changed)
        head.addWidget(self.quantity)
        self.pc = PlotControls(); self.pc.add_to(head)
        plot_lay.addLayout(head)
        self.plot = _make_plot("R", "V")
        self.legend = self.plot.addLegend(offset=(10, 10), labelTextColor=COLORS["text"])
        import pyqtgraph as pg
        acc = COLORS["accent"]
        self.curves = {
            "r": self.plot.plot([], [], pen=pg.mkPen(acc, width=2), name="R"),
            "x": self.plot.plot([], [], pen=pg.mkPen(acc, width=2), name="X"),
            "y": self.plot.plot([], [], pen=pg.mkPen(COLORS["quad"], width=2), name="Y"),
            "theta": self.plot.plot([], [], pen=pg.mkPen(acc, width=2), name="theta"),
        }
        plot_lay.addWidget(self.plot, 1)
        grid.addWidget(plot_card, 1, 1, 1, 2)

        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)
        self._unit = "V"
        self._quantity_changed(self.quantity.currentText())

    def _quantity_changed(self, name: str):
        shown = self.QUANTITIES[name]
        # the legend lists every curve ever added -- rebuild it with the visible ones
        self.legend.clear()
        for key, curve in self.curves.items():
            curve.setVisible(key in shown)
            if key in shown:
                self.legend.addItem(curve, curve.name())
        if name == "Theta":
            self.plot.setLabel("left", "theta", units="deg")
        else:
            self.plot.setLabel("left", name, units=self._unit)
        self.redraw()

    def refresh(self, s):
        self.controls.refresh(s)
        if s.unit != self._unit:
            self._unit = s.unit
            self._quantity_changed(self.quantity.currentText())
        live, u = s.live, s.unit
        v, unit = si(live.get("r"), u)
        self.r_val.setText(v); self.r_unit.setText(unit)
        th = live.get("theta_deg")
        self.th_val.setText(f"{th:+.2f} deg" if _finite(th) else "--")
        xv, xu = si(live.get("x"), u); yv, yu = si(live.get("y"), u)
        self.xy.setText(f"X {xv} {xu}    Y {yv} {yu}")
        self.freq.setText(f"f ref {fmt_hz(s.ref_freq_Hz)}   harm {s.harmonic}   "
                          f"phase {s.phase_deg:+.2f} deg")
        aux = live.get("aux_in") or []
        self.aux.setText("aux in  " + "  ".join(
            f"{a:+.3f}" if _finite(a) else "--" for a in aux) + " V")
        smp = s.sample
        if smp.get("acq_id"):
            rv, ru = si(smp.get("r"), smp.get("unit", u))
            ovl = "  OVERLOAD" if smp.get("overload") else ""
            self.sample.setText(f"last settled sample #{smp['acq_id']}: R {rv} {ru}, "
                                f"theta {smp.get('theta_deg', float('nan')):+.2f} deg{ovl}")
        self.meter.set_state(live.get("x"), live.get("y"), s.full_scale, s.sensitivity,
                             s.overload or {}, s.unlocked, s.acquiring)

    def redraw(self):
        if self.pc.pause.isChecked():
            return
        keys = ("r", "x", "y", "theta")
        t, vals = self.win.history.window(keys, self.pc.seconds)
        for q in keys:
            if self.curves[q].isVisible():
                self.curves[q].setData(t, vals[q], connect="finite")


class AuxTab(QtWidgets.QWidget):
    """AUX IN 1..4 readouts + plot, AUX OUT 1..4 setpoints."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        import pyqtgraph as pg
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0); v.setSpacing(10)

        row = QtWidgets.QHBoxLayout(); row.setSpacing(10)
        self.vals, self.stats = [], []
        for k in range(4):
            card, lay = _card(f"AUX IN {k + 1}")
            val = QtWidgets.QLabel("--"); val.setObjectName("midValue")
            lay.addWidget(val)
            stat = QtWidgets.QLabel("--"); stat.setObjectName("hint"); stat.setWordWrap(True)
            lay.addWidget(stat)
            row.addWidget(card, 1)
            self.vals.append(val); self.stats.append(stat)
        v.addLayout(row)

        mid = QtWidgets.QHBoxLayout(); mid.setSpacing(10)
        card, lay = _card()
        head = QtWidgets.QHBoxLayout(); head.setSpacing(8)
        title = QtWidgets.QLabel("AUX INPUTS  -  LIVE PLOT"); title.setObjectName("cardTitle")
        head.addWidget(title); head.addStretch(1)
        self.pc = PlotControls(); self.pc.add_to(head)
        clear = QtWidgets.QPushButton("Clear")
        clear.setToolTip("Forget the recorded history of every plot")
        clear.clicked.connect(win.history.clear)
        head.addWidget(clear)
        lay.addLayout(head)
        self.plot = _make_plot("AUX IN", "V")
        self.plot.addLegend(offset=(10, 10), labelTextColor=COLORS["text"])
        colours = (COLORS["accent"], COLORS["quad"], COLORS["ok"], COLORS["muted"])
        self.curves = [self.plot.plot([], [], pen=pg.mkPen(colours[k], width=2),
                                      name=f"AUX IN {k + 1}") for k in range(4)]
        lay.addWidget(self.plot, 1)
        mid.addWidget(card, 1)

        ocard, olay = _card("Aux out")
        lim = win.cfg.limits
        self.out_spins = []
        for k in range(4):
            sp = QtWidgets.QDoubleSpinBox()
            sp.setRange(lim.aux_out_min_V, lim.aux_out_max_V)
            sp.setDecimals(3); sp.setSingleStep(0.01); sp.setSuffix(" V")
            sp.setValue(win.cfg.aux_out.get(k))
            b = QtWidgets.QPushButton("Set"); b.setObjectName("primary")
            b.clicked.connect(lambda _=False, kk=k, spin=sp: win.call(
                win.ctrl.set_aux_out, kk + 1, spin.value()))
            olay.addLayout(_row(f"Out {k + 1}", sp, b))
            self.out_spins.append(sp)
        note = QtWidgets.QLabel("Rear-panel DC outputs, 1 mV resolution. Set to 0 V when "
                                "the service stops (Settings > Safety).")
        note.setObjectName("hint"); note.setWordWrap(True)
        olay.addWidget(note)
        olay.addStretch(1)
        ocard.setFixedWidth(320)
        mid.addWidget(ocard)
        v.addLayout(mid, 1)
        self._synced = [None] * 4

    def refresh(self, s):
        import numpy as np
        aux = s.live.get("aux_in") or [None] * 4
        smp = s.sample
        _, vals = self.win.history.window([f"aux{k + 1}" for k in range(4)], self.pc.seconds)
        for k in range(4):
            a = aux[k] if k < len(aux) else None
            self.vals[k].setText(f"{a:+.4f} V" if _finite(a) else "--")
            arr = vals[f"aux{k + 1}"]
            finite = arr[np.isfinite(arr)] if arr.size else arr
            parts = []
            if finite.size:
                parts.append(f"window {finite.min():+.3f} .. {finite.max():+.3f}, "
                             f"mean {finite.mean():+.4f} V")
            if smp.get("acq_id") and smp.get("aux_in"):
                parts.append(f"sample #{smp['acq_id']}: {smp['aux_in'][k]:+.4f} V")
            self.stats[k].setText("\n".join(parts) or "--")
            want = s.aux_out_set_V[k] if k < len(s.aux_out_set_V) else None
            sp = self.out_spins[k]
            if _finite(want) and want != self._synced[k] and not sp.hasFocus():
                self._synced[k] = want
                sp.setValue(want)

    def redraw(self):
        if self.pc.pause.isChecked():
            return
        keys = [f"aux{k + 1}" for k in range(4)]
        t, vals = self.win.history.window(keys, self.pc.seconds)
        for k in range(4):
            self.curves[k].setData(t, vals[keys[k]], connect="finite")


class InstrumentTab(QtWidgets.QWidget):
    """Connection, every setting, and the full log."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 8, 0, 0); grid.setSpacing(10)

        card, lay = _card("Instrument")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        self.f_conn = QtWidgets.QLabel("--")
        self.f_idn = QtWidgets.QLabel("--"); self.f_idn.setWordWrap(True)
        self.f_res = QtWidgets.QLabel("--")
        self.f_input = QtWidgets.QLabel("--")
        self.f_error = QtWidgets.QLabel("none"); self.f_error.setWordWrap(True)
        for label, w in (("connection", self.f_conn), ("instrument", self.f_idn),
                         ("address", self.f_res), ("input", self.f_input),
                         ("hardware error", self.f_error)):
            form.addRow(_cap(label), w)
        lay.addLayout(form)
        lay.addStretch(1)
        grid.addWidget(card, 0, 0)

        scard, slay = _card("Settings")
        self.settings = SettingsPanel(win.ctrl, win.cfg, win._on_settings_applied, self)
        self.settings.add_apply_button("Apply")
        slay.addWidget(self.settings, 1)
        grid.addWidget(scard, 0, 1, 2, 1)

        lcard, llay = _card("Log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(2000)
        llay.addWidget(self.log)
        grid.addWidget(lcard, 1, 0)

        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1); grid.setRowStretch(1, 2)

    def refresh(self, s):
        cfg = self.win.cfg
        self.f_conn.setText("connected" if s.connected else "offline")
        self.f_conn.setStyleSheet(f"color:{COLORS['ok'] if s.connected else COLORS['danger']};"
                                  f" font-weight:700;")
        self.f_idn.setText(s.idn or "--")
        self.f_res.setText("remote service" if self.win._remote else cfg.hardware.resource)
        self.f_input.setText(f"{s.input_source}, {s.input_coupling}, shield {s.input_ground}, "
                             f"notch {s.line_filter}")
        self.f_error.setText(s.hw_error or "none")
        self.f_error.setStyleSheet(f"color:{COLORS['danger'] if s.hw_error else COLORS['muted']};")


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    TAB_INSTRUMENT = 2

    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self.history = History()
        self.setWindowTitle("SR830 - Lock-in Amplifier" + ("  (remote)" if remote else ""))
        self.resize(1280, 860)

        root = QtWidgets.QWidget(); root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(14, 10, 14, 14); outer.setSpacing(8)
        outer.addWidget(self._build_strip())

        self.tabs = QtWidgets.QTabWidget()
        self.main_tab = LockInTab(self)
        self.aux_tab = AuxTab(self)
        self.inst_tab = InstrumentTab(self)
        self.tabs.addTab(self.main_tab, "Lock-in")
        self.tabs.addTab(self.aux_tab, "Aux I/O")
        self.tabs.addTab(self.inst_tab, "Instrument")
        self.tabs.currentChanged.connect(self._tab_changed)
        outer.addWidget(self.tabs, 1)

        self.controls = self.main_tab.controls
        self.log = self.inst_tab.log

        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

        self.ctrl.start()
        self._t0 = time.monotonic()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

    # ---- the shared strip -------------------------------------------------------

    def _build_strip(self):
        card = QtWidgets.QFrame(); card.setObjectName("card")
        h = QtWidgets.QHBoxLayout(card)
        h.setContentsMargins(14, 8, 14, 8); h.setSpacing(14)
        title = QtWidgets.QLabel("SR830")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; "
                            f"letter-spacing:2px;")
        h.addWidget(title)
        self.conn_dot = QtWidgets.QLabel("connecting")
        h.addWidget(self.conn_dot)
        self.idn_label = QtWidgets.QLabel("--")
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        h.addWidget(self.idn_label)
        h.addSpacing(8)
        self.acq_btn = QtWidgets.QPushButton("Acquire"); self.acq_btn.setObjectName("primary")
        self.acq_btn.setToolTip("Wait the settling time, then latch one sample")
        self.acq_btn.clicked.connect(lambda: self.call(self.ctrl.acquire))
        h.addWidget(self.acq_btn)
        self.acq_bar = QtWidgets.QProgressBar(); self.acq_bar.setRange(0, 1000)
        self.acq_bar.setTextVisible(False); self.acq_bar.setFixedSize(100, 10)
        h.addWidget(self.acq_bar)
        self.sample_label = QtWidgets.QLabel("no sample yet"); self.sample_label.setObjectName("mono")
        # Ignored: a long sample line must be cut off, not widen the whole window
        self.sample_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                        QtWidgets.QSizePolicy.Preferred)
        h.addWidget(self.sample_label, 1)
        self.last_msg = QtWidgets.QLabel("")
        self.last_msg.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        self.last_msg.setMaximumWidth(420)
        h.addWidget(self.last_msg)
        return card

    # ---- actions ------------------------------------------------------------------

    def call(self, fn, *args):
        """Run a command and surface a refusal.

        A local brain raises ValueError; a remote client returns
        {"ok": false, "error": ...} (or raises). Both end up in the log.
        """
        try:
            r = fn(*args)
        except Exception as exc:
            self._on_event("error", str(exc))
            return None
        if isinstance(r, dict) and r.get("ok") is False:
            self._on_event("error", r.get("error", "refused"))
        return r

    def _tab_changed(self, index: int):
        if index == self.TAB_INSTRUMENT:
            # show the settings actually in use (a scan or console may have
            # changed them since this tab was last open)
            self.inst_tab.settings.reload()
        self._redraw_visible()

    def _on_settings_applied(self):
        c = self.controls
        lim = self.cfg.limits
        c.sine_spin.setRange(lim.sine_min_V, lim.sine_max_V)
        for sp in self.aux_tab.out_spins:
            sp.setRange(lim.aux_out_min_V, lim.aux_out_max_V)
        c._last_mode = None            # force the reference buttons to re-sync

    # ---- refresh & events -----------------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = COLORS["danger"] if level == "error" else (
            COLORS["accent"] if level == "warn" else COLORS["muted"])
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(
            f'<span style="color:{COLORS["accent_dim"]}">{stamp}</span> '
            f'<span style="color:{color}">{msg}</span>')
        self.last_msg.setText(f"{stamp}  {msg}")
        self.last_msg.setToolTip(msg)
        self.last_msg.setStyleSheet(f"color:{color}; font-size:11px;"
                                    + (" font-weight:700;" if level != "info" else ""))

    def _refresh(self):
        s = self.ctrl.status()

        if s.hw_error:
            self.conn_dot.setText("hardware error")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        elif s.connected:
            self.conn_dot.setText("connected")
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        if s.idn:
            self.idn_label.setText(s.idn)

        self.acq_bar.setValue(int(1000 * (s.acq_progress if s.acquiring else 0)))
        self.acq_btn.setEnabled(bool(s.connected) and not s.acquiring and not s.auto_busy)
        smp = s.sample
        if smp.get("acq_id"):
            rv, ru = si(smp.get("r"), smp.get("unit", s.unit))
            ovl = "  OVERLOAD" if smp.get("overload") else ""
            self.sample_label.setText(
                f"#{smp['acq_id']}  settle {fmt_seconds(smp.get('settle_s'))}, "
                f"{smp.get('n_avg', 1)} pts  |  R {rv} {ru}  "
                f"theta {smp.get('theta_deg', float('nan')):+.2f} deg{ovl}")

        self.history.add(time.monotonic() - self._t0, s.live)
        self.main_tab.refresh(s)        # settings must stay in sync even when hidden
        self.aux_tab.refresh(s)
        self.inst_tab.refresh(s)
        self._redraw_visible()

    def _redraw_visible(self):
        """Only the visible tab's plots are redrawn -- the rest record silently."""
        current = self.tabs.currentWidget()
        if current is self.main_tab or current is self.aux_tab:
            current.redraw()

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app. The theme is chosen ONCE here, before any widget."""
    set_theme(getattr(cfg.ui, "theme", "dark"))
    # Numbers with a '.' decimal point and no thousands separator, whatever the
    # Windows locale (gotcha #18).
    loc = QtCore.QLocale.c()
    loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
    QtCore.QLocale.setDefault(loc)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from .theme import apply_window_icon
    apply_window_icon(app)
    app.setStyle("Fusion")
    apply_palette(app)
    app.setStyleSheet(build_stylesheet() + _EXTRA_QSS())
    win = MainWindow(ctrl, cfg, remote=remote)
    win.show()
    return app.exec()


def _EXTRA_QSS() -> str:
    """Styles only this module needs, built from the active palette."""
    return f"""
QLabel#midValue {{ font-size: 20px; font-weight: 700; color: {COLORS['text']}; }}
QLabel#mono {{ font-family: "Cascadia Code", "Consolas", monospace; font-size: 12px;
              color: {COLORS['text']}; }}
QProgressBar {{ background: {COLORS['panel_hi']}; border: 1px solid {COLORS['border']};
               border-radius: 5px; }}
QProgressBar::chunk {{ background: {COLORS['accent']}; border-radius: 4px; }}
QPushButton:disabled {{ color: {COLORS['muted']}; }}
QDoubleSpinBox:disabled, QComboBox:disabled {{ color: {COLORS['muted']}; }}
"""


def main(theme: str | None = None) -> int:
    """Default: run against the built-in simulator, in-process."""
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    li, _ = build_sim_system(cfg)
    return run_app(li, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
