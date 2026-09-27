"""Control GUI for the Signal Recovery 7230 lock-in.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

The window holds a LockIn-like object: a real in-process LockIn, or an
Sr7230Client facade for a remote service. It sends settings on user actions
and reads a status snapshot on a 60 ms timer.

Layout: a strip at the top that every tab shares -- connection, Acquire and
the last settled sample, the latest message -- and three tabs:

    Lock-in     every front-panel setting, live readouts, the meter, a live plot
    ADC in      the rear ADC1 / ADC2 inputs: readouts and live plots
    Instrument  status, every setting (generated from config), the log

All live plots draw from ONE rolling History that the refresh timer fills, so
a tab opened later already shows the last minutes.

The signature widget is the SensitivityMeter: an analog panel meter, as on the
lock-ins this one replaced, whose needle shows R as a fraction of the FULL-SCALE
SENSITIVITY. The band where auto-sensitivity aims (30-90 %) is marked, beyond
full scale the arc turns red, and an OVL lamp lights on any overload. Choosing
the sensitivity is the thing a lock-in user most often gets wrong; the meter
makes it visible at a glance. A small dial in its corner shows the phase.
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets

from .. import filters, tables
from ..config import Config, REF_SOURCES, INPUT_MODES
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
    lbl.setMinimumWidth(62)
    return lbl


def si_value(v, unit: str = "V") -> tuple[str, str]:
    """0.00123, 'V' -> ('1.230', 'mV'). NaN/None -> ('--', unit)."""
    if v is None or not math.isfinite(v):
        return "--", unit
    a = abs(v)
    for scale, p in ((1.0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p"),
                     (1e-15, "f")):
        if a >= scale or scale == 1e-15:
            return f"{v / scale:.3f}", p + unit
    return f"{v:.3f}", unit


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
    for scale, unit in ((1e3, "kHz"), (1.0, "Hz")):
        if abs(f) >= scale or scale == 1.0:
            return f"{f / scale:,.6g} {unit}"


_FREQ_UNITS = {"Hz": 1.0, "kHz": 1e3}


# ------------------------------------------------------------- the meter

class SensitivityMeter(QtWidgets.QWidget):
    """An analog panel meter: needle = R / full scale.

    The scale runs 0 .. 120 % of full scale. 30-90 % (auto-sensitivity's
    target) is a soft band; above 100 % the arc is red, and beyond 120 % the
    needle pegs against the stop. The needle moves with simple meter
    BALLISTICS (a damped spring, on the widget's own ~33 ms timer), so it
    swings like the real thing rather than jumping with every status frame.
    While an acquisition runs the arc glows; OVL and REF lamps sit under it,
    and a small phase dial in the corner points at theta.
    """

    SPAN = 1.2              # the arc covers 0 .. 120 % of full scale
    A0, A1 = 215.0, -35.0   # arc start / end angle in degrees (Qt: 0 = 3 o'clock, CCW +)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(300, 260)
        self.target = 0.0           # R / FS, where the needle is heading
        self.shown = 0.0            # where it is drawn
        self._vel = 0.0
        self.fs_label = "--"
        self.r_text = "--"
        self.theta = math.nan
        self.overload = False
        self.locked: object = None  # None = internal reference (lamp grey)
        self.acquiring = False
        self._glow = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, r_fs, fs_label: str, r_text: str, theta_deg, overload: bool,
                  locked, acquiring: bool):
        if r_fs is not None and math.isfinite(r_fs):
            self.target = max(0.0, float(r_fs))
        self.fs_label, self.r_text = fs_label, r_text
        self.theta = theta_deg if theta_deg is not None else math.nan
        self.overload, self.locked, self.acquiring = bool(overload), locked, bool(acquiring)

    def _tick(self):
        # damped spring towards the target, clipped at the end stop
        goal = min(self.target, self.SPAN * 1.03)
        self._vel = 0.55 * self._vel + 0.22 * (goal - self.shown)
        self.shown = max(0.0, min(self.SPAN * 1.03, self.shown + self._vel))
        self._glow = (self._glow + 0.12) % (2 * math.pi) if self.acquiring else 0.0
        self.update()

    def _angle(self, frac: float) -> float:
        frac = max(0.0, min(frac, self.SPAN * 1.03)) / self.SPAN
        return self.A0 + (self.A1 - self.A0) * frac

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        # the arc sits in the upper part; the strip below it (lamps, phase
        # dial) is kept clear of the arc's two ends
        rad = max(40.0, min(w * 0.40, (h - 44) * 0.62))
        cx, cy = w / 2.0, 8 + rad * 1.05
        rect = QtCore.QRectF(cx - rad, cy - rad, 2 * rad, 2 * rad)

        def arc(frac0, frac1, color, width, r=rad):
            a0, a1 = self._angle(frac0), self._angle(frac1)
            pen = QtGui.QPen(QtGui.QColor(color), width)
            pen.setCapStyle(QtCore.Qt.FlatCap)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.NoBrush)
            rr = QtCore.QRectF(cx - r, cy - r, 2 * r, 2 * r)
            p.drawArc(rr, int(a0 * 16), int((a1 - a0) * 16))

        # the scale: base arc, auto-sensitivity band, overload zone
        arc(0.0, self.SPAN, COLORS["border"], 3)
        band = QtGui.QColor(COLORS["ok"]); band.setAlpha(90)
        arc(0.3, 0.9, band, 7, rad - 6)
        arc(1.0, self.SPAN, COLORS["danger"], 5)
        if self.acquiring:
            glow = QtGui.QColor(COLORS["accent"])
            glow.setAlpha(int(60 + 60 * math.sin(self._glow)))
            arc(0.0, self.SPAN, glow, 10, rad + 8)

        # ticks every 10 %, labels every 20 %
        f = p.font(); f.setPointSize(7); f.setBold(True); p.setFont(f)
        for k in range(13):
            frac = k / 10.0
            a = math.radians(self._angle(frac))
            major = k % 2 == 0
            r0, r1 = rad - (13 if major else 8), rad
            p.setPen(QtGui.QPen(QtGui.QColor(COLORS["muted"] if frac <= 1.0 else COLORS["danger"]),
                                1.6 if major else 1.0))
            p.drawLine(QtCore.QPointF(cx + r0 * math.cos(a), cy - r0 * math.sin(a)),
                       QtCore.QPointF(cx + r1 * math.cos(a), cy - r1 * math.sin(a)))
            if major:
                rl = rad - 26
                p.drawText(QtCore.QRectF(cx + rl * math.cos(a) - 16, cy - rl * math.sin(a) - 7,
                                         32, 14), QtCore.Qt.AlignCenter, f"{k * 10}")

        # needle
        a = math.radians(self._angle(self.shown))
        tip = QtCore.QPointF(cx + (rad - 4) * math.cos(a), cy - (rad - 4) * math.sin(a))
        col = QtGui.QColor(COLORS["danger"] if self.shown > 1.0 else COLORS["accent"])
        pen = QtGui.QPen(col, 2.6); pen.setCapStyle(QtCore.Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(QtCore.QPointF(cx, cy), tip)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(COLORS["text"]))
        p.drawEllipse(QtCore.QPointF(cx, cy), 6, 6)
        p.setBrush(col)
        p.drawEllipse(QtCore.QPointF(cx, cy), 3, 3)

        # readout under the pivot
        f.setPointSize(13); p.setFont(f)
        p.setPen(QtGui.QColor(COLORS["text"]))
        p.drawText(QtCore.QRectF(0, cy + 12, w, 22), QtCore.Qt.AlignCenter, self.r_text)
        f.setPointSize(8); p.setFont(f)
        p.setPen(QtGui.QColor(COLORS["muted"]))
        p.drawText(QtCore.QRectF(0, cy + 36, w, 16), QtCore.Qt.AlignCenter,
                   f"SCALE IN % OF {self.fs_label} FULL SCALE")

        # lamps: OVL and REF
        def lamp(x, text, on_color):
            c = QtGui.QColor(on_color) if on_color else QtGui.QColor(COLORS["panel_hi"])
            p.setPen(QtGui.QPen(QtGui.QColor(COLORS["border"]), 1))
            p.setBrush(c)
            p.drawEllipse(QtCore.QPointF(x, h - 16), 6, 6)
            p.setPen(QtGui.QColor(COLORS["text"] if on_color else COLORS["muted"]))
            p.drawText(QtCore.QRectF(x + 10, h - 23, 50, 14), QtCore.Qt.AlignLeft, text)
        lamp(14, "OVL", COLORS["danger"] if self.overload else None)
        ref_col = None if self.locked is None else (COLORS["ok"] if self.locked else COLORS["danger"])
        lamp(74, "REF", ref_col)

        # the phase dial, bottom right
        pr = 14
        pc = QtCore.QPointF(w - pr - 8, h - 16)
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["border"]), 1.2))
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawEllipse(pc, pr, pr)
        p.drawLine(QtCore.QPointF(pc.x() - pr, pc.y()), QtCore.QPointF(pc.x() + pr, pc.y()))
        if math.isfinite(self.theta):
            t = math.radians(self.theta)
            p.setPen(QtGui.QPen(QtGui.QColor(COLORS["trace2"]), 2.2))
            p.drawLine(pc, QtCore.QPointF(pc.x() + (pr - 3) * math.cos(t),
                                          pc.y() - (pr - 3) * math.sin(t)))
        p.setPen(QtGui.QColor(COLORS["muted"]))
        p.drawText(QtCore.QRectF(pc.x() - pr - 50, pc.y() - 7, 46, 14),
                   QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, "PHASE")
        p.end()


# ------------------------------------------------------------- the settings card

class LockInControls(QtWidgets.QFrame):
    """The front panel's settings, grouped as on the instrument."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        cfg = win.cfg
        self.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12); lay.setSpacing(7)
        call, ctrl = win.call, win.ctrl

        # -- reference ------------------------------------------------------
        lay.addWidget(self._section("Reference"))
        ref_row = QtWidgets.QHBoxLayout(); ref_row.setSpacing(6)
        ref_row.addWidget(_cap("Source"))
        self.ref_btns = {}
        for src, text in zip(REF_SOURCES, ("Internal", "Ext TTL", "Ext analog")):
            b = QtWidgets.QPushButton(text); b.setCheckable(True)
            # .clicked fires only for the USER, never for setChecked (gotcha #13)
            b.clicked.connect(lambda _=False, s=src: call(ctrl.set_reference, s))
            ref_row.addWidget(b, 1)
            self.ref_btns[src] = b
        lay.addLayout(ref_row)

        f_row = QtWidgets.QHBoxLayout(); f_row.setSpacing(6)
        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setDecimals(4); self.freq_spin.setRange(0.0, 1e6)
        self.freq_unit = QtWidgets.QComboBox(); self.freq_unit.addItems(list(_FREQ_UNITS))
        f0 = cfg.reference.frequency_Hz
        self.freq_unit.setCurrentText("kHz" if f0 >= 1e3 else "Hz")
        self._freq_scale = _FREQ_UNITS[self.freq_unit.currentText()]
        self.freq_spin.setValue(f0 / self._freq_scale)
        self.freq_unit.currentTextChanged.connect(self._freq_unit_changed)
        b = QtWidgets.QPushButton("Set"); b.setObjectName("primary")
        b.clicked.connect(lambda: call(ctrl.set_frequency, self.freq_spin.value() * self._freq_scale))
        f_row.addWidget(_cap("Osc freq")); f_row.addWidget(self.freq_spin, 1)
        f_row.addWidget(self.freq_unit); f_row.addWidget(b)
        lay.addLayout(f_row)

        a_row = QtWidgets.QHBoxLayout(); a_row.setSpacing(6)
        self.amp_spin = QtWidgets.QDoubleSpinBox()
        self.amp_spin.setDecimals(4); self.amp_spin.setRange(0.0, cfg.limits.amplitude_max_V)
        self.amp_spin.setSuffix(" V rms"); self.amp_spin.setValue(cfg.reference.amplitude_V)
        b = QtWidgets.QPushButton("Set"); b.setObjectName("danger")
        b.setToolTip("OSC OUT drives whatever is connected to it")
        b.clicked.connect(lambda: call(ctrl.set_amplitude, self.amp_spin.value()))
        a_row.addWidget(_cap("Osc amp")); a_row.addWidget(self.amp_spin, 1); a_row.addWidget(b)
        lay.addLayout(a_row)

        p_row = QtWidgets.QHBoxLayout(); p_row.setSpacing(6)
        self.phase_spin = QtWidgets.QDoubleSpinBox()
        self.phase_spin.setDecimals(2); self.phase_spin.setRange(-180.0, 180.0)
        self.phase_spin.setSuffix(" deg"); self.phase_spin.setValue(cfg.reference.phase_deg)
        b = QtWidgets.QPushButton("Set"); b.setObjectName("primary")
        b.clicked.connect(lambda: call(ctrl.set_phase, self.phase_spin.value()))
        self.btn_aqn = QtWidgets.QPushButton("Auto")
        self.btn_aqn.setToolTip("Auto-phase: rotate the phase so the signal lies on +X")
        self.btn_aqn.clicked.connect(lambda: call(ctrl.auto, "auto_phase"))
        p_row.addWidget(_cap("Phase")); p_row.addWidget(self.phase_spin, 1)
        p_row.addWidget(b); p_row.addWidget(self.btn_aqn)
        lay.addLayout(p_row)

        h_row = QtWidgets.QHBoxLayout(); h_row.setSpacing(6)
        self.harm_spin = QtWidgets.QSpinBox(); self.harm_spin.setRange(1, 127)
        self.harm_spin.setPrefix("n = "); self.harm_spin.setValue(cfg.reference.harmonic)
        b = QtWidgets.QPushButton("Set"); b.setObjectName("primary")
        b.clicked.connect(lambda: call(ctrl.set_harmonic, self.harm_spin.value()))
        h_row.addWidget(_cap("Harmonic")); h_row.addWidget(self.harm_spin, 1); h_row.addWidget(b)
        lay.addLayout(h_row)

        # -- signal ----------------------------------------------------------------
        lay.addWidget(self._section("Signal"))
        i_row = QtWidgets.QHBoxLayout(); i_row.setSpacing(6)
        self.input_combo = QtWidgets.QComboBox(); self.input_combo.addItems(list(INPUT_MODES))
        # `activated` = the user picked something; programmatic changes do not fire it
        self.input_combo.activated.connect(
            lambda *_: call(ctrl.set_input, self.input_combo.currentText()))
        self.coupling_combo = QtWidgets.QComboBox(); self.coupling_combo.addItems(["AC", "DC"])
        self.coupling_combo.activated.connect(
            lambda *_: call(ctrl.set_coupling, self.coupling_combo.currentText()))
        i_row.addWidget(_cap("Input")); i_row.addWidget(self.input_combo, 1)
        i_row.addWidget(self.coupling_combo)
        lay.addLayout(i_row)

        s_row = QtWidgets.QHBoxLayout(); s_row.setSpacing(6)
        self.sens_combo = QtWidgets.QComboBox()
        self.sens_combo.setMaxVisibleItems(14)
        self.sens_combo.activated.connect(
            lambda *_: call(ctrl.set_sensitivity, self.sens_combo.currentText()))
        self.btn_as = QtWidgets.QPushButton("Auto")
        self.btn_as.setToolTip("Auto-sensitivity: R to 30-90 % of full scale")
        self.btn_as.clicked.connect(lambda: call(ctrl.auto, "auto_sensitivity"))
        self.btn_asm = QtWidgets.QPushButton("Auto meas.")
        self.btn_asm.setToolTip("Auto-measure: auto-sensitivity, then auto-phase")
        self.btn_asm.clicked.connect(lambda: call(ctrl.auto, "auto_measure"))
        s_row.addWidget(_cap("Sens")); s_row.addWidget(self.sens_combo, 1)
        s_row.addWidget(self.btn_as); s_row.addWidget(self.btn_asm)
        lay.addLayout(s_row)

        # -- filter ------------------------------------------------------------------
        lay.addWidget(self._section("Output filter"))
        t_row = QtWidgets.QHBoxLayout(); t_row.setSpacing(6)
        self.tc_combo = QtWidgets.QComboBox(); self.tc_combo.setMaxVisibleItems(14)
        self.tc_combo.activated.connect(self._tc_picked)
        self.slope_combo = QtWidgets.QComboBox()
        self.slope_combo.activated.connect(
            lambda *_: call(ctrl.set_slope, self.slope_combo.currentText()))
        t_row.addWidget(_cap("TC")); t_row.addWidget(self.tc_combo, 1)
        t_row.addWidget(self.slope_combo, 1)
        lay.addLayout(t_row)
        fm_row = QtWidgets.QHBoxLayout()
        self.fast_box = QtWidgets.QCheckBox("Fast mode  (TC down to 10 us, max 12 dB/oct)")
        self.fast_box.clicked.connect(lambda on: call(ctrl.set_fast_mode, bool(on)))
        fm_row.addWidget(_cap("")); fm_row.addWidget(self.fast_box, 1)
        lay.addLayout(fm_row)

        self.applied = QtWidgets.QLabel("--")
        self.applied.setObjectName("hint"); self.applied.setWordWrap(True)
        lay.addWidget(self.applied)
        lay.addStretch(1)

        self._synced = {"freq": f0, "amp": cfg.reference.amplitude_V,
                        "phase": cfg.reference.phase_deg, "harm": cfg.reference.harmonic}
        self._options_key = None

    @staticmethod
    def _section(text):
        lbl = QtWidgets.QLabel(text.upper())
        lbl.setObjectName("sectionLabel")
        lbl.setStyleSheet(f"color:{COLORS['accent']}; font-size:11px; font-weight:800; "
                          f"letter-spacing:1px; padding-top:4px;")
        return lbl

    def _freq_unit_changed(self, unit):
        hz = self.freq_spin.value() * self._freq_scale
        self._freq_scale = _FREQ_UNITS[unit]
        self.freq_spin.setValue(hz / self._freq_scale)

    def _tc_picked(self, *_):
        tc = self.tc_combo.currentData()
        if tc is not None:
            self.win.call(self.win.ctrl.set_time_constant, float(tc))

    # ---- following the instrument ---------------------------------------------------

    def _rebuild_options(self, s):
        """The lists on offer depend on the mode (fast mode, input). Rebuild
        them only when that changes, never while the user has one open."""
        cfg = self.win.cfg
        key = (s.input, bool(s.fast_mode), cfg.limits.tc_min_s, cfg.limits.tc_max_s)
        if key == self._options_key:
            return
        self._options_key = key
        for combo in (self.sens_combo, self.tc_combo, self.slope_combo):
            combo.blockSignals(True)
            combo.clear()
        for i in sorted(tables.sensitivity_table(s.input)):
            self.sens_combo.addItem(tables.sensitivity_label(i, s.input))
        for tc in tables.allowed_time_constants(bool(s.fast_mode), cfg.limits.tc_min_s,
                                                cfg.limits.tc_max_s):
            self.tc_combo.addItem(tables.tc_label(tc), tc)
        for db in tables.allowed_slopes(bool(s.fast_mode)):
            self.slope_combo.addItem(tables.slope_label(db))
        for combo in (self.sens_combo, self.tc_combo, self.slope_combo):
            combo.blockSignals(False)

    @staticmethod
    def _show(combo: QtWidgets.QComboBox, text: str):
        if combo.currentText() != text and combo.findText(text) >= 0:
            combo.blockSignals(True)
            combo.setCurrentText(text)
            combo.blockSignals(False)

    def _sync_spin(self, key, spin, value, scale=1.0):
        """Follow a setpoint changed ELSEWHERE (console, scan, auto-phase) --
        only when it really moved, and never into a box being typed in."""
        if value is None or not math.isfinite(value):
            return
        if value != self._synced[key] and not spin.hasFocus():
            self._synced[key] = value
            spin.setValue(value / scale)

    def refresh(self, s):
        self._rebuild_options(s)
        for src, b in self.ref_btns.items():
            on = s.ref_source == src
            if b.isChecked() != on or b.objectName() != ("primary" if on else ""):
                b.setChecked(on)
                b.setObjectName("primary" if on else "")
                b.style().unpolish(b); b.style().polish(b)
        self._show(self.input_combo, s.input)
        self._show(self.coupling_combo, s.coupling)
        self._show(self.sens_combo, s.sensitivity)
        self._show(self.tc_combo, tables.tc_label(s.tc_s) if s.tc_s and math.isfinite(s.tc_s) else "")
        self._show(self.slope_combo, s.slope)
        if self.fast_box.isChecked() != bool(s.fast_mode):
            self.fast_box.setChecked(bool(s.fast_mode))
        self._sync_spin("freq", self.freq_spin, s.freq_set_Hz, self._freq_scale)
        self._sync_spin("amp", self.amp_spin, s.amplitude_V)
        self._sync_spin("phase", self.phase_spin, s.phase_deg)
        self._sync_spin("harm", self.harm_spin, s.harmonic)
        busy = bool(s.auto_busy)
        for b in (self.btn_aqn, self.btn_as, self.btn_asm):
            b.setEnabled(s.connected and not busy)
        tc = s.tc_s
        order = max(1, int(s.slope_db) // 6)
        bw = filters.enbw_Hz(tc, order) if tc and math.isfinite(tc) and tc > 0 else float("nan")
        pct = self.win.cfg.acquisition.settle_percent
        self.applied.setText(
            f"applied {fmt_seconds(tc)}, {s.slope}  -  settles {pct:g} % in "
            f"{fmt_seconds(s.settle_s)}  -  ENBW {bw:.3g} Hz"
            + (f"  -  {s.auto_op.replace('_', '-')} running" if busy else ""))


# ------------------------------------------------------------- live history + plots

_WINDOWS = {"10 s": 10.0, "30 s": 30.0, "1 min": 60.0, "5 min": 300.0}


class History:
    """A rolling record of every live value, shared by all plots.

    Filled once per refresh (~60 ms), so 6000 points hold a bit over 5 minutes.
    """

    MAX = 6000
    KEYS = ("x", "y", "r", "theta", "r_fs", "adc1", "adc2")

    def __init__(self):
        self.t = deque(maxlen=self.MAX)
        self.v = {k: deque(maxlen=self.MAX) for k in self.KEYS}

    def add(self, t: float, live: dict) -> None:
        def val(x):
            return float(x) if x is not None and math.isfinite(x) else float("nan")
        adc = live.get("adc") or [None, None]
        self.t.append(t)
        self.v["x"].append(val(live.get("x")))
        self.v["y"].append(val(live.get("y")))
        self.v["r"].append(val(live.get("r")))
        self.v["theta"].append(val(live.get("theta_deg")))
        self.v["r_fs"].append(val(live.get("r_fs")))
        self.v["adc1"].append(val(adc[0]))
        self.v["adc2"].append(val(adc[1]))

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

class LockInTab(QtWidgets.QWidget):
    """Settings, live readouts, the meter and a live plot."""

    QUANTITIES = {"R": ("r",), "X and Y": ("x", "y"), "Theta": ("theta",),
                  "R / full scale": ("r_fs",)}

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 10, 0, 0); grid.setSpacing(12)

        self.controls = LockInControls(win)
        self.controls.setFixedWidth(430)
        grid.addWidget(self.controls, 0, 0, 2, 1)

        card, lay = _card("Live")
        big = QtWidgets.QHBoxLayout(); big.setSpacing(24)
        r_box = QtWidgets.QVBoxLayout(); r_box.setSpacing(0)
        r_box.addWidget(_cap("R"))
        r_line = QtWidgets.QHBoxLayout()
        self.r_val = QtWidgets.QLabel("--"); self.r_val.setObjectName("bigValue")
        self.r_val.setMinimumWidth(150)
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
        self.ovl = QtWidgets.QLabel("--"); self.ovl.setObjectName("mono")
        lay.addWidget(self.ovl)
        self.sample = QtWidgets.QLabel("no settled sample yet"); self.sample.setObjectName("hint")
        self.sample.setWordWrap(True)
        lay.addWidget(self.sample)
        lay.addStretch(1)
        grid.addWidget(card, 0, 1)

        mcard, mlay = _card("Meter")
        self.meter = SensitivityMeter()
        mlay.addWidget(self.meter, 1)
        grid.addWidget(mcard, 0, 2)

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
            "y": self.plot.plot([], [], pen=pg.mkPen(COLORS["trace2"], width=2), name="Y"),
            "theta": self.plot.plot([], [], pen=pg.mkPen(acc, width=2), name="theta"),
            "r_fs": self.plot.plot([], [], pen=pg.mkPen(acc, width=2), name="R / FS"),
        }
        plot_lay.addWidget(self.plot, 1)
        grid.addWidget(plot_card, 1, 1, 1, 2)

        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)
        self._unit = "V"
        self._quantity_changed(self.quantity.currentText())

    def _quantity_changed(self, name: str):
        shown = self.QUANTITIES[name]
        # the legend lists every curve ever added, hidden or not -- rebuild it
        self.legend.clear()
        for key, curve in self.curves.items():
            curve.setVisible(key in shown)
            if key in shown:
                self.legend.addItem(curve, curve.name())
        if name == "Theta":
            self.plot.setLabel("left", "theta", units="deg")
        elif name == "R / full scale":
            self.plot.setLabel("left", "R / FS", units="")
        else:
            self.plot.setLabel("left", name, units=self._unit)
        self.redraw()

    def refresh(self, s):
        self.controls.refresh(s)
        live, u = s.live, s.unit
        if u != self._unit:
            self._unit = u
            self._quantity_changed(self.quantity.currentText())
        v, vu = si_value(live["r"], u)
        self.r_val.setText(v); self.r_unit.setText(vu)
        th = live["theta_deg"]
        self.th_val.setText("--" if th is None or not math.isfinite(th) else f"{th:+.2f} deg")
        xv, xu = si_value(live["x"], u); yv, yu = si_value(live["y"], u)
        self.xy.setText(f"X {xv} {xu}    Y {yv} {yu}")
        lock = "" if s.ref_locked is None else ("  LOCKED" if s.ref_locked else "  UNLOCKED")
        self.freq.setText(f"ref {fmt_hz(s.ref_freq_Hz)}{lock}   detect {fmt_hz(s.demod_freq_Hz)}"
                          f" (n = {s.harmonic})")
        o = s.overload
        if o.get("input") or o.get("output"):
            what = " + ".join(k for k in ("input", "output") if o.get(k))
            self.ovl.setText(f"OVERLOAD: {what}")
            self.ovl.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        else:
            self.ovl.setText(f"no overload  -  R at {100 * live['r_fs']:.1f} % of {s.sensitivity}"
                             if live["r_fs"] is not None and math.isfinite(live["r_fs"])
                             else "no overload")
            self.ovl.setStyleSheet("")
        smp = s.sample
        if smp.get("acq_id"):
            rv, ru = si_value(smp["r"], smp.get("unit", u))
            flags = ("  OVERLOADED" if smp.get("overload") else "") + \
                    ("  UNLOCKED" if smp.get("ref_locked") is False else "")
            self.sample.setText(f"last settled sample #{smp['acq_id']}: R {rv} {ru}, "
                                f"theta {smp['theta_deg']:+.2f} deg{flags}")
        self.meter.set_state(live["r_fs"], s.sensitivity, f"{v} {vu}", th,
                             bool(o.get("input") or o.get("output")), s.ref_locked,
                             s.acquiring)

    def redraw(self):
        if self.pc.pause.isChecked():
            return
        keys = list(self.curves)
        t, vals = self.win.history.window(keys, self.pc.seconds)
        for q in keys:
            if self.curves[q].isVisible():
                self.curves[q].setData(t, vals[q], connect="finite")


class AdcTab(QtWidgets.QWidget):
    """The rear ADC1 and ADC2 inputs: live readouts and a live plot each."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        import pyqtgraph as pg
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 10, 0, 0); v.setSpacing(12)

        row = QtWidgets.QHBoxLayout(); row.setSpacing(12)
        self.vals, self.stats, self.latched = [], [], []
        for k in range(2):
            card, lay = _card(f"ADC{k + 1} (rear panel)")
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
        title = QtWidgets.QLabel("ADC INPUTS  -  LIVE PLOT"); title.setObjectName("cardTitle")
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
        colours = (COLORS["accent"], COLORS["trace2"])
        for k in range(2):
            p = _make_plot(f"ADC{k + 1}", "V")
            if k == 1:
                p.setXLink(self.plots[0])
            self.curves.append(p.plot([], [], pen=pg.mkPen(colours[k], width=2)))
            self.plots.append(p)
            lay.addWidget(p, 1)
        v.addWidget(card, 1)

    def refresh(self, s):
        import numpy as np
        adc = s.live["adc"]
        smp = s.sample
        t, vals = self.win.history.window(("adc1", "adc2"), self.pc.seconds)
        for k in range(2):
            a = adc[k]
            self.vals[k].setText("--" if a is None or not math.isfinite(a) else f"{a:+.4f} V")
            arr = vals[f"adc{k + 1}"]
            finite = arr[np.isfinite(arr)] if arr.size else arr
            if finite.size:
                self.stats[k].setText(f"window: min {finite.min():+.4f}  max {finite.max():+.4f}"
                                      f"  mean {finite.mean():+.4f} V")
            if smp.get("acq_id") and smp.get("adc"):
                self.latched[k].setText(f"settled sample #{smp['acq_id']}: "
                                        f"{smp['adc'][k]:+.4f} V")

    def redraw(self):
        if self.pc.pause.isChecked():
            return
        t, vals = self.win.history.window(("adc1", "adc2"), self.pc.seconds)
        for k in range(2):
            self.curves[k].setData(t, vals[f"adc{k + 1}"], connect="finite")


class InstrumentTab(QtWidgets.QWidget):
    """Status, every setting, and the full log."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 10, 0, 0); grid.setSpacing(12)

        card, lay = _card("Instrument")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        self.f = {}
        for key, label in (("conn", "connection"), ("idn", "instrument"),
                           ("addr", "address"), ("range", "frequency range"),
                           ("input", "input"), ("auto", "last auto operation"),
                           ("error", "hardware error")):
            w = QtWidgets.QLabel("--"); w.setWordWrap(True)
            self.f[key] = w
            form.addRow(_cap(label), w)
        lay.addLayout(form)
        note = QtWidgets.QLabel("Ethernet, port 50000: every reply carries the status and "
                                "overload bytes, so overloads are seen with each reading.")
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
        f = self.f
        f["conn"].setText("connected" if s.connected else "offline")
        f["conn"].setStyleSheet(f"color:{COLORS['ok'] if s.connected else COLORS['danger']};"
                                f" font-weight:700;")
        f["idn"].setText(s.idn or "--")
        hw = cfg.hardware
        f["addr"].setText("remote service" if self.win._remote else
                          (f"{hw.host}:{hw.port} (TCP)" if hw.host else "simulator (no address set)"))
        f["range"].setText(f"up to {fmt_hz(s.freq_max_Hz)} for the oscillator"
                           f" ({'250 kHz option' if hw.option_250kHz else '120 kHz standard'})")
        f["input"].setText(f"{s.input}, {s.coupling}, full scale {s.sensitivity} "
                           f"(SEN {s.sensitivity_index})")
        auto = "--" if not s.auto_id else \
            f"#{s.auto_id} {s.auto_op.replace('_', '-')}: " + \
            ("running" if s.auto_busy else (s.auto_error or "done"))
        f["auto"].setText(auto)
        f["error"].setText(s.hw_error or "none")
        f["error"].setStyleSheet(f"color:{COLORS['danger'] if s.hw_error else COLORS['muted']};")


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    TAB_INSTRUMENT = 2

    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self.history = History()
        self.setWindowTitle("7230 - DSP Lock-in Amplifier" + ("  (remote)" if remote else ""))
        self.resize(1400, 900)

        root = QtWidgets.QWidget(); root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(16, 12, 16, 16); outer.setSpacing(10)
        outer.addWidget(self._build_strip())

        self.tabs = QtWidgets.QTabWidget()
        self.lockin_tab = LockInTab(self)
        self.adc_tab = AdcTab(self)
        self.inst_tab = InstrumentTab(self)
        self.tabs.addTab(self.lockin_tab, "Lock-in")
        self.tabs.addTab(self.adc_tab, "ADC in")
        self.tabs.addTab(self.inst_tab, "Instrument")
        self.tabs.currentChanged.connect(self._tab_changed)
        outer.addWidget(self.tabs, 1)

        self.controls = self.lockin_tab.controls
        self.meter = self.lockin_tab.meter
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
        title = QtWidgets.QLabel("7230")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        h.addWidget(title)
        self.conn_dot = QtWidgets.QLabel("connecting")
        h.addWidget(self.conn_dot)
        self.idn_label = QtWidgets.QLabel("--")
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        h.addWidget(self.idn_label)
        h.addSpacing(10)
        self.acq_btn = QtWidgets.QPushButton("Acquire"); self.acq_btn.setObjectName("primary")
        self.acq_btn.setToolTip("Wait the settling time, then latch one sample")
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
        self.controls._options_key = None        # limits may have moved: rebuild lists
        self.controls.amp_spin.setMaximum(self.cfg.limits.amplitude_max_V)

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
            rv, ru = si_value(smp["r"], smp.get("unit", s.unit))
            flag = "  OVL" if smp.get("overload") else ""
            self.sample_label.setText(
                f"#{smp['acq_id']}  settle {fmt_seconds(smp.get('settle_s'))}, "
                f"{smp.get('n_avg', 1)} pts  |  R {rv} {ru}  theta "
                f"{smp['theta_deg']:+.2f} deg{flag}")

        self.history.add(time.monotonic() - self._t0, s.live)
        self.lockin_tab.refresh(s)      # settings must stay in sync even when hidden
        self.adc_tab.refresh(s)
        self.inst_tab.refresh(s)
        self._redraw_visible()

    def _redraw_visible(self):
        """Only the visible tab's plots are redrawn -- the rest record silently."""
        current = self.tabs.currentWidget()
        if current in (self.lockin_tab, self.adc_tab):
            current.redraw()

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app. The theme is chosen ONCE here, before any widget."""
    set_theme(getattr(cfg.ui, "theme", "dark"))
    # Numbers with a '.' decimal point and no thousands separator, whatever the
    # Windows locale (gotcha #18): under a comma-decimal locale a 10 ms time
    # constant would show as "10,000".
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
    """Default: run against the built-in simulator, in-process.

    The simulated signal is ~2 mV, so the demo starts on the 5 mV range (the
    config default, 100 mV, would leave the meter's needle near zero).
    """
    cfg = Config()
    cfg.signal.sensitivity_index = 20          # 5 mV full scale
    if theme:
        cfg.ui.theme = theme
    li, _ = build_sim_system(cfg)
    return run_app(li, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
