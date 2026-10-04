"""Front panel for the Windfreak SynthHD PRO v2 two-channel RF synthesizer.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a Synthesizer-like object (a real
in-process Synthesizer, or a WindfreakClient facade for a remote service). It
sends commands (set_rf / set_frequency / set_power / set_phase / set_reference)
and reads a status snapshot (a flat dict: `a_frequency_Hz`, `b_locked`, ...) on
a Qt timer. Events arrive on a Qt signal so they can safely cross into the GUI
thread.

The signature widget is the DualToneIndicator: the two outputs drawn as two
scrolling waves (more cycles = higher frequency, taller = more power), and a
phasor dial on the right showing the phase of A and B. When both channels run
at the SAME frequency the dial holds still and reads their phase difference --
the quadrature-LO use of this instrument; when they differ the B phasor turns
at the beat and the caption gives the frequency offset instead. An unlocked PLL
shows as a ragged red trace, because an unlocked synthesizer is not making the
frequency it says.
"""

from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import Config, REFERENCE_SOURCES
from ..sim_system import build_sim_system
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog
from .control_bar import ControlBar, mark_always
from ..control import ControlRefused

_REF_LABELS = {"internal_10MHz": "Internal 10 MHz",
               "internal_27MHz": "Internal 27 MHz",
               "external": "External"}


# ------------------------------------------------------------- signal bridge

class Bridge(QtCore.QObject):
    """Carries synthesizer events across the thread boundary into the GUI."""
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


def _spin() -> QtWidgets.QDoubleSpinBox:
    """A QDoubleSpinBox pinned to the C locale, whatever the app default is."""
    w = QtWidgets.QDoubleSpinBox()
    w.setLocale(_c_locale())
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


# ------------------------------------------------------------- the indicator

class DualToneIndicator(QtWidgets.QWidget):
    """Two scrolling waves (A and B) and a phasor dial of their phases.

    set_state() takes one dict per channel with keys rf_on, locked, freq_Hz,
    power_dBm, phase_deg, plus the power envelope for scaling the amplitude.
    Colours are read from COLORS at paint time, so the theme is honoured.
    """

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(200)
        self.setMinimumWidth(360)
        self._ch = {c: {"rf_on": False, "locked": False, "freq_Hz": 1e9,
                        "power_dBm": -10.0, "phase_deg": 0.0} for c in "ab"}
        self._pmin, self._pmax = -50.0, 20.0
        self._t = 0.0                 # animation clock (seconds of wall time)
        self._beat = 0.0              # accumulated visual beat angle (rad)
        self._last = time.monotonic()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)   # ~30 fps, only while something radiates
        self._timer.timeout.connect(self._tick)

    def set_state(self, a: dict, b: dict, pmin: float, pmax: float):
        self._ch["a"].update(a)
        self._ch["b"].update(b)
        self._pmin, self._pmax = pmin, pmax
        live = a.get("rf_on") or b.get("rf_on")
        if live and not self._timer.isActive():
            self._last = time.monotonic()
            self._timer.start()
        elif not live and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        now = time.monotonic()
        dt, self._last = now - self._last, now
        self._t += dt
        # the B phasor turns at a VISUAL beat rate: 0 when the frequencies are
        # equal, otherwise ~log of the offset (real beats are far too fast)
        df = self._ch["b"]["freq_Hz"] - self._ch["a"]["freq_Hz"]
        if abs(df) >= 1.0:
            rate = math.copysign(0.4 + 0.25 * math.log10(abs(df)), df)
            self._beat = (self._beat + rate * dt) % (2 * math.pi)
        else:
            self._beat = 0.0
        self.update()

    # -- helpers -----------------------------------------------------------

    def _cycles(self, hz: float) -> float:
        """Cycles drawn across a lane: 2 at 10 MHz up to ~9 at 24 GHz (log)."""
        g = math.log10(max(hz, 1e7) / 1e7) / math.log10(2400.0)
        return 2.0 + 7.0 * max(0.0, min(1.0, g))

    def _amp(self, dBm: float) -> float:
        span = max(1e-6, self._pmax - self._pmin)
        return 0.2 + 0.8 * max(0.0, min(1.0, (dBm - self._pmin) / span))

    @staticmethod
    def _color(ch: str) -> QtGui.QColor:
        return QtGui.QColor(COLORS["accent"] if ch == "a" else COLORS["text"])

    # -- drawing -----------------------------------------------------------

    def paintEvent(self, ev):
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QPainter, QPen, QPainterPath, QColor

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        dial = min(h - 40, w * 0.36)
        lanes_w = w - dial - 36
        lane_h = (h - 30) / 2.0
        f = p.font(); f.setBold(True); f.setPointSize(8); p.setFont(f)

        # ---- the two wave lanes -----------------------------------------
        for row, ch in enumerate("ab"):
            s = self._ch[ch]
            top = 6 + row * lane_h
            mid = top + lane_h / 2.0
            x0, x1 = 34.0, lanes_w
            # lane background and axis
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(COLORS["code_bg"]))
            p.drawRoundedRect(QRectF(x0 - 4, top + 3, x1 - x0 + 8, lane_h - 6), 6, 6)
            p.setPen(QPen(QColor(COLORS["grid"]), 1))
            p.drawLine(QPointF(x0, mid), QPointF(x1, mid))
            # channel letter
            col = self._color(ch)
            p.setPen(col if s["rf_on"] else QColor(COLORS["muted"]))
            p.drawText(QRectF(4, mid - 9, 24, 18), Qt.AlignCenter, ch.upper())

            if not s["rf_on"]:
                p.setPen(QPen(QColor(COLORS["muted"]), 1.4, Qt.DashLine))
                p.drawLine(QPointF(x0, mid), QPointF(x1, mid))
                p.setPen(QColor(COLORS["muted"]))
                p.drawText(QRectF(x0, top + 4, x1 - x0, 14), Qt.AlignRight, "RF off  ")
                continue

            amp = self._amp(s["power_dBm"]) * (lane_h / 2.0 - 8)
            cyc = self._cycles(s["freq_Hz"])
            ph = math.radians(s["phase_deg"])
            path = QPainterPath()
            n = 160
            unlocked = not s["locked"]
            for i in range(n + 1):
                u = i / n
                arg = 2 * math.pi * (cyc * u - 0.6 * self._t) + ph
                y = mid - amp * math.sin(arg)
                if unlocked:
                    # a ragged trace: the frequency is not what it says
                    y += amp * 0.35 * math.sin(37.0 * u + 11.0 * self._t) * math.sin(5.0 * u)
                pt = QPointF(x0 + u * (x1 - x0), y)
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            pen = QPen(QColor(COLORS["danger"]) if unlocked else col, 2.2)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            p.setPen(QColor(COLORS["danger"]) if unlocked else QColor(COLORS["muted"]))
            label = "NOT LOCKED  " if unlocked else f"{s['freq_Hz']/1e9:.4f} GHz  "
            p.drawText(QRectF(x0, top + 4, x1 - x0, 14), Qt.AlignRight, label)

        # ---- the phasor dial --------------------------------------------
        cx = w - dial / 2.0 - 12
        cy = 6 + dial / 2.0
        r = dial / 2.0 - 4
        p.setBrush(QColor(COLORS["code_bg"]))
        p.setPen(QPen(QColor(COLORS["border"]), 1.2))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setPen(QPen(QColor(COLORS["grid"]), 1))
        p.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
        p.drawLine(QPointF(cx, cy - r), QPointF(cx, cy + r))
        a, b = self._ch["a"], self._ch["b"]
        same_f = abs(a["freq_Hz"] - b["freq_Hz"]) < 1.0
        for ch, extra in (("a", 0.0), ("b", self._beat)):
            s = self._ch[ch]
            if not s["rf_on"]:
                continue
            ang = math.radians(s["phase_deg"]) + extra
            length = r * (0.45 + 0.5 * self._amp(s["power_dBm"]))
            tip = QPointF(cx + length * math.cos(ang), cy - length * math.sin(ang))
            p.setPen(QPen(self._color(ch), 3.0, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(cx, cy), tip)
            p.setPen(Qt.NoPen); p.setBrush(self._color(ch))
            p.drawEllipse(tip, 3.5, 3.5)
        p.setBrush(QColor(COLORS["muted"])); p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), 2.5, 2.5)

        # caption under the dial
        if a["rf_on"] and b["rf_on"]:
            if same_f:
                d = (b["phase_deg"] - a["phase_deg"]) % 360.0
                cap, col = f"B - A = {d:.1f} deg", QColor(COLORS["accent_hi"])
            else:
                df = (b["freq_Hz"] - a["freq_Hz"]) / 1e6
                cap, col = f"beat {df:+.4f} MHz", QColor(COLORS["muted"])
        else:
            cap, col = "phase of A and B", QColor(COLORS["muted"])
        p.setPen(col)
        p.drawText(QRectF(cx - dial / 2 - 20, cy + r + 6, dial + 40, 16),
                   Qt.AlignHCenter, cap)
        p.end()


# ------------------------------------------------------------- channel card

_FREQ_UNITS = {"MHz": 1e6, "GHz": 1e9}


class ChannelCard(QtWidgets.QFrame):
    """The controls and readouts of ONE output channel."""

    def __init__(self, ch: str, ctrl, cfg: Config, safe=None):
        super().__init__()
        self.ch, self.ctrl, self.cfg = ch, ctrl, cfg
        # how a command is run: the main window's _safe (a refusal because
        # another PC holds control goes to its log); plain call otherwise
        self._safe = safe or (lambda fn, *args: fn(*args))
        self.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(9)

        # header: channel name, lamps
        top = QtWidgets.QHBoxLayout()
        name = QtWidgets.QLabel(f"CHANNEL {ch.upper()}  -  RFout{ch.upper()}")
        name.setObjectName("cardTitle")
        top.addWidget(name); top.addStretch(1)
        self.lock_lamp = QtWidgets.QLabel("LOCK")
        self.level_lamp = QtWidgets.QLabel("LEVEL")
        for lamp in (self.lock_lamp, self.level_lamp):
            lamp.setAlignment(QtCore.Qt.AlignCenter)
            lamp.setMinimumWidth(52)
            top.addWidget(lamp)
        lay.addLayout(top)

        # readouts
        self.freq_value = self._readout(lay, "Frequency", "MHz")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(18)
        self.power_value = self._readout(row, "Power", "dBm", small=True)
        self.phase_value = self._readout(row, "Phase", "deg", small=True)
        row.addStretch(1)
        lay.addLayout(row)

        # RF toggle
        self.rf_btn = QtWidgets.QPushButton("Turn RF On")
        self.rf_btn.setObjectName("primary")
        self.rf_btn.setMinimumHeight(38)
        self.rf_btn.clicked.connect(self._toggle_rf)
        lay.addWidget(self.rf_btn)
        self._rf_on = None

        lim = cfg.limits
        start = cfg.channel(ch)
        # frequency: spin + unit + Set
        frow = QtWidgets.QHBoxLayout()
        self.freq_spin = _spin()
        self.freq_spin.setDecimals(6)   # 1 Hz shown in MHz; the unit sits in the combo
        self.unit_combo = QtWidgets.QComboBox()
        self.unit_combo.addItems(list(_FREQ_UNITS))
        self._unit = "MHz"
        self.unit_combo.currentTextChanged.connect(self._change_unit)
        b = QtWidgets.QPushButton("Set"); b.clicked.connect(self._set_frequency)
        frow.addWidget(QtWidgets.QLabel("Freq")); frow.addWidget(self.freq_spin, 1)
        frow.addWidget(self.unit_combo); frow.addWidget(b)
        lay.addLayout(frow)
        self.apply_limits(initial_hz=start.frequency_Hz)

        prow = QtWidgets.QHBoxLayout()
        self.power_spin = _spin()
        self.power_spin.setDecimals(2); self.power_spin.setSingleStep(0.5)
        self.power_spin.setSuffix("  dBm")
        self.power_spin.setRange(lim.power_min_dBm, lim.power_max_dBm)
        self.power_spin.setValue(start.power_dBm)
        b = QtWidgets.QPushButton("Set"); b.clicked.connect(self._set_power)
        prow.addWidget(QtWidgets.QLabel("Power")); prow.addWidget(self.power_spin, 1)
        prow.addWidget(b)
        lay.addLayout(prow)

        hrow = QtWidgets.QHBoxLayout()
        self.phase_spin = _spin()
        self.phase_spin.setDecimals(2); self.phase_spin.setSingleStep(1.0)
        self.phase_spin.setSuffix("  deg")
        self.phase_spin.setRange(lim.phase_min_deg, lim.phase_max_deg)
        self.phase_spin.setValue(start.phase_deg)
        b = QtWidgets.QPushButton("Set"); b.clicked.connect(self._set_phase)
        hrow.addWidget(QtWidgets.QLabel("Phase")); hrow.addWidget(self.phase_spin, 1)
        hrow.addWidget(b)
        lay.addLayout(hrow)
        self.readback = QtWidgets.QLabel("readback -")
        self.readback.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        lay.addWidget(self.readback)

    def _readout(self, parent, label, unit, small=False):
        box = QtWidgets.QVBoxLayout(); box.setSpacing(1)
        cap = QtWidgets.QLabel(label.upper())
        cap.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; font-weight:700; "
                          f"letter-spacing:1px;")
        line = QtWidgets.QHBoxLayout(); line.setSpacing(5)
        val = QtWidgets.QLabel("-")
        if small:
            val.setStyleSheet(f"color:{COLORS['text']}; font-size:20px; font-weight:700;")
        else:
            val.setObjectName("bigValue")
        u = QtWidgets.QLabel(unit); u.setObjectName("unit")
        line.addWidget(val); line.addWidget(u, 0, QtCore.Qt.AlignBottom); line.addStretch(1)
        box.addWidget(cap); box.addLayout(line)
        holder = QtWidgets.QWidget(); holder.setLayout(box)
        parent.addWidget(holder)
        return val

    # ---- frequency unit handling ----------------------------------------

    def apply_limits(self, initial_hz=None):
        """Set the spin ranges from the (possibly new) limits, keeping values."""
        lim = self.cfg.limits
        scale = _FREQ_UNITS[self._unit]
        cur_hz = initial_hz if initial_hz is not None else self.current_freq_hz()
        self.freq_spin.blockSignals(True)
        # 1 Hz resolution in either unit (decimals BEFORE the value, or Qt
        # rounds the value to the old number of decimals)
        self.freq_spin.setDecimals(6 if self._unit == "MHz" else 9)
        self.freq_spin.setRange(lim.freq_min_Hz / scale, lim.freq_max_Hz / scale)
        self.freq_spin.setSingleStep(1.0 if self._unit == "MHz" else 0.001)
        self.freq_spin.setValue(cur_hz / scale)
        self.freq_spin.blockSignals(False)
        if hasattr(self, "power_spin"):
            self.power_spin.setRange(lim.power_min_dBm, lim.power_max_dBm)
            self.phase_spin.setRange(lim.phase_min_deg, lim.phase_max_deg)

    def seed_inputs(self):
        """Put the instrument's values into the input boxes. Called once after
        the service/brain has started: the boxes were built from the config
        BEFORE the start read the instrument (read-only start), and a "Set"
        pressed on a stale box would send a value nobody chose."""
        c = self.cfg.channel(self.ch)
        self.apply_limits(initial_hz=c.frequency_Hz)
        self.power_spin.setValue(c.power_dBm)
        self.phase_spin.setValue(c.phase_deg)

    def current_freq_hz(self) -> float:
        return self.freq_spin.value() * _FREQ_UNITS[self._unit]

    def _change_unit(self, unit: str):
        hz = self.current_freq_hz()
        self._unit = unit
        self.apply_limits(initial_hz=hz)

    # ---- actions ---------------------------------------------------------

    def _toggle_rf(self):
        self._safe(self.ctrl.set_rf, self.ch, not bool(self._rf_on))

    def _set_frequency(self):
        self._safe(self.ctrl.set_frequency, self.ch, self.current_freq_hz())

    def _set_power(self):
        self._safe(self.ctrl.set_power, self.ch, self.power_spin.value())

    def _set_phase(self):
        self._safe(self.ctrl.set_phase, self.ch, self.phase_spin.value())

    # ---- refresh ---------------------------------------------------------

    def refresh(self, s: dict):
        ch = self.ch
        f = _num(s.get(f"{ch}_frequency_Hz")) or 0.0
        self.freq_value.setText(f"{f/1e6:.6f}")
        self.power_value.setText(f"{_num(s.get(f'{ch}_power_dBm')) or 0.0:.2f}")
        self.phase_value.setText(f"{_num(s.get(f'{ch}_phase_deg')) or 0.0:.2f}")
        fa = _num(s.get(f"{ch}_frequency_actual_Hz"))
        self.readback.setText("instrument reports -" if fa is None else
                              f"instrument reports {fa/1e6:.7f} MHz")
        rf_on = bool(s.get(f"{ch}_rf_on"))
        if rf_on != self._rf_on:
            self._rf_on = rf_on
            self.rf_btn.setText("Turn RF Off" if rf_on else "Turn RF On")
            self.rf_btn.setObjectName("danger" if rf_on else "primary")
            self.rf_btn.style().unpolish(self.rf_btn)
            self.rf_btn.style().polish(self.rf_btn)
        powered = bool(s.get(f"{ch}_pll_on"))
        self._lamp(self.lock_lamp, "LOCK", bool(s.get(f"{ch}_locked")), powered)
        self._lamp(self.level_lamp, "LEVEL", bool(s.get(f"{ch}_leveled")), rf_on)

    @staticmethod
    def _lamp(label, text, ok: bool, relevant: bool):
        """Green when fine, red when it matters and is not fine, grey otherwise."""
        color = COLORS["ok"] if ok else (COLORS["danger"] if relevant else COLORS["muted"])
        label.setText(text)
        label.setStyleSheet(f"color:{color}; border:1px solid {color}; border-radius:8px; "
                            f"padding:2px 6px; font-size:10px; font-weight:700;")


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        # Number widgets follow the Windows locale otherwise: "2,500.000" or
        # "2500,000" (gotcha #18). C locale, no group separator, everywhere.
        QtCore.QLocale.setDefault(_c_locale())
        self.setLocale(_c_locale())
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        title = "Windfreak SynthHD PRO v2 - RF synthesizer"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1280, 760)

        root = QtWidgets.QWidget(); root.setObjectName("root")
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(14)
        outer.addLayout(self._build_header())

        mid = QtWidgets.QHBoxLayout(); mid.setSpacing(14)
        self.cards = {ch: ChannelCard(ch, ctrl, cfg, safe=self._safe) for ch in "ab"}
        for card in self.cards.values():
            card.setFixedWidth(340)
            mid.addWidget(card, 0)
        icard, ilay = _card("Outputs")
        self.tone = DualToneIndicator()
        ilay.addWidget(self.tone, 1)
        mid.addWidget(icard, 1)
        outer.addLayout(mid, 0)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(110)
        llay.addWidget(self.log)
        outer.addWidget(lcard, 1)

        # Control or viewer (control_bar.py): a bar across the top, only for a
        # GUI on a service whose client knows about control -- a local GUI
        # owns its synthesizer and has nobody to share it with.
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

        # synthesizer events -> log
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

        # start the synthesizer (opens the backend and READS its state --
        # nothing is changed, a running output keeps running) and the timer
        self.ctrl.start()
        # the start copied the instrument's state into cfg: show it in the
        # input boxes too (locally; a remote client pulled the service's cfg)
        for card in self.cards.values():
            card.seed_inputs()
        self.ext_spin.setValue(self.cfg.reference.ext_MHz)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

        # The first GUI to connect gets control; a later one opens as a viewer
        # (control_bar.py). Only once the log exists, so the bar can say so.
        if self._control_bar is not None:
            self._control_bar.claim_if_free()

    # ---- layout ----------------------------------------------------------

    def _build_header(self):
        row = QtWidgets.QHBoxLayout(); row.setSpacing(12)
        title = QtWidgets.QLabel("SYNTHHD PRO")
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

        row.addWidget(QtWidgets.QLabel("Reference"))
        self.ref_combo = QtWidgets.QComboBox()
        for src in REFERENCE_SOURCES:
            self.ref_combo.addItem(_REF_LABELS[src], src)
        # `activated` fires only on a USER choice, so refreshing the combo from
        # status can never send a command back (gotcha #13)
        self.ref_combo.activated.connect(self._set_reference)
        row.addWidget(self.ref_combo)
        self.ext_spin = _spin()
        self.ext_spin.setDecimals(3); self.ext_spin.setSuffix("  MHz")
        self.ext_spin.setRange(self.cfg.limits.ext_ref_min_MHz, self.cfg.limits.ext_ref_max_MHz)
        self.ext_spin.setValue(self.cfg.reference.ext_MHz)
        self.ext_spin.setToolTip("Frequency of the signal on REF IN (external reference only)")
        row.addWidget(self.ext_spin)
        self.ext_btn = QtWidgets.QPushButton("Set")
        self.ext_btn.clicked.connect(
            lambda: self._safe(self.ctrl.set_ext_ref, self.ext_spin.value()))
        row.addWidget(self.ext_btn)
        self.temp_label = QtWidgets.QLabel("- C")
        self.temp_label.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        row.addWidget(self.temp_label)

        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        mark_always(settings_btn)    # a viewer may LOOK; the service refuses the OK
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        row.addWidget(settings_btn)
        off_btn = QtWidgets.QPushButton("All RF off"); off_btn.setObjectName("danger")
        # the SAFETY verb (net/service.py): works for a viewer too
        off_btn.clicked.connect(lambda: self._safe(self.ctrl.all_rf_off))
        mark_always(off_btn)
        row.addWidget(off_btn)
        return row

    # ---- actions ---------------------------------------------------------

    def _set_reference(self, index: int):
        source = self.ref_combo.itemData(index)
        ext = self.ext_spin.value() if source == "external" else None
        self._safe(self.ctrl.set_reference, source, ext)

    def _safe(self, fn, *args):
        """Run a command; a refusal because another PC holds control goes to
        the log (the service emits no event for that; normally the viewer
        guard of the control bar stops the click before it gets here)."""
        try:
            fn(*args)
        except ControlRefused as exc:
            self._on_event("error", str(exc))

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        for card in self.cards.values():
            card.apply_limits()
        self.ext_spin.setRange(self.cfg.limits.ext_ref_min_MHz, self.cfg.limits.ext_ref_max_MHz)

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

        src = s.get("reference")
        if src in REFERENCE_SOURCES and not self.ref_combo.view().isVisible():
            i = self.ref_combo.findData(src)
            if i != self.ref_combo.currentIndex():
                self.ref_combo.blockSignals(True)
                self.ref_combo.setCurrentIndex(i)
                self.ref_combo.blockSignals(False)
        external = src == "external"
        self.ext_spin.setEnabled(external); self.ext_btn.setEnabled(external)

        t = _num(s.get("temperature_C"))
        hot = t is not None and t > self.cfg.hardware.temp_warn_C
        self.temp_label.setText("- C" if t is None else f"{t:.1f} C")
        self.temp_label.setStyleSheet(
            f"color:{COLORS['danger'] if hot else COLORS['muted']}; font-weight:600;")

        lim = self.cfg.limits
        chans = []
        for ch in "ab":
            chans.append({"rf_on": bool(s.get(f"{ch}_rf_on")),
                          "locked": bool(s.get(f"{ch}_locked")),
                          "freq_Hz": _num(s.get(f"{ch}_frequency_Hz")) or 1e9,
                          "power_dBm": _num(s.get(f"{ch}_power_dBm")) or 0.0,
                          "phase_deg": _num(s.get(f"{ch}_phase_deg")) or 0.0})
        self.tone.set_state(chans[0], chans[1], lim.power_min_dBm, lim.power_max_dBm)

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()          # local: both RF off + disconnect; remote: close client
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Synthesizer-like object (a real in-process
    Synthesizer, or a WindfreakClient facade for a remote service). The theme is
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
    synth, _ = build_sim_system(cfg)
    return run_app(synth, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
