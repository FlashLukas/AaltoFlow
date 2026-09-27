"""Control GUI for the Cornerstone 260 monochromator.

Run it (after `uv sync --extra gui`) with:
    uv run scripts/run_gui.py                 # local simulator
    uv run scripts/run_gui.py --connect HOST  # a running service

Architecture in one breath: this window holds a Monochromator-like object (the
in-process brain, or a Cs260Client facade for a remote service). It sends
commands (set_wavelength / set_grating / set_shutter / ...) and reads a status
snapshot on a Qt timer. Brain events arrive on a Qt signal so they can safely
cross into the GUI thread.

The signature widget is the DispersionIndicator: white light enters, hits the
grating, fans out into its colours, and only the selected wavelength passes the
exit slit -- unless the shutter is closed. Underneath, the grating's whole range
as a spectrum ruler with a marker where the drive is.
"""

from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import Config, parse_labels
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


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def wavelength_color(nm: float, light_theme: bool = False) -> QtGui.QColor:
    """An RGB colour for a wavelength, for DRAWING (not colorimetry).

    Visible 380-780 nm: the usual piecewise approximation of the spectrum, with
    the intensity rolled off at both ends where the eye stops seeing. Outside:
    UV drawn as a dim violet, IR as a dim deep red, zero order (the grating
    acting as a mirror) as the theme's text colour -- 'white light'. On the
    light theme the colours are darkened so yellow still shows on white."""
    if nm < 60.0:
        return QtGui.QColor(COLORS["text"])
    if nm < 380.0:
        r, g, b, f = 0.45, 0.25, 0.75, 0.55
    elif nm > 780.0:
        f = max(0.35, 0.7 - (nm - 780.0) / 3000.0)
        r, g, b = 0.75, 0.1, 0.15
    else:
        if nm < 440:
            r, g, b = -(nm - 440) / 60.0, 0.0, 1.0
        elif nm < 490:
            r, g, b = 0.0, (nm - 440) / 50.0, 1.0
        elif nm < 510:
            r, g, b = 0.0, 1.0, -(nm - 510) / 20.0
        elif nm < 580:
            r, g, b = (nm - 510) / 70.0, 1.0, 0.0
        elif nm < 645:
            r, g, b = 1.0, -(nm - 645) / 65.0, 0.0
        else:
            r, g, b = 1.0, 0.0, 0.0
        if nm < 420:
            f = 0.3 + 0.7 * (nm - 380) / 40.0
        elif nm > 700:
            f = 0.3 + 0.7 * (780 - nm) / 80.0
        else:
            f = 1.0
    k = 0.72 if light_theme else 1.0
    return QtGui.QColor(int(255 * r * f * k), int(255 * g * f * k), int(255 * b * f * k))


def _is_light() -> bool:
    return QtGui.QColor(COLORS["bg"]).lightness() > 128


# ------------------------------------------------------------- the indicator

class DispersionIndicator(QtWidgets.QWidget):
    """A grating dispersing light, with the exit slit picking one colour.

    The grating tilts with the wavelength (as the real turret does), the fan of
    colours shifts so the SELECTED wavelength is always the ray through the
    slit, and a closed shutter blocks it. While the drive moves, a dashed tick
    marks the target on the ruler and the fan shimmers."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(210)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self._wl = 0.0
        self._target = float("nan")
        self._lo, self._hi = 0.0, 1000.0
        self._moving = False
        self._shutter = False
        self._grating = 1
        self._lines = 1200
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)            # ~30 fps, only while moving
        self._timer.timeout.connect(self._tick)

    def set_state(self, wl, target, lo, hi, moving, shutter_open, grating, lines):
        self._wl = float(wl) if _finite(wl) else 0.0
        self._target = float(target) if _finite(target) else float("nan")
        self._lo, self._hi = float(lo), max(float(lo) + 1.0, float(hi))
        self._moving = bool(moving)
        self._shutter = bool(shutter_open)
        self._grating, self._lines = int(grating or 1), int(lines or 1200)
        if self._moving and not self._timer.isActive():
            self._timer.start()
        elif not self._moving and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        self._phase = (self._phase + 0.06) % 1.0
        self.update()

    def paintEvent(self, ev):
        QPainter, QColor, QPen = QtGui.QPainter, QtGui.QColor, QtGui.QPen
        QPointF, QRectF, Qt = QtCore.QPointF, QtCore.QRectF, QtCore.Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        light = _is_light()
        metal = QColor(COLORS["muted"])
        text = QColor(COLORS["text"])

        bench_h = h - 58                       # optics on top, ruler below
        gy = bench_h * 0.55
        gx = w * 0.30
        slit_x = w * 0.80

        # ---- entrance beam: white light from the left -------------------------
        beam = QColor(text); beam.setAlpha(170)
        p.setPen(QPen(beam, 3))
        p.drawLine(QPointF(10, gy - 30), QPointF(gx, gy))
        p.setPen(QPen(metal, 2))                # entrance slit
        p.drawLine(QPointF(24, gy - 44), QPointF(24, gy - 30 - 5))
        p.drawLine(QPointF(24, gy - 30 + 5), QPointF(24, gy - 16))

        # ---- the fan of colours --------------------------------------------------
        # Rays at angles -a..+a around the axis to the slit; ray k carries the
        # wavelength wl + k * span, so the SELECTED one (k = 0) goes to the slit.
        # Every ray ends on the slit plane, and the fan is kept inside the
        # bench so it never runs into the ruler below.
        span = max(4.0, 25.0 * 1200.0 / max(1, self._lines))   # nm per ray step
        n = 12
        half = min(gy - 8, bench_h - gy - 4)                     # px above/below the axis
        dx = slit_x - gx
        for k in range(-n, n + 1):
            lam = self._wl + k * span
            if lam < 0:
                continue
            ang = math.atan2(half * k / n, dx)
            length = dx / math.cos(ang)
            col = wavelength_color(lam, light)
            alpha = 90 if k else 255
            if self._moving:
                alpha = int(alpha * (0.6 + 0.4 * math.sin(2 * math.pi * (self._phase + k / 7.0)) ** 2))
            col.setAlpha(alpha)
            p.setPen(QPen(col, 3.2 if k == 0 else 2.0))
            end = QPointF(gx + length * math.cos(ang), gy + length * math.sin(ang))
            p.drawLine(QPointF(gx, gy), end)

        # ---- the grating: a ruled block, tilted with the wavelength --------------
        frac = (self._wl - self._lo) / (self._hi - self._lo) if self._hi > self._lo else 0.0
        tilt = -25.0 + 40.0 * min(1.0, max(0.0, frac))
        p.save()
        p.translate(gx, gy)
        p.rotate(tilt)
        p.setPen(QPen(metal, 1.5))
        p.setBrush(QColor(COLORS["panel_hi"]))
        p.drawRect(QRectF(-5, -26, 12, 52))
        p.setPen(QPen(QColor(COLORS["accent"]), 1.2))
        for i in range(-22, 23, 4):              # the rulings
            p.drawLine(QPointF(-5, i), QPointF(-1, i))
        p.restore()
        p.setPen(QColor(COLORS["muted"]))
        f = p.font(); f.setPointSize(8); f.setBold(True); p.setFont(f)
        p.drawText(QRectF(gx - 60, gy + 34, 120, 14), Qt.AlignHCenter,
                   f"G{self._grating}  {self._lines} l/mm")

        # ---- exit slit + shutter ----------------------------------------------------
        gap = 7.0
        p.setPen(QPen(metal, 3))
        jaw = half + 6                          # the jaws catch the whole fan
        p.drawLine(QPointF(slit_x, gy - jaw), QPointF(slit_x, gy - gap))
        p.drawLine(QPointF(slit_x, gy + gap), QPointF(slit_x, gy + jaw))
        sel = wavelength_color(self._wl, light)
        if self._shutter:
            # the selected colour leaves the instrument
            glow = QColor(sel); glow.setAlpha(60)
            p.setPen(QPen(glow, 10))
            p.drawLine(QPointF(slit_x, gy), QPointF(w - 10, gy))
            p.setPen(QPen(sel, 3.2))
            p.drawLine(QPointF(slit_x, gy), QPointF(w - 10, gy))
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(COLORS["danger"]))
            p.drawRoundedRect(QRectF(slit_x + 4, gy - 16, 8, 32), 2, 2)
            p.setPen(QColor(COLORS["danger"]))
            p.drawText(QRectF(slit_x + 16, gy - 7, w - slit_x - 16, 14),
                       Qt.AlignLeft | Qt.AlignVCenter, "SHUTTER")

        # ---- spectrum ruler over the grating's range -----------------------------------
        ry, rh = h - 44, 12
        x0, x1 = 12.0, w - 12.0
        steps = 160
        for i in range(steps):
            lam = self._lo + (self._hi - self._lo) * (i + 0.5) / steps
            c = wavelength_color(lam, light)
            p.setPen(Qt.NoPen); p.setBrush(c)
            xa = x0 + (x1 - x0) * i / steps
            p.drawRect(QRectF(xa, ry, (x1 - x0) / steps + 0.6, rh))
        p.setBrush(Qt.NoBrush); p.setPen(QPen(QColor(COLORS["border"]), 1))
        p.drawRect(QRectF(x0, ry, x1 - x0, rh))

        def xof(lam):
            return x0 + (x1 - x0) * (lam - self._lo) / (self._hi - self._lo)

        p.setPen(QColor(COLORS["muted"]))
        f.setBold(False); f.setPointSize(7); p.setFont(f)
        p.drawText(QRectF(x0, ry + rh + 2, 80, 12), Qt.AlignLeft, f"{self._lo:g} nm")
        p.drawText(QRectF(x1 - 80, ry + rh + 2, 80, 12), Qt.AlignRight, f"{self._hi:g} nm")

        if self._moving and _finite(self._target):
            xt = xof(min(self._hi, max(self._lo, self._target)))
            pen = QPen(QColor(COLORS["accent_hi"]), 1.6, Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(xt, ry - 8), QPointF(xt, ry + rh + 3))
        xm = xof(min(self._hi, max(self._lo, self._wl)))
        tri = QtGui.QPolygonF([QPointF(xm - 6, ry - 9), QPointF(xm + 6, ry - 9),
                               QPointF(xm, ry - 1)])
        p.setPen(Qt.NoPen); p.setBrush(QColor(COLORS["accent"]))
        p.drawPolygon(tri)
        p.setPen(QPen(QColor(COLORS["accent"]), 1.4))
        p.drawLine(QPointF(xm, ry - 1), QPointF(xm, ry + rh))

        # ---- caption -------------------------------------------------------------------
        if self._moving:
            cap, col = "MOVING", QColor(COLORS["accent_hi"])
        elif not self._shutter:
            cap, col = "SHUTTER CLOSED", QColor(COLORS["muted"])
        else:
            cap, col = "LIGHT OUT", QColor(COLORS["ok"])
        f.setBold(True); f.setPointSize(8); p.setFont(f)
        p.setPen(col)
        p.drawText(QRectF(w - 150, 4, 140, 14), Qt.AlignRight, cap)
        p.end()


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg: Config, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        title = "Cornerstone 260 - Monochromator"
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
        self._fill_choices()
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
        title = QtWidgets.QLabel("CS260")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        if self._remote:
            settings_btn.setToolTip("Edits the service's settings over the network.")
        header.addWidget(settings_btn)
        col.addLayout(header)

        rcard, rlay = _card()
        top = QtWidgets.QHBoxLayout()
        self.state_badge = QtWidgets.QLabel("IDLE")
        self.state_badge.setObjectName("stateBadge")
        top.addWidget(self.state_badge); top.addStretch(1)
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        self.conn_dot.setStyleSheet(f"color:{COLORS['muted']}; font-weight:600;")
        top.addWidget(self.conn_dot)
        rlay.addLayout(top)
        self.idn_label = QtWidgets.QLabel("-")
        self.idn_label.setStyleSheet(f"color:{COLORS['muted']}; font-size:11px;")
        self.idn_label.setWordWrap(True)
        rlay.addWidget(self.idn_label)
        col.addWidget(rcard)

        # wavelength
        wcard, wlay = _card("Wavelength")
        wrow = QtWidgets.QHBoxLayout()
        self.wl_spin = QtWidgets.QDoubleSpinBox()
        self.wl_spin.setDecimals(3); self.wl_spin.setSingleStep(1.0)
        self.wl_spin.setSuffix("  nm")
        self.wl_spin.setRange(0.0, 3000.0)
        go = QtWidgets.QPushButton("Go"); go.setObjectName("primary")
        go.clicked.connect(self._go_wavelength)
        self.wl_spin.lineEdit().returnPressed.connect(self._go_wavelength)
        wrow.addWidget(self.wl_spin, 1); wrow.addWidget(go)
        wlay.addLayout(wrow)
        self.range_hint = QtWidgets.QLabel(""); self.range_hint.setObjectName("hint")
        wlay.addWidget(self.range_hint)
        col.addWidget(wcard)

        # grating + shutter
        gcard, glay = _card("Grating")
        grow = QtWidgets.QHBoxLayout()
        self.grat_combo = QtWidgets.QComboBox()
        gset = QtWidgets.QPushButton("Set"); gset.setObjectName("primary")
        gset.clicked.connect(self._set_grating)
        grow.addWidget(self.grat_combo, 1); grow.addWidget(gset)
        glay.addLayout(grow)
        col.addWidget(gcard)

        scard, slay = _card("Shutter")
        self.shutter_btn = QtWidgets.QPushButton("Open shutter")
        self.shutter_btn.setObjectName("primary")
        self.shutter_btn.setMinimumHeight(36)
        self.shutter_btn.clicked.connect(self._toggle_shutter)
        slay.addWidget(self.shutter_btn)
        col.addWidget(scard)
        self._shutter_open = False

        # accessories (present or not, the card says so)
        acard, alay = _card("Accessories")
        frow = QtWidgets.QHBoxLayout()
        frow.addWidget(QtWidgets.QLabel("Filter"))
        self.filter_combo = QtWidgets.QComboBox()
        self.filter_btn = QtWidgets.QPushButton("Set")
        self.filter_btn.clicked.connect(self._set_filter)
        frow.addWidget(self.filter_combo, 1); frow.addWidget(self.filter_btn)
        alay.addLayout(frow)
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(QtWidgets.QLabel("Exit port"))
        self.port_combo = QtWidgets.QComboBox()
        self.port_btn = QtWidgets.QPushButton("Set")
        self.port_btn.clicked.connect(self._set_port)
        prow.addWidget(self.port_combo, 1); prow.addWidget(self.port_btn)
        alay.addLayout(prow)
        self.acc_hint = QtWidgets.QLabel(""); self.acc_hint.setObjectName("hint")
        self.acc_hint.setWordWrap(True)
        alay.addWidget(self.acc_hint)
        col.addWidget(acard)

        col.addStretch(1)
        abort = QtWidgets.QPushButton("Abort motion"); abort.setObjectName("danger")
        abort.setMinimumHeight(38)
        abort.clicked.connect(self._abort)
        col.addWidget(abort)
        return panel

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        ocard, olay = _card("Exit slit")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(24)
        self.wl_value = self._readout(row, "Wavelength", "nm", minw=170)
        self.target_value = self._readout(row, "Target", "nm", minw=150)
        self.bp_value = self._readout(row, "Bandpass", "nm", minw=90)
        self.grat_value = self._readout(row, "Grating", "l/mm", minw=90)
        row.addStretch(1)
        olay.addLayout(row)
        self.disp = DispersionIndicator()
        olay.addWidget(self.disp)
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
        val = QtWidgets.QLabel("-"); val.setObjectName("bigValue")
        val.setMinimumWidth(minw)
        u = QtWidgets.QLabel(unit); u.setObjectName("unit")
        line.addWidget(val); line.addWidget(u, 0, QtCore.Qt.AlignBottom)
        box.addWidget(cap); box.addLayout(line)
        holder = QtWidgets.QWidget(); holder.setLayout(box)
        row.addWidget(holder)
        return val

    def _fill_choices(self):
        """(Re)build the grating / filter / port lists from cfg."""
        g = self.cfg.gratings
        self.grat_combo.clear()
        for n in range(1, max(1, min(3, int(g.count))) + 1):
            lines, label, _, _ = g.of(n)
            self.grat_combo.addItem(f"{n}  -  {lines} l/mm  {label}", n)
        acc = self.cfg.accessories
        self.filter_combo.clear()
        labels = parse_labels(acc.filter_labels, 6)
        for n in range(1, max(1, min(6, int(acc.filter_count))) + 1):
            self.filter_combo.addItem(f"{n}  -  {labels[n - 1]}", n)
        self.port_combo.clear()
        for n, lab in enumerate(parse_labels(acc.port_labels, 2), start=1):
            self.port_combo.addItem(f"{n}  -  {lab}", n)
        for w in (self.filter_combo, self.filter_btn):
            w.setEnabled(bool(acc.filter_wheel))
        for w in (self.port_combo, self.port_btn):
            w.setEnabled(bool(acc.dual_port))
        missing = [n for n, on in (("filter wheel", acc.filter_wheel),
                                   ("second exit port", acc.dual_port)) if not on]
        self.acc_hint.setText(("Not fitted: " + ", ".join(missing) +
                               " (Settings > Accessories).") if missing else
                              ("Order sorting: automatic." if acc.auto_filter else ""))

    # ---- actions ---------------------------------------------------------

    def _call(self, fn, *args):
        """Run a command; a refusal (clamp is not one) goes to the log."""
        try:
            return fn(*args)
        except Exception as exc:
            self._on_event("error", str(exc))
            return None

    def _go_wavelength(self):
        self._call(self.ctrl.set_wavelength, self.wl_spin.value())

    def _set_grating(self):
        n = self.grat_combo.currentData()
        if n:
            self._call(self.ctrl.set_grating, int(n))

    def _toggle_shutter(self):
        self._call(self.ctrl.set_shutter, not self._shutter_open)

    def _set_filter(self):
        n = self.filter_combo.currentData()
        if n:
            self._call(self.ctrl.set_filter, int(n))

    def _set_port(self):
        n = self.port_combo.currentData()
        if n:
            self._call(self.ctrl.set_port, int(n))

    def _abort(self):
        self._call(self.ctrl.abort)

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        dlg = SettingsDialog(self.ctrl, self.cfg, self._on_settings_applied, self)
        dlg.exec()

    def _on_settings_applied(self):
        self._fill_choices()

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
        self._shutter_open = bool(s.shutter_open)

        self.wl_value.setText(f"{s.wavelength_nm:.3f}" if _finite(s.wavelength_nm) else "-")
        self.target_value.setText(f"{s.target_nm:.3f}" if _finite(s.target_nm) else "-")
        self.bp_value.setText(f"{s.bandpass_nm:.2f}" if _finite(s.bandpass_nm) else "-")
        self.grat_value.setText(f"{s.grating_lines}" if s.grating_lines else "-")

        lo, hi = float(s.wl_min_nm), float(s.wl_max_nm)
        if (lo, hi) != getattr(self, "_range", None):
            self._range = (lo, hi)
            self.wl_spin.setRange(lo, hi)
            self.range_hint.setText(f"Grating {s.grating_target}: {lo:g} .. {hi:g} nm")
            if not getattr(self, "_spin_primed", False) and _finite(s.target_nm):
                self.wl_spin.setValue(float(s.target_nm))
                self._spin_primed = True

        state = ("MOVING " + s.busy.upper()) if s.moving else "IDLE"
        if state != getattr(self, "_badge_state", None):
            self._badge_state = state
            self.state_badge.setText(state)
            self._badge_color(COLORS["accent"] if s.moving else COLORS["ok"])

        if s.shutter_open != getattr(self, "_btn_state", None):
            self._btn_state = s.shutter_open
            self.shutter_btn.setText("Close shutter" if s.shutter_open else "Open shutter")
            self.shutter_btn.setObjectName("danger" if s.shutter_open else "primary")
            # re-apply QSS after the objectName (selector) changed
            self.shutter_btn.style().unpolish(self.shutter_btn)
            self.shutter_btn.style().polish(self.shutter_btn)

        if s.connected and not s.hw_error:
            self.conn_dot.setText("●  connected" + ("  (sim)" if s.simulated else ""))
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
        else:
            self.conn_dot.setText("●  " + ("hardware error" if s.hw_error else "offline"))
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        if s.idn:
            self.idn_label.setText(s.idn)

        self.disp.set_state(s.wavelength_nm, s.target_nm, lo, hi, s.moving,
                            s.shutter_open, s.grating, s.grating_lines)

    def _badge_color(self, color):
        self.state_badge.setStyleSheet(
            f"QLabel#stateBadge {{ color:{color}; border-color:{color}; "
            f"background:{COLORS['panel_hi']}; border-radius:10px; padding:4px 12px; "
            f"font-weight:700; letter-spacing:1px; }}")

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()          # local: closes the instrument; remote: closes the client
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Monochromator-like object (the in-process brain,
    or a Cs260Client facade for a remote service). The theme is chosen ONCE
    here, from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))     # swap the active palette first
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # Qt number widgets follow the Windows locale ("10,000" for 10 ms on the lab
    # PC, gotcha #18): use the C locale, no group separators.
    loc = QtCore.QLocale.c()
    loc.setNumberOptions(QtCore.QLocale.OmitGroupSeparator)
    QtCore.QLocale.setDefault(loc)
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
    mono, _ = build_sim_system(cfg)
    return run_app(mono, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
