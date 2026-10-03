"""Control GUI for the SuperK EXTREME supercontinuum laser + SuperK SELECT AOTF.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a brain-like object (the real
in-process SuperK brain, or a SuperkClient facade for a remote service). It
sends commands and reads a status snapshot on a Qt timer to update the numbers
and the spectrum picture. Brain events arrive on a Qt signal so they can safely
cross into the GUI thread.

The signature widget is the SpectrumIndicator: the white-light envelope of the
EXTREME from 400 to 2400 nm, the tuning windows of the AOTF crystals (the
active one lit), and the up-to-8 lines the AOTF diffracts, each drawn at its
wavelength in its own colour (visible lines in the colour the eye sees, IR
lines in the accent) and as tall as the model says it is bright. While the
laser is not emitting the envelope and the lines are only outlined -- a picture
of what WOULD come out, not of light that is there.

CLASS 4 LASER: the Emission ON button asks for confirmation, and is refused by
the brain while the interlock is not OK (the refusal appears in the log).
"""

from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from .. import model
from ..config import Config, N_LINES, names, floats
from ..sim_system import build_sim_system
from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog
from .control_bar import ControlBar, mark_always


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
    w.setRange(lo, hi); w.setDecimals(dec); w.setSingleStep(step)
    w.setValue(value); w.setSuffix("  " + suffix)
    # C locale: "700.000", never "700,000" on a Finnish Windows (gotcha #18)
    w.setLocale(QtCore.QLocale.c())
    return w


def filter_table(cfg: Config) -> list[tuple[str, float, float]]:
    """[(name, min_nm, max_nm), ...] from the config's comma lists."""
    n = names(cfg.filters.names)
    lo = floats(cfg.filters.min_nm, len(n), 400.0)
    hi = floats(cfg.filters.max_nm, len(n), 2400.0)
    return list(zip(n, lo, hi))


# ------------------------------------------------------------- the spectrum

class SpectrumIndicator(QtWidgets.QWidget):
    """White-light envelope + AOTF crystal windows + the diffracted lines."""

    NM_LO, NM_HI = model.SC_MIN_NM, model.SC_MAX_NM
    Y_FULL = 3.0          # shape value drawn at full height (the 1064 nm pump peak clips)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(190)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self._emitting = False
        self._rf = False
        self._power = 0.0
        self._filters: list[tuple[str, float, float]] = []
        self._active = ""
        self._lines: list[tuple[float, float]] = []
        self._phase = 0.0
        # the shimmer runs on its own timer so it is smooth whatever the poll rate
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_state(self, emitting, rf_on, power_pct, filters, active, lines):
        self._emitting = bool(emitting)
        self._rf = bool(rf_on)
        self._power = float(power_pct)
        self._filters = list(filters)
        self._active = active
        self._lines = list(lines)
        live = self._emitting and self._rf
        if live and not self._timer.isActive():
            self._timer.start()
        elif not live and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        self._phase = (self._phase + 0.05) % (2 * math.pi)
        self.update()

    # -- drawing -----------------------------------------------------------

    def paintEvent(self, ev):
        from PySide6.QtGui import QPainter, QColor, QPen, QPainterPath, QLinearGradient
        from PySide6.QtCore import QRectF, QPointF, Qt

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        left, right, top, bottom = 34.0, 12.0, 22.0, 34.0
        pw, ph = w - left - right, h - top - bottom
        y0 = top + ph

        def X(nm):
            return left + (nm - self.NM_LO) / (self.NM_HI - self.NM_LO) * pw

        def Y(density):
            return y0 - min(1.0, density / self.Y_FULL) * ph

        text = QColor(COLORS["text"]); muted = QColor(COLORS["muted"])
        accent = QColor(COLORS["accent"]); accent_hi = QColor(COLORS["accent_hi"])
        f = p.font(); f.setPointSize(7); p.setFont(f)

        # ---- plot frame + wavelength grid -----------------------------------
        p.setPen(Qt.NoPen); p.setBrush(QColor(COLORS["code_bg"]))
        p.drawRoundedRect(QRectF(left, top, pw, ph), 6, 6)
        p.setPen(QPen(QColor(COLORS["grid"]), 1))
        for nm in range(500, 2400, 250):
            p.drawLine(QPointF(X(nm), top), QPointF(X(nm), y0))
        p.setPen(muted)
        for nm in range(500, 2401, 500):
            p.drawText(QRectF(X(nm) - 30, y0 + 3, 60, 12), Qt.AlignHCenter, f"{nm}")
        p.drawText(QRectF(left + pw - 60, y0 + 3, 60, 12), Qt.AlignRight, "nm")

        # ---- crystal windows (all outlined, the active one lit) --------------
        for name, lo, hi in self._filters:
            active = name == self._active
            c = QColor(accent); c.setAlpha(55 if active else 0)
            border = QColor(accent if active else muted)
            border.setAlpha(230 if active else 110)
            p.setBrush(c)
            pen = QPen(border, 1.4 if active else 1.0)
            if not active:
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            r = QRectF(X(lo), top + 2, X(hi) - X(lo), ph - 2)
            p.drawRect(r)
            p.setPen(accent_hi if active else muted)
            fb = p.font(); fb.setBold(active); p.setFont(fb)
            p.drawText(QRectF(r.left(), top - 16, r.width(), 14), Qt.AlignHCenter,
                       name)
        fb = p.font(); fb.setBold(False); p.setFont(fb)

        # ---- supercontinuum envelope -------------------------------------
        # The SHAPE of a supercontinuum hardly changes with the power level,
        # its brightness does. So the curve is the normalised shape, and the
        # power level sets how bright the fill is (and a label says it).
        power = max(0.0, self._power)
        path = QPainterPath(); path.moveTo(X(self.NM_LO), y0)
        steps = 240
        for k in range(steps + 1):
            nm = self.NM_LO + (self.NM_HI - self.NM_LO) * k / steps
            path.lineTo(X(nm), Y(model.sc_density_mW_per_nm(nm, 100.0)))
        path.lineTo(X(self.NM_HI), y0); path.closeSubpath()
        if self._emitting:
            grad = QLinearGradient(0, top, 0, y0)
            c1 = QColor(accent_hi); c1.setAlpha(int(60 + 150 * min(1.0, power / 100)))
            c2 = QColor(accent); c2.setAlpha(20)
            grad.setColorAt(0, c1); grad.setColorAt(1, c2)
            p.setBrush(grad); p.setPen(QPen(accent_hi, 1.6))
        else:
            p.setBrush(Qt.NoBrush)
            pen = QPen(muted, 1.2); pen.setStyle(Qt.DashLine); p.setPen(pen)
        p.drawPath(path)
        p.setPen(accent_hi if self._emitting else muted)
        p.drawText(QRectF(left + pw - 130, top + 4, 124, 12), Qt.AlignRight,
                   f"power level {power:.1f} %")

        # ---- the AOTF lines ------------------------------------------------
        live = self._emitting and self._rf
        for i, (wl, amp) in enumerate(self._lines):
            if amp <= 0 or wl <= 0:
                continue
            rgb = model.wavelength_rgb(wl)
            col = QColor(*rgb) if rgb else QColor(accent_hi)
            # line height = the envelope there x the AOTF efficiency at this RF
            # amplitude (sin^2, saturating) -- so it always sits under the curve
            dens = model.sc_density_mW_per_nm(wl, 100.0) * model.aotf_efficiency(amp)
            top_y = Y(dens)
            x = X(wl)
            if live:
                shimmer = 0.85 + 0.15 * math.sin(self._phase + i)
                glow = QColor(col); glow.setAlpha(int(70 * shimmer))
                p.setPen(QPen(glow, 9, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(QPointF(x, y0), QPointF(x, top_y))
                p.setPen(QPen(col, 3, Qt.SolidLine, Qt.RoundCap))
            else:
                pen = QPen(col, 2, Qt.DashLine); p.setPen(pen)
            p.drawLine(QPointF(x, y0), QPointF(x, top_y))
            p.setPen(text)
            p.drawText(QRectF(x - 12, top_y - 14, 24, 12), Qt.AlignHCenter, str(i + 1))

        # ---- caption -------------------------------------------------------
        if live:
            cap, col = "EMITTING - AOTF LINES ON", accent_hi
        elif self._emitting:
            cap, col = "EMITTING - RF OFF (no line out)", accent
        else:
            cap, col = "emission off - outline shows the set spectrum", muted
        p.setPen(col)
        fb = p.font(); fb.setBold(True); p.setFont(fb)
        p.drawText(QRectF(0, h - 14, w, 13), Qt.AlignHCenter, cap)
        # y-axis label
        p.setPen(muted); fb.setBold(False); p.setFont(fb)
        p.save(); p.translate(12, top + ph / 2); p.rotate(-90)
        p.drawText(QRectF(-60, -8, 120, 14), Qt.AlignHCenter, "relative density (model)")
        p.restore()
        p.end()


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self._range = (0.0, 0.0)
        title = "SuperK EXTREME + SELECT - Supercontinuum laser"
        if remote:
            title += "  (remote)"
        self.setWindowTitle(title)
        self.resize(1280, 860)

        root = QtWidgets.QWidget(); root.setObjectName("root")
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)
        # Control or viewer (control_bar.py): a bar across the top, only for a
        # GUI on a service whose client knows about control -- a local GUI
        # owns its laser and has nobody to share it with.
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

        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        self.ctrl._on_event = lambda lvl, msg: self.bridge.event.emit(lvl, msg)

        self.ctrl.start()
        self._fill_filters()
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
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(14)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("SUPERK")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; "
                            f"font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        mark_always(settings_btn)    # a viewer may LOOK; the service refuses the OK
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        header.addWidget(settings_btn)
        col.addLayout(header)

        # emission state + interlock
        scard, slay = _card()
        top = QtWidgets.QHBoxLayout()
        self.state_badge = QtWidgets.QLabel("EMISSION OFF")
        self.state_badge.setObjectName("stateBadge")
        top.addWidget(self.state_badge); top.addStretch(1)
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        self.conn_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        top.addWidget(self.conn_dot)
        slay.addLayout(top)
        self.interlock_label = QtWidgets.QLabel("Interlock: -")
        slay.addWidget(self.interlock_label)
        self.idn_label = QtWidgets.QLabel("-")
        self.idn_label.setWordWrap(True)
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        slay.addWidget(self.idn_label)
        col.addWidget(scard)

        # emission buttons: ON is the dangerous one -> red, and it asks first
        erow = QtWidgets.QHBoxLayout()
        self.em_on_btn = QtWidgets.QPushButton("Emission ON")
        self.em_on_btn.setObjectName("danger"); self.em_on_btn.setMinimumHeight(42)
        self.em_on_btn.setToolTip("Class 4 laser: asks for confirmation. Refused "
                                  "while the interlock is not OK.")
        self.em_on_btn.clicked.connect(lambda: self._emission_on(confirm=True))
        self.em_off_btn = QtWidgets.QPushButton("Emission OFF")
        self.em_off_btn.setObjectName("primary"); self.em_off_btn.setMinimumHeight(42)
        # the SAFETY verb emission_off (net/service.py): works for a viewer too
        self.em_off_btn.clicked.connect(lambda: self._safe(self.ctrl.emission_off))
        mark_always(self.em_off_btn)
        erow.addWidget(self.em_on_btn); erow.addWidget(self.em_off_btn)
        col.addLayout(erow)
        reset = QtWidgets.QPushButton("Reset interlock")
        reset.clicked.connect(lambda: self._safe(self.ctrl.reset_interlock))
        col.addWidget(reset)

        # power level
        pcard, play = _card("Power level")
        prow = QtWidgets.QHBoxLayout()
        lim = self.cfg.limits
        self.power_spin = _dspin(lim.power_min_pct, lim.power_max_pct, 1, 1.0,
                                 self.cfg.startup.power_pct, "%")
        set_pow = QtWidgets.QPushButton("Set"); set_pow.setObjectName("primary")
        set_pow.clicked.connect(self._set_power)
        prow.addWidget(self.power_spin, 1); prow.addWidget(set_pow)
        play.addLayout(prow)
        col.addWidget(pcard)

        # AOTF: crystal + RF
        fcard, flay = _card("AOTF filter")
        frow = QtWidgets.QHBoxLayout()
        self.filter_combo = QtWidgets.QComboBox()
        # `activated` fires only on a USER choice, so the poll updating the
        # combo cannot send set_filter back (same idea as gotcha #13)
        self.filter_combo.activated.connect(self._set_filter)
        self.rf_btn = QtWidgets.QPushButton("RF On"); self.rf_btn.setObjectName("primary")
        self.rf_btn.clicked.connect(self._toggle_rf)
        frow.addWidget(self.filter_combo, 1); frow.addWidget(self.rf_btn)
        flay.addLayout(frow)
        self.range_label = QtWidgets.QLabel("-")
        self.range_label.setStyleSheet(f"color:{COLORS['muted']};")
        flay.addWidget(self.range_label)
        col.addWidget(fcard)

        # line 1 = the scan line
        lcard, llay = _card("Line 1 (scan line)")
        wl0 = floats(self.cfg.startup.wavelengths_nm, N_LINES, 650.0)
        am0 = floats(self.cfg.startup.amplitudes_pct, N_LINES, 0.0)
        self.wl1_spin = _dspin(400, 2400, 3, 1.0, wl0[0], "nm")
        self.amp1_spin = _dspin(0, lim.amplitude_max_pct, 1, 5.0, am0[0], "%")
        grid = QtWidgets.QGridLayout()
        grid.addWidget(QtWidgets.QLabel("Wavelength"), 0, 0); grid.addWidget(self.wl1_spin, 0, 1)
        grid.addWidget(QtWidgets.QLabel("Amplitude"), 1, 0); grid.addWidget(self.amp1_spin, 1, 1)
        llay.addLayout(grid)
        set_l1 = QtWidgets.QPushButton("Set line 1"); set_l1.setObjectName("primary")
        set_l1.clicked.connect(lambda: self._set_line(1))
        llay.addWidget(set_l1)
        col.addWidget(lcard)

        col.addStretch(1)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(14)

        ocard, olay = _card("Output")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(28)
        self.power_value = self._readout(row, "Power level", "%", minw=90)
        self.wl_value = self._readout(row, "Line 1", "nm", minw=150)
        self.amp_value = self._readout(row, "Amplitude", "%", minw=90)
        self.inlet_value = self._readout(row, "Inlet", "C", minw=70)
        self.xtal_value = self._readout(row, "Crystal", "C", minw=70)
        row.addStretch(1)
        olay.addLayout(row)
        self.spectrum = SpectrumIndicator()
        olay.addWidget(self.spectrum)
        colw.addWidget(ocard)

        # lines 2..8
        tcard, tlay = _card("Lines 2 - 8   (amplitude 0 = line off)")
        grid = QtWidgets.QGridLayout(); grid.setHorizontalSpacing(10); grid.setVerticalSpacing(4)
        wl0 = floats(self.cfg.startup.wavelengths_nm, N_LINES, 650.0)
        am0 = floats(self.cfg.startup.amplitudes_pct, N_LINES, 0.0)
        self.line_wl, self.line_amp, self.line_now = {}, {}, {}
        for k, n in enumerate(range(2, N_LINES + 1)):
            r, c = k % 4, (k // 4) * 5
            lab = QtWidgets.QLabel(f"{n}")
            lab.setStyleSheet(f"color:{COLORS['accent']}; font-weight:800;")
            wl = _dspin(400, 2400, 1, 1.0, wl0[n - 1], "nm")
            am = _dspin(0, self.cfg.limits.amplitude_max_pct, 1, 5.0, am0[n - 1], "%")
            btn = QtWidgets.QPushButton("Set")
            btn.clicked.connect(lambda _=False, n=n: self._set_line(n))
            now = QtWidgets.QLabel("-"); now.setMinimumWidth(110)
            now.setStyleSheet(f"color:{COLORS['muted']};")
            grid.addWidget(lab, r, c); grid.addWidget(wl, r, c + 1)
            grid.addWidget(am, r, c + 2); grid.addWidget(btn, r, c + 3)
            grid.addWidget(now, r, c + 4)
            self.line_wl[n], self.line_amp[n], self.line_now[n] = wl, am, now
        tlay.addLayout(grid)
        colw.addWidget(tcard)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(110)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 1)
        return panel

    def _readout(self, row, label, unit, minw=120):
        box = QtWidgets.QVBoxLayout(); box.setSpacing(2)
        cap = QtWidgets.QLabel(label.upper())
        cap.setStyleSheet(f"color:{COLORS['muted']}; font-size:10px; "
                          f"font-weight:700; letter-spacing:1px;")
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

    def _safe(self, fn, *args):
        """Run a command; a refusal (SafetyError / ValueError from the client,
        ControlRefused when another PC holds control) goes to the log in red
        instead of crashing the GUI."""
        try:
            fn(*args)
        except Exception as exc:
            self._on_event("error", str(exc))

    def _emission_on(self, confirm: bool = True):
        if confirm:
            ans = QtWidgets.QMessageBox.warning(
                self, "Class 4 laser",
                "Switch the SuperK emission ON?\n\nCheck that the beam path is "
                "enclosed and everyone in the lab wears laser goggles.",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Cancel)
            if ans != QtWidgets.QMessageBox.Yes:
                return
        self._safe(self.ctrl.set_emission, True)

    def _set_power(self):
        self._safe(self.ctrl.set_power, self.power_spin.value())

    def _set_filter(self, index: int):
        self._safe(self.ctrl.set_filter, self.filter_combo.itemText(index))

    def _toggle_rf(self):
        self._safe(self.ctrl.set_rf, not bool(self.ctrl.status().rf_set))

    def _set_line(self, n: int):
        if n == 1:
            wl, am = self.wl1_spin.value(), self.amp1_spin.value()
        else:
            wl, am = self.line_wl[n].value(), self.line_amp[n].value()
        self._safe(self.ctrl.set_line, n, wl, am)

    def _open_settings(self):
        self._safe(self.ctrl.get_config)   # fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        lim = self.cfg.limits
        self.power_spin.setRange(lim.power_min_pct, lim.power_max_pct)
        for sp in [self.amp1_spin] + list(self.line_amp.values()):
            sp.setMaximum(lim.amplitude_max_pct)
        self._fill_filters()

    def _fill_filters(self):
        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        self.filter_combo.addItems([n for n, _, _ in filter_table(self.cfg)])
        self.filter_combo.blockSignals(False)

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
        self.power_value.setText(f"{s.power_pct:.1f}")
        self.wl_value.setText(f"{s.wavelength_nm[0]:.3f}")
        self.amp_value.setText(f"{s.amplitude_pct[0]:.1f}")
        self.inlet_value.setText(f"{s.inlet_temp_C:.1f}")
        self.xtal_value.setText(f"{s.crystal_temp_C:.1f}")

        # emission badge -- restyle only when the state changes
        state = s.emission_state
        if state != getattr(self, "_badge_state", None):
            self._badge_state = state
            text, color = {
                "on": ("EMISSION ON", COLORS["danger"]),
                "starting": ("STARTING", COLORS["accent"]),
                "interlock": ("INTERLOCK", COLORS["accent"]),
                "error": ("HW ERROR", COLORS["danger"]),
            }.get(state, ("EMISSION OFF", COLORS["muted"]))
            self.state_badge.setText(text)
            self.state_badge.setStyleSheet(
                f"QLabel#stateBadge {{ color:{color}; border:1px solid {color}; "
                f"background:{COLORS['panel_hi']}; border-radius:10px; padding:4px 12px; "
                f"font-weight:700; letter-spacing:1px; }}")
        ok = s.interlock_ok
        self.interlock_label.setText(f"Interlock: {s.interlock or '-'}")
        self.interlock_label.setStyleSheet(
            f"color:{COLORS['ok'] if ok else COLORS['danger']}; font-weight:600;")

        if s.connected:
            self.conn_dot.setText("●  connected")
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        self.idn_label.setText(s.hw_error or s.idn or "-")

        if bool(s.rf_on) != getattr(self, "_rf_state", None):
            self._rf_state = bool(s.rf_on)
            self.rf_btn.setText("RF Off" if s.rf_on else "RF On")
            self.rf_btn.setObjectName("danger" if s.rf_on else "primary")
            self.rf_btn.style().unpolish(self.rf_btn)
            self.rf_btn.style().polish(self.rf_btn)

        # crystal combo + the live wavelength range of every line spin box
        if not self.filter_combo.view().isVisible():
            i = self.filter_combo.findText(s.filter)
            if i >= 0 and i != self.filter_combo.currentIndex():
                self.filter_combo.blockSignals(True)
                self.filter_combo.setCurrentIndex(i)
                self.filter_combo.blockSignals(False)
        rng = (s.filter_min_nm, s.filter_max_nm)
        if rng != self._range and rng[1] > rng[0]:
            self._range = rng
            for sp in [self.wl1_spin] + list(self.line_wl.values()):
                sp.setRange(*rng)
            self.range_label.setText(f"{s.filter}: {rng[0]:g} - {rng[1]:g} nm")

        # The input boxes start from the presets; the first time the laser is
        # seen they are filled with what it is REALLY set to (the service
        # adopted it at start), so a "Set" never sends a stale preset by
        # accident. Only once: afterwards they belong to the operator.
        if s.connected and not getattr(self, "_seeded", False):
            self._seeded = True
            self.power_spin.setValue(s.power_set_pct)
            self.wl1_spin.setValue(s.wavelength_set_nm[0])
            self.amp1_spin.setValue(s.amplitude_set_pct[0])
            for n in self.line_wl:
                self.line_wl[n].setValue(s.wavelength_set_nm[n - 1])
                self.line_amp[n].setValue(s.amplitude_set_pct[n - 1])

        for n, lab in self.line_now.items():
            a = s.amplitude_pct[n - 1]
            lab.setText(f"{s.wavelength_nm[n - 1]:.1f} nm  {a:.0f} %" if a > 0 else "off")

        self.spectrum.set_state(
            s.emission_on, s.rf_on, s.power_pct if s.power_pct else s.power_set_pct,
            filter_table(self.cfg), s.filter,
            list(zip(s.wavelength_nm, s.amplitude_pct)))

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        # local brain: RF + emission off and disconnect. Remote client: only
        # disconnects; if this GUI switched emission on, its pings stop and
        # the service's lost-client guard switches emission off.
        self.ctrl.shutdown()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a brain-like object (the in-process SuperK, or a
    SuperkClient facade). The theme is chosen ONCE here, BEFORE any widget."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # swap the active palette first
    loc = QtCore.QLocale.c()
    loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
    QtCore.QLocale.setDefault(loc)                  # gotcha #18
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
    laser, _ = build_sim_system(cfg)
    return run_app(laser, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
