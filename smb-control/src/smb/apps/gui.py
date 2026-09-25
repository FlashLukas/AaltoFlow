"""Dark-theme control GUI for the SMB100A RF signal generator.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a Generator-like object (a real
in-process Generator, or an SmbClient facade for a remote service). It sends
commands (set_rf / set_power / set_frequency / set_phase) and reads a status
snapshot on a Qt timer to update the numbers and the antenna glyph. Generator
events arrive on a Qt signal so they can safely cross into the GUI thread.

The star of the show is the AntennaIndicator: a little transmitter tower that
radiates animated amber waves whenever the RF output is ON, brighter the higher
the power. It is the RF-generator counterpart to clMag's glowing MagnetIndicator.
"""

from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import Config
from ..sim_system import build_sim_system
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog


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


# ------------------------------------------------------------- the antenna

class AntennaIndicator(QtWidgets.QWidget):
    """A transmitter tower that radiates animated waves when the RF is on.

    Two lobes of concentric arcs sweep outward from the antenna tip (the classic
    "broadcasting" glyph). They only animate while RF is ON; their brightness and
    how many arcs are visible scale with `intensity` (0..1, mapped from output
    power). A faint reminder of the current frequency/power sits underneath.
    """

    def __init__(self):
        super().__init__()
        # small, matching clMag's MagnetIndicator footprint
        self.setFixedHeight(96)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self._on = False
        self._intensity = 0.0        # 0..1, drives brightness / number of arcs
        self._freq_hz = 0.0
        self._power_dBm = 0.0
        self._phase = 0.0            # animation phase, advances each tick

        # the animation runs on its own timer so the waves are smooth regardless
        # of how often status() is polled.
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)          # ~30 fps
        self._timer.timeout.connect(self._tick)

    def set_state(self, rf_on: bool, intensity: float, freq_hz: float, power_dBm: float):
        self._on = bool(rf_on)
        self._intensity = max(0.0, min(1.0, intensity))
        self._freq_hz = freq_hz
        self._power_dBm = power_dBm
        if self._on and not self._timer.isActive():
            self._timer.start()
        elif not self._on and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        # advance the wave phase; a touch faster at higher power
        self._phase = (self._phase + 0.018 + 0.02 * self._intensity) % 1.0
        self.update()

    # -- drawing -----------------------------------------------------------

    def paintEvent(self, ev):
        from PySide6.QtGui import QPainter, QColor, QPen
        from PySide6.QtCore import QRectF, Qt

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # antenna geometry: a tower rising to a tip; waves emanate from the tip
        base_y = h * 0.72
        tip_y = h * 0.22
        cx = w / 2.0
        half_base = 15.0

        # ---- radiating waves (behind the tower) --------------------------
        if self._on:
            n = 5
            max_r = min(w, h) * 0.46
            accent = QColor(COLORS["accent"])
            for side in (-1, +1):                      # left lobe, right lobe
                # each lobe is an arc centred on the horizontal, opening outward
                centre_deg = 0 if side > 0 else 180
                span_deg = 66
                start = int((centre_deg - span_deg / 2) * 16)
                span = int(span_deg * 16)
                for i in range(n):
                    frac = ((i + self._phase) / n)
                    r = 18 + frac * max_r
                    fade = (1.0 - frac)
                    a = int(30 + 200 * fade * (0.35 + 0.65 * self._intensity))
                    pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), a))
                    pen.setWidthF(2.2)
                    p.setPen(pen)
                    p.setBrush(Qt.NoBrush)
                    p.drawArc(QRectF(cx - r, tip_y - r, 2 * r, 2 * r), start, span)

            # a soft glow dot at the feed point
            glow = QColor(accent.red(), accent.green(), accent.blue(),
                          int(120 + 120 * self._intensity))
            p.setPen(Qt.NoPen); p.setBrush(glow)
            p.drawEllipse(QRectF(cx - 5, tip_y - 5, 10, 10))

        # ---- the tower ---------------------------------------------------
        metal = QColor(COLORS["accent"]) if self._on else QColor("#5b6470")
        pen = QPen(metal, 2.4); p.setPen(pen); p.setBrush(QtCore.Qt.NoBrush)
        # two legs converging from base to tip
        p.drawLine(int(cx - half_base), int(base_y), int(cx), int(tip_y))
        p.drawLine(int(cx + half_base), int(base_y), int(cx), int(tip_y))
        # cross-braces
        for frac in (0.28, 0.52, 0.76):
            y = tip_y + (base_y - tip_y) * frac
            hw = half_base * frac
            p.drawLine(int(cx - hw), int(y), int(cx + hw), int(y))
        # ground line
        p.setPen(QPen(QColor("#3a4048"), 2))
        p.drawLine(int(cx - half_base - 14), int(base_y), int(cx + half_base + 14), int(base_y))

        # ---- caption -----------------------------------------------------
        if self._on:
            cap = "RADIATING"
            col = QColor(COLORS["accent_hi"])
        else:
            cap = "RF off"
            col = QColor(COLORS["muted"])
        p.setPen(col)
        f = p.font(); f.setBold(True); f.setPointSize(8); p.setFont(f)
        p.drawText(QRectF(0, h - 15, w, 13), QtCore.Qt.AlignHCenter, cap)
        p.end()


# ------------------------------------------------------------- main window

_FREQ_UNITS = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self._freq_unit = "MHz"
        title = "SMB100A · RF Signal Generator"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1080, 660)

        root = QtWidgets.QWidget(); root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)

        # generator events -> log
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

        # start the generator (opens the backend) and the refresh timer
        self.ctrl.start()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

    # ---- layout ----------------------------------------------------------

    def _build_sidebar(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(360)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(16)

        # header: title + settings
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("SMB100A")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("⚙  Settings")
        settings_btn.clicked.connect(self._open_settings)
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        header.addWidget(settings_btn)
        col.addLayout(header)

        # RF state readout card
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
        rlay.addWidget(self.idn_label)
        col.addWidget(rcard)

        # RF on/off toggle (big)
        self.rf_btn = QtWidgets.QPushButton("Turn RF On")
        self.rf_btn.setObjectName("primary")
        self.rf_btn.setMinimumHeight(44)
        self.rf_btn.clicked.connect(self._toggle_rf)
        col.addWidget(self.rf_btn)
        self._rf_on = False

        # frequency control (spin + unit + set)
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
        self._apply_freq_unit_range(initial_hz=self.cfg.signal.frequency_Hz)

        # power control
        pcard, play = _card("Power level")
        prow = QtWidgets.QHBoxLayout()
        self.power_spin = QtWidgets.QDoubleSpinBox()
        self.power_spin.setRange(self.cfg.limits.power_min_dBm, self.cfg.limits.power_max_dBm)
        self.power_spin.setDecimals(2); self.power_spin.setSingleStep(0.5)
        self.power_spin.setValue(self.cfg.signal.power_dBm); self.power_spin.setSuffix("  dBm")
        set_pow = QtWidgets.QPushButton("Set"); set_pow.setObjectName("primary")
        set_pow.clicked.connect(self._set_power)
        prow.addWidget(self.power_spin, 1); prow.addWidget(set_pow)
        play.addLayout(prow)
        col.addWidget(pcard)

        # phase control
        phcard, phlay = _card("Phase")
        phrow = QtWidgets.QHBoxLayout()
        self.phase_spin = QtWidgets.QDoubleSpinBox()
        self.phase_spin.setRange(self.cfg.limits.phase_min_deg, self.cfg.limits.phase_max_deg)
        self.phase_spin.setDecimals(2); self.phase_spin.setSingleStep(1.0)
        self.phase_spin.setValue(self.cfg.signal.phase_deg); self.phase_spin.setSuffix("  deg")
        set_ph = QtWidgets.QPushButton("Set"); set_ph.setObjectName("primary")
        set_ph.clicked.connect(self._set_phase)
        phrow.addWidget(self.phase_spin, 1); phrow.addWidget(set_ph)
        phlay.addLayout(phrow)
        col.addWidget(phcard)

        col.addStretch(1)
        off_btn = QtWidgets.QPushButton("RF Off"); off_btn.setObjectName("danger")
        off_btn.setMinimumHeight(38)
        off_btn.clicked.connect(lambda: self.ctrl.set_rf(False))
        col.addWidget(off_btn)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        # current-output readouts + the compact antenna indicator, one row —
        # no separate panel, so the indicator stays small and unobtrusive
        ocard, olay = _card("Current output")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(24)
        self.freq_value = self._readout(row, "Frequency", "MHz", minw=200)
        self.power_value = self._readout(row, "Power", "dBm", minw=110)
        self.phase_value = self._readout(row, "Phase", "deg", minw=100)
        row.addStretch(1)
        self.antenna = AntennaIndicator()
        self.antenna.setFixedWidth(220)
        row.addWidget(self.antenna, 0, QtCore.Qt.AlignVCenter)
        olay.addLayout(row)
        colw.addWidget(ocard)

        # status log (takes the remaining height)
        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(130)
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

    # ---- frequency unit handling ----------------------------------------

    def _apply_freq_unit_range(self, initial_hz=None):
        """Set the freq spin's range/step for the current unit, preserving the Hz."""
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

    def _toggle_rf(self):
        self.ctrl.set_rf(not self._rf_on)

    def _set_frequency(self):
        self.ctrl.set_frequency(self._current_freq_hz())

    def _set_power(self):
        self.ctrl.set_power(self.power_spin.value())

    def _set_phase(self):
        self.ctrl.set_phase(self.phase_spin.value())

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        self.power_spin.setRange(self.cfg.limits.power_min_dBm, self.cfg.limits.power_max_dBm)
        self.phase_spin.setRange(self.cfg.limits.phase_min_deg, self.cfg.limits.phase_max_deg)
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
        s = self.ctrl.status()
        self._rf_on = bool(s.rf_on)

        # readouts
        self.freq_value.setText(f"{s.frequency_Hz/1e6:,.3f}")
        self.power_value.setText(f"{s.power_dBm:.2f}")
        self.phase_value.setText(f"{s.phase_deg:.2f}")

        # RF badge + toggle button (only restyle when the state actually flips,
        # so we don't churn the stylesheet every 60 ms)
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

        # connection
        if s.connected:
            self.conn_dot.setText("●  connected")
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        if s.idn:
            self.idn_label.setText(s.idn)

        # antenna: intensity from power position within the limit band
        lim = self.cfg.limits
        span = max(1e-6, lim.power_max_dBm - lim.power_min_dBm)
        intensity = (s.power_dBm - lim.power_min_dBm) / span
        self.antenna.set_state(s.rf_on, intensity, s.frequency_Hz, s.power_dBm)

    def _badge_color(self, color):
        self.state_badge.setStyleSheet(
            f"QLabel#stateBadge {{ color:{color}; border-color:{color}; "
            f"background:{COLORS['panel_hi']}; border-radius:10px; padding:4px 12px; "
            f"font-weight:700; letter-spacing:1px; }}")

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()          # RF off + disconnect
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Generator-like object (a real in-process Generator,
    or an SmbClient facade for a remote service). The theme is chosen ONCE here,
    from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # swap the active palette first
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
