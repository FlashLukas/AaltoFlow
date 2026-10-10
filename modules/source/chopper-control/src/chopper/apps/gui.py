"""Control GUI for the Thorlabs MC2000B optical chopper (dark or light theme).

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a Chopper-like object (the
in-process brain, or a ChopperClient facade for a remote service). It sends
commands (set_frequency / set_phase / set_enable / set_blade / ...) and reads a
status snapshot on a Qt timer. Brain events arrive on a Qt signal so they can
safely cross into the GUI thread.

The signature widget is the WheelIndicator: the mounted blade drawn with its
real slot count (both rings on the 10/100 blade), turning while the motor
runs, with the beam spot on the ring the reference locks to, and a rim that
turns green when the wheel is LOCKED.
"""

from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ..blades import blade_by_name
from ..config import Config
from ..sim_system import build_sim_system
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog
from .control_bar import ALWAYS_PROPERTY, ControlBar, mark_always


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


def _fmt(v: float, dec: int = 2) -> str:
    return "--" if (v is None or not math.isfinite(v)) else f"{v:.{dec}f}"


# ------------------------------------------------------------- the wheel

class WheelIndicator(QtWidgets.QWidget):
    """The chopper blade, turning.

    What it shows, and why:
      * the disc with the SLOTS of the mounted blade: 60 on the MC1F60; 100
        outer + 10 inner on the MC1F10HP. A physicist looking at it should
        recognise the wheel on the table.
      * rotation while the motor runs. The real wheel turns at f / N rev/s
        (150 Hz on 10 slots = 15 rev/s), far too fast to draw, so the drawing
        turns at a slow, log-scaled pace: faster wheel -> visibly faster, never
        a blur. It coasts to a stop when disabled, like the real one.
      * the BEAM: an amber spot at 12 o'clock on the ring the reference locks
        to. It is bright while a slot passes under it and dark when the blade
        blocks it -- the chopping itself.
      * the RIM: green when locked, amber while spinning up, grey in standby.
    """

    def __init__(self):
        super().__init__()
        self.setMinimumSize(260, 250)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._blade = blade_by_name("MC1F10HP")
        self._ring = "inner"
        self._running = False
        self._locked = False
        self._freq = 0.0
        self._angle = 0.0            # drawing angle in degrees
        self._spin = 0.0             # drawing speed in deg per tick (eases like a flywheel)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)  # ~30 fps; runs only while something moves
        self._timer.timeout.connect(self._tick)

    def set_state(self, blade: str, ref_mode: str, running: bool, locked: bool,
                  freq_Hz: float):
        try:
            self._blade = blade_by_name(blade)
        except ValueError:
            pass
        self._ring = self._blade.ring_of(ref_mode) if self._blade.two_ring else "outer"
        self._running = bool(running)
        self._locked = bool(locked)
        self._freq = freq_Hz if (freq_Hz is not None and math.isfinite(freq_Hz)) else 0.0
        if (self._running or self._spin > 0.01) and not self._timer.isActive():
            self._timer.start()
        self.update()

    def _goal_spin(self) -> float:
        if not self._running:
            return 0.0
        rps = max(self._freq, 0.0) / max(self._blade.slots(self._ring), 1)
        # 1 rev/s -> ~1.3 deg/tick; 100 rev/s -> ~4 deg/tick: visible, never a blur
        return 0.6 + 1.7 * math.log10(1.0 + rps)

    def _tick(self):
        goal = self._goal_spin()
        self._spin += (goal - self._spin) * (0.08 if goal > self._spin else 0.03)
        self._angle = (self._angle + self._spin) % 360.0
        if not self._running and self._spin < 0.01:
            self._spin = 0.0
            self._timer.stop()
        self.update()

    # -- drawing -----------------------------------------------------------

    @staticmethod
    def _slot_ring(p, cx, cy, r_in, r_out, n, angle, colour):
        """n slots (open sectors of 50 % duty) between two radii."""
        path = QtGui.QPainterPath()
        half = 180.0 / n / 2.0        # half of a slot's angular width, degrees
        for k in range(n):
            a0 = angle + k * 360.0 / n - half
            outer = QtCore.QRectF(cx - r_out, cy - r_out, 2 * r_out, 2 * r_out)
            inner = QtCore.QRectF(cx - r_in, cy - r_in, 2 * r_in, 2 * r_in)
            sub = QtGui.QPainterPath()
            sub.arcMoveTo(outer, -a0)
            sub.arcTo(outer, -a0, -2 * half)
            sub.arcTo(inner, -(a0 + 2 * half), 2 * half)
            sub.closeSubpath()
            path.addPath(sub)
        p.fillPath(path, colour)

    def _open_at_top(self, n: int) -> float:
        """0..1: how open the slot under the beam (12 o'clock) is right now."""
        pitch = 360.0 / n
        rel = ((90.0 - self._angle) % pitch) / pitch      # 0 = slot centre
        d = min(rel, 1.0 - rel)                            # 0..0.5
        return max(0.0, 1.0 - d / 0.25)                   # open within +-quarter pitch

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cap_h = 34
        R = max(20.0, min(w, h - cap_h) / 2.0 - 10)
        cx, cy = w / 2.0, (h - cap_h) / 2.0 + 4
        bg = QtGui.QColor(COLORS["panel"])
        metal = QtGui.QColor("#5a626e")
        metal_hi = QtGui.QColor("#78818d")
        accent = QtGui.QColor(COLORS["accent"])

        # rim: the lock state at a glance
        if self._locked and self._running:
            rim = QtGui.QColor(COLORS["ok"])
        elif self._running:
            rim = accent
        else:
            rim = QtGui.QColor(COLORS["border"])
        p.setPen(QtGui.QPen(rim, 4))
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawEllipse(QtCore.QPointF(cx, cy), R + 6, R + 6)

        # the blade: a metal disc with slots cut through (slots show the panel)
        p.setPen(QtCore.Qt.NoPen)
        grad = QtGui.QRadialGradient(cx - R * 0.3, cy - R * 0.3, R * 1.4)
        grad.setColorAt(0.0, metal_hi)
        grad.setColorAt(1.0, metal)
        p.setBrush(grad)
        p.drawEllipse(QtCore.QPointF(cx, cy), R, R)

        b = self._blade
        if b.two_ring:
            self._slot_ring(p, cx, cy, R * 0.80, R * 0.97, b.outer_slots, self._angle, bg)
            self._slot_ring(p, cx, cy, R * 0.42, R * 0.72, b.inner_slots, self._angle, bg)
        else:
            self._slot_ring(p, cx, cy, R * 0.45, R * 0.95, b.outer_slots, self._angle, bg)

        # hub
        p.setBrush(QtGui.QColor(COLORS["panel_hi"]))
        p.setPen(QtGui.QPen(metal, 2))
        p.drawEllipse(QtCore.QPointF(cx, cy), R * 0.18, R * 0.18)
        for k in range(3):                        # the three mounting screws
            a = math.radians(self._angle + 120 * k)
            p.setBrush(metal)
            p.drawEllipse(QtCore.QPointF(cx + R * 0.1 * math.cos(a),
                                         cy - R * 0.1 * math.sin(a)), 2.2, 2.2)

        # the beam, on the ring the reference locks to
        if b.two_ring and self._ring == "inner":
            r_beam, n_beam = R * 0.57, b.inner_slots
        elif b.two_ring:
            r_beam, n_beam = R * 0.885, b.outer_slots
        else:
            r_beam, n_beam = R * 0.70, b.outer_slots
        open_frac = self._open_at_top(n_beam) if self._running or self._spin > 0 else 0.5
        a = int(60 + 195 * open_frac)
        glow = QtGui.QColor(accent.red(), accent.green(), accent.blue(), a)
        p.setPen(QtCore.Qt.NoPen)
        halo = QtGui.QColor(accent.red(), accent.green(), accent.blue(), int(a * 0.35))
        p.setBrush(halo)
        p.drawEllipse(QtCore.QPointF(cx, cy - r_beam), 11, 11)
        p.setBrush(glow)
        p.drawEllipse(QtCore.QPointF(cx, cy - r_beam), 5.5, 5.5)

        # caption
        if not self._running:
            cap, col = f"STANDBY  -  {b.name}", QtGui.QColor(COLORS["muted"])
        elif self._locked:
            cap, col = f"LOCKED  -  {b.name}", QtGui.QColor(COLORS["ok"])
        else:
            cap, col = f"LOCKING ...  -  {b.name}", QtGui.QColor(COLORS["accent_hi"])
        p.setPen(col)
        f = p.font(); f.setBold(True); f.setPointSize(9); p.setFont(f)
        p.drawText(QtCore.QRectF(0, h - cap_h + 8, w, 20), QtCore.Qt.AlignHCenter, cap)
        p.end()


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self._mode_sig = None        # (blade, ref, output, owned) the combos show
        self._limits_sig = None
        self._btn_state = None
        title = "MC2000B - Optical Chopper"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1180, 760)

        root = QtWidgets.QWidget(); root.setObjectName("root")
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)
        # Control or viewer (control_bar.py): a bar across the top, only for a
        # GUI on a service whose client knows about control -- a local GUI
        # owns its chopper and has nobody to share it with.
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

        # start the brain (opens the backend, adopts its state) and the timer
        self.ctrl.start()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()
        self._refresh()
        st = self.ctrl.status()
        if math.isfinite(st.setpoint_frequency_Hz):
            self.freq_spin.setValue(st.setpoint_frequency_Hz)
        self.phase_spin.setValue(st.phase_deg)

        # The first GUI to connect gets control; a later one opens as a viewer
        # (control_bar.py). Only once the log exists, so the bar can say so.
        if self._control_bar is not None:
            self._control_bar.claim_if_free()

    # ---- layout ----------------------------------------------------------

    def _build_sidebar(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(370)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(14)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("MC2000B")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
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
        self.state_badge = QtWidgets.QLabel("STANDBY")
        self.state_badge.setObjectName("stateBadge")
        top.addWidget(self.state_badge)
        self.lock_badge = QtWidgets.QLabel("NOT LOCKED")
        self.lock_badge.setObjectName("stateBadge")
        top.addWidget(self.lock_badge)
        top.addStretch(1)
        self.conn_dot = QtWidgets.QLabel("connecting")
        self.conn_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        top.addWidget(self.conn_dot)
        rlay.addLayout(top)
        self.idn_label = QtWidgets.QLabel("--")
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        self.idn_label.setWordWrap(True)
        rlay.addWidget(self.idn_label)
        col.addWidget(rcard)

        self.run_btn = QtWidgets.QPushButton("Start")
        self.run_btn.setObjectName("primary")
        self.run_btn.setMinimumHeight(44)
        self.run_btn.clicked.connect(self._toggle_run)
        col.addWidget(self.run_btn)

        # frequency
        fcard, flay = _card("Chopping frequency")
        frow = QtWidgets.QHBoxLayout()
        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setDecimals(1); self.freq_spin.setSuffix("  Hz")
        self.freq_spin.setRange(1.0, 10_000.0); self.freq_spin.setSingleStep(10.0)
        self.freq_set = QtWidgets.QPushButton("Set"); self.freq_set.setObjectName("primary")
        self.freq_set.clicked.connect(self._set_frequency)
        frow.addWidget(self.freq_spin, 1); frow.addWidget(self.freq_set)
        flay.addLayout(frow)
        # SWEEP (2026-10-10): walk the frequency CONTINUOUSLY to the value
        # above at a set pace -- what a fly scan does row by row, by hand.
        # "Stop" ends it where it is.
        srow = QtWidgets.QHBoxLayout()
        lim = self.cfg.limits
        self.sweep_rate = QtWidgets.QDoubleSpinBox()
        self.sweep_rate.setDecimals(2); self.sweep_rate.setSuffix("  Hz/s")
        self.sweep_rate.setRange(lim.sweep_rate_min_Hz_per_s, lim.sweep_rate_max_Hz_per_s)
        self.sweep_rate.setValue(max(lim.sweep_rate_min_Hz_per_s,
                                     min(lim.sweep_rate_max_Hz_per_s, 5.0)))
        self.sweep_btn = QtWidgets.QPushButton("Sweep to")
        self.sweep_btn.setToolTip("Sweep the frequency continuously to the value above "
                                  "at this pace")
        self.sweep_btn.clicked.connect(
            lambda: self._try(self.ctrl.ramp_frequency, self.freq_spin.value(),
                              self.sweep_rate.value()))
        sweep_stop = QtWidgets.QPushButton("Stop sweep")
        sweep_stop.setToolTip("End the sweep where it is (the wheel keeps running; the motor Stop is above)")
        sweep_stop.clicked.connect(lambda: self._try(self.ctrl.ramp_stop))
        mark_always(sweep_stop)      # ramp_stop is a safety verb: a viewer may stop
        srow.addWidget(self.sweep_rate, 1); srow.addWidget(self.sweep_btn)
        srow.addWidget(sweep_stop)
        flay.addLayout(srow)
        self.range_hint = QtWidgets.QLabel("")
        self.range_hint.setObjectName("hint")
        self.range_hint.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        flay.addWidget(self.range_hint)
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(QtWidgets.QLabel("Phase"))
        self.phase_spin = QtWidgets.QDoubleSpinBox()
        self.phase_spin.setDecimals(0); self.phase_spin.setSuffix("  deg")
        self.phase_spin.setRange(self.cfg.limits.phase_min_deg, self.cfg.limits.phase_max_deg)
        set_ph = QtWidgets.QPushButton("Set"); set_ph.clicked.connect(self._set_phase)
        prow.addWidget(self.phase_spin, 1); prow.addWidget(set_ph)
        flay.addLayout(prow)
        col.addWidget(fcard)

        # blade and modes (standby only)
        bcard, blay = _card("Blade and reference (standby only)")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        self.blade_combo = QtWidgets.QComboBox()
        self.blade_combo.currentTextChanged.connect(self._blade_picked)
        self.ref_combo = QtWidgets.QComboBox()
        self.out_combo = QtWidgets.QComboBox()
        form.addRow("Blade", self.blade_combo)
        form.addRow("Reference in", self.ref_combo)
        form.addRow("Reference out", self.out_combo)
        hrow = QtWidgets.QHBoxLayout()
        self.n_spin = QtWidgets.QSpinBox(); self.n_spin.setRange(1, 15); self.n_spin.setPrefix("N ")
        self.d_spin = QtWidgets.QSpinBox(); self.d_spin.setRange(1, 15); self.d_spin.setPrefix("D ")
        hrow.addWidget(self.n_spin); hrow.addWidget(self.d_spin)
        form.addRow("Ext. harmonics", hrow)
        blay.addLayout(form)
        self.apply_modes = QtWidgets.QPushButton("Apply blade and modes")
        self.apply_modes.clicked.connect(self._apply_modes)
        blay.addWidget(self.apply_modes)
        col.addWidget(bcard)

        col.addStretch(1)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        ocard, olay = _card("Wheel")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(24)
        readouts = QtWidgets.QVBoxLayout(); readouts.setSpacing(14)
        self.set_value = self._readout(readouts, "Target", "Hz")
        self.meas_value = self._readout(readouts, "Measured", "Hz")
        self.err_value = self._readout(readouts, "Error", "Hz", small=True)
        self.aux_label = QtWidgets.QLabel("")
        self.aux_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:12px;")
        self.aux_label.setWordWrap(True)
        readouts.addWidget(self.aux_label)
        readouts.addStretch(1)
        holder = QtWidgets.QWidget(); holder.setLayout(readouts); holder.setMinimumWidth(260)
        row.addWidget(holder, 0)
        self.wheel = WheelIndicator()
        row.addWidget(self.wheel, 1)
        olay.addLayout(row)
        colw.addWidget(ocard, 3)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(120)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 2)
        return panel

    def _readout(self, box, label, unit, small=False):
        cap = QtWidgets.QLabel(label.upper())
        cap.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; font-weight:700; letter-spacing:1px;")
        line = QtWidgets.QHBoxLayout(); line.setSpacing(5)
        val = QtWidgets.QLabel("--"); val.setObjectName("bigValue")
        if small:
            val.setStyleSheet("font-size:22px;")
        val.setMinimumWidth(125)
        u = QtWidgets.QLabel(unit); u.setObjectName("unit")
        line.addWidget(val); line.addWidget(u, 0, QtCore.Qt.AlignBottom); line.addStretch(1)
        box.addWidget(cap); box.addLayout(line)
        return val

    # ---- actions ---------------------------------------------------------

    def _try(self, fn, *args):
        """Run a command; a refusal (ValueError locally, RuntimeError from the
        service) goes to the log in red instead of crashing the window."""
        try:
            return fn(*args)
        except (ValueError, RuntimeError) as exc:
            self._on_event("error", str(exc))
            return None

    def _toggle_run(self):
        # Running -> the button reads "Stop" and sends the SAFETY verb (which a
        # viewer may send too, see _refresh); standby -> "Start", a change.
        if self.ctrl.status().enabled:
            self._try(self.ctrl.standby)
        else:
            self._try(self.ctrl.set_enable, True)

    def _set_frequency(self):
        self._try(self.ctrl.set_frequency, self.freq_spin.value())

    def _set_phase(self):
        self._try(self.ctrl.set_phase, self.phase_spin.value())

    def _blade_picked(self, name: str):
        """Offer the modes of the blade just PICKED (not yet applied)."""
        try:
            b = blade_by_name(name)
        except ValueError:
            return
        st = self.ctrl.status()
        for combo, opts, cur in ((self.ref_combo, b.ref_modes, st.ref_mode),
                                 (self.out_combo, b.output_modes, st.output_mode)):
            combo.blockSignals(True)
            combo.clear(); combo.addItems(list(opts))
            if cur in opts:
                combo.setCurrentText(cur)
            combo.blockSignals(False)

    def _apply_modes(self):
        st = self.ctrl.status()
        blade = self.blade_combo.currentText()
        ref, out = self.ref_combo.currentText(), self.out_combo.currentText()
        if blade and blade != st.blade:
            if self._try(self.ctrl.set_blade, blade) is None and \
                    self.ctrl.status().blade != blade:
                return
        st = self.ctrl.status()
        if ref and ref != st.ref_mode:
            self._try(self.ctrl.set_ref_mode, ref)
        if out and out != st.output_mode:
            self._try(self.ctrl.set_output_mode, out)
        if (self.n_spin.value(), self.d_spin.value()) != (st.nharmonic, st.dharmonic):
            self._try(self.ctrl.set_harmonics, self.n_spin.value(), self.d_spin.value())
        self._mode_sig = None          # re-sync the combos with what was accepted

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        self.phase_spin.setRange(self.cfg.limits.phase_min_deg, self.cfg.limits.phase_max_deg)
        self._mode_sig = None
        self._limits_sig = None

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
        try:
            res = blade_by_name(s.blade).resolution_Hz
        except ValueError:
            res = 1.0
        dec = 0 if res >= 1 else 1

        self.set_value.setText(_fmt(s.target_frequency_Hz, dec))
        self.meas_value.setText(_fmt(s.frequency_Hz, 2) if s.lock_source == "measured"
                                else "blind")
        self.err_value.setText(_fmt(s.freq_error_Hz, 3))
        extra = [f"REF OUT {_fmt(s.refout_frequency_Hz, 2)} Hz ({s.output_mode})",
                 f"reference in: {s.ref_mode}"]
        if s.external:
            extra.append(f"EXT REF IN {_fmt(s.input_frequency_Hz, 2)} Hz  x {s.nharmonic}/{s.dharmonic}")
        if s.lock_source == "timer":
            extra.append("REF OUT is on 'target': the wheel is not measured, "
                         "the lock is a timer")
        if s.hw_error:
            extra.append(f"hardware: {s.hw_error}")
        self.aux_label.setText("\n".join(extra))

        # limits -> spin range (only when they move, so typing is not disturbed)
        lim_sig = (s.freq_min_Hz, s.freq_max_Hz, dec)
        if lim_sig != self._limits_sig and math.isfinite(s.freq_min_Hz):
            self._limits_sig = lim_sig
            self.freq_spin.setDecimals(dec)
            self.freq_spin.setSingleStep(max(res, 1.0) * (10 if res >= 1 else 1))
            self.freq_spin.setRange(s.freq_min_Hz, s.freq_max_Hz)
            self.range_hint.setText(f"{s.blade}: {s.freq_min_Hz:g} - {s.freq_max_Hz:g} Hz")
        self.freq_spin.setEnabled(not s.external)
        self.freq_set.setEnabled(not s.external)
        self.sweep_btn.setEnabled(not s.external)     # a sweep needs internal reference
        if s.external:
            self.range_hint.setText("external reference: frequency = EXT REF IN x N / D")

        # blade / mode combos follow the controller when IT changes
        sig = (s.blade, s.ref_mode, s.output_mode, tuple(s.owned_blades),
               s.nharmonic, s.dharmonic)
        if sig != self._mode_sig:
            self._mode_sig = sig
            opts = list(s.owned_blades) + ([s.blade] if s.blade not in s.owned_blades else [])
            self.blade_combo.blockSignals(True)
            self.blade_combo.clear(); self.blade_combo.addItems(opts)
            self.blade_combo.setCurrentText(s.blade)
            self.blade_combo.blockSignals(False)
            self._blade_picked(s.blade)
            self.n_spin.setValue(s.nharmonic); self.d_spin.setValue(s.dharmonic)
        standby = not s.enabled
        for w in (self.blade_combo, self.ref_combo, self.out_combo, self.n_spin,
                  self.d_spin, self.apply_modes):
            w.setEnabled(standby and s.connected)

        # run / lock badges (restyle only when the state flips)
        state = (s.enabled, s.locked, s.connected)
        if state != self._btn_state:
            self._btn_state = state
            if s.enabled:
                self.state_badge.setText("RUNNING")
                self._badge(self.state_badge, COLORS["accent"])
                self.run_btn.setText("Stop"); self.run_btn.setObjectName("danger")
            else:
                self.state_badge.setText("STANDBY")
                self._badge(self.state_badge, COLORS["muted"])
                self.run_btn.setText("Start"); self.run_btn.setObjectName("primary")
            # One button, two jobs: while it says "Stop" it is the safety
            # button (a viewer may press it, control_bar.py); while it says
            # "Start" the viewer guard blocks it like any other change.
            self.run_btn.setProperty(ALWAYS_PROPERTY, bool(s.enabled))
            self.run_btn.style().unpolish(self.run_btn)
            self.run_btn.style().polish(self.run_btn)
            if s.locked:
                self.lock_badge.setText("LOCKED")
                self._badge(self.lock_badge, COLORS["ok"])
            else:
                self.lock_badge.setText("NOT LOCKED")
                self._badge(self.lock_badge, COLORS["muted"])
            if s.connected:
                self.conn_dot.setText("connected")
                self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
            else:
                self.conn_dot.setText("offline")
                self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        if s.idn:
            self.idn_label.setText(s.idn + ("  (simulated)" if s.simulated and
                                            "simulated" not in s.idn else ""))

        shown = s.frequency_Hz if math.isfinite(s.frequency_Hz) else (
            s.target_frequency_Hz if s.enabled else 0.0)
        self.wheel.set_state(s.blade, s.ref_mode, s.enabled, s.locked, shown)

    def _badge(self, label, color):
        label.setStyleSheet(
            f"QLabel#stateBadge {{ color:{color}; border:1px solid {color}; "
            f"background:{COLORS['panel_hi']}; border-radius:10px; padding:4px 12px; "
            f"font-weight:700; letter-spacing:1px; }}")

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()          # disconnect; the wheel is left as it is
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Chopper-like object (the in-process brain, or a
    ChopperClient facade for a remote service). The theme is chosen ONCE here,
    from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # swap the active palette first
    # Number widgets follow the Windows locale otherwise: "1,000" for 1000 Hz
    # on the lab PC (gotcha #18). C locale, no group separator.
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
    ch, _ = build_sim_system(cfg)
    return run_app(ch, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
