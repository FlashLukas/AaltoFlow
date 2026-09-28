"""Control GUI for the DS Instruments SG12000L microwave signal generator.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a Synthesizer-like object (a real
in-process Synthesizer, or a DssgClient facade for a remote service). It sends
commands (set_rf / set_frequency / set_power / set_phase / set_reference) and
reads the status SNAPSHOT on a Qt timer to update the numbers and the spectrum
glyph. Brain events arrive on a Qt signal so they can safely cross into the GUI
thread.

The signature widget is the SpectrumIndicator: a miniature spectrum-analyser
screen on a logarithmic frequency axis. The unit's usable band is shaded, and
when RF is on the carrier stands up out of the noise floor at its frequency and
power, with its 2nd and 3rd harmonics below it (the SG series is unfiltered, so
they are really there). A small dial in the corner shows the phase setting.
"""

from __future__ import annotations

import math
import random
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import Config, REFERENCES
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
    if title:
        lbl = QtWidgets.QLabel(title.upper())
        lbl.setObjectName("cardTitle")
        lay.addWidget(lbl)
    return frame, lay


def _fmt_freq(hz: float) -> str:
    """Engineering notation a physicist reads at a glance: 2.45 GHz, 400 MHz."""
    if hz >= 1e9:
        return f"{hz / 1e9:.6g} GHz"
    if hz >= 1e6:
        return f"{hz / 1e6:.6g} MHz"
    return f"{hz / 1e3:.6g} kHz"


# ------------------------------------------------------------- the spectrum

class SpectrumIndicator(QtWidgets.QWidget):
    """A tiny spectrum-analyser screen: what is coming out of the SMA port.

    x: log10 frequency, 10 MHz .. 40 GHz (so the harmonics of a 12 GHz carrier
       still fit); y: power in dBm, -90 .. +20.
    Harmonic levels are illustrative typical values for an unfiltered
    fractional-N source (2nd ~ -25 dBc, 3rd ~ -35 dBc), not a measurement.
    """

    F_LO, F_HI = 1e7, 4e10
    P_LO, P_HI = -90.0, 20.0
    FLOOR_DBM = -78.0

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(180)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self._on = False
        self._f = 1e9
        self._p = -20.0
        self._phase = 0.0
        self._has_phase = True
        self._band = (25e6, 12e9)
        self._rng = random.Random(7)
        self._floor = [self._rng.gauss(0.0, 1.6) for _ in range(160)]
        # the noise floor "breathes" only while RF is on, on its own timer, so
        # the animation is smooth regardless of how often status() is polled
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(66)
        self._timer.timeout.connect(self._tick)

    def set_state(self, rf_on: bool, f_hz: float, p_dbm: float, phase_deg: float,
                  has_phase: bool, band: tuple[float, float]):
        self._on = bool(rf_on)
        self._f, self._p = float(f_hz), float(p_dbm)
        self._phase, self._has_phase = float(phase_deg), bool(has_phase)
        if band[1] > band[0] > 0:
            self._band = band
        if self._on and not self._timer.isActive():
            self._timer.start()
        elif not self._on and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        # replace a few floor samples each frame: a noise floor that twinkles
        for _ in range(12):
            i = self._rng.randrange(len(self._floor))
            self._floor[i] = self._rng.gauss(0.0, 1.6)
        self.update()

    # -- geometry helpers ----------------------------------------------------

    def _x(self, f, r: QtCore.QRectF) -> float:
        lo, hi = math.log10(self.F_LO), math.log10(self.F_HI)
        t = (math.log10(max(f, self.F_LO)) - lo) / (hi - lo)
        return r.left() + min(max(t, 0.0), 1.0) * r.width()

    def _y(self, p, r: QtCore.QRectF) -> float:
        t = (p - self.P_LO) / (self.P_HI - self.P_LO)
        return r.bottom() - min(max(t, 0.0), 1.0) * r.height()

    # -- drawing -----------------------------------------------------------

    def paintEvent(self, ev):
        from PySide6.QtGui import QPainter, QColor, QPen, QPainterPath
        from PySide6.QtCore import QRectF, QPointF, Qt

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        # colours are read HERE, at paint time, so the widget follows the theme
        c_bg, c_grid = QColor(COLORS["code_bg"]), QColor(COLORS["grid"])
        c_border, c_muted = QColor(COLORS["border"]), QColor(COLORS["muted"])
        c_acc, c_hi, c_text = (QColor(COLORS["accent"]), QColor(COLORS["accent_hi"]),
                               QColor(COLORS["text"]))

        screen = QRectF(0.5, 0.5, w - 1, h - 1)
        p.setPen(QPen(c_border, 1)); p.setBrush(c_bg)
        p.drawRoundedRect(screen, 8, 8)
        r = QRectF(40, 12, w - 52, h - 34)          # the plot area

        f = p.font(); f.setPointSize(7); p.setFont(f)

        # usable band of this unit, shaded
        band = QColor(c_acc); band.setAlpha(28)
        x0, x1 = self._x(self._band[0], r), self._x(self._band[1], r)
        p.fillRect(QRectF(x0, r.top(), x1 - x0, r.height()), band)

        # grid: decades in x, 20 dB in y
        p.setPen(QPen(c_grid, 1))
        for dec, lab in ((1e8, "100M"), (1e9, "1G"), (1e10, "10G")):
            x = self._x(dec, r)
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        for dbm in range(-80, 21, 20):
            y = self._y(dbm, r)
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(c_muted)
        for dec, lab in ((1e8, "100 MHz"), (1e9, "1 GHz"), (1e10, "10 GHz")):
            x = self._x(dec, r)
            p.drawText(QRectF(x - 30, r.bottom() + 3, 60, 12), Qt.AlignHCenter, lab)
        for dbm in (-80, -40, 0):
            y = self._y(dbm, r)
            p.drawText(QRectF(2, y - 6, 34, 12), Qt.AlignRight | Qt.AlignVCenter, f"{dbm}")
        p.drawText(QRectF(2, r.top() - 11, 36, 10), Qt.AlignRight, "dBm")

        # noise floor
        path = QPainterPath()
        n = len(self._floor)
        for i, dv in enumerate(self._floor):
            x = r.left() + r.width() * i / (n - 1)
            y = self._y(self.FLOOR_DBM + dv, r)
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        p.setPen(QPen(c_muted, 1.0)); p.setBrush(Qt.NoBrush)
        p.drawPath(path)

        if self._on:
            # carrier + two harmonics: vertical spectral lines out of the floor
            lines = [(self._f, self._p, 1.0), (2 * self._f, self._p - 25.0, 0.45),
                     (3 * self._f, self._p - 35.0, 0.30)]
            for freq, lvl, strength in lines:
                if freq > self.F_HI:
                    continue
                x = self._x(freq, r)
                y_top, y_bot = self._y(lvl, r), self._y(self.FLOOR_DBM, r)
                glow = QColor(c_acc); glow.setAlpha(int(60 * strength))
                p.setPen(QPen(glow, 7)); p.drawLine(QPointF(x, y_bot), QPointF(x, y_top))
                core = QColor(c_hi if strength == 1.0 else c_acc)
                core.setAlpha(int(255 * max(strength, 0.5)))
                p.setPen(QPen(core, 2)); p.drawLine(QPointF(x, y_bot), QPointF(x, y_top))
            # marker on the carrier peak
            x, y = self._x(self._f, r), self._y(self._p, r)
            p.setPen(Qt.NoPen); p.setBrush(c_hi)
            p.drawPolygon([QPointF(x, y - 2), QPointF(x - 5, y - 10), QPointF(x + 5, y - 10)])
            p.setPen(c_text)
            f.setBold(True); p.setFont(f)
            label = f"{_fmt_freq(self._f)}  {self._p:+.1f} dBm"
            tx = min(max(x - 70, r.left()), r.right() - 140)
            p.drawText(QRectF(tx, y - 24, 140, 12), Qt.AlignHCenter, label)
        else:
            p.setPen(c_muted)
            f.setBold(True); f.setPointSize(9); p.setFont(f)
            p.drawText(r, Qt.AlignCenter, "RF off")

        # phase dial, top-right corner
        if self._has_phase:
            rad = 15.0
            c = QPointF(r.right() - rad - 4, r.top() + rad + 4)
            p.setPen(QPen(c_border, 1.4)); p.setBrush(QColor(COLORS["panel_hi"]))
            p.drawEllipse(c, rad, rad)
            a = math.radians(self._phase)
            tip = QPointF(c.x() + rad * 0.85 * math.cos(a), c.y() - rad * 0.85 * math.sin(a))
            p.setPen(QPen(c_acc if self._on else c_muted, 2)); p.drawLine(c, tip)
            p.setPen(c_muted)
            f.setBold(False); f.setPointSize(7); p.setFont(f)
            p.drawText(QRectF(c.x() - 40, c.y() + rad + 1, 80, 11), Qt.AlignHCenter,
                       f"phase {self._phase:.0f} deg")
        p.end()


# ------------------------------------------------------------- main window

_FREQ_UNITS = {"MHz": 1e6, "GHz": 1e9}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self._freq_unit = "MHz"
        self._prev_scale = _FREQ_UNITS[self._freq_unit]
        title = "SG12000L - Microwave Signal Generator"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1080, 700)

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

        # start the brain (opens the backend) and the refresh timer
        if not remote:
            self.ctrl.start()
        self._apply_limits()
        self._seed_from_status()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

    # ---- layout ----------------------------------------------------------

    def _build_sidebar(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(360)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(14)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("SG12000L")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
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
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        self.conn_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        top.addWidget(self.conn_dot)
        rlay.addLayout(top)
        self.idn_label = QtWidgets.QLabel("—")
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        self.idn_label.setWordWrap(True)
        rlay.addWidget(self.idn_label)
        col.addWidget(rcard)

        self.rf_btn = QtWidgets.QPushButton("Turn RF On")
        self.rf_btn.setObjectName("primary")
        self.rf_btn.setMinimumHeight(44)
        self.rf_btn.clicked.connect(self._toggle_rf)
        col.addWidget(self.rf_btn)
        self._rf_on = False

        # frequency (spin + unit + set)
        fcard, flay = _card("Frequency")
        frow = QtWidgets.QHBoxLayout()
        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setDecimals(6)
        self.unit_combo = QtWidgets.QComboBox()
        self.unit_combo.addItems(list(_FREQ_UNITS.keys()))
        self.unit_combo.setCurrentText(self._freq_unit)
        self.unit_combo.currentTextChanged.connect(self._change_freq_unit)
        set_freq = QtWidgets.QPushButton("Set"); set_freq.setObjectName("primary")
        set_freq.clicked.connect(self._set_frequency)
        frow.addWidget(self.freq_spin, 1); frow.addWidget(self.unit_combo); frow.addWidget(set_freq)
        flay.addLayout(frow)
        col.addWidget(fcard)

        # power
        pcard, play = _card("Power level")
        prow = QtWidgets.QHBoxLayout()
        self.power_spin = QtWidgets.QDoubleSpinBox()
        self.power_spin.setDecimals(2)
        self.power_spin.setSingleStep(self.cfg.hardware.power_step_dB or 0.5)
        self.power_spin.setSuffix("  dBm")
        set_pow = QtWidgets.QPushButton("Set"); set_pow.setObjectName("primary")
        set_pow.clicked.connect(self._set_power)
        prow.addWidget(self.power_spin, 1); prow.addWidget(set_pow)
        play.addLayout(prow)
        self.power_hint = QtWidgets.QLabel("")
        self.power_hint.setObjectName("hint")
        play.addWidget(self.power_hint)
        col.addWidget(pcard)

        # phase
        phcard, phlay = _card("Phase")
        phrow = QtWidgets.QHBoxLayout()
        self.phase_spin = QtWidgets.QDoubleSpinBox()
        self.phase_spin.setDecimals(2); self.phase_spin.setSingleStep(1.0)
        self.phase_spin.setSuffix("  deg")
        self.set_ph = QtWidgets.QPushButton("Set"); self.set_ph.setObjectName("primary")
        self.set_ph.clicked.connect(self._set_phase)
        phrow.addWidget(self.phase_spin, 1); phrow.addWidget(self.set_ph)
        phlay.addLayout(phrow)
        col.addWidget(phcard)

        # 10 MHz reference
        refcard, reflay = _card("10 MHz reference")
        refrow = QtWidgets.QHBoxLayout()
        self.ref_combo = QtWidgets.QComboBox()
        self.ref_combo.addItems(list(REFERENCES))
        self.ref_combo.setCurrentText(self.cfg.signal.reference)
        set_ref = QtWidgets.QPushButton("Set"); set_ref.setObjectName("primary")
        set_ref.clicked.connect(self._set_reference)
        refrow.addWidget(self.ref_combo, 1); refrow.addWidget(set_ref)
        reflay.addLayout(refrow)
        self.ext_ref_label = QtWidgets.QLabel("external input: -")
        self.ext_ref_label.setObjectName("hint")
        reflay.addWidget(self.ext_ref_label)
        col.addWidget(refcard)

        col.addStretch(1)
        off_btn = QtWidgets.QPushButton("RF Off"); off_btn.setObjectName("danger")
        off_btn.setMinimumHeight(38)
        off_btn.clicked.connect(lambda: self._do(self.ctrl.set_rf, False))
        col.addWidget(off_btn)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        ocard, olay = _card("Output (read back from the unit)")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(24)
        self.freq_value = self._readout(row, "Frequency", "MHz", minw=210)
        self.power_value = self._readout(row, "Power", "dBm", minw=100)
        self.phase_value = self._readout(row, "Phase", "deg", minw=90)
        self.volts_value = self._readout(row, "USB supply", "V", minw=70)
        row.addStretch(1)
        olay.addLayout(row)
        self.spectrum = SpectrumIndicator()
        olay.addWidget(self.spectrum)
        colw.addWidget(ocard)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(120)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 1)
        return panel

    def _readout(self, row, label, unit, minw=120):
        box = QtWidgets.QVBoxLayout(); box.setSpacing(2)
        cap = QtWidgets.QLabel(label.upper())
        cap.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; font-weight:700; letter-spacing:1px;")
        line = QtWidgets.QHBoxLayout(); line.setSpacing(5)
        val = QtWidgets.QLabel("—"); val.setObjectName("bigValue")
        val.setMinimumWidth(minw)
        u = QtWidgets.QLabel(unit); u.setObjectName("unit")
        line.addWidget(val); line.addWidget(u, 0, QtCore.Qt.AlignBottom)
        box.addWidget(cap); box.addLayout(line)
        holder = QtWidgets.QWidget(); holder.setLayout(box)
        row.addWidget(holder)
        return val

    # ---- limits: the EFFECTIVE envelope (cfg AND the unit's own range) ------

    def _apply_limits(self):
        lim = self.ctrl.limits()
        self._lim = lim
        self.power_spin.setRange(lim["power_min_dBm"], lim["power_max_dBm"])
        self.power_hint.setText(f"allowed {lim['power_min_dBm']:g} .. "
                                f"{lim['power_max_dBm']:g} dBm, "
                                f"{self.cfg.hardware.power_step_dB:g} dB steps")
        self.phase_spin.setRange(lim["phase_min_deg"], lim["phase_max_deg"])
        self._apply_freq_unit_range(initial_hz=self._current_freq_hz())

    def _seed_from_status(self):
        """Fill the entry fields with what the unit is ACTUALLY doing, once, at
        start: a spin box showing a value the instrument does not hold invites
        a surprise when someone presses Set on a different field."""
        s = self.ctrl.status()
        self.freq_spin.setValue(s.frequency_Hz / _FREQ_UNITS[self._freq_unit])
        self.power_spin.setValue(s.power_dBm)
        self.phase_spin.setValue(s.phase_deg)
        self.ref_combo.setCurrentText(s.reference)

    def _apply_freq_unit_range(self, initial_hz=None):
        """Set the freq spin's range/step for the current unit, preserving the Hz."""
        scale = _FREQ_UNITS[self._freq_unit]
        cur_hz = initial_hz if initial_hz is not None else self.freq_spin.value() * self._prev_scale
        lim = getattr(self, "_lim", None) or self.ctrl.limits()
        self.freq_spin.blockSignals(True)
        self.freq_spin.setRange(lim["freq_min_Hz"] / scale, lim["freq_max_Hz"] / scale)
        self.freq_spin.setSingleStep({"MHz": 1.0, "GHz": 0.001}[self._freq_unit])
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

    def _do(self, fn, *args):
        """Run one command; a refusal goes to the log instead of a traceback."""
        try:
            fn(*args)
        except Exception as exc:
            self._on_event("error", str(exc))

    def _toggle_rf(self):
        self._do(self.ctrl.set_rf, not self._rf_on)

    def _set_frequency(self):
        self._do(self.ctrl.set_frequency, self._current_freq_hz())

    def _set_power(self):
        self._do(self.ctrl.set_power, self.power_spin.value())

    def _set_phase(self):
        self._do(self.ctrl.set_phase, self.phase_spin.value())

    def _set_reference(self):
        self._do(self.ctrl.set_reference, self.ref_combo.currentText())

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        self._apply_limits()

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
        self._rf_on = bool(s.rf_on)

        self.freq_value.setText(f"{s.frequency_Hz / 1e6:.6f}")
        self.power_value.setText(f"{s.power_dBm:.2f}")
        self.phase_value.setText(f"{s.phase_deg:.2f}" if s.has_phase else "n/a")
        self.volts_value.setText(f"{s.usb_volts:.2f}" if s.connected else "—")
        self.phase_spin.setEnabled(bool(s.has_phase))
        self.set_ph.setEnabled(bool(s.has_phase))
        self.ext_ref_label.setText(
            f"reference in use: {s.reference}   -   external 10 MHz "
            f"{'DETECTED' if s.ext_ref_detected else 'not detected'}")

        # the effective range can change after connect (the unit reports its own)
        band = (s.freq_min_Hz, s.freq_max_Hz, s.power_min_dBm, s.power_max_dBm)
        if s.connected and band != getattr(self, "_band_seen", None):
            self._band_seen = band
            self._apply_limits()

        # RF badge + toggle button (restyle only when the state flips)
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

        if s.connected and not s.hw_error:
            self.conn_dot.setText("●  connected")
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        elif s.connected:
            self.conn_dot.setText("●  read-back error")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        if s.idn:
            self.idn_label.setText(s.idn)

        self.spectrum.set_state(s.rf_on, s.frequency_Hz, s.power_dBm, s.phase_deg,
                                s.has_phase, (s.freq_min_Hz, s.freq_max_Hz))

    def _badge_color(self, color):
        self.state_badge.setStyleSheet(
            f"QLabel#stateBadge {{ color:{color}; border-color:{color}; "
            f"background:{COLORS['panel_hi']}; border-radius:10px; padding:4px 12px; "
            f"font-weight:700; letter-spacing:1px; }}")

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()          # local: RF off + disconnect; remote: close sockets
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Synthesizer-like object (a real in-process
    Synthesizer, or a DssgClient facade for a remote service). The theme is
    chosen ONCE here, from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # swap the active palette first
    # Number widgets follow the Windows locale otherwise (gotcha #18): on a
    # Finnish PC 2.5 would show as "2,5" and 1000 as "1 000".
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
