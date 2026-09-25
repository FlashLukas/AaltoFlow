"""Control GUI for the HF2LI lock-in (two channels + aux inputs).

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

The window holds a LockIn-like object: a real in-process LockIn, or an
Hf2Client facade for a remote service. It sends settings on button clicks and
reads a status snapshot on a 60 ms timer.

Layout (Lukas, 2026-09-15): a strip at the top that every tab shares --
connection, Acquire + the last settled sample, the latest message -- and four
tabs:

    Channel 1   settings, live readouts, phasor dial, live plot (R, X/Y or theta)
    Channel 2   the same for the second demodulator
    Aux in      AUX IN 1 / 2 readouts and their live plots
    Instrument  status, routing, every setting (generated from config), the log

All live plots draw from ONE rolling History that the refresh timer fills, so a
tab you open later already shows the last minutes, and only the visible tab's
plots are redrawn.

The signature widget is the PhasorIndicator: a polar dial with one arrow per
channel pointing at theta, plus a fading trail of recent tips. The trail is the
noise cloud, so lengthening the time constant visibly shrinks it -- which is
the single most useful thing to be able to SEE on a lock-in.
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import Config
from .. import filters
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
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(8)
    if title:
        lbl = QtWidgets.QLabel(title.upper())
        lbl.setObjectName("cardTitle")
        lay.addWidget(lbl)
    return frame, lay


def _cap(text: str) -> QtWidgets.QLabel:
    lbl = QtWidgets.QLabel(text.upper())
    lbl.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; font-weight:700; "
                      f"letter-spacing:1px;")
    return lbl


def si_volts(v) -> tuple[str, str]:
    """0.00123 -> ('1.230', 'mV'). NaN/None -> ('--', 'V')."""
    if v is None or not math.isfinite(v):
        return "--", "V"
    a = abs(v)
    for scale, unit in ((1.0, "V"), (1e-3, "mV"), (1e-6, "uV"), (1e-9, "nV")):
        if a >= scale or scale == 1e-9:
            return f"{v / scale:.3f}", unit
    return f"{v:.3f}", "V"


def fmt_seconds(s) -> str:
    if s is None or not math.isfinite(s):
        return "--"
    if s >= 1:
        return f"{s:.3g} s"
    if s >= 1e-3:
        return f"{s * 1e3:.3g} ms"
    return f"{s * 1e6:.3g} us"


def fmt_hz(f) -> str:
    if f is None or not math.isfinite(f):
        return "--"
    for scale, unit in ((1e6, "MHz"), (1e3, "kHz"), (1.0, "Hz")):
        if abs(f) >= scale or scale == 1.0:
            return f"{f / scale:,.6g} {unit}"


_TC_UNITS = {"us": 1e-6, "ms": 1e-3, "s": 1.0}
_FREQ_UNITS = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6}


# ------------------------------------------------------------- the phasor dial

class PhasorIndicator(QtWidgets.QWidget):
    """A polar dial: one arrow per channel at angle theta, plus a noise trail.

    Arrow length is R relative to that channel's own recent full scale, so a
    0.5 mV and a 2 mV signal are both readable; the ring labels say what full
    scale is. The drawn arrow EASES towards the measurement on the widget's own
    ~33 ms timer, so the motion is smooth regardless of the status poll rate.
    """

    TRAIL = 60

    def __init__(self, channels=(0, 1)):
        super().__init__()
        self.channels = tuple(channels)      # which channels this dial draws
        self.setMinimumSize(250, 250)
        self._target = [(0.0, 0.0), (0.0, 0.0)]      # (x, y) per channel, volts
        self._shown = [(0.0, 0.0), (0.0, 0.0)]
        self._scale = [1e-9, 1e-9]
        self._trail = [deque(maxlen=self.TRAIL), deque(maxlen=self.TRAIL)]
        self._acquiring = False
        self._sweep = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, x, y, acquiring: bool):
        self._acquiring = bool(acquiring)
        for i in range(2):
            xi, yi = x[i], y[i]
            if xi is None or yi is None or not (math.isfinite(xi) and math.isfinite(yi)):
                continue
            self._target[i] = (xi, yi)
            self._trail[i].append((xi, yi))
            r_max = max(math.hypot(a, b) for a, b in self._trail[i])
            # full scale = a "nice" 1-2-5 number just above the recent maximum
            self._scale[i] = _nice_ceiling(max(r_max * 1.15, 1e-9))

    def _tick(self):
        for i in range(2):
            (tx, ty), (sx, sy) = self._target[i], self._shown[i]
            self._shown[i] = (sx + 0.35 * (tx - sx), sy + 0.35 * (ty - sy))
        if self._acquiring:
            self._sweep = (self._sweep + 6.0) % 360.0
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        rad = min(w, h) / 2.0 - 26
        cx, cy = w / 2.0, h / 2.0 + 4

        grid = QtGui.QColor(COLORS["border"])
        muted = QtGui.QColor(COLORS["muted"])

        # rings + axes
        p.setBrush(QtCore.Qt.NoBrush)
        for k in (1.0, 0.5):
            p.setPen(QtGui.QPen(grid, 1.2 if k == 1.0 else 1.0))
            p.drawEllipse(QtCore.QPointF(cx, cy), rad * k, rad * k)
        p.setPen(QtGui.QPen(grid, 1.0))
        p.drawLine(QtCore.QPointF(cx - rad, cy), QtCore.QPointF(cx + rad, cy))
        p.drawLine(QtCore.QPointF(cx, cy - rad), QtCore.QPointF(cx, cy + rad))

        # acquisition sweep: a soft rotating wedge while a sample is being taken
        if self._acquiring:
            acc = QtGui.QColor(COLORS["accent"])
            grad = QtGui.QConicalGradient(QtCore.QPointF(cx, cy), -self._sweep)
            acc.setAlpha(70); grad.setColorAt(0.0, acc)
            acc.setAlpha(0); grad.setColorAt(0.15, acc)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QBrush(grad))
            p.drawEllipse(QtCore.QPointF(cx, cy), rad, rad)

        f = p.font(); f.setPointSize(7); f.setBold(True); p.setFont(f)
        p.setPen(muted)
        p.drawText(QtCore.QRectF(cx + 4, cy - rad - 16, 60, 14), "+Y")
        p.drawText(QtCore.QRectF(cx + rad - 18, cy + 2, 30, 14), "+X")

        colors = [QtGui.QColor(COLORS["accent"]), QtGui.QColor(COLORS["ch2"])]
        for row, i in enumerate(self.channels):
            col = colors[i]
            s = self._scale[i]
            # trail = the noise cloud
            n = len(self._trail[i])
            for k, (tx, ty) in enumerate(self._trail[i]):
                c = QtGui.QColor(col); c.setAlpha(int(20 + 120 * (k + 1) / max(1, n)))
                p.setPen(QtCore.Qt.NoPen); p.setBrush(c)
                px, py = cx + rad * tx / s, cy - rad * ty / s
                p.drawEllipse(QtCore.QPointF(px, py), 1.8, 1.8)
            # arrow
            sx, sy = self._shown[i]
            ex, ey = cx + rad * sx / s, cy - rad * sy / s
            pen = QtGui.QPen(col, 2.6); pen.setCapStyle(QtCore.Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(cx, cy), QtCore.QPointF(ex, ey))
            ang = math.atan2(cy - ey, ex - cx)
            head = QtGui.QPolygonF([
                QtCore.QPointF(ex, ey),
                QtCore.QPointF(ex - 10 * math.cos(ang - 0.4), ey + 10 * math.sin(ang - 0.4)),
                QtCore.QPointF(ex - 10 * math.cos(ang + 0.4), ey + 10 * math.sin(ang + 0.4)),
            ])
            p.setPen(QtCore.Qt.NoPen); p.setBrush(col)
            p.drawPolygon(head)
            # full-scale legend, one line per channel
            val, unit = si_volts(s)
            p.setPen(col)
            p.drawText(QtCore.QRectF(4, h - 17 - 13 * (len(self.channels) - 1 - row), w - 8, 13),
                       QtCore.Qt.AlignLeft, f"ch{i + 1} ring = {val.rstrip('0').rstrip('.')} {unit}")
        p.setBrush(QtGui.QColor(COLORS["text"])); p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(QtCore.QPointF(cx, cy), 2.5, 2.5)
        p.end()


def _nice_ceiling(v: float) -> float:
    e = math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if m * 10 ** e >= v:
            return m * 10 ** e
    return 10 ** (e + 1)


# ------------------------------------------------------------- one channel's settings

class ChannelControls(QtWidgets.QFrame):
    """The sidebar card for one channel."""

    def __init__(self, win: "MainWindow", index: int):
        super().__init__()
        self.win = win
        self.i = index
        self.n = index + 1
        ch = win.cfg.channel(index)
        self.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12); lay.setSpacing(8)

        colour = COLORS["accent"] if index == 0 else COLORS["ch2"]
        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(f"CHANNEL {self.n}")
        title.setStyleSheet(f"color:{colour}; font-size:12px; font-weight:800; letter-spacing:1px;")
        self.route = QtWidgets.QLabel()
        self.route.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        head.addWidget(title); head.addStretch(1); head.addWidget(self.route)
        lay.addLayout(head)

        # reference: two checkable buttons behave like a segmented switch
        ref_row = QtWidgets.QHBoxLayout(); ref_row.setSpacing(6)
        ref_row.addWidget(_cap("Ref"))
        self.btn_int = QtWidgets.QPushButton("Internal")
        self.btn_ext = QtWidgets.QPushButton("External")
        for b, mode in ((self.btn_int, "internal"), (self.btn_ext, "external")):
            b.setCheckable(True)
            # .clicked fires only for the USER, never for setChecked (gotcha #13)
            b.clicked.connect(lambda _=False, m=mode: self.win.call(
                self.win.ctrl.set_reference, self.n, m))
            ref_row.addWidget(b, 1)
        self.lock_badge = QtWidgets.QLabel("")
        self.lock_badge.setMinimumWidth(70)
        self.lock_badge.setAlignment(QtCore.Qt.AlignCenter)
        ref_row.addWidget(self.lock_badge)
        lay.addLayout(ref_row)

        # frequency
        f_row = QtWidgets.QHBoxLayout(); f_row.setSpacing(6)
        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setDecimals(4); self.freq_spin.setRange(0.0, 1e9)
        self.freq_unit = QtWidgets.QComboBox(); self.freq_unit.addItems(list(_FREQ_UNITS))
        self.freq_unit.setCurrentText("kHz" if ch.frequency_Hz >= 1e3 else "Hz")
        self.freq_spin.setValue(ch.frequency_Hz / _FREQ_UNITS[self.freq_unit.currentText()])
        self.freq_unit.currentTextChanged.connect(self._freq_unit_changed)
        self._freq_scale = _FREQ_UNITS[self.freq_unit.currentText()]
        self.freq_set = QtWidgets.QPushButton("Set"); self.freq_set.setObjectName("primary")
        self.freq_set.clicked.connect(lambda: self.win.call(
            self.win.ctrl.set_frequency, self.n, self.freq_spin.value() * self._freq_scale))
        f_row.addWidget(_cap("Freq")); f_row.addWidget(self.freq_spin, 1)
        f_row.addWidget(self.freq_unit); f_row.addWidget(self.freq_set)
        lay.addLayout(f_row)

        # time constant
        t_row = QtWidgets.QHBoxLayout(); t_row.setSpacing(6)
        self.tc_spin = QtWidgets.QDoubleSpinBox()
        self.tc_spin.setDecimals(3); self.tc_spin.setRange(0.0, 1e6)
        self.tc_unit = QtWidgets.QComboBox(); self.tc_unit.addItems(list(_TC_UNITS))
        tc = ch.time_constant_s
        self.tc_unit.setCurrentText("s" if tc >= 1 else ("ms" if tc >= 1e-3 else "us"))
        self.tc_spin.setValue(tc / _TC_UNITS[self.tc_unit.currentText()])
        self._tc_scale = _TC_UNITS[self.tc_unit.currentText()]
        self.tc_unit.currentTextChanged.connect(self._tc_unit_changed)
        tc_set = QtWidgets.QPushButton("Set"); tc_set.setObjectName("primary")
        tc_set.clicked.connect(lambda: self.win.call(
            self.win.ctrl.set_time_constant, self.n, self.tc_spin.value() * self._tc_scale))
        t_row.addWidget(_cap("TC")); t_row.addWidget(self.tc_spin, 1)
        t_row.addWidget(self.tc_unit); t_row.addWidget(tc_set)
        lay.addLayout(t_row)

        # order
        o_row = QtWidgets.QHBoxLayout(); o_row.setSpacing(6)
        self.order_spin = QtWidgets.QSpinBox()
        self.order_spin.setRange(win.cfg.limits.order_min, win.cfg.limits.order_max)
        self.order_spin.setValue(ch.order)
        self.order_spin.setSuffix(" order")
        o_set = QtWidgets.QPushButton("Set"); o_set.setObjectName("primary")
        o_set.clicked.connect(lambda: self.win.call(
            self.win.ctrl.set_order, self.n, self.order_spin.value()))
        o_row.addWidget(_cap("Filter")); o_row.addWidget(self.order_spin, 1); o_row.addWidget(o_set)
        lay.addLayout(o_row)

        # what the hardware applied, and what it means
        self.applied = QtWidgets.QLabel("--")
        self.applied.setObjectName("hint"); self.applied.setWordWrap(True)
        lay.addWidget(self.applied)
        self._last_mode = None
        # the setpoints last copied INTO the boxes, to spot changes made elsewhere
        self._synced = {"tc": ch.time_constant_s, "freq": ch.frequency_Hz, "order": ch.order}

    def _freq_unit_changed(self, unit):
        hz = self.freq_spin.value() * self._freq_scale
        self._freq_scale = _FREQ_UNITS[unit]
        self.freq_spin.setValue(hz / self._freq_scale)

    def _tc_unit_changed(self, unit):
        s = self.tc_spin.value() * self._tc_scale
        self._tc_scale = _TC_UNITS[unit]
        self.tc_spin.setValue(s / self._tc_scale)

    def _sync_boxes(self, s, external: bool):
        """Follow setpoints changed ELSEWHERE (console, scan, another GUI).

        Only when the value really moved since we last copied it in, and never
        into a box that has keyboard focus -- overwriting what someone is
        typing every 60 ms would make the panel unusable. With an external
        reference the (disabled) frequency box shows the MEASURED frequency.
        """
        i = self.i
        tc = s.tc_set_s[i]
        if tc is not None and tc != self._synced["tc"] and not self.tc_spin.hasFocus():
            self._synced["tc"] = tc
            self.tc_spin.setValue(tc / self._tc_scale)
        order = s.order[i]
        if order != self._synced["order"] and not self.order_spin.hasFocus():
            self._synced["order"] = order
            self.order_spin.setValue(int(order))
        f = s.ref_freq_Hz[i] if external else s.freq_set_Hz[i]
        if (f is not None and math.isfinite(f) and f != self._synced["freq"]
                and not self.freq_spin.hasFocus()):
            self._synced["freq"] = f
            self.freq_spin.setValue(f / self._freq_scale)

    def refresh(self, s):
        i = self.i
        ch = self.win.cfg.channel(i)
        self.route.setText(f"in {ch.signal_input + 1} - demod {ch.demod + 1} - osc {ch.oscillator + 1}")
        mode = s.reference[i]
        if mode != self._last_mode:
            self._last_mode = mode
            self.btn_int.setChecked(mode == "internal")
            self.btn_ext.setChecked(mode == "external")
            for b in (self.btn_int, self.btn_ext):
                b.setObjectName("primary" if b.isChecked() else "")
                b.style().unpolish(b); b.style().polish(b)
            external = mode == "external"
            self.freq_spin.setEnabled(not external)
            self.freq_unit.setEnabled(not external)
            self.freq_set.setEnabled(not external)
        self._sync_boxes(s, mode == "external")
        locked = s.pll_locked[i]
        if mode == "external":
            ok = bool(locked)
            col = COLORS["ok"] if ok else COLORS["danger"]
            self.lock_badge.setText("LOCKED" if ok else "UNLOCKED")
            self.lock_badge.setStyleSheet(f"color:{col}; font-weight:800; font-size:11px;")
        else:
            self.lock_badge.setText("")
        tc, order = s.tc_s[i], s.order[i]
        bw = filters.enbw_Hz(tc, order) if tc and math.isfinite(tc) and tc > 0 else float("nan")
        pct = self.win.cfg.acquisition.settle_percent
        self.applied.setText(
            f"applied {fmt_seconds(tc)}, order {order}  -  settles {pct:g} % in "
            f"{fmt_seconds(s.settle_s[i])}  -  ENBW {bw:.3g} Hz  -  f = {fmt_hz(s.ref_freq_Hz[i])}")


# ------------------------------------------------------------- live history + plots

_WINDOWS = {"10 s": 10.0, "30 s": 30.0, "1 min": 60.0, "5 min": 300.0}


class History:
    """A rolling record of every live value, shared by all plots.

    Filled once per refresh (~60 ms), so 6000 points hold a bit over 5 minutes.
    One store instead of one per plot: a tab opened later already has its past,
    and switching the plotted quantity does not lose anything.
    """

    MAX = 6000
    KEYS = ("x1", "y1", "r1", "theta1", "x2", "y2", "r2", "theta2", "aux1", "aux2")

    def __init__(self):
        self.t = deque(maxlen=self.MAX)
        self.v = {k: deque(maxlen=self.MAX) for k in self.KEYS}

    def add(self, t: float, live: dict) -> None:
        def val(key, i):
            seq = live.get(key) or [None, None]
            x = seq[i] if i < len(seq) else None
            return float(x) if x is not None and math.isfinite(x) else float("nan")
        self.t.append(t)
        for i in range(2):
            n = i + 1
            self.v[f"x{n}"].append(val("x", i))
            self.v[f"y{n}"].append(val("y", i))
            self.v[f"r{n}"].append(val("r", i))
            self.v[f"theta{n}"].append(val("theta_deg", i))
            self.v[f"aux{n}"].append(val("aux_in", i))

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
    w.setMinimumHeight(150)
    ax_pen = pg.mkPen(COLORS["muted"])
    for name in ("left", "bottom"):
        ax = w.getAxis(name)
        ax.setPen(ax_pen)
        ax.setTextPen(ax_pen)
    w.setLabel("left", y_label, units=units)
    w.setLabel("bottom", "time", units="s")
    w.showGrid(x=True, y=True, alpha=0.15)
    return w


class PlotControls(QtWidgets.QHBoxLayout):
    """Window length + Pause, shared shape for every live plot card."""

    def __init__(self):
        super().__init__()
        self.setSpacing(8)
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

class ChannelTab(QtWidgets.QWidget):
    """Everything about ONE channel: settings, readouts, phasor, live plot."""

    QUANTITIES = {"R": ("r",), "X and Y": ("x", "y"), "Theta": ("theta",)}

    def __init__(self, win: "MainWindow", index: int):
        super().__init__()
        self.win, self.i, self.n = win, index, index + 1
        self.colour = COLORS["accent"] if index == 0 else COLORS["ch2"]
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 10, 0, 0); grid.setSpacing(12)

        self.controls = ChannelControls(win, index)
        self.controls.setFixedWidth(410)
        grid.addWidget(self.controls, 0, 0)

        # live readouts
        card, lay = _card(f"Channel {self.n}  -  live")
        card.findChild(QtWidgets.QLabel, "cardTitle").setStyleSheet(
            f"color:{self.colour}; font-size:11px; font-weight:700; letter-spacing:1px;")
        big = QtWidgets.QHBoxLayout(); big.setSpacing(24)
        r_box = QtWidgets.QVBoxLayout(); r_box.setSpacing(0)
        r_box.addWidget(_cap("R"))
        r_line = QtWidgets.QHBoxLayout()
        self.r_val = QtWidgets.QLabel("--"); self.r_val.setObjectName("bigValue")
        self.r_val.setMinimumWidth(160)
        self.r_unit = QtWidgets.QLabel("V"); self.r_unit.setObjectName("unit")
        r_line.addWidget(self.r_val); r_line.addWidget(self.r_unit, 0, QtCore.Qt.AlignBottom)
        r_box.addLayout(r_line)
        big.addLayout(r_box)
        th_box = QtWidgets.QVBoxLayout(); th_box.setSpacing(0)
        th_box.addWidget(_cap("Theta"))
        self.th_val = QtWidgets.QLabel("--"); self.th_val.setObjectName("midValue")
        th_box.addWidget(self.th_val)
        big.addLayout(th_box)
        big.addStretch(1)
        lay.addLayout(big)
        self.xy = QtWidgets.QLabel("--"); self.xy.setObjectName("mono")
        lay.addWidget(self.xy)
        self.freq = QtWidgets.QLabel("--"); self.freq.setObjectName("mono")
        lay.addWidget(self.freq)
        self.sample = QtWidgets.QLabel("no settled sample yet"); self.sample.setObjectName("hint")
        self.sample.setWordWrap(True)
        lay.addWidget(self.sample)
        lay.addStretch(1)
        grid.addWidget(card, 0, 1)

        pcard, play = _card("Phasor")
        self.phasor = PhasorIndicator(channels=(index,))
        self.phasor.setMinimumSize(260, 260)
        play.addWidget(self.phasor, 1)
        grid.addWidget(pcard, 0, 2)

        # live plot
        plot_card, plot_lay = _card()
        head = QtWidgets.QHBoxLayout(); head.setSpacing(8)
        title = QtWidgets.QLabel(f"CHANNEL {self.n}  -  LIVE PLOT"); title.setObjectName("cardTitle")
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
        self.curves = {
            "r": self.plot.plot([], [], pen=pg.mkPen(self.colour, width=2), name="R"),
            "x": self.plot.plot([], [], pen=pg.mkPen(self.colour, width=2), name="X"),
            "y": self.plot.plot([], [], pen=pg.mkPen(COLORS["muted"], width=2), name="Y"),
            "theta": self.plot.plot([], [], pen=pg.mkPen(self.colour, width=2), name="theta"),
        }
        plot_lay.addWidget(self.plot, 1)
        grid.addWidget(plot_card, 1, 0, 1, 3)

        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)
        self._quantity_changed(self.quantity.currentText())

    def _quantity_changed(self, name: str):
        shown = self.QUANTITIES[name]
        # the legend lists every curve ever added, hidden or not -- rebuild it
        # with just the ones on screen
        self.legend.clear()
        for key, curve in self.curves.items():
            curve.setVisible(key in shown)
            if key in shown:
                self.legend.addItem(curve, curve.name())
        if name == "Theta":
            self.plot.setLabel("left", "theta", units="deg")
        else:
            self.plot.setLabel("left", name, units="V")
        self.redraw()

    def refresh(self, s):
        i = self.i
        self.controls.refresh(s)
        live = s.live
        v, u = si_volts(live["r"][i])
        self.r_val.setText(v); self.r_unit.setText(u)
        th = live["theta_deg"][i]
        self.th_val.setText("--" if th is None or not math.isfinite(th) else f"{th:+.2f} deg")
        xv, xu = si_volts(live["x"][i]); yv, yu = si_volts(live["y"][i])
        self.xy.setText(f"X {xv} {xu}    Y {yv} {yu}")
        self.freq.setText(f"f demod {fmt_hz(live['freq_Hz'][i])}")
        smp = s.sample
        if smp.get("acq_id"):
            rv, ru = si_volts(smp["r"][i])
            self.sample.setText(f"last settled sample #{smp['acq_id']}: R {rv} {ru}, "
                                f"theta {smp['theta_deg'][i]:+.2f} deg")
        self.phasor.set_state(live["x"], live["y"], s.acquiring)

    def redraw(self):
        if self.pc.pause.isChecked():
            return
        n = self.n
        keys = [f"{q}{n}" for q in ("r", "x", "y", "theta")]
        t, vals = self.win.history.window(keys, self.pc.seconds)
        for q in ("r", "x", "y", "theta"):
            if self.curves[q].isVisible():
                self.curves[q].setData(t, vals[f"{q}{n}"], connect="finite")


class AuxTab(QtWidgets.QWidget):
    """AUX IN 1 and 2: big live readouts and a live plot each."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        import pyqtgraph as pg
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 10, 0, 0); v.setSpacing(12)

        row = QtWidgets.QHBoxLayout(); row.setSpacing(12)
        self.vals, self.stats, self.latched = [], [], []
        for k in range(2):
            card, lay = _card(f"AUX IN {k + 1}")
            val = QtWidgets.QLabel("--"); val.setObjectName("bigValue")
            lay.addWidget(val)
            stat = QtWidgets.QLabel("--"); stat.setObjectName("mono")
            lay.addWidget(stat)
            lat = QtWidgets.QLabel("no settled sample yet"); lat.setObjectName("hint")
            lay.addWidget(lat)
            row.addWidget(card, 1)
            self.vals.append(val); self.stats.append(stat); self.latched.append(lat)
        v.addLayout(row)

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
        # Two plots, not two curves on one: the inputs usually carry different
        # signals at different levels, and a shared axis flattens the smaller one.
        self.plots, self.curves = [], []
        colours = (COLORS["accent"], COLORS["ch2"])
        for k in range(2):
            p = _make_plot(f"AUX {k + 1}", "V")
            if k == 1:
                p.setXLink(self.plots[0])
            self.curves.append(p.plot([], [], pen=pg.mkPen(colours[k], width=2)))
            self.plots.append(p)
            lay.addWidget(p, 1)
        v.addWidget(card, 1)

    def refresh(self, s):
        aux = s.live["aux_in"]
        smp = s.sample
        t, vals = self.win.history.window(("aux1", "aux2"), self.pc.seconds)
        for k in range(2):
            a = aux[k]
            self.vals[k].setText("--" if a is None or not math.isfinite(a) else f"{a:+.4f} V")
            arr = vals[f"aux{k + 1}"]
            import numpy as np
            finite = arr[np.isfinite(arr)] if arr.size else arr
            if finite.size:
                self.stats[k].setText(f"window: min {finite.min():+.4f}  max {finite.max():+.4f}"
                                      f"  mean {finite.mean():+.4f} V")
            if smp.get("acq_id"):
                self.latched[k].setText(f"settled sample #{smp['acq_id']}: "
                                        f"{smp['aux_in'][k]:+.4f} V")

    def redraw(self):
        if self.pc.pause.isChecked():
            return
        t, vals = self.win.history.window(("aux1", "aux2"), self.pc.seconds)
        for k in range(2):
            self.curves[k].setData(t, vals[f"aux{k + 1}"], connect="finite")


class InstrumentTab(QtWidgets.QWidget):
    """Status, signal routing, every setting, and the full log."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 10, 0, 0); grid.setSpacing(12)

        card, lay = _card("Instrument")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        self.f_conn = QtWidgets.QLabel("--")
        self.f_idn = QtWidgets.QLabel("--")
        self.f_server = QtWidgets.QLabel("--")
        self.f_error = QtWidgets.QLabel("none"); self.f_error.setWordWrap(True)
        for label, w in (("connection", self.f_conn), ("instrument", self.f_idn),
                         ("data server", self.f_server), ("hardware error", self.f_error)):
            form.addRow(_cap(label), w)
        lay.addLayout(form)

        lay.addWidget(_cap("Signal routing"))
        self.routing = QtWidgets.QTableWidget(2, 7)
        self.routing.setHorizontalHeaderLabels(
            ["demod", "input", "oscillator", "reference", "range", "coupling", "impedance"])
        self.routing.setVerticalHeaderLabels(["ch1", "ch2"])
        self.routing.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.routing.setFixedHeight(92)
        self.routing.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.routing.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.routing.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        lay.addWidget(self.routing)
        note = QtWidgets.QLabel("Indices are shown 1-based as on the front panel; the settings "
                                "use LabOne's 0-based node numbers.")
        note.setObjectName("hint"); note.setWordWrap(True)
        lay.addWidget(note)
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
        grid.setRowStretch(0, 1); grid.setRowStretch(1, 1)

    def refresh(self, s):
        cfg = self.win.cfg
        self.f_conn.setText("connected" if s.connected else "offline")
        self.f_conn.setStyleSheet(f"color:{COLORS['ok'] if s.connected else COLORS['danger']};"
                                  f" font-weight:700;")
        self.f_idn.setText(s.idn or "--")
        hw = cfg.hardware
        self.f_server.setText("remote service" if self.win._remote else
                              f"{hw.device_id} via {hw.server_host}:{hw.server_port} "
                              f"(API level {hw.api_level})")
        self.f_error.setText(s.hw_error or "none")
        self.f_error.setStyleSheet(f"color:{COLORS['danger'] if s.hw_error else COLORS['muted']};")
        for row in range(2):
            ch = cfg.channel(row)
            cells = [ch.demod + 1, ch.signal_input + 1, ch.oscillator + 1,
                     ch.reference + (f" (in {ch.ref_input + 1})" if ch.reference == "external" else ""),
                     f"{ch.input_range_V:g} V", "AC" if ch.input_ac else "DC",
                     "50 ohm" if ch.input_50ohm else "1 Mohm"]
            for col, text in enumerate(cells):
                item = self.routing.item(row, col)
                if item is None:
                    item = QtWidgets.QTableWidgetItem()
                    self.routing.setItem(row, col, item)
                if item.text() != str(text):
                    item.setText(str(text))


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    TAB_INSTRUMENT = 3

    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self.history = History()
        self.setWindowTitle("HF2LI - Lock-in Amplifier" + ("  (remote)" if remote else ""))
        self.resize(1400, 900)

        root = QtWidgets.QWidget(); root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(16, 12, 16, 16); outer.setSpacing(10)
        outer.addWidget(self._build_strip())

        self.tabs = QtWidgets.QTabWidget()
        self.ch_tabs = [ChannelTab(self, 0), ChannelTab(self, 1)]
        self.aux_tab = AuxTab(self)
        self.inst_tab = InstrumentTab(self)
        self.tabs.addTab(self.ch_tabs[0], "Channel 1")
        self.tabs.addTab(self.ch_tabs[1], "Channel 2")
        self.tabs.addTab(self.aux_tab, "Aux in")
        self.tabs.addTab(self.inst_tab, "Instrument")
        self.tabs.currentChanged.connect(self._tab_changed)
        outer.addWidget(self.tabs, 1)

        # the widgets older code and tests reach for directly
        self.channels = [t.controls for t in self.ch_tabs]
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
        title = QtWidgets.QLabel("HF2LI")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        h.addWidget(title)
        self.conn_dot = QtWidgets.QLabel("connecting")
        h.addWidget(self.conn_dot)
        self.idn_label = QtWidgets.QLabel("--")
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        h.addWidget(self.idn_label)
        h.addSpacing(10)
        self.acq_btn = QtWidgets.QPushButton("Acquire"); self.acq_btn.setObjectName("primary")
        self.acq_btn.setToolTip("Wait the settling time of both channels, then latch one sample")
        self.acq_btn.clicked.connect(lambda: self.call(self.ctrl.acquire))
        h.addWidget(self.acq_btn)
        self.acq_bar = QtWidgets.QProgressBar(); self.acq_bar.setRange(0, 1000)
        self.acq_bar.setTextVisible(False); self.acq_bar.setFixedSize(120, 10)
        h.addWidget(self.acq_bar)
        self.sample_label = QtWidgets.QLabel("no sample yet"); self.sample_label.setObjectName("mono")
        h.addWidget(self.sample_label, 1)
        self.last_msg = QtWidgets.QLabel("")
        self.last_msg.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        self.last_msg.setMaximumWidth(460)
        h.addWidget(self.last_msg)
        return card

    # ---- actions ------------------------------------------------------------------

    def call(self, fn, *args):
        """Run a command and surface a refusal.

        A local LockIn raises ValueError; a remote client returns
        {"ok": false, "error": ...}. The window treats both the same way.
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
        for c in self.channels:
            c.order_spin.setRange(self.cfg.limits.order_min, self.cfg.limits.order_max)
            c._last_mode = None          # force the reference buttons to re-sync

    # ---- refresh & events -----------------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = COLORS["danger"] if level == "error" else (
            COLORS["accent"] if level == "warn" else COLORS["muted"])
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(
            f'<span style="color:{COLORS["accent_dim"]}">{stamp}</span> '
            f'<span style="color:{color}">{msg}</span>')
        # the latest message is visible from every tab, errors in red
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
        self.acq_btn.setEnabled(s.connected and not s.acquiring)
        smp = s.sample
        if smp.get("acq_id"):
            r1, u1 = si_volts(smp["r"][0]); r2, u2 = si_volts(smp["r"][1])
            self.sample_label.setText(
                f"#{smp['acq_id']}  settle {fmt_seconds(smp.get('settle_s'))}, "
                f"{smp.get('n_avg', 1)} pts  |  R1 {r1} {u1}  R2 {r2} {u2}")

        self.history.add(time.monotonic() - self._t0, s.live)
        for tab in self.ch_tabs:
            tab.refresh(s)          # settings cards must stay in sync even when hidden
        self.aux_tab.refresh(s)
        self.inst_tab.refresh(s)
        self._redraw_visible()

    def _redraw_visible(self):
        """Only the visible tab's plots are redrawn -- the rest record silently."""
        current = self.tabs.currentWidget()
        if current in self.ch_tabs or current is self.aux_tab:
            current.redraw()

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app. The theme is chosen ONCE here, before any widget."""
    set_theme(getattr(cfg.ui, "theme", "dark"))
    # Numbers with a '.' decimal point and no thousands separator, whatever the
    # Windows locale. Under a comma-decimal locale a 10 ms time constant shows
    # as "10,000" -- which any English reader takes for ten thousand.
    loc = QtCore.QLocale.c()
    loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
    QtCore.QLocale.setDefault(loc)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # The module's own icon in the title bar, Alt-Tab and the taskbar.
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
