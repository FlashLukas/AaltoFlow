"""Control GUI for the DS Instruments PS6000L RF phase shifter.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a PhaseShifter-like object (the
real in-process brain, or a DsphaseClient facade for a remote service). It sends
commands (set_output / set_phase / set_attenuation / set_frequency) and reads a
status snapshot on a Qt timer. Events arrive on a Qt signal so they can safely
cross into the GUI thread.

The signature indicator is the PhaseDial: a phasor diagram (reference at 0 deg,
the shifted phasor at the unit's READBACK phase, the arc between them) next to
the two waveforms it implies -- the reference sine and the shifted one, whose
height follows the output attenuator. It shows at a glance what a phase shifter
does, and it draws what the unit reports, not what was typed.
"""

from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import Config
from ..phasemath import decimals_for
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


def _dspin(lo, hi, dec, step, value, suffix):
    w = QtWidgets.QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setSingleStep(step)
    w.setValue(value)
    w.setSuffix("  " + suffix)
    return w


# ------------------------------------------------------------- the phase dial

class PhaseDial(QtWidgets.QWidget):
    """Phasor dial + reference/shifted waveforms.

    Left: a unit circle with the REFERENCE phasor at 0 deg (muted) and the
    SHIFTED phasor at the unit's phase (accent), the swept arc between them.
    Positive phase is drawn counter-clockwise, the textbook convention.
    Right: the two sine waves over two periods, the shifted one displaced by
    the phase and scaled by the output attenuator (amplitude 10^(-att/20)).
    While the output is ON the waves travel (a ~33 ms timer of its own); when
    it is OFF the shifted wave is drawn flat and grey -- no signal leaves the
    box.
    """

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(230)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._on = False
        self._phase = 0.0            # degrees, as reported (caller's branch)
        self._att = 0.0
        self._step = 0.5
        self._t = 0.0                # animation time, radians of carrier
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, on: bool, phase_deg: float, att_dB: float, step_deg: float):
        self._on = bool(on)
        self._phase = float(phase_deg)
        self._att = float(att_dB)
        self._step = float(step_deg)
        if self._on and not self._timer.isActive():
            self._timer.start()
        elif not self._on and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        self._t = (self._t + 0.12) % (2 * math.pi)
        self.update()

    # -- drawing -----------------------------------------------------------

    def paintEvent(self, ev):
        from PySide6.QtGui import QPainter, QColor, QPen, QPainterPath
        from PySide6.QtCore import QRectF, QPointF, Qt

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        accent = QColor(COLORS["accent"])
        muted = QColor(COLORS["muted"])
        border = QColor(COLORS["border"])
        grid = QColor(COLORS["grid"])
        text = QColor(COLORS["text"])
        live = accent if self._on else muted
        phi = math.radians(self._phase)

        # ---------------- dial ----------------
        r = min(h * 0.40, w * 0.18)
        cx, cy = 16 + r + 8, h / 2.0 - 6
        p.setPen(QPen(border, 1.5)); p.setBrush(QColor(COLORS["panel_hi"]))
        p.drawEllipse(QPointF(cx, cy), r, r)
        # ticks: long every 90 deg, short every 30 deg
        for k in range(12):
            a = math.radians(k * 30)
            inner = r * (0.84 if k % 3 == 0 else 0.92)
            p.setPen(QPen(muted if k % 3 == 0 else border, 1.4))
            p.drawLine(QPointF(cx + inner * math.cos(a), cy - inner * math.sin(a)),
                       QPointF(cx + r * math.cos(a), cy - r * math.sin(a)))
        # axis labels
        f = p.font(); f.setPointSize(7); f.setBold(False); p.setFont(f)
        p.setPen(muted)
        for deg, (dx, dy) in ((0, (1, 0)), (90, (0, -1)), (180, (-1, 0)), (-90, (0, 1))):
            tx, ty = cx + (r + 12) * dx, cy + (r + 10) * dy
            p.drawText(QRectF(tx - 16, ty - 7, 32, 14), Qt.AlignCenter, f"{deg:+d}" if deg else "0")

        # the swept arc from the reference to the shifted phasor (short way
        # round in the device's -180..+180 sense, since that is what it does)
        dev = math.degrees(math.atan2(math.sin(phi), math.cos(phi)))
        ra = r * 0.42
        pen = QPen(QColor(live.red(), live.green(), live.blue(), 150), 3.0)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(cx - ra, cy - ra, 2 * ra, 2 * ra), 0, int(dev * 16))

        # reference phasor (dashed, muted)
        ref_pen = QPen(muted, 2.0); ref_pen.setStyle(Qt.DashLine)
        p.setPen(ref_pen)
        p.drawLine(QPointF(cx, cy), QPointF(cx + r * 0.9, cy))

        # shifted phasor with an arrow head
        tip = QPointF(cx + r * 0.9 * math.cos(phi), cy - r * 0.9 * math.sin(phi))
        p.setPen(QPen(live, 3.2, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(cx, cy), tip)
        head = QPainterPath()
        for i, da in enumerate((0.0, 2.6, -2.6)):
            L = 0 if i == 0 else 11
            pt = QPointF(tip.x() + L * math.cos(phi + da), tip.y() - L * math.sin(phi + da))
            head.moveTo(pt) if i == 0 else head.lineTo(pt)
        head.closeSubpath()
        p.setPen(Qt.NoPen); p.setBrush(live); p.drawPath(head)
        p.setBrush(text if self._on else muted)
        p.drawEllipse(QPointF(cx, cy), 3.2, 3.2)

        # ---------------- waveforms ----------------
        x0 = cx + r + 40
        x1 = w - 14
        if x1 - x0 > 60:
            mid = cy
            amp = r * 0.85
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(x0, mid), QPointF(x1, mid))
            span = x1 - x0
            n = max(40, int(span / 2))
            scale = 10 ** (-self._att / 20.0)     # attenuator -> amplitude

            def wave(offset, a):
                path = QPainterPath()
                for i in range(n + 1):
                    x = x0 + span * i / n
                    arg = 4 * math.pi * i / n - self._t
                    y = mid - a * math.sin(arg + offset)
                    path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
                return path

            ref = QPen(muted, 1.6); ref.setStyle(Qt.DashLine)
            p.setPen(ref); p.setBrush(Qt.NoBrush)
            p.drawPath(wave(0.0, amp))
            if self._on:
                p.setPen(QPen(accent, 2.6))
                p.drawPath(wave(phi, amp * scale))
            else:
                p.setPen(QPen(muted, 2.0))
                p.drawLine(QPointF(x0, mid), QPointF(x1, mid))
            # legend
            f.setPointSize(8); p.setFont(f)
            p.setPen(muted)
            p.drawText(QRectF(x0, 4, span, 14), Qt.AlignLeft, "- - reference")
            p.setPen(accent if self._on else muted)
            p.drawText(QRectF(x0, 4, span, 14), Qt.AlignRight,
                       f"shifted  x{scale:.2f} amplitude" if self._on else "output OFF")

        # ---------------- caption ----------------
        f.setPointSize(9); f.setBold(True); p.setFont(f)
        p.setPen(QColor(COLORS["accent_hi"]) if self._on else muted)
        p.drawText(QRectF(0, h - 18, w, 16), Qt.AlignHCenter,
                   f"shift {self._phase:+.{decimals_for(self._step)}f} deg"
                   f"   |   step {self._step:g} deg"
                   f"   |   {'RF OUT' if self._on else 'RF off'}")
        p.end()


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        title = f"{cfg.device.model} - RF Phase Shifter"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1120, 700)

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
        self.ctrl.start()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()
        self._refresh()

    # ---- layout ----------------------------------------------------------

    def _build_sidebar(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(360)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(14)

        header = QtWidgets.QHBoxLayout()
        self.title_lbl = QtWidgets.QLabel(self.cfg.device.model)
        self.title_lbl.setObjectName("brand")
        header.addWidget(self.title_lbl); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        header.addWidget(settings_btn)
        col.addLayout(header)

        # state card
        rcard, rlay = _card()
        top = QtWidgets.QHBoxLayout()
        self.state_badge = QtWidgets.QLabel("OUTPUT OFF")
        self.state_badge.setObjectName("stateBadge")
        top.addWidget(self.state_badge); top.addStretch(1)
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        self.conn_dot.setObjectName("muted")
        top.addWidget(self.conn_dot)
        rlay.addLayout(top)
        self.idn_label = QtWidgets.QLabel("-")
        self.idn_label.setObjectName("small")
        self.idn_label.setWordWrap(True)
        rlay.addWidget(self.idn_label)
        col.addWidget(rcard)

        self.out_btn = QtWidgets.QPushButton("Turn Output On")
        self.out_btn.setObjectName("primary")
        self.out_btn.setMinimumHeight(42)
        self.out_btn.clicked.connect(self._toggle_output)
        col.addWidget(self.out_btn)
        self._out_on = False

        # phase card: value + nudges by one device step + quick presets
        lim, dev = self.cfg.limits, self.cfg.device
        pcard, play = _card("Phase shift")
        prow = QtWidgets.QHBoxLayout()
        self.phase_spin = _dspin(lim.phase_min_deg, lim.phase_max_deg,
                                 decimals_for(dev.phase_step_deg), dev.phase_step_deg,
                                 0.0, "deg")      # seeded from the unit (_seed_inputs)
        set_ph = QtWidgets.QPushButton("Set"); set_ph.setObjectName("primary")
        set_ph.clicked.connect(self._set_phase)
        prow.addWidget(self.phase_spin, 1); prow.addWidget(set_ph)
        play.addLayout(prow)
        # one device step down/up, then the four quarter-turn presets
        nrow = QtWidgets.QHBoxLayout(); nrow.setSpacing(6)
        self.minus_btn = QtWidgets.QPushButton("-1 step")
        self.minus_btn.clicked.connect(lambda: self._nudge(-1))
        self.plus_btn = QtWidgets.QPushButton("+1 step")
        self.plus_btn.clicked.connect(lambda: self._nudge(+1))
        nrow.addWidget(self.minus_btn); nrow.addWidget(self.plus_btn)
        play.addLayout(nrow)
        qrow = QtWidgets.QHBoxLayout(); qrow.setSpacing(6)
        for v in (-90, 0, 90, 180):
            b = QtWidgets.QPushButton(f"{v:+d}°" if v else "0°")
            b.setObjectName("preset")
            b.clicked.connect(lambda _=False, v=v: self._goto_phase(v))
            qrow.addWidget(b)
        play.addLayout(qrow)
        col.addWidget(pcard)

        # attenuation card
        acard, alay = _card("Output attenuator")
        arow = QtWidgets.QHBoxLayout()
        self.att_spin = _dspin(lim.att_min_dB, lim.att_max_dB,
                               decimals_for(dev.att_step_dB), dev.att_step_dB,
                               lim.att_min_dB, "dB")  # seeded from the unit (_seed_inputs)
        set_att = QtWidgets.QPushButton("Set"); set_att.setObjectName("primary")
        set_att.clicked.connect(self._set_attenuation)
        arow.addWidget(self.att_spin, 1); arow.addWidget(set_att)
        alay.addLayout(arow)
        col.addWidget(acard)

        # carrier card
        fcard, flay = _card("Carrier frequency")
        frow = QtWidgets.QHBoxLayout()
        self.freq_spin = _dspin(lim.freq_min_MHz, lim.freq_max_MHz, 1, 10.0,
                                self.cfg.signal.frequency_MHz, "MHz")
        set_f = QtWidgets.QPushButton("Set"); set_f.setObjectName("primary")
        set_f.clicked.connect(self._set_frequency)
        frow.addWidget(self.freq_spin, 1); frow.addWidget(set_f)
        flay.addLayout(frow)
        self.freq_hint = QtWidgets.QLabel("")
        self.freq_hint.setObjectName("hint"); self.freq_hint.setWordWrap(True)
        flay.addWidget(self.freq_hint)
        col.addWidget(fcard)
        self._update_freq_hint()

        col.addStretch(1)
        off_btn = QtWidgets.QPushButton("RF Output Off"); off_btn.setObjectName("danger")
        off_btn.setMinimumHeight(36)
        off_btn.clicked.connect(lambda: self.ctrl.set_output(False))
        col.addWidget(off_btn)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        ocard, olay = _card("Unit reads back")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(26)
        self.phase_value = self._readout(row, "Phase shift", "deg", minw=120)
        self.dev_value = self._readout(row, "Device (-180..180)", "deg", minw=120)
        self.att_value = self._readout(row, "Attenuation", "dB", minw=100)
        self.freq_value = self._readout(row, "Carrier", "MHz", minw=110)
        row.addStretch(1)
        olay.addLayout(row)
        self.acc_label = QtWidgets.QLabel("")
        self.acc_label.setObjectName("small")
        olay.addWidget(self.acc_label)
        colw.addWidget(ocard)

        dcard, dlay = _card("Phasor")
        self.dial = PhaseDial()
        dlay.addWidget(self.dial)
        colw.addWidget(dcard, 3)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(110)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 2)
        return panel

    def _readout(self, row, label, unit, minw=120):
        box = QtWidgets.QVBoxLayout(); box.setSpacing(2)
        cap = QtWidgets.QLabel(label.upper()); cap.setObjectName("capLabel")
        line = QtWidgets.QHBoxLayout(); line.setSpacing(5)
        val = QtWidgets.QLabel("-"); val.setObjectName("bigValue")
        val.setMinimumWidth(minw)
        u = QtWidgets.QLabel(unit); u.setObjectName("unit")
        line.addWidget(val); line.addWidget(u, 0, QtCore.Qt.AlignBottom)
        box.addWidget(cap); box.addLayout(line)
        holder = QtWidgets.QWidget(); holder.setLayout(box)
        row.addWidget(holder)
        return val

    # ---- actions ---------------------------------------------------------

    def _toggle_output(self):
        self.ctrl.set_output(not self._out_on)

    def _set_phase(self):
        self.ctrl.set_phase(self.phase_spin.value())

    def _goto_phase(self, v: float):
        self.phase_spin.setValue(v)
        self.ctrl.set_phase(v)

    def _nudge(self, sign: int):
        """One device step up or down from what the unit reports now."""
        s = self.ctrl.status()
        target = s.phase_deg + sign * self.cfg.device.phase_step_deg
        self.phase_spin.setValue(target)
        self.ctrl.set_phase(target)

    def _set_attenuation(self):
        self.ctrl.set_attenuation(self.att_spin.value())

    def _set_frequency(self):
        self.ctrl.set_frequency(self.freq_spin.value())

    def _update_freq_hint(self):
        if self.cfg.device.freq_command:
            self.freq_hint.setText("Sent to the unit (device.freq_command).")
        else:
            self.freq_hint.setText("Bookkeeping only: the PS6000L command list has no "
                                   "frequency command. It selects the accuracy band.")

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        lim, dev = self.cfg.limits, self.cfg.device
        self.phase_spin.setRange(lim.phase_min_deg, lim.phase_max_deg)
        self.phase_spin.setDecimals(decimals_for(dev.phase_step_deg))
        self.phase_spin.setSingleStep(dev.phase_step_deg)
        self.att_spin.setRange(lim.att_min_dB, lim.att_max_dB)
        self.att_spin.setDecimals(decimals_for(dev.att_step_dB))
        self.att_spin.setSingleStep(dev.att_step_dB)
        self.freq_spin.setRange(lim.freq_min_MHz, lim.freq_max_MHz)
        self.title_lbl.setText(dev.model)
        self._update_freq_hint()

    # ---- refresh & events ------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = COLORS["danger"] if level == "error" else (
            COLORS["accent"] if level == "warn" else COLORS["muted"])
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(
            f'<span style="color:{COLORS["accent_dim"]}">{stamp}</span> '
            f'<span style="color:{color}">{msg}</span>')

    def _seed_inputs(self, s):
        """Put the unit's ADOPTED state into the input boxes, once.

        The service reads the phase and attenuation from the unit at start
        instead of pushing config values, so the boxes must start from what the
        unit holds -- otherwise pressing "Set" next to an untouched box would
        silently change the RF. Done on the first status frame that says the
        state has been read (for a remote GUI that frame arrives a moment after
        the window opens); later frames never overwrite what the user types."""
        self._seeded = True
        for spin, value in ((self.phase_spin, s.phase_set_deg),
                            (self.att_spin, s.attenuation_set_dB),
                            (self.freq_spin, s.frequency_MHz)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def _refresh(self):
        s = self.ctrl.status()
        if not getattr(self, "_seeded", False) and getattr(s, "adopted", False):
            self._seed_inputs(s)
        self._out_on = bool(s.output_on)
        dec = decimals_for(s.phase_step_deg or 0.5)
        self.phase_value.setText(f"{s.phase_deg:+.{dec}f}")
        self.dev_value.setText(f"{s.phase_device_deg:+.{dec}f}")
        self.att_value.setText(f"{s.attenuation_dB:.2f}")
        self.freq_value.setText(f"{s.frequency_MHz:.1f}")
        self.acc_label.setText(
            f"Datasheet accuracy at this carrier and setting: ±{s.accuracy_deg:g} deg"
            f"   |   asked for {s.phase_set_deg:+.{dec}f} deg, "
            f"{s.attenuation_set_dB:.2f} dB")

        if s.output_on != getattr(self, "_btn_state", None):
            self._btn_state = s.output_on
            if s.output_on:
                self.state_badge.setText("OUTPUT ON")
                self._badge_color(COLORS["ok"])
                self.out_btn.setText("Turn Output Off")
                self.out_btn.setObjectName("danger")
            else:
                self.state_badge.setText("OUTPUT OFF")
                self._badge_color(COLORS["muted"])
                self.out_btn.setText("Turn Output On")
                self.out_btn.setObjectName("primary")
            # re-apply QSS after the objectName (selector) changed
            self.out_btn.style().unpolish(self.out_btn)
            self.out_btn.style().polish(self.out_btn)

        if s.hw_error:
            self.conn_dot.setText("●  read error")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        elif s.connected:
            self.conn_dot.setText("●  connected")
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        self.idn_label.setText(s.hw_error or s.idn or "-")

        self.dial.set_state(s.output_on, s.phase_deg, s.attenuation_dB,
                            s.phase_step_deg or 0.5)

    def _badge_color(self, color):
        self.state_badge.setStyleSheet(
            f"QLabel#stateBadge {{ color:{color}; border-color:{color}; "
            f"background:{COLORS['panel_hi']}; border-radius:10px; padding:4px 12px; "
            f"font-weight:700; letter-spacing:1px; }}")

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()          # output off + disconnect (local); close socket (remote)
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a PhaseShifter-like object. The theme is chosen
    ONCE here, from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # swap the active palette first
    # Number widgets follow the Windows locale otherwise: "10,000" for 10 dB on
    # a Finnish/Czech PC (gotcha #18). The C locale gives "10.00".
    loc = QtCore.QLocale.c()
    loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
    QtCore.QLocale.setDefault(loc)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
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
    brain, _ = build_sim_system(cfg)
    return run_app(brain, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
