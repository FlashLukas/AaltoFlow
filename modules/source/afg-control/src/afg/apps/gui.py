"""Front panel for the Tektronix AFG1062 two-channel function generator.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a Generator-like object (a real
in-process Generator, or an AfgClient facade for a remote service). It sends
commands (set_output / set_waveform / set_frequency / ...) and reads a status
snapshot (a flat dict: `ch1_frequency_Hz`, `ch2_settled`, ...) on a Qt timer.
Events arrive on a Qt signal so they can safely cross into the GUI thread.

The signature widget is the OutputsView: both outputs drawn against ONE time
axis, the way the scope next to the generator shows them (CH1 -> scope CH1,
CH2 -> scope CH2 and EXT TRIG on the bench). The dashed lines are each
channel's peak limit, so you see at a glance how close a setting is to the
ceiling; an output that is off is a flat dashed line at 0 V.
"""

from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import Config
from ..sim_system import build_sim_system
from .. import waveforms
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog
from .control_bar import ControlBar, mark_always
from ..control import ControlRefused

_WAVE_LABELS = {"sine": "Sine", "square": "Square", "pulse": "Pulse",
                "ramp": "Ramp", "noise": "Noise", "dc": "DC", "arb": "Arb (kept)"}
_FREQ_UNITS = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6}


# ------------------------------------------------------------- signal bridge

class Bridge(QtCore.QObject):
    """Carries generator events across the thread boundary into the GUI."""
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


def _c_locale() -> QtCore.QLocale:
    """The C locale without group separators. Number widgets otherwise follow
    the Windows locale and show "2500,000" or "2,500.000" (gotcha #18)."""
    loc = QtCore.QLocale.c()
    loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
    return loc


def _spin(decimals=4, step=0.1, suffix="") -> QtWidgets.QDoubleSpinBox:
    """A QDoubleSpinBox pinned to the C locale, whatever the app default is."""
    w = QtWidgets.QDoubleSpinBox()
    w.setLocale(_c_locale())
    w.setDecimals(decimals)
    w.setSingleStep(step)
    w.setKeyboardTracking(False)
    if suffix:
        w.setSuffix("  " + suffix)
    return w


def _num(value):
    """A status number, or None when it is missing / NaN / null."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def _fmt_hz(hz: float) -> tuple[str, str]:
    """A frequency as (number, unit) the way the AFG's display shows it."""
    if hz >= 1e6:
        return f"{hz / 1e6:.6f}", "MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:.6f}", "kHz"
    return f"{hz:.6f}", "Hz"


def _ch_color(ch: str) -> QtGui.QColor:
    """CH1 in the accent colour, CH2 in the text colour (read at paint time)."""
    return QtGui.QColor(COLORS["accent"] if ch == "ch1" else COLORS["text"])


# ------------------------------------------------------------- the indicator

class OutputsView(QtWidgets.QWidget):
    """Both outputs against one time axis, like the scope beside the AFG.

    The span is two periods of CH1 (or of CH2 if CH1 has no frequency); the
    vertical scale fits the larger peak limit of the two channels. A channel
    whose frequency differs is drawn at its true rate on the same axis, so
    "CH2 follows CH1" visibly locks the two together."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(220)
        self.setMinimumWidth(380)
        self._s: dict = {}

    def set_status(self, s: dict):
        self._s = dict(s)
        self.update()

    def _setting(self, ch: str) -> dict:
        s = self._s
        return {k: s.get(f"{ch}_{k}") for k in
                ("output", "waveform", "frequency_Hz", "amplitude_Vpp", "offset_V",
                 "phase_deg", "duty_pct", "symmetry_pct")}

    def paintEvent(self, ev):
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QPainter, QPen, QPainterPath, QColor

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        f = p.font(); f.setBold(True); f.setPointSize(8); p.setFont(f)
        x0, x1, y0, y1 = 44.0, w - 10.0, 8.0, h - 26.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(COLORS["code_bg"]))
        p.drawRoundedRect(QRectF(x0 - 6, y0 - 4, x1 - x0 + 12, y1 - y0 + 8), 6, 6)
        # graticule: 10 x 8 divisions, as on the scope
        p.setPen(QPen(QColor(COLORS["grid"]), 1))
        for i in range(11):
            x = x0 + (x1 - x0) * i / 10
            p.drawLine(QPointF(x, y0), QPointF(x, y1))
        for j in range(9):
            y = y0 + (y1 - y0) * j / 8
            p.drawLine(QPointF(x0, y), QPointF(x1, y))
        mid = (y0 + y1) / 2

        chans = [c for c in ("ch1", "ch2") if f"{c}_waveform" in self._s]
        if not chans:
            p.setPen(QColor(COLORS["muted"]))
            p.drawText(QRectF(x0, y0, x1 - x0, y1 - y0), Qt.AlignCenter, "no status yet")
            p.end()
            return
        vmax = max([_num(self._s.get(f"{c}_peak_max_V")) or 5.0 for c in chans] + [0.1])
        scale = (y1 - y0) / 2 / (vmax * 1.08)

        def yv(v):
            return mid - v * scale

        # time span: two periods of the first channel that has a frequency
        span = 2e-3
        for c in chans:
            st = self._setting(c)
            fr = _num(st["frequency_Hz"])
            if st["waveform"] not in ("dc", "noise") and fr and fr > 0:
                span = 2.0 / fr
                break

        # peak limits (dashed) and 0 V
        for c in chans:
            pk = _num(self._s.get(f"{c}_peak_max_V"))
            if pk:
                col = _ch_color(c); col.setAlpha(110)
                p.setPen(QPen(col, 1, Qt.DashLine))
                for v in (pk, -pk):
                    p.drawLine(QPointF(x0, yv(v)), QPointF(x1, yv(v)))
        p.setPen(QPen(QColor(COLORS["border"]), 1.2))
        p.drawLine(QPointF(x0, mid), QPointF(x1, mid))

        n = 400
        for c in chans:
            st = self._setting(c)
            col = _ch_color(c)
            if not st["output"]:
                p.setPen(QPen(QColor(COLORS["muted"]), 1.4, Qt.DashLine))
                p.drawLine(QPointF(x0, mid), QPointF(x1, mid))
                continue
            setting = {k: (v if v is not None else 0.0) for k, v in st.items()}
            setting["output"] = True
            setting["waveform"] = st["waveform"] or "sine"
            path = QPainterPath()
            for i in range(n + 1):
                t = span * i / n
                v = waveforms.value(setting, t)
                pt = QPointF(x0 + (x1 - x0) * i / n, yv(v))
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            p.setPen(QPen(col, 2.0)); p.setBrush(Qt.NoBrush)
            p.drawPath(path)

        # channel markers at the left edge (their offset), like a scope; a
        # marker that would sit on top of the previous one moves below it
        used = []
        for c in chans:
            off = _num(self._s.get(f"{c}_offset_V")) or 0.0
            y = yv(off) if self._s.get(f"{c}_output") else mid
            while any(abs(y - u) < 17 for u in used):
                y += 18
            used.append(y)
            p.setPen(Qt.NoPen); p.setBrush(_ch_color(c))
            p.drawRoundedRect(QRectF(4, y - 8, 30, 16), 3, 3)
            p.setPen(QColor(COLORS["bg"]))
            p.drawText(QRectF(4, y - 8, 30, 16), Qt.AlignCenter, c.upper())

        p.setPen(QColor(COLORS["muted"]))
        div_v = vmax * 1.08 / 4
        p.drawText(QRectF(x0, y1 + 6, x1 - x0, 16), Qt.AlignLeft,
                   f"{span * 1e3 / 10:.4g} ms/div   {div_v:.3g} V/div")
        caption = "dashed = peak limit"
        if self._s.get("follow"):
            caption = f"CH2 follows CH1, {self._s.get('phase_offset_deg', 0):g} deg   " + caption
        p.drawText(QRectF(x0, y1 + 6, x1 - x0, 16), Qt.AlignRight, caption)
        p.end()


# ------------------------------------------------------------- channel card

class ChannelCard(QtWidgets.QFrame):
    """The controls and readouts of ONE output channel."""

    def __init__(self, ch: str, ctrl, safe=None):
        super().__init__()
        self.ch, self.ctrl = ch, ctrl
        # how a command is run: the main window's _safe (a refusal because
        # another PC holds control goes to its log); plain call otherwise
        self._safe = safe or (lambda fn, *args: fn(*args))
        self._shape = None            # (waveform, load, follow) the ranges were made for
        self._out = None
        self.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        top = QtWidgets.QHBoxLayout()
        name = QtWidgets.QLabel(f"CHANNEL {ch[-1]}")
        name.setObjectName("cardTitle")
        name.setStyleSheet(f"color:{_ch_color(ch).name()};")
        top.addWidget(name); top.addStretch(1)
        self.mode_lamp = QtWidgets.QLabel("")
        self.settled_lamp = QtWidgets.QLabel("SET")
        for lamp in (self.mode_lamp, self.settled_lamp):
            lamp.setAlignment(QtCore.Qt.AlignCenter)
            top.addWidget(lamp)
        lay.addLayout(top)

        # big readout: frequency, then amplitude / offset
        self.freq_value, self.freq_unit = self._readout(lay, "Frequency", "Hz")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(18)
        self.amp_value, _ = self._readout(row, "Amplitude", "Vpp", small=True)
        self.off_value, _ = self._readout(row, "Offset", "V", small=True)
        self.peak_value, _ = self._readout(row, "Peak", "V", small=True)
        row.addStretch(1)
        lay.addLayout(row)

        self.out_btn = QtWidgets.QPushButton("Output On")
        self.out_btn.setObjectName("primary")
        self.out_btn.setMinimumHeight(36)
        self.out_btn.clicked.connect(
            lambda: self._safe(self.ctrl.set_output, self.ch, not bool(self._out)))
        lay.addWidget(self.out_btn)

        grid = QtWidgets.QGridLayout(); grid.setHorizontalSpacing(8); grid.setVerticalSpacing(6)
        r = 0
        self.wave_combo = QtWidgets.QComboBox()
        for wf in ctrl.caps.get("waveforms", ()):
            self.wave_combo.addItem(_WAVE_LABELS.get(wf, wf), wf)
        # `activated` fires only on a USER choice, so refreshing the combo from
        # status can never send a command back (gotcha #13)
        self.wave_combo.activated.connect(
            lambda i: self._safe(self.ctrl.set_waveform, self.ch, self.wave_combo.itemData(i)))
        grid.addWidget(QtWidgets.QLabel("Waveform"), r, 0)
        grid.addWidget(self.wave_combo, r, 1, 1, 3); r += 1

        self.freq_spin = _spin(6, 1.0)
        self.unit_combo = QtWidgets.QComboBox(); self.unit_combo.addItems(list(_FREQ_UNITS))
        self._unit = "Hz"
        self.unit_combo.currentTextChanged.connect(self._change_unit)
        self.freq_btn = QtWidgets.QPushButton("Set")
        self.freq_btn.clicked.connect(
            lambda: self._safe(self.ctrl.set_frequency, self.ch, self.current_freq_hz()))
        grid.addWidget(QtWidgets.QLabel("Freq"), r, 0)
        grid.addWidget(self.freq_spin, r, 1); grid.addWidget(self.unit_combo, r, 2)
        grid.addWidget(self.freq_btn, r, 3); r += 1

        def knob(label, spin, setter):
            nonlocal r
            btn = QtWidgets.QPushButton("Set")
            btn.clicked.connect(lambda: self._safe(setter, self.ch, spin.value()))
            lbl = QtWidgets.QLabel(label)
            grid.addWidget(lbl, r, 0); grid.addWidget(spin, r, 1, 1, 2)
            grid.addWidget(btn, r, 3); r += 1
            return lbl, btn

        self.amp_spin = _spin(4, 0.01, "Vpp")
        self.amp_row = knob("Amplitude", self.amp_spin, self.ctrl.set_amplitude)
        self.off_spin = _spin(4, 0.01, "V")
        self.off_row = knob("Offset", self.off_spin, self.ctrl.set_offset)
        self.phase_spin = _spin(2, 1.0, "deg"); self.phase_spin.setRange(-180.0, 180.0)
        self.phase_row = knob("Phase", self.phase_spin, self.ctrl.set_phase)
        self.duty_spin = _spin(2, 1.0, "%")
        self.duty_row = knob("Duty", self.duty_spin, self.ctrl.set_duty)
        self.sym_spin = _spin(2, 1.0, "%"); self.sym_spin.setRange(0.0, 100.0)
        self.sym_row = knob("Symmetry", self.sym_spin, self.ctrl.set_symmetry)

        self.load_combo = QtWidgets.QComboBox()
        self.load_combo.addItem("50 ohm", "50"); self.load_combo.addItem("High-Z", "high-Z")
        self.load_combo.activated.connect(
            lambda i: self._safe(self.ctrl.set_load, self.ch, self.load_combo.itemData(i)))
        self.load_combo.setToolTip("What the AFG assumes is connected. The volts above "
                                   "are INTO this load: 1 Vpp at 50 ohm is 2 Vpp on an "
                                   "open (high-Z) input.")
        grid.addWidget(QtWidgets.QLabel("Load"), r, 0)
        grid.addWidget(self.load_combo, r, 1, 1, 3); r += 1
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        self.note = QtWidgets.QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        lay.addWidget(self.note)
        lay.addStretch(1)

    def _readout(self, parent, label, unit, small=False):
        box = QtWidgets.QVBoxLayout(); box.setSpacing(1)
        cap = QtWidgets.QLabel(label.upper())
        cap.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; font-weight:700; "
                          f"letter-spacing:1px;")
        line = QtWidgets.QHBoxLayout(); line.setSpacing(5)
        val = QtWidgets.QLabel("-")
        if small:
            val.setStyleSheet(f"color:{COLORS['text']}; font-size:18px; font-weight:700;")
        else:
            val.setObjectName("bigValue")
        u = QtWidgets.QLabel(unit); u.setObjectName("unit")
        line.addWidget(val); line.addWidget(u, 0, QtCore.Qt.AlignBottom); line.addStretch(1)
        box.addWidget(cap); box.addLayout(line)
        holder = QtWidgets.QWidget(); holder.setLayout(box)
        parent.addWidget(holder)
        return val, u

    # ---- ranges -------------------------------------------------------------

    def apply_limits(self):
        """Spin ranges from the brain's live envelope (lab limits narrowed by
        the instrument's range for this waveform and load), keeping values."""
        try:
            env = self.ctrl.envelope(self.ch)
        except Exception:
            return
        if not env:
            return
        scale = _FREQ_UNITS[self._unit]
        hz = self.current_freq_hz()
        if env.get("freq_max_Hz") is not None:
            self.freq_spin.blockSignals(True)
            self.freq_spin.setDecimals(6)
            self.freq_spin.setRange(env["freq_min_Hz"] / scale, env["freq_max_Hz"] / scale)
            self.freq_spin.setValue(hz / scale)
            self.freq_spin.blockSignals(False)
        self.amp_spin.setRange(env["amp_min_Vpp"], env["amp_max_Vpp"])
        self.off_spin.setRange(-env["peak_max_V"], env["peak_max_V"])
        self.duty_spin.setRange(env.get("duty_min_pct", 0.0), env.get("duty_max_pct", 100.0))

    def seed_inputs(self, s: dict):
        """Put the instrument's values into the input boxes. Called once after
        the start read the instrument (read-only start): a "Set" pressed on a
        box still holding the config default would send a value nobody chose."""
        ch = self.ch
        self.apply_limits()
        hz = _num(s.get(f"{ch}_frequency_Hz")) or 1000.0
        unit = "MHz" if hz >= 1e6 else ("kHz" if hz >= 1e3 else "Hz")
        self.unit_combo.setCurrentText(unit)
        self._set_freq_box(hz)
        for spin, key in ((self.amp_spin, "amplitude_Vpp"), (self.off_spin, "offset_V"),
                          (self.phase_spin, "phase_deg"), (self.duty_spin, "duty_pct"),
                          (self.sym_spin, "symmetry_pct")):
            v = _num(s.get(f"{ch}_{key}"))
            if v is not None:
                spin.setValue(v)

    def _set_freq_box(self, hz: float):
        self.freq_spin.blockSignals(True)
        self.freq_spin.setValue(hz / _FREQ_UNITS[self._unit])
        self.freq_spin.blockSignals(False)

    def current_freq_hz(self) -> float:
        return self.freq_spin.value() * _FREQ_UNITS[self._unit]

    def _change_unit(self, unit: str):
        hz = self.current_freq_hz()
        self._unit = unit
        self.apply_limits()
        self._set_freq_box(hz)
        self.freq_spin.setSingleStep({"Hz": 1.0, "kHz": 0.1, "MHz": 0.01}[unit])

    # ---- refresh ---------------------------------------------------------

    def refresh(self, s: dict):
        ch = self.ch
        wf = s.get(f"{ch}_waveform") or "sine"
        follows = ch == "ch2" and bool(s.get("follow"))
        shape = (wf, s.get(f"{ch}_load"), follows, s.get("describe_rev"))
        if shape != self._shape:
            self._shape = shape
            self.apply_limits()
            has_freq = wf not in ("dc", "noise")
            for wdg in (self.freq_spin, self.unit_combo, self.freq_btn):
                wdg.setEnabled(has_freq and not follows)
            for wdg in (self.phase_spin, *self.phase_row):
                wdg.setEnabled(has_freq and not follows)
            for wdg in (self.amp_spin, *self.amp_row):
                wdg.setEnabled(wf != "dc")
            self.off_row[0].setText("DC level" if wf == "dc" else "Offset")
            for wdg in (self.duty_spin, *self.duty_row):
                wdg.setVisible(wf == "pulse")
            for wdg in (self.sym_spin, *self.sym_row):
                wdg.setVisible(wf == "ramp")
        # A knob the instrument cannot report (firmware without the query, e.g.
        # ramp symmetry on the AFG1062 FV:V1.0.2) shows the value last SET from
        # here -- say so next to it, so nobody takes it for a measurement.
        nrb = s.get(f"{ch}_not_read_back") or ""
        for row, key, name in ((self.duty_row, "duty_pct", "Duty"),
                               (self.sym_row, "symmetry_pct", "Symmetry")):
            unread = key in nrb
            row[0].setText(name + (" *" if unread else ""))
            row[0].setToolTip("* not read back: this instrument cannot report it; "
                              "the value is the one last set from here" if unread else "")
        i = self.wave_combo.findData(wf)
        if i < 0:
            self.wave_combo.addItem(_WAVE_LABELS.get(wf, wf), wf)
            i = self.wave_combo.findData(wf)
        if i != self.wave_combo.currentIndex() and not self.wave_combo.view().isVisible():
            self.wave_combo.setCurrentIndex(i)
        load = s.get(f"{ch}_load")
        j = self.load_combo.findData(load)
        if j < 0 and load:
            self.load_combo.addItem(f"{load} ohm", load)
            j = self.load_combo.findData(load)
        if j != self.load_combo.currentIndex() and not self.load_combo.view().isVisible():
            self.load_combo.setCurrentIndex(j)

        hz = _num(s.get(f"{ch}_frequency_Hz"))
        if wf in ("dc", "noise") or hz is None:
            self.freq_value.setText("-"); self.freq_unit.setText("")
        else:
            num, unit = _fmt_hz(hz)
            self.freq_value.setText(num); self.freq_unit.setText(unit)
            if follows:                         # the box tracks CH1
                self._set_freq_box(hz)
        if follows:
            ph = _num(s.get(f"{ch}_phase_deg"))
            if ph is not None and not self.phase_spin.hasFocus():
                self.phase_spin.setValue(ph)
        self.amp_value.setText("-" if wf == "dc" else f"{_num(s.get(f'{ch}_amplitude_Vpp')) or 0:.4g}")
        self.off_value.setText(f"{_num(s.get(f'{ch}_offset_V')) or 0:.4g}")
        pk, pkmax = _num(s.get(f"{ch}_peak_V")) or 0.0, _num(s.get(f"{ch}_peak_max_V"))
        self.peak_value.setText(f"{pk:.3g}")
        near = pkmax is not None and pk >= 0.95 * pkmax
        self.peak_value.setStyleSheet(
            f"color:{COLORS['accent'] if near else COLORS['text']}; font-size:18px; "
            f"font-weight:700;")

        out = bool(s.get(f"{ch}_output"))
        if out != self._out:
            self._out = out
            self.out_btn.setText("Output Off" if out else "Output On")
            self.out_btn.setObjectName("danger" if out else "primary")
            self.out_btn.style().unpolish(self.out_btn)
            self.out_btn.style().polish(self.out_btn)
        settled = bool(s.get(f"{ch}_settled"))
        mismatch = s.get(f"{ch}_mismatch") or ""
        self._lamp(self.settled_lamp, "SET" if settled else ("CHECK" if mismatch else "..."),
                   settled, bool(mismatch))
        mode = s.get(f"{ch}_mode") or "continuous"
        self.mode_lamp.setVisible(mode != "continuous")
        self._lamp(self.mode_lamp, mode.upper(), False, True)
        notes = []
        if mismatch:
            notes.append(f"Instrument differs: {mismatch}")
        if follows:
            notes.append("Frequency and phase follow CH1.")
        if wf == "arb":
            notes.append("Arbitrary waveform kept from the instrument.")
        self.note.setText("  ".join(notes))
        self.note.setStyleSheet(
            f"color:{COLORS['danger'] if mismatch else COLORS['muted']}; font-size:11px;")

    @staticmethod
    def _lamp(label, text, ok: bool, bad: bool):
        """Green when fine, red when wrong, grey while waiting."""
        color = COLORS["ok"] if ok else (COLORS["danger"] if bad else COLORS["muted"])
        label.setText(text)
        label.setStyleSheet(f"color:{color}; border:1px solid {color}; border-radius:8px; "
                            f"padding:2px 6px; font-size:10px; font-weight:700;")


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        # Number widgets follow the Windows locale otherwise (gotcha #18).
        QtCore.QLocale.setDefault(_c_locale())
        self.setLocale(_c_locale())
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        title = "Function generator - Tektronix AFG1062"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1320, 800)

        root = QtWidgets.QWidget(); root.setObjectName("root")
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(14)
        outer.addLayout(self._build_header())

        mid = QtWidgets.QHBoxLayout(); mid.setSpacing(14)
        self.cards = {ch: ChannelCard(ch, ctrl, safe=self._safe) for ch in ctrl.channels}
        for card in self.cards.values():
            card.setFixedWidth(330)
            mid.addWidget(card, 0)
        right = QtWidgets.QVBoxLayout(); right.setSpacing(14)
        icard, ilay = _card("Outputs")
        self.view = OutputsView()
        ilay.addWidget(self.view, 1)
        right.addWidget(icard, 1)
        right.addWidget(self._build_coupling())
        mid.addLayout(right, 1)
        outer.addLayout(mid, 0)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(100)
        llay.addWidget(self.log)
        outer.addWidget(lcard, 1)

        # Control or viewer (control_bar.py): a bar across the top, only for a
        # GUI on a service -- a local GUI owns its generator.
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

        # generator events -> log
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

        # start the generator (opens the backend and READS its state --
        # nothing is changed, a running output keeps running) and the timer
        self.ctrl.start()
        s = self.ctrl.status()
        for card in self.cards.values():
            card.seed_inputs(s)
        self.offset_spin.setValue(_num(s.get("phase_offset_deg")) or 0.0)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

        if self._control_bar is not None:
            self._control_bar.claim_if_free()

    # ---- layout ----------------------------------------------------------

    def _build_header(self):
        row = QtWidgets.QHBoxLayout(); row.setSpacing(12)
        title = QtWidgets.QLabel("AFG1062")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; "
                            f"letter-spacing:2px;")
        row.addWidget(title)
        self.conn_dot = QtWidgets.QLabel("o  connecting")
        self.conn_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        row.addWidget(self.conn_dot)
        self.idn_label = QtWidgets.QLabel("")
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        row.addWidget(self.idn_label)
        row.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        mark_always(settings_btn)    # a viewer may LOOK; the service refuses the OK
        row.addWidget(settings_btn)
        off_btn = QtWidgets.QPushButton("All outputs off"); off_btn.setObjectName("danger")
        # the SAFETY verb (net/service.py): works for a viewer too
        off_btn.clicked.connect(lambda: self._safe(self.ctrl.outputs_off))
        mark_always(off_btn)
        row.addWidget(off_btn)
        return row

    def _build_coupling(self):
        card, lay = _card("Coupling")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(10)
        self.follow_box = QtWidgets.QCheckBox("CH2 follows CH1")
        self.follow_box.setToolTip("CH2 takes CH1's frequency; its phase is CH1's plus "
                                   "the offset; the channels are re-aligned after each "
                                   "change. For a trigger square next to a drive signal.")
        # `clicked` = user only; refresh uses blockSignals (gotcha #13)
        self.follow_box.clicked.connect(
            lambda on: self._safe(self.ctrl.set_follow, bool(on), self.offset_spin.value()))
        row.addWidget(self.follow_box)
        row.addSpacing(12)
        row.addWidget(QtWidgets.QLabel("Phase offset"))
        self.offset_spin = _spin(2, 1.0, "deg"); self.offset_spin.setRange(-180.0, 180.0)
        row.addWidget(self.offset_spin)
        self.offset_btn = QtWidgets.QPushButton("Set")
        self.offset_btn.clicked.connect(
            lambda: self._safe(self.ctrl.set_phase_offset, self.offset_spin.value()))
        row.addWidget(self.offset_btn)
        row.addStretch(1)
        self.align_btn = QtWidgets.QPushButton("Align phase")
        self.align_btn.setToolTip("Restart both channels' phase together. The outputs "
                                  "restart: a short glitch on whatever CH1 drives.")
        self.align_btn.clicked.connect(lambda: self._safe(self.ctrl.align_phase))
        row.addWidget(self.align_btn)
        lay.addLayout(row)
        return card

    # ---- actions ---------------------------------------------------------

    def _safe(self, fn, *args):
        """Run a command; a refusal (another PC holds control, or the brain
        refuses, e.g. CH2's frequency while it follows CH1) goes to the log."""
        try:
            r = fn(*args)
        except ControlRefused as exc:
            self._on_event("error", str(exc))
        except ValueError as exc:                 # local brain refused it
            self._on_event("error", str(exc))
        else:
            if isinstance(r, dict) and r.get("ok") is False:   # remote refusal
                self._on_event("error", r.get("error", "refused"))

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        for card in self.cards.values():
            card._shape = None          # ranges re-read at the next refresh

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
        for card in self.cards.values():
            card.refresh(s)
        self.view.set_status(s)

        if s.get("connected"):
            err = s.get("hw_error")
            self.conn_dot.setText("o  hardware error" if err else "o  connected")
            self.conn_dot.setStyleSheet(
                f"color:{COLORS['danger'] if err else COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("o  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        if s.get("idn"):
            self.idn_label.setText(s["idn"])
        follow = bool(s.get("follow"))
        if follow != self.follow_box.isChecked():
            self.follow_box.blockSignals(True)
            self.follow_box.setChecked(follow)
            self.follow_box.blockSignals(False)
        self.offset_spin.setEnabled(follow); self.offset_btn.setEnabled(follow)
        off = _num(s.get("phase_offset_deg"))
        if follow and off is not None and not self.offset_spin.hasFocus()                 and self.offset_spin.value() != off:
            # the offset can be set from elsewhere (a scan, a script): show it
            self.offset_spin.setValue(off)

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()          # local: outputs off + disconnect; remote: close client
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Generator-like object (a real in-process
    Generator, or an AfgClient facade for a remote service). The theme is
    chosen ONCE here, from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # swap the active palette first
    QtCore.QLocale.setDefault(_c_locale())          # before any widget exists
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
    """Default: run against the built-in simulator, in-process. `theme` (if given)
    overrides cfg.ui.theme for this launch."""
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    gen, _ = build_sim_system(cfg)
    return run_app(gen, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
