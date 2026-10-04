"""Front panel for the DS Instruments variable-gain RF amplifier.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: the window holds an Amplifier-like object (a real
in-process Amplifier, or a DsampClient facade for a remote service). It sends
commands (set_amp / set_gain / set_frequency / set_input_power) and reads a
status snapshot on a Qt timer to update the numbers and the indicator. Brain
events arrive on a Qt signal so they cross safely into the GUI thread.

The signature widget is the GainStageIndicator: the textbook amplifier triangle
with a small sine going in and a bigger one coming out (bigger with more gain,
animated while the stage is on), next to the gain-vs-frequency curve of the
current setting -- so you SEE that "10 dB" at 6 GHz is really ~0 dB, and how
far the setting sits below the safety ceiling.
"""

from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from .. import model
from ..config import Config
from ..sim_system import build_sim_system
from ..control import ControlRefused
from .control_bar import ALWAYS_PROPERTY, ControlBar, mark_always
from .settings_dialog import SettingsDialog
from .theme import COLORS, apply_palette, build_stylesheet, set_theme


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


def _hint(text: str) -> QtWidgets.QLabel:
    lbl = QtWidgets.QLabel(text)
    lbl.setObjectName("hint")
    lbl.setWordWrap(True)
    return lbl


# ------------------------------------------------------------- the indicator

class GainStageIndicator(QtWidgets.QWidget):
    """Amplifier triangle with input/output sines + the gain-vs-frequency curve.

    Left: a small sine enters the triangle and a larger one leaves it; the
    output amplitude follows the ESTIMATED gain at the signal frequency, so a
    high setting at 6 GHz visibly amplifies less. The waves travel only while
    the stage is on; off, the triangle is grey and the output is a flat line.

    Right: estimated gain vs frequency (10 MHz - 6 GHz) for the current setting
    (accent), the same curve at the safety ceiling (muted, dashed), and a dot at
    the operating frequency. All colours are read from COLORS at paint time, so
    the widget follows the theme chosen at start-up.
    """

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(210)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self._on = False
        self._setting = 0.0
        self._ceiling = 10.0
        self._dev_max = 31.0
        self._freq = 2e9
        self._est = 0.0
        self._warn = False
        self._phase = 0.0
        # the animation has its own timer so the waves move smoothly whatever
        # the status poll rate is (guide section 7: animated indicators)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, on: bool, setting_dB: float, ceiling_dB: float,
                  device_max_dB: float, freq_Hz: float, est_gain_dB: float,
                  warning: bool) -> None:
        self._on = bool(on)
        self._setting = float(setting_dB)
        self._ceiling = float(ceiling_dB)
        self._dev_max = max(1.0, float(device_max_dB))
        self._freq = float(freq_Hz)
        self._est = float(est_gain_dB)
        self._warn = bool(warning)
        if self._on and not self._timer.isActive():
            self._timer.start()
        elif not self._on and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        self._phase = (self._phase + 0.06) % (2 * math.pi)
        self.update()

    # -- drawing -----------------------------------------------------------

    def paintEvent(self, ev):
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        accent = QColor(COLORS["accent"])
        muted = QColor(COLORS["muted"])
        text = QColor(COLORS["text"])
        metal = QColor("#5a626e")              # the suite's neutral "metal" grey
        live = QColor(COLORS["danger"]) if (self._on and self._warn) else accent

        # ================= left: the gain stage ===========================
        lw = min(w * 0.46, 420.0)
        cy = h * 0.46
        tri_l, tri_r = lw * 0.36, lw * 0.64
        tri_h = min(h * 0.58, (tri_r - tri_l) * 1.25)

        # output amplitude: map estimated gain (-5 .. device max dB) onto
        # (input amplitude .. almost half the height); a log quantity on a
        # linear pixel scale, which is what a physicist expects of "dB"
        a_in = h * 0.07
        frac = max(0.0, min(1.0, (self._est + 5.0) / (self._dev_max + 5.0)))
        a_out = a_in + frac * (h * 0.36 - a_in)

        def sine(x0, x1, amp, colour, width, cycles):
            path = QPainterPath()
            n = 80
            for i in range(n + 1):
                x = x0 + (x1 - x0) * i / n
                ph = 2 * math.pi * cycles * i / n - (self._phase if self._on else 0.0)
                y = cy - amp * math.sin(ph)
                path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
            pen = QPen(colour, width)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

        # input (always drawn: the signal is there whether the amp is on or not)
        sine(8, tri_l - 6, a_in, muted, 1.8, 2.0)
        # output: amplified sine when on, a flat line when off
        if self._on:
            sine(tri_r + 6, lw - 8, a_out, live, 2.4, 2.0)
        else:
            p.setPen(QPen(metal, 1.8))
            p.drawLine(QPointF(tri_r + 6, cy), QPointF(lw - 8, cy))

        # the triangle
        tri = QPainterPath()
        tri.moveTo(tri_l, cy - tri_h / 2)
        tri.lineTo(tri_r, cy)
        tri.lineTo(tri_l, cy + tri_h / 2)
        tri.closeSubpath()
        fill = QColor(live)
        fill.setAlpha(60 if self._on else 0)
        p.setBrush(fill)
        p.setPen(QPen(live if self._on else metal, 2.6))
        p.drawPath(tri)
        # the gain setting, inside the triangle
        f = p.font()
        f.setBold(True)
        f.setPointSizeF(max(8.0, tri_h * 0.12))
        p.setFont(f)
        p.setPen(text if self._on else muted)
        p.drawText(QRectF(tri_l, cy - tri_h * 0.2, (tri_r - tri_l) * 0.8, tri_h * 0.4),
                   Qt.AlignCenter, f"{self._setting:g}")

        # caption
        f.setPointSize(8)
        p.setFont(f)
        if self._on:
            cap = f"AMPLIFYING  {self._est:+.1f} dB at {self._freq/1e9:.2f} GHz"
            p.setPen(QColor(COLORS["danger"]) if self._warn else QColor(COLORS["accent_hi"]))
        else:
            cap = "amplifier off"
            p.setPen(muted)
        p.drawText(QRectF(0, h - 18, lw, 14), Qt.AlignHCenter, cap)

        # ================= right: gain vs frequency =======================
        x0, x1 = lw + 46, w - 26            # room for the "6 GHz" label
        y0, y1 = 14, h - 30                    # top, bottom of the plot area
        if x1 - x0 > 60:
            g_lo, g_hi = -10.0, self._dev_max + 2.0

            def X(f_hz):
                return x0 + (x1 - x0) * (f_hz / 6e9)

            def Y(g):
                g = max(g_lo, min(g_hi, g))
                return y1 - (y1 - y0) * (g - g_lo) / (g_hi - g_lo)

            # grid and axes
            p.setPen(QPen(QColor(COLORS["grid"]), 1))
            for g in range(0, int(g_hi) + 1, 10):
                p.drawLine(QPointF(x0, Y(g)), QPointF(x1, Y(g)))
            for fg in range(1, 7):
                p.drawLine(QPointF(X(fg * 1e9), y0), QPointF(X(fg * 1e9), y1))
            p.setPen(QPen(QColor(COLORS["border"]), 1))
            p.setBrush(Qt.NoBrush)             # the triangle's fill is still set
            p.drawRect(QRectF(x0, y0, x1 - x0, y1 - y0))
            f.setBold(False)
            f.setPointSize(8)
            p.setFont(f)
            p.setPen(muted)
            for g in range(0, int(g_hi) + 1, 10):
                p.drawText(QRectF(x0 - 44, Y(g) - 7, 38, 14),
                           Qt.AlignRight | Qt.AlignVCenter, f"{g} dB")
            for fg in (0, 2, 4, 6):
                p.drawText(QRectF(X(fg * 1e9) - 20, y1 + 3, 40, 14),
                           Qt.AlignHCenter, f"{fg} GHz")

            def curve(setting, pen):
                path = QPainterPath()
                n = 120
                for i in range(n + 1):
                    fz = 10e6 + (6e9 - 10e6) * i / n
                    pt = QPointF(X(fz), Y(model.est_gain_dB(setting, fz)))
                    path.moveTo(pt) if i == 0 else path.lineTo(pt)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)

            dash = QPen(muted, 1.4)
            dash.setStyle(Qt.DashLine)
            curve(self._ceiling, dash)
            p.setPen(muted)
            p.drawText(QRectF(x1 - 120, Y(model.est_gain_dB(self._ceiling, 0.5e9)) - 17,
                              114, 14), Qt.AlignRight, "safety ceiling")
            curve(self._setting, QPen(live if self._on else metal, 2.4))

            # the operating point
            px, py = X(self._freq), Y(self._est)
            p.setPen(QPen(QColor(COLORS["accent_dim"]), 1, Qt.DotLine))
            p.drawLine(QPointF(px, y0), QPointF(px, y1))
            p.setPen(Qt.NoPen)
            p.setBrush(live if self._on else metal)
            p.drawEllipse(QPointF(px, py), 5.0, 5.0)
        p.end()


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        title = "DS Instruments - RF Amplifier"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1180, 760)

        root = QtWidgets.QWidget()
        root.setObjectName("root")
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)
        # Control or viewer (control_bar.py): a bar across the top, only for a
        # GUI on a service whose client knows about control -- a local GUI
        # owns its amplifier and has nobody to share it with.
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

        # brain events -> log
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

        # "10.5", never "10,5", even if run_app was bypassed (gotcha #18)
        for spin in (self.gain_spin, self.freq_spin, self.input_spin):
            spin.setLocale(QtCore.QLocale.c())

        # start the brain (opens the backend) and the refresh timer
        self.ctrl.start()
        self._range = None
        self._gain_seeded = False       # the gain box starts from the ADOPTED gain
        self._apply_gain_range()
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
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(16)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("RF AMPLIFIER")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; "
                            f"font-weight:800; letter-spacing:2px;")
        header.addWidget(title)
        header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        mark_always(settings_btn)    # a viewer may LOOK; the service refuses the OK
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        header.addWidget(settings_btn)
        col.addLayout(header)

        # state card
        rcard, rlay = _card()
        top = QtWidgets.QHBoxLayout()
        self.state_badge = QtWidgets.QLabel("AMP OFF")
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
        self.err_label = QtWidgets.QLabel("")
        self.err_label.setWordWrap(True)
        self.err_label.setStyleSheet(f"color:{COLORS['danger']}; font-size:11px;")
        self.err_label.hide()
        rlay.addWidget(self.err_label)
        col.addWidget(rcard)

        # big on/off toggle
        self.amp_btn = QtWidgets.QPushButton("Amplifier On")
        self.amp_btn.setObjectName("primary")
        self.amp_btn.setMinimumHeight(44)
        self.amp_btn.setToolTip("Terminate the output (50 ohm) before switching on.")
        self.amp_btn.clicked.connect(self._toggle_amp)
        col.addWidget(self.amp_btn)
        self._amp_on = False

        # gain
        gcard, glay = _card("Gain setting")
        grow = QtWidgets.QHBoxLayout()
        self.gain_spin = QtWidgets.QDoubleSpinBox()
        self.gain_spin.setDecimals(2)
        self.gain_spin.setSuffix("  dB")
        set_gain = QtWidgets.QPushButton("Set")
        set_gain.setObjectName("primary")
        set_gain.clicked.connect(self._set_gain)
        grow.addWidget(self.gain_spin, 1)
        grow.addWidget(set_gain)
        glay.addLayout(grow)
        self.gain_hint = _hint("")
        glay.addWidget(self.gain_hint)
        col.addWidget(gcard)

        # operating point
        ocard, olay = _card("Operating point")
        form = QtWidgets.QFormLayout()
        form.setSpacing(8)
        lim = self.cfg.limits
        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setDecimals(3)
        self.freq_spin.setRange(lim.freq_min_Hz / 1e6, lim.freq_max_Hz / 1e6)
        self.freq_spin.setSingleStep(10.0)
        self.freq_spin.setSuffix("  MHz")
        self.freq_spin.setValue(self.cfg.amp.frequency_Hz / 1e6)
        self.input_spin = QtWidgets.QDoubleSpinBox()
        self.input_spin.setDecimals(2)
        self.input_spin.setRange(lim.input_min_dBm, lim.input_max_dBm)
        self.input_spin.setSingleStep(0.5)
        self.input_spin.setSuffix("  dBm")
        self.input_spin.setValue(self.cfg.amp.input_dBm)
        form.addRow("Signal frequency", self.freq_spin)
        form.addRow("Input level", self.input_spin)
        olay.addLayout(form)
        set_op = QtWidgets.QPushButton("Set operating point")
        set_op.clicked.connect(self._set_operating_point)
        olay.addWidget(set_op)
        olay.addWidget(_hint("The amplifier has no frequency setting: these only feed "
                             "the gain / output estimate and its warning."))
        col.addWidget(ocard)

        col.addStretch(1)
        off_btn = QtWidgets.QPushButton("AMPLIFIER OFF")
        off_btn.setObjectName("danger")
        off_btn.setMinimumHeight(38)
        off_btn.clicked.connect(lambda: self._safe(self.ctrl.amp_off))
        mark_always(off_btn)         # the SAFETY verb (net/service.py): a viewer too
        col.addWidget(off_btn)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0)
        colw.setSpacing(16)

        rcard, rlay = _card("Readback")
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(22)
        self.gain_value = self._readout(row, "Gain setting", "dB", 90)
        self.est_value = self._readout(row, "Est. gain", "dB", 90)
        self.out_value = self._readout(row, "Est. output", "dBm", 100)
        self.temp_value = self._readout(row, "Temperature", "C", 80)
        self.volt_value = self._readout(row, "USB supply", "V", 80)
        row.addStretch(1)
        rlay.addLayout(row)
        colw.addWidget(rcard)

        icard, ilay = _card("Gain stage")
        self.indicator = GainStageIndicator()
        ilay.addWidget(self.indicator)
        colw.addWidget(icard)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(120)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 1)
        return panel

    def _readout(self, row, label, unit, minw=100):
        box = QtWidgets.QVBoxLayout()
        box.setSpacing(2)
        cap = QtWidgets.QLabel(label.upper())
        cap.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; "
                          f"font-weight:700; letter-spacing:1px;")
        line = QtWidgets.QHBoxLayout()
        line.setSpacing(5)
        val = QtWidgets.QLabel("—")
        val.setObjectName("bigValue")
        val.setMinimumWidth(minw)
        u = QtWidgets.QLabel(unit)
        u.setObjectName("unit")
        line.addWidget(val)
        line.addWidget(u, 0, QtCore.Qt.AlignBottom)
        box.addWidget(cap)
        box.addLayout(line)
        holder = QtWidgets.QWidget()
        holder.setLayout(box)
        row.addWidget(holder)
        return val

    # ---- gain range (it is LIVE: limits and device range can change) -----

    def _apply_gain_range(self, lo=None, hi=None):
        if lo is None or hi is None:
            lo, hi = self.ctrl.gain_range()
        if self._range == (lo, hi):
            return
        self._range = (lo, hi)
        step = float(self.cfg.hardware.gain_step_dB)
        cur = self.gain_spin.value()
        self.gain_spin.blockSignals(True)
        self.gain_spin.setRange(lo, hi)
        self.gain_spin.setSingleStep(step)
        if self._range is not None and not (lo <= cur <= hi):
            self.gain_spin.setValue(lo)
        self.gain_spin.blockSignals(False)
        self.gain_hint.setText(
            f"Allowed {lo:g} .. {hi:g} dB in {step:g} dB steps. The ceiling is "
            f"the safety limit (Settings > Limits) or the device maximum.")

    # ---- actions ---------------------------------------------------------

    def _safe(self, fn, *args):
        """Run a command; a refusal because another PC holds control
        (ControlRefused -- normally the viewer guard stops the click first)
        goes to the log instead of a traceback."""
        try:
            fn(*args)
        except ControlRefused as exc:
            self._on_event("error", str(exc))

    def _toggle_amp(self):
        if self._amp_on:
            # OFF = the safety verb amp_off, which a viewer may send too
            self._safe(self.ctrl.amp_off)
        else:
            self._safe(self.ctrl.set_amp, True)

    def _set_gain(self):
        self._safe(self.ctrl.set_gain, self.gain_spin.value())

    def _set_operating_point(self):
        def both():
            self.ctrl.set_frequency(self.freq_spin.value() * 1e6)
            self.ctrl.set_input_power(self.input_spin.value())
        self._safe(both)

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        lim = self.cfg.limits
        self.freq_spin.setRange(lim.freq_min_Hz / 1e6, lim.freq_max_Hz / 1e6)
        self.input_spin.setRange(lim.input_min_dBm, lim.input_max_dBm)
        self._range = None              # force the hint/range to be rebuilt
        self._apply_gain_range()

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
        self._amp_on = bool(s.amp_on)
        # While it reads "Amplifier Off" the big button sends the safety verb,
        # so a viewer may press it (control_bar.py); reading "Amplifier On" it
        # is blocked like every other input.
        self.amp_btn.setProperty(ALWAYS_PROPERTY, self._amp_on)

        self.gain_value.setText(f"{s.gain_dB:.2f}")
        self.est_value.setText(f"{s.est_gain_dB:+.2f}")
        self.out_value.setText(f"{s.est_output_dBm:+.2f}")
        self.temp_value.setText(f"{s.temperature_C:.1f}" if s.connected else "—")
        self.volt_value.setText(f"{s.supply_V:.2f}" if s.connected else "—")
        self.out_value.setStyleSheet(
            f"color:{COLORS['danger']};" if s.output_warning else "")

        # badge + toggle: restyle only when the state flips, not every 60 ms
        if s.amp_on != getattr(self, "_btn_state", None):
            self._btn_state = s.amp_on
            if s.amp_on:
                self.state_badge.setText("AMP ON")
                self._badge_color(COLORS["ok"])
                self.amp_btn.setText("Amplifier Off")
                self.amp_btn.setObjectName("danger")
            else:
                self.state_badge.setText("AMP OFF")
                self._badge_color(COLORS["muted"])
                self.amp_btn.setText("Amplifier On")
                self.amp_btn.setObjectName("primary")
            # re-apply QSS after the objectName (selector) changed
            self.amp_btn.style().unpolish(self.amp_btn)
            self.amp_btn.style().polish(self.amp_btn)

        if s.connected:
            self.conn_dot.setText("●  connected")
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        if s.idn:
            self.idn_label.setText(s.idn)
        if s.hw_error:
            self.err_label.setText(f"hardware: {s.hw_error}")
            self.err_label.show()
        else:
            self.err_label.hide()

        if s.connected:
            self._apply_gain_range(s.gain_min_dB, s.gain_max_dB)
            if not self._gain_seeded:
                # Once, from the first connected frame: the module adopts the
                # amplifier's gain at start, so the box should show that value,
                # not 0 -- pressing Set must not silently change the amplifier.
                # (If the device holds more than the ceiling, the box can only
                # show the ceiling; the readout on the right shows the truth.)
                self.gain_spin.blockSignals(True)
                self.gain_spin.setValue(s.gain_set_dB)
                self.gain_spin.blockSignals(False)
                self._gain_seeded = True
        self.indicator.set_state(s.amp_on, s.gain_dB, s.gain_max_dB,
                                 self.cfg.hardware.gain_max_dB, s.frequency_Hz,
                                 s.est_gain_dB, s.output_warning)

    def _badge_color(self, color):
        self.state_badge.setStyleSheet(
            f"QLabel#stateBadge {{ color:{color}; border-color:{color}; "
            f"background:{COLORS['panel_hi']}; border-radius:10px; padding:4px 12px; "
            f"font-weight:700; letter-spacing:1px; }}")

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()          # local: stage OFF + disconnect; remote: close sockets
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with an Amplifier-like object (in-process brain, or a
    DsampClient for a remote service). The theme is chosen ONCE here, from
    cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # swap the active palette first
    # Number widgets follow the Windows locale otherwise: "10,000" for 10 dB on
    # a PC with a comma decimal separator (docs/DEVELOPER_NOTES.md gotcha #18).
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
    amp, _ = build_sim_system(cfg)
    return run_app(amp, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
