# COPIED from afg-control (src/afg/apps/gui.py: the helpers, OutputsView,
# ChannelCard) -- the suite copies shared code instead of importing across
# modules. Channels renamed ch1/ch2 -> w1/w2. GeneratorPanel at the end is
# this module's own.
"""The GENERATOR tab of the scope window (an instrument with a generator: the
Analog Discovery's W1 / W2), plus its power supplies (V+ / V-).

The two output cards and the drawing of both outputs are afg-control's front
panel, so the AD's generator handles exactly like the AFG1062. `gen` is the
generator brain (a local scope's `scope.gen`) or the remote stand-in
(ScopeClient.gen); `status` is the generator's own status (generator/wire.py
takes it out of the scope's).
"""

from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ..generator import waveforms
from .theme import COLORS
from .control_bar import mark_always

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
    """W1 in the accent colour, W2 in the text colour (read at paint time)."""
    return QtGui.QColor(COLORS["accent"] if ch == "w1" else COLORS["text"])


# a box typed in this recently is left alone by the status refresh
_EDIT_HOLD_S = 2.0


class _EditWatch(QtCore.QObject):
    """Notes when the user types in or scrolls a number box (event filter)."""

    def __init__(self, edited: dict):
        super().__init__()
        self._edited = edited

    def eventFilter(self, obj, ev):
        if ev.type() in (QtCore.QEvent.KeyPress, QtCore.QEvent.Wheel):
            spin = obj if isinstance(obj, QtWidgets.QAbstractSpinBox) else obj.parent()
            self._edited[spin] = time.monotonic()
        return False


class OutputsView(QtWidgets.QWidget):
    """Both outputs against one time axis, like the scope beside the AFG.

    The span is two periods of W1 (or of W2 if W1 has no frequency); the
    vertical scale fits the larger peak limit of the two channels. A channel
    whose frequency differs is drawn at its true rate on the same axis, so
    "W2 follows W1" visibly locks the two together."""

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

        chans = [c for c in ("w1", "w2") if f"{c}_waveform" in self._s]
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

        # Where each channel's phase 0 sits (a small triangle on top, in the
        # channel's colour). A 2-degree phase step moves a trace by only 1/180
        # of a period -- it looks frozen; the marker and the caption show it
        # (Lukas 2026-10-07, a scan sweeping W1's phase).
        for k, c in enumerate(chans):
            st = self._setting(c)
            fr = _num(st["frequency_Hz"])
            if not st["output"] or st["waveform"] in ("dc", "noise") or not fr:
                continue
            ph = _num(st["phase_deg"]) or 0.0
            t_zero = ((-ph / 360.0) % 1.0) / fr        # value(): frac = f t + phase/360
            if t_zero > span:
                continue
            x = x0 + (x1 - x0) * t_zero / span
            yt = y0 + 1 + 9 * k                         # W2's a little lower
            tri = QPainterPath()
            tri.moveTo(QPointF(x - 5, yt)); tri.lineTo(QPointF(x + 5, yt))
            tri.lineTo(QPointF(x, yt + 8)); tri.closeSubpath()
            p.setPen(Qt.NoPen); p.setBrush(_ch_color(c))
            p.drawPath(tri)
            col = _ch_color(c); col.setAlpha(70)
            p.setPen(QPen(col, 1, Qt.DotLine))
            p.drawLine(QPointF(x, yt + 8), QPointF(x, y1))

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
        scale_text = f"{span * 1e3 / 10:.4g} ms/div   {div_v:.3g} V/div"
        phases = "   ".join(
            f"{c.upper()} {(_num(self._s.get(f'{c}_phase_deg')) or 0.0):.4g} deg"
            for c in chans if self._s.get(f"{c}_waveform") not in ("dc", "noise"))
        caption = (phases + "  (v = phase 0)   " if phases else "") + "dashed = peak limit"
        if self._s.get("follow"):
            caption = ((f"W2 follows W1, {self._s.get('phase_offset_deg', 0):g} deg   "
                        if self._s.get("phase_follow") else "W2 frequency follows W1   ")
                       + caption)
        # a narrow view (the scope window's Generator tab): drop what does not
        # fit rather than print one text over the other -- the phases stay
        fm = p.fontMetrics()
        room = x1 - x0
        if fm.horizontalAdvance(scale_text + "   " + caption) > room:
            caption = caption.replace("   dashed = peak limit", "")
        if fm.horizontalAdvance(scale_text + "   " + caption) > room:
            scale_text = ""
        if scale_text:
            p.drawText(QRectF(x0, y1 + 6, room, 16), Qt.AlignLeft, scale_text)
        p.drawText(QRectF(x0, y1 + 6, room, 16), Qt.AlignRight, caption)
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
        name = QtWidgets.QLabel(f"OUTPUT {ch.upper()}")
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
        # (the phase sits beside the frequency: a fourth readout in the row
        # below does not fit the card's width)
        top_row = QtWidgets.QHBoxLayout(); top_row.setSpacing(18)
        self.freq_value, self.freq_unit = self._readout(top_row, "Frequency", "Hz")
        self.phase_value, _ = self._readout(top_row, "Phase", "deg", small=True)
        top_row.setStretch(0, 1)
        lay.addLayout(top_row)
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
        self.phase_spin = _spin(2, 1.0, "deg"); self.phase_spin.setRange(-180.0, 360.0)
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
        load_lbl = QtWidgets.QLabel("Load")
        grid.addWidget(load_lbl, r, 0)
        grid.addWidget(self.load_combo, r, 1, 1, 3); r += 1
        if not ctrl.caps.get("load_settable", True):
            # (the Analog Discovery's outputs have no load setting)
            load_lbl.hide(); self.load_combo.hide()
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        # When each box was last typed in / scrolled: a box being edited
        # (focused, or touched within _EDIT_HOLD_S) is not overwritten by the
        # refresh. Key and wheel EVENTS, not valueChanged: the code sets the
        # boxes too (seed, ranges), and that is no user edit.
        self._edited: dict = {}
        self._edit_watch = _EditWatch(self._edited)
        for spin in (self.freq_spin, self.amp_spin, self.off_spin, self.phase_spin,
                     self.duty_spin, self.sym_spin):
            spin.installEventFilter(self._edit_watch)
            spin.lineEdit().installEventFilter(self._edit_watch)

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

    def _editing(self, spin) -> bool:
        return spin.hasFocus() or \
            time.monotonic() - self._edited.get(spin, -1e9) < _EDIT_HOLD_S

    def _track(self, spin, value) -> None:
        """Show the instrument's setpoint in a box the user is not editing
        (lab PC 2026-10-07: during a scan of W1's phase the Phase box stayed
        at the value seen when the window opened). blockSignals: a refresh is
        not a user edit and sends nothing (gotcha #13)."""
        if value is None or self._editing(spin) or abs(spin.value() - value) < 1e-12:
            return
        spin.blockSignals(True)
        spin.setValue(value)
        spin.blockSignals(False)

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
        follows = ch == "w2" and bool(s.get("follow"))          # frequency
        phase_follows = ch == "w2" and bool(s.get("phase_follow"))
        shape = (wf, s.get(f"{ch}_load"), follows, phase_follows, s.get("describe_rev"))
        if shape != self._shape:
            self._shape = shape
            self.apply_limits()
            has_freq = wf not in ("dc", "noise")
            for wdg in (self.freq_spin, self.unit_combo, self.freq_btn):
                wdg.setEnabled(has_freq and not follows)
            for wdg in (self.phase_spin, *self.phase_row):
                wdg.setEnabled(has_freq and not phase_follows)
            for wdg in (self.amp_spin, *self.amp_row):
                wdg.setEnabled(wf != "dc")
            self.off_row[0].setText("DC level" if wf == "dc" else "Offset")
            for wdg in (self.duty_spin, *self.duty_row):
                wdg.setVisible(wf == "pulse")
            for wdg in (self.sym_spin, *self.sym_row):
                # only where the instrument has it (not the AFG1062)
                wdg.setVisible(wf == "ramp" and self.ctrl.caps.get("ramp_symmetry", True))
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
            if not self._editing(self.freq_spin) and \
                    abs(self.current_freq_hz() - hz) > 1e-9 * max(hz, 1.0):
                self._set_freq_box(hz)
        # every box shows the instrument's setpoint unless it is being edited
        # (a scan or another client may have changed it)
        for spin, key in ((self.amp_spin, "amplitude_Vpp"), (self.off_spin, "offset_V"),
                          (self.phase_spin, "phase_deg"), (self.duty_spin, "duty_pct"),
                          (self.sym_spin, "symmetry_pct")):
            self._track(spin, _num(s.get(f"{ch}_{key}")))
        ph = _num(s.get(f"{ch}_phase_deg"))
        self.phase_value.setText("-" if ph is None or wf in ("dc", "noise") else f"{ph:.4g}")
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
            notes.append("Frequency and phase follow W1." if phase_follows
                         else "Frequency follows W1; the phase is set here.")
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




# ------------------------------------------------------------- sweep box

#: what a sweep of each knob reads in: (status key, unit, rate unit, cfg name)
_SWEEP_KNOBS = {"frequency": ("frequency_Hz", "Hz", "Hz/s", "freq"),
                "amplitude": ("amplitude_Vpp", "Vpp", "Vpp/s", "amp"),
                "offset": ("offset_V", "V", "V/s", "offset"),
                "phase": ("phase_deg", "deg", "deg/s", "phase")}


class SweepBox(QtWidgets.QFrame):
    """SWEEP a knob at a set pace (ramp_start; fly scans fly the same
    sweeps): channel, knob, target, rate, Sweep, Stop. The output is never
    switched by a sweep; a set of the knob, or Stop, ends it where it is."""

    def __init__(self, ctrl, safe=None):
        super().__init__()
        self.ctrl = ctrl
        self._safe = safe or (lambda fn, *args: fn(*args))
        self.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14); lay.setSpacing(8)
        title = QtWidgets.QLabel("SWEEP"); title.setObjectName("cardTitle")
        lay.addWidget(title)
        row = QtWidgets.QHBoxLayout(); row.setSpacing(8)
        self.ch_combo = QtWidgets.QComboBox()
        for ch in ctrl.channels:
            self.ch_combo.addItem(ch.upper(), ch)
        self.knob_combo = QtWidgets.QComboBox()
        for k in _SWEEP_KNOBS:
            self.knob_combo.addItem(k, k)
        self.to_spin = _spin(6, 1.0)
        self.to_spin.setRange(-1e12, 1e12)
        self.rate_spin = _spin(6, 1.0)
        self.rate_spin.setRange(0.0, 1e12)
        for w in (self.ch_combo, self.knob_combo):
            row.addWidget(w)
        row.addWidget(QtWidgets.QLabel("to")); row.addWidget(self.to_spin, 1)
        row.addWidget(QtWidgets.QLabel("at")); row.addWidget(self.rate_spin, 1)
        self.go_btn = QtWidgets.QPushButton("Sweep"); self.go_btn.setObjectName("primary")
        self.go_btn.clicked.connect(self._go)
        self.stop_btn = QtWidgets.QPushButton("Stop"); self.stop_btn.setObjectName("danger")
        self.stop_btn.clicked.connect(lambda: self._safe(self.ctrl.ramp_stop))
        mark_always(self.stop_btn)          # a stop: a viewer may use it too
        row.addWidget(self.go_btn); row.addWidget(self.stop_btn)
        lay.addLayout(row)
        self.state = QtWidgets.QLabel("idle")
        self.state.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        lay.addWidget(self.state)
        self.ch_combo.currentIndexChanged.connect(lambda _i: self._seed())
        self.knob_combo.currentIndexChanged.connect(lambda _i: self._seed())
        self._seed_rate()
        self._seeded = False

    def _knob(self):
        return self.ch_combo.currentData(), self.knob_combo.currentData()

    def _seed_rate(self):
        _ch, knob = self._knob()
        key, unit, runit, name = _SWEEP_KNOBS[knob]
        hw = getattr(getattr(self.ctrl, "cfg", None), "hardware", None)
        rate = float(getattr(hw, f"ramp_{name}_rate_default", 1.0)) if hw else 1.0
        self.rate_spin.setSuffix("  " + runit)
        self.to_spin.setSuffix("  " + unit)
        self.rate_spin.blockSignals(True); self.rate_spin.setValue(rate)
        self.rate_spin.blockSignals(False)

    def _seed(self, s: dict | None = None):
        """The target box starts at where the knob is now."""
        self._seed_rate()
        ch, knob = self._knob()
        s = s if s is not None else self.ctrl.status()
        v = _num(s.get(f"{ch}_{_SWEEP_KNOBS[knob][0]}"))
        if v is not None:
            self.to_spin.blockSignals(True); self.to_spin.setValue(v)
            self.to_spin.blockSignals(False)

    def _go(self):
        ch, knob = self._knob()
        self._safe(self.ctrl.ramp_start, ch, knob, self.to_spin.value(),
                   self.rate_spin.value())

    def refresh(self, s: dict):
        if not self._seeded and s.get("connected"):
            self._seed(s)
            self._seeded = True
        if s.get("ramping"):
            knob = str(s.get("ramp_knob") or "")
            ch, _, k = knob.partition("_")
            unit = _SWEEP_KNOBS.get(k, ("", "", "", ""))[1]
            self.state.setText(
                f"sweeping {ch.upper()} {k}: {_num(s.get('ramp_value')) or 0:.6g} {unit}"
                f" -> {_num(s.get('ramp_target')) or 0:.6g} {unit} "
                f"(#{s.get('ramp_id')})")
            self.state.setStyleSheet(f"color:{COLORS['accent']}; font-size:11px;")
        else:
            err = s.get("ramp_error") or ""
            self.state.setText(f"idle{'  -- last sweep failed: ' + err if err else ''}")
            self.state.setStyleSheet(
                f"color:{COLORS['danger'] if err else COLORS['muted']}; font-size:11px;")


# ------------------------------------------------------------- this module's own

class GeneratorPanel(QtWidgets.QWidget):
    """Both output cards, the coupling row, the drawing -- and the supplies."""

    def __init__(self, gen, scope, call):
        super().__init__()
        self.gen, self.scope = gen, scope
        self._call = call                     # the window's command wrapper (logs refusals)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 8, 0, 0); outer.setSpacing(10)
        top = QtWidgets.QHBoxLayout(); top.setSpacing(10)
        self.cards = {}
        for ch in gen.channels:
            card = ChannelCard(ch, gen, safe=self._call)
            self.cards[ch] = card
            top.addWidget(card, 1)
        right = QtWidgets.QVBoxLayout(); right.setSpacing(10)
        self.view = OutputsView()
        right.addWidget(self.view, 1)
        right.addWidget(self._build_coupling())
        self.sweep = SweepBox(gen, safe=self._call)
        right.addWidget(self.sweep)
        top.addLayout(right, 2)
        outer.addLayout(top, 1)
        self.supplies_card = self._build_supplies()
        outer.addWidget(self.supplies_card)
        self._seeded = False

    # ---- coupling (W2 follows W1) -------------------------------------------------
    def _build_coupling(self):
        card, lay = _card("Coupling")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(10)
        self.follow_box = QtWidgets.QCheckBox("W2 frequency follows W1")
        self.follow_box.clicked.connect(
            lambda on: self._call(self.gen.set_follow, bool(on), self.offset_spin.value()))
        row.addWidget(self.follow_box)
        self.phase_follow_box = QtWidgets.QCheckBox("Phase follows")
        self.phase_follow_box.clicked.connect(
            lambda on: self._call(self.gen.set_phase_follow, bool(on)))
        row.addWidget(self.phase_follow_box)
        row.addWidget(QtWidgets.QLabel("Offset"))
        self.offset_spin = _spin(2, 1.0, "deg"); self.offset_spin.setRange(-180.0, 360.0)
        row.addWidget(self.offset_spin)
        self.offset_btn = QtWidgets.QPushButton("Set")
        self.offset_btn.clicked.connect(
            lambda: self._call(self.gen.set_phase_offset, self.offset_spin.value()))
        row.addWidget(self.offset_btn)
        lay.addLayout(row)
        row2 = QtWidgets.QHBoxLayout()
        self.align_btn = QtWidgets.QPushButton("Align phase")
        self.align_btn.setToolTip("Restart W1 and W2 together (W2 slaved to W1).")
        self.align_btn.clicked.connect(lambda: self._call(self.gen.align_phase))
        row2.addWidget(self.align_btn)
        off_btn = QtWidgets.QPushButton("All outputs off"); off_btn.setObjectName("danger")
        off_btn.clicked.connect(lambda: self._call(self.gen.outputs_off))
        mark_always(off_btn)                  # the safety verb: a viewer may use it
        row2.addWidget(off_btn); row2.addStretch(1)
        lay.addLayout(row2)
        return card

    # ---- supplies (V+ / V-) ----------------------------------------------------------
    def _build_supplies(self):
        card, lay = _card("Power supplies")
        grid = QtWidgets.QGridLayout(); grid.setHorizontalSpacing(10)
        self.sup = {}
        for r, (k, name) in enumerate((("vplus", "V+"), ("vminus", "V-"))):
            on = QtWidgets.QPushButton(f"{name} on"); on.setObjectName("primary")
            on.setMinimumWidth(90)
            spin = _spin(3, 0.1, "V")
            setb = QtWidgets.QPushButton("Set")
            meas = QtWidgets.QLabel("-")
            on.clicked.connect(lambda _c=False, k=k: self._call(
                self.scope.set_supply, k, not self._sup_on(k), None))
            setb.clicked.connect(lambda _c=False, k=k, sp=spin: self._call(
                self.scope.set_supply, k, None, sp.value()))
            grid.addWidget(QtWidgets.QLabel(name), r, 0)
            grid.addWidget(on, r, 1); grid.addWidget(spin, r, 2); grid.addWidget(setb, r, 3)
            grid.addWidget(meas, r, 4)
            self.sup[k] = {"on": on, "spin": spin, "meas": meas, "state": False}
        grid.setColumnStretch(4, 1)
        lay.addLayout(grid)
        off = QtWidgets.QPushButton("All supplies off"); off.setObjectName("danger")
        off.clicked.connect(lambda: self._call(self.scope.supplies_off))
        mark_always(off)
        self.monitors = QtWidgets.QLabel("")
        self.monitors.setObjectName("hint"); self.monitors.setWordWrap(True)
        row = QtWidgets.QHBoxLayout(); row.addWidget(off); row.addWidget(self.monitors, 1)
        lay.addLayout(row)
        return card

    def _sup_on(self, k) -> bool:
        return bool(self.sup[k]["state"])

    # ---- refresh -----------------------------------------------------------------------
    def refresh(self, scope_status: dict, gen_status: dict):
        s = gen_status
        if not self._seeded and s.get("connected"):
            for card in self.cards.values():
                card.seed_inputs(s)
            self._seeded = True
        for card in self.cards.values():
            card.refresh(s)
        self.view.set_status(s)
        self.sweep.refresh(s)
        follow = bool(s.get("follow"))
        phase_follow = bool(s.get("phase_follow"))
        for box, val in ((self.follow_box, follow),
                         (self.phase_follow_box, bool(s.get("phase_follow_set", True)))):
            if val != box.isChecked():
                box.blockSignals(True); box.setChecked(val); box.blockSignals(False)
        self.phase_follow_box.setEnabled(follow)
        self.offset_spin.setEnabled(phase_follow); self.offset_btn.setEnabled(phase_follow)
        off = _num(s.get("phase_offset_deg"))
        if (off is not None and not self.offset_spin.hasFocus()
                and abs(self.offset_spin.value() - off) > 1e-9):
            # the offset can be set from elsewhere (a scan, a script): show it
            self.offset_spin.blockSignals(True)
            self.offset_spin.setValue(off)
            self.offset_spin.blockSignals(False)
        # supplies
        has = bool(scope_status.get("supplies"))
        self.supplies_card.setVisible(has)
        if has:
            for k, w in self.sup.items():
                on = bool(scope_status.get(f"supply_{k}_on"))
                w["state"] = on
                name = "V+" if k == "vplus" else "V-"
                w["on"].setText(f"{name} off" if on else f"{name} on")
                w["on"].setObjectName("danger" if on else "primary")
                w["on"].style().unpolish(w["on"]); w["on"].style().polish(w["on"])
                v = _num(scope_status.get(f"supply_{k}_V"))
                if v is not None and not w["spin"].hasFocus() and abs(w["spin"].value() - v) > 1e-9:
                    w["spin"].blockSignals(True); w["spin"].setValue(v); w["spin"].blockSignals(False)
                mv, ma = (_num(scope_status.get(f"supply_{k}_meas_V")),
                          _num(scope_status.get(f"supply_{k}_meas_A")))
                w["meas"].setText(("-" if mv is None else f"{mv:.3f} V")
                                  + ("" if ma is None else f"   {ma * 1e3:.1f} mA"))
            mon = scope_status.get("monitors") or {}
            self.monitors.setText("   ".join(f"{k}: {v:.3g}" for k, v in mon.items()
                                             if isinstance(v, (int, float))))
