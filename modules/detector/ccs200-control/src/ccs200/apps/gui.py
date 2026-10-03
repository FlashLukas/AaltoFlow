"""Control GUI for the Thorlabs CCS200 spectrometer, or the simulator.

    uv run scripts/run_gui.py                  # local simulator
    uv run scripts/run_gui.py --real           # the CCS200, in this process
    uv run scripts/run_gui.py --connect HOST   # a running service (either kind)

The window holds a Spectrometer-like object (an in-process Spectrometer or a
Ccs200Client facade) and never cares which. A 60 ms timer reads status(); a new
spectrum is fetched only when `trace_id` moved, and at most ~7 times a second
(a 10 ms integration makes ~70 scans a second -- nobody reads that fast).
Events cross into the GUI thread on a Qt signal.

Signature widget: PrismIndicator -- the spectrum as the eye would see it
through a spectroscope: a strip from 200 to 1000 nm in which every wavelength
glows in its own colour, as bright as the light there. Emission lines are
bright bars, the lamp a faint rainbow, UV and IR (which the eye cannot see) in
neutral tones. Beside it an exposure gauge: the highest raw pixel against full
scale, red at the top where the CCD saturates.
"""

from __future__ import annotations

import math
import time

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from .theme import COLORS, build_stylesheet, apply_palette, set_theme
from .settings_dialog import SettingsDialog
from .control_bar import ControlBar, mark_always

SHOW = ("Latest scan", "Last acquisition", "Dark spectrum")
_WHICH = ("last", "sample", "dark")


class Bridge(QtCore.QObject):
    """Carries spectrometer events across the thread boundary into the GUI."""
    event = QtCore.Signal(str, str)


def _card(title: str | None = None):
    frame = QtWidgets.QFrame()
    frame.setObjectName("card")
    lay = QtWidgets.QVBoxLayout(frame)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(8)
    if title:
        lbl = QtWidgets.QLabel(title.upper())
        lbl.setObjectName("cardTitle")
        lay.addWidget(lbl)
    return frame, lay


def _fmt(v, fmt: str, none: str = "--") -> str:
    return format(v, fmt) if isinstance(v, (int, float)) and math.isfinite(v) else none


def _age(seconds) -> str:
    if not (isinstance(seconds, (int, float)) and math.isfinite(seconds)):
        return "--"
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


def wavelength_rgb(nm: float) -> tuple[int, int, int]:
    """The colour of monochromatic light, roughly as the eye sees it (Dan
    Bruton's piecewise approximation). Outside 380-780 nm the eye sees nothing,
    so UV is drawn a dim violet-grey and IR a dim red-grey: still visible as
    "light is here", not pretending to be a colour.

    These are PHYSICAL colours, not theme colours -- the same in both themes,
    like a real rainbow."""
    if nm < 380:
        return (120, 100, 150)
    if nm > 780:
        return (150, 95, 95)
    if nm < 440:
        r, g, b = -(nm - 440) / 60, 0.0, 1.0
    elif nm < 490:
        r, g, b = 0.0, (nm - 440) / 50, 1.0
    elif nm < 510:
        r, g, b = 0.0, 1.0, -(nm - 510) / 20
    elif nm < 580:
        r, g, b = (nm - 510) / 70, 1.0, 0.0
    elif nm < 645:
        r, g, b = 1.0, -(nm - 645) / 65, 0.0
    else:
        r, g, b = 1.0, 0.0, 0.0
    # the eye's sensitivity falls off at both ends of the visible band
    f = 0.3 + 0.7 * (nm - 380) / 40 if nm < 420 else (
        0.3 + 0.7 * (780 - nm) / 80 if nm > 700 else 1.0)
    return tuple(int(round(255 * (c * f) ** 0.8)) for c in (r, g, b))


# ------------------------------------------------------------- the indicator

class PrismIndicator(QtWidgets.QWidget):
    """The spectrum as coloured light, plus an exposure gauge."""

    BINS = 200                        # columns of the strip (downsampled by MAX)
    WL_LO, WL_HI = 200.0, 1000.0

    def __init__(self):
        super().__init__()
        self.setMinimumSize(280, 130)
        self._levels = np.zeros(self.BINS)
        self._exposure = math.nan
        self._saturated = False
        self._peak_nm = math.nan
        self._scanning = False
        self._phase = 0.0
        self._colors = [QtGui.QColor(*wavelength_rgb(self._bin_nm(i))) for i in range(self.BINS)]
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _bin_nm(self, i: int) -> float:
        return self.WL_LO + (i + 0.5) * (self.WL_HI - self.WL_LO) / self.BINS

    def set_spectrum(self, wl_nm, spectrum):
        """Bin the spectrum onto the strip. MAX per bin, not mean: a 1.5 nm
        emission line is narrower than a bin and must not be averaged away."""
        wl = np.asarray(wl_nm, dtype=float)
        y = np.asarray(spectrum, dtype=float)
        if wl.size == 0 or wl.size != y.size:
            return
        idx = np.clip(((wl - self.WL_LO) / (self.WL_HI - self.WL_LO) * self.BINS).astype(int),
                      0, self.BINS - 1)
        levels = np.zeros(self.BINS)
        np.maximum.at(levels, idx, np.nan_to_num(y, nan=0.0))
        top = max(float(levels.max()), 0.05)     # normalise, but let a dim spectrum look dim
        self._levels = np.clip(levels / top, 0.0, 1.0)

    def set_state(self, exposure, saturated, peak_nm, scanning):
        self._exposure = exposure
        self._saturated = bool(saturated)
        self._peak_nm = peak_nm
        self._scanning = bool(scanning)

    def _tick(self):
        if not self.isVisible():
            return
        self._phase = (self._phase + 0.05) % 1.0
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        gauge_w = 30
        left, top, bottom = 6, 8, 20
        sw = w - left - gauge_w - 18
        sh = h - top - bottom
        if sw < 40 or sh < 20:
            p.end()
            return

        # the strip: every bin glows in its own colour, alpha = its brightness
        frame = QtCore.QRectF(left, top, sw, sh)
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["border"]), 1))
        p.setBrush(QtGui.QColor(COLORS["code_bg"]))
        p.drawRoundedRect(frame, 4, 4)
        bw = sw / self.BINS
        p.setPen(QtCore.Qt.NoPen)
        for i, lvl in enumerate(self._levels):
            if lvl < 0.01:
                continue
            c = QtGui.QColor(self._colors[i])
            c.setAlpha(int(40 + 215 * lvl))
            # the brighter the light, the taller the bar: a line stands out
            # even in a greyscale print of the panel
            bh = sh * (0.35 + 0.65 * lvl)
            p.setBrush(c)
            p.drawRect(QtCore.QRectF(left + i * bw, top + (sh - bh) / 2, bw + 0.6, bh))

        # the peak marker
        if math.isfinite(self._peak_nm):
            x = left + (self._peak_nm - self.WL_LO) / (self.WL_HI - self.WL_LO) * sw
            p.setBrush(QtGui.QColor(COLORS["accent_hi"]))
            tri = QtGui.QPolygonF([QtCore.QPointF(x - 5, top + sh + 7), QtCore.QPointF(x + 5, top + sh + 7),
                                   QtCore.QPointF(x, top + sh + 1)])
            p.drawPolygon(tri)

        # axis labels
        p.setPen(QtGui.QColor(COLORS["muted"]))
        f = p.font(); f.setPointSize(7); p.setFont(f)
        for nm in (200, 400, 600, 800, 1000):
            x = left + (nm - self.WL_LO) / (self.WL_HI - self.WL_LO) * sw
            align = QtCore.Qt.AlignLeft if nm == 200 else (
                QtCore.Qt.AlignRight if nm == 1000 else QtCore.Qt.AlignHCenter)
            rx = x if nm == 200 else (x - 40 if nm == 1000 else x - 20)
            p.drawText(QtCore.QRectF(rx, top + sh + 7, 40, 12), align, f"{nm}")

        # the exposure gauge: 0 at the bottom, full scale at the top, the top
        # tenth red because that is where the CCD clips
        gx = left + sw + 12
        g = QtCore.QRectF(gx, top, gauge_w, sh)
        p.setPen(QtGui.QPen(QtGui.QColor(COLORS["border"]), 1))
        p.setBrush(QtGui.QColor(COLORS["code_bg"]))
        p.drawRoundedRect(g, 4, 4)
        red = QtGui.QColor(COLORS["danger"]); red.setAlpha(60)
        p.setPen(QtCore.Qt.NoPen); p.setBrush(red)
        p.drawRect(QtCore.QRectF(gx + 1, top + 1, gauge_w - 2, sh * 0.1))
        e = self._exposure
        if isinstance(e, (int, float)) and math.isfinite(e):
            e = min(max(e, 0.0), 1.0)
            col = QtGui.QColor(COLORS["danger"] if self._saturated or e >= 0.9 else COLORS["accent"])
            if self._scanning:
                # a gentle pulse while light is being collected; never pale
                # enough to vanish on the light theme's white
                col.setAlpha(int(225 + 30 * math.sin(2 * math.pi * self._phase)))
            p.setBrush(col)
            fh = (sh - 4) * e
            p.drawRoundedRect(QtCore.QRectF(gx + 4, top + sh - 2 - fh, gauge_w - 8, fh), 2, 2)
        p.setPen(QtGui.QColor(COLORS["muted"]))
        cap = "SAT" if self._saturated else (f"{100 * e:.0f}%" if isinstance(e, float)
                                             and math.isfinite(e) else "--")
        p.drawText(QtCore.QRectF(gx - 6, top + sh + 7, gauge_w + 12, 12),
                   QtCore.Qt.AlignHCenter, cap)
        p.end()


# ------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctrl, cfg, remote: bool = False):
        super().__init__()
        self.ctrl = ctrl
        self.cfg = cfg
        self._remote = remote
        self._simulated = None           # learnt from the first status
        self.setWindowTitle("Spectrometer" + ("  (remote)" if remote else ""))
        self.resize(1240, 800)

        self._trace = None
        self._trace_id = -1
        self._last_fetch = 0.0
        self._last_acq = 0
        self._dark_id = None
        self._fetch_error = ""
        self._window = (math.nan, math.nan)

        root = QtWidgets.QWidget(); root.setObjectName("root")
        outer = QtWidgets.QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(16)
        outer.addWidget(self._build_sidebar(), 0)
        outer.addWidget(self._build_main(), 1)
        # Control or viewer (control_bar.py): a bar across the top, only for a
        # GUI on a service whose client knows about control -- a local GUI
        # owns its spectrometer and has nobody to share it with.
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

        try:
            self.ctrl.start()
        except Exception as exc:          # show it, don't crash
            self._on_event("error", f"start failed: {exc}")
        try:
            self._wl = np.asarray(self.ctrl.wavelengths(), dtype=float)
        except Exception:
            self._wl = np.zeros(0)
        self._sync_inputs(force=True)
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
        panel = QtWidgets.QWidget(); panel.setFixedWidth(340)
        col = QtWidgets.QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0); col.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("CCS200")
        title.setStyleSheet(f"color:{COLORS['accent']}; font-size:20px; font-weight:800; letter-spacing:2px;")
        header.addWidget(title)
        self.kind_label = QtWidgets.QLabel(""); self.kind_label.setObjectName("hint")
        header.addWidget(self.kind_label); header.addStretch(1)
        settings_btn = QtWidgets.QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        mark_always(settings_btn)    # a viewer may LOOK; the service refuses the OK
        header.addWidget(settings_btn)
        col.addLayout(header)

        ccard, clay = _card()
        self.conn_dot = QtWidgets.QLabel("●  connecting")
        clay.addWidget(self.conn_dot)
        col.addWidget(ccard)

        # scan settings
        scard, slay = _card("Scan")
        form = QtWidgets.QFormLayout(); form.setSpacing(6)
        lim = self.cfg.limits
        self.int_spin = self._dspin(lim.integration_min_s * 1e3, lim.integration_max_s * 1e3,
                                    3, "  ms", 1.0)
        self.avg_spin = QtWidgets.QSpinBox(); self.avg_spin.setRange(lim.averages_min, lim.averages_max)
        form.addRow("Integration", self.int_spin)
        form.addRow("Averages", self.avg_spin)
        slay.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        self.scan_time_label = QtWidgets.QLabel("—"); self.scan_time_label.setObjectName("hint")
        row.addWidget(self.scan_time_label, 1)
        apply = QtWidgets.QPushButton("Apply"); apply.setObjectName("primary")
        apply.clicked.connect(self._apply_scan)
        row.addWidget(apply)
        slay.addLayout(row)
        self.cont_chk = QtWidgets.QCheckBox("Continuous scanning")
        # .clicked fires for USER clicks only, so a status update cannot echo back (gotcha #13)
        self.cont_chk.clicked.connect(lambda on: self._call(self.ctrl.set_continuous, on))
        slay.addWidget(self.cont_chk)
        col.addWidget(scard)

        # acquisition
        acard, alay = _card("Acquire (scan-safe spectrum)")
        row = QtWidgets.QHBoxLayout()
        self.acq_btn = QtWidgets.QPushButton("Acquire"); self.acq_btn.setObjectName("primary")
        self.acq_btn.setMinimumHeight(34)
        self.acq_btn.clicked.connect(lambda: self._call(self.ctrl.acquire))
        self.abort_btn = QtWidgets.QPushButton("Abort")
        self.abort_btn.clicked.connect(lambda: self._call(self.ctrl.abort))
        mark_always(self.abort_btn)  # the SAFETY verb: works for a viewer too
        row.addWidget(self.acq_btn, 1); row.addWidget(self.abort_btn)
        alay.addLayout(row)
        self.acq_bar = QtWidgets.QProgressBar(); self.acq_bar.setRange(0, 100)
        self.acq_bar.setTextVisible(False); self.acq_bar.setFixedHeight(6)
        alay.addWidget(self.acq_bar)
        self.sample_label = QtWidgets.QLabel("no acquisition yet"); self.sample_label.setObjectName("hint")
        self.sample_label.setWordWrap(True)
        alay.addWidget(self.sample_label)
        col.addWidget(acard)

        # dark
        dcard, dlay = _card("Dark spectrum")
        row = QtWidgets.QHBoxLayout()
        self.dark_btn = QtWidgets.QPushButton("Take dark")
        self.dark_btn.setToolTip("Block the light first (cap the fibre). Acquires with the "
                                 "same averaging and keeps the mean as the dark.")
        self.dark_btn.clicked.connect(lambda: self._call(self.ctrl.take_dark))
        self.clear_dark_btn = QtWidgets.QPushButton("Clear")
        self.clear_dark_btn.clicked.connect(lambda: self._call(self.ctrl.clear_dark))
        row.addWidget(self.dark_btn, 1); row.addWidget(self.clear_dark_btn)
        dlay.addLayout(row)
        self.sub_chk = QtWidgets.QCheckBox("Subtract dark")
        self.sub_chk.clicked.connect(lambda on: self._call(self.ctrl.set_dark_subtract, on))
        dlay.addWidget(self.sub_chk)
        self.dark_label = QtWidgets.QLabel("none"); self.dark_label.setObjectName("hint")
        self.dark_label.setWordWrap(True)
        dlay.addWidget(self.dark_label)
        col.addWidget(dcard)

        # analysis window
        wcard, wlay = _card("Analysis window")
        row = QtWidgets.QHBoxLayout()
        self.wmin_spin = self._dspin(150.0, 1100.0, 1, "  nm", 1.0)
        self.wmax_spin = self._dspin(150.0, 1100.0, 1, "  nm", 1.0)
        b = QtWidgets.QPushButton("Set")
        b.setToolTip("Peak and integrated intensity look only inside this window")
        b.clicked.connect(self._apply_window)
        row.addWidget(self.wmin_spin, 1); row.addWidget(QtWidgets.QLabel("to"))
        row.addWidget(self.wmax_spin, 1); row.addWidget(b)
        wlay.addLayout(row)
        col.addWidget(wcard)

        # EDITED BUT NOT APPLIED. The status poll keeps the boxes following
        # changes made elsewhere (console, scan) -- but it used to skip only
        # the box with keyboard focus, so a value typed into Integration was
        # put back the moment you clicked into Averages, and Apply sent the old
        # one (the signalhound bug, Lukas 2026-10-01; developer notes gotcha
        # #45). A box the user changed is now "dirty": the poll leaves it
        # alone, it gets an amber outline, and its button (or Enter in it)
        # sends it and clears it.
        self._dirty: set = set()
        self._syncing = False            # True while the POLL sets values
        self._watch_edits((self.int_spin, self.avg_spin), self._apply_scan)
        self._watch_edits((self.wmin_spin, self.wmax_spin), self._apply_window)

        # the simulated light (hidden on the real instrument)
        self.sim_card, simlay = _card("Simulated light")
        self.light_chk = QtWidgets.QCheckBox("Light on the input fibre")
        self.light_chk.setToolTip("Untick to 'cap the fibre' before Take dark")
        self.light_chk.clicked.connect(lambda on: self._call(self.ctrl.set_light, on))
        simlay.addWidget(self.light_chk)
        hint = QtWidgets.QLabel("A lamp continuum plus Hg/Ar lines. Levels, dark current "
                                "and noise: Settings > Sim.")
        hint.setObjectName("hint"); hint.setWordWrap(True)
        simlay.addWidget(hint)
        col.addWidget(self.sim_card)
        col.addStretch(1)
        return panel

    @staticmethod
    def _dspin(lo, hi, decimals, suffix, step):
        s = QtWidgets.QDoubleSpinBox()
        s.setDecimals(decimals); s.setRange(lo, hi); s.setSuffix(suffix); s.setSingleStep(step)
        return s

    def _build_main(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        colw = QtWidgets.QVBoxLayout(panel)
        colw.setContentsMargins(0, 0, 0, 0); colw.setSpacing(16)

        rcard, rlay = _card("Readout")
        row = QtWidgets.QHBoxLayout(); row.setSpacing(24)
        self.big, self.big_cap = {}, {}
        for key, label, unit in (("peak", "PEAK", "nm"), ("height", "PEAK HEIGHT", "FS"),
                                 ("area", "INTEGRATED", "FS nm")):
            box = QtWidgets.QVBoxLayout(); box.setSpacing(0)
            box.addStretch(1)                 # keep caption and number together, centred
            cap = QtWidgets.QLabel(label); cap.setObjectName("hint")
            box.addWidget(cap)
            line = QtWidgets.QHBoxLayout(); line.setSpacing(6)
            v = QtWidgets.QLabel("—"); v.setObjectName("bigValue")
            v.setMinimumWidth(130); v.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            u = QtWidgets.QLabel(unit); u.setObjectName("unit")
            line.addWidget(v); line.addWidget(u, 0, QtCore.Qt.AlignBottom)
            box.addLayout(line)
            box.addStretch(1)
            self.big[key], self.big_cap[key] = v, cap
            row.addLayout(box)
        row.addStretch(1)
        self.indicator = PrismIndicator(); self.indicator.setFixedWidth(330)
        row.addWidget(self.indicator)
        rlay.addLayout(row)
        colw.addWidget(rcard)

        tcard, tlay = _card("Spectrum")
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Show"))
        self.show_combo = QtWidgets.QComboBox()
        self.show_combo.addItems(list(SHOW))
        self.show_combo.currentIndexChanged.connect(lambda _i: self._force_fetch())
        bar.addWidget(self.show_combo)
        self.log_chk = QtWidgets.QCheckBox("Log scale")
        self.log_chk.toggled.connect(self._set_log)
        # which trace to show and a log axis change only this window's view:
        # fine for a viewer
        mark_always(self.show_combo, self.log_chk)
        bar.addWidget(self.log_chk)
        bar.addStretch(1)
        self.trace_label = QtWidgets.QLabel(""); self.trace_label.setObjectName("hint")
        bar.addWidget(self.trace_label)
        tlay.addLayout(bar)
        import pyqtgraph as pg
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget(background=COLORS["code_bg"])
        self.plot.setMinimumHeight(200)
        pen = pg.mkPen(COLORS["muted"])
        for axis in ("left", "bottom"):
            ax = self.plot.getAxis(axis); ax.setPen(pen); ax.setTextPen(pen)
            ax.enableAutoSIPrefix(False)
        self.plot.setLabel("left", "intensity", units="full scale")
        self.plot.setLabel("bottom", "wavelength", units="nm")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        # the analysis window, shaded (drawn first, so the curve sits on top)
        band = QtGui.QColor(COLORS["accent"]); band.setAlpha(28)
        self.window_region = pg.LinearRegionItem(values=(200, 1000), movable=False,
                                                 brush=pg.mkBrush(band),
                                                 pen=pg.mkPen(COLORS["accent_dim"]))
        self.plot.addItem(self.window_region)
        self.curve = self.plot.plot([], [], pen=pg.mkPen(COLORS["accent"], width=1.4))
        self.peak_line = pg.InfiniteLine(angle=90, movable=False,
                                         pen=pg.mkPen(COLORS["accent_hi"], width=1,
                                                      style=QtCore.Qt.DashLine))
        self.sat_line = pg.InfiniteLine(pos=1.0, angle=0, movable=False,
                                        pen=pg.mkPen(COLORS["danger"], width=1,
                                                     style=QtCore.Qt.DotLine))
        self.plot.addItem(self.peak_line); self.plot.addItem(self.sat_line)
        tlay.addWidget(self.plot, 1)
        colw.addWidget(tcard, 1)

        lcard, llay = _card("Status log")
        self.log = QtWidgets.QPlainTextEdit(); self.log.setObjectName("log")
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMaximumHeight(120)
        llay.addWidget(self.log)
        colw.addWidget(lcard, 0)
        return panel

    # ---- actions ---------------------------------------------------------

    def _call(self, fn, *args):
        try:
            r = fn(*args)
            if isinstance(r, dict) and r.get("ok") is False:
                self._on_event("warn", r.get("error", "refused"))
        except Exception as exc:
            self._on_event("warn", f"refused: {exc}")

    def _apply_scan(self):
        """Send only what changed: each change restarts a running acquisition
        and logs an event."""
        s = self.ctrl.status()
        want_t = self.int_spin.value() / 1e3
        if not (math.isfinite(s.integration_time_s)
                and math.isclose(want_t, s.integration_time_s, rel_tol=1e-9)):
            self._call(self.ctrl.set_integration_time, want_t)
        if int(self.avg_spin.value()) != int(s.averages):
            self._call(self.ctrl.set_averages, int(self.avg_spin.value()))
        # sent: the boxes follow the instrument again (a refused value is put
        # back by the next poll, and the log says why)
        self._clear_dirty(self.int_spin, self.avg_spin)

    def _apply_window(self):
        self._call(self.ctrl.set_window, self.wmin_spin.value(), self.wmax_spin.value())
        self._clear_dirty(self.wmin_spin, self.wmax_spin)

    def _watch_edits(self, spins, apply_fn):
        """A user change marks a box dirty; Enter in it sends its group."""
        for spin in spins:
            spin.valueChanged.connect(lambda _v, s=spin: self._mark_dirty(s))
            spin.lineEdit().returnPressed.connect(apply_fn)

    def _mark_dirty(self, spin):
        if self._syncing or spin in self._dirty:
            return                       # the poll set it, or already marked
        self._dirty.add(spin)
        spin.setStyleSheet(f"border: 1px solid {COLORS['accent']};")
        spin.setToolTip("changed here, not sent yet -- press Apply / Set (or Enter)")

    def _clear_dirty(self, *spins):
        for spin in spins:
            if spin in self._dirty:
                self._dirty.discard(spin)
                spin.setStyleSheet("")
                spin.setToolTip("")

    def _set_log(self, on: bool):
        self.plot.setLogMode(y=bool(on))
        self._redraw()

    def _open_settings(self):
        self.ctrl.get_config()          # no-op locally; fetch over the socket if remote
        SettingsDialog(self.ctrl, self.cfg, lambda: self._sync_inputs(force=True), self).exec()

    # ---- refresh & events ------------------------------------------------

    def _on_event(self, level: str, msg: str):
        color = COLORS["danger"] if level == "error" else (
            COLORS["accent"] if level == "warn" else COLORS["muted"])
        stamp = time.strftime("%H:%M:%S")
        self.log.appendHtml(
            f'<span style="color:{COLORS["accent_dim"]}">{stamp}</span> '
            f'<span style="color:{color}">{msg}</span>')

    def _sync_inputs(self, force: bool = False):
        """Input boxes follow settings changed ELSEWHERE (console, scan), but
        never while the user is typing in them, and never a box the user
        changed and has not applied yet (``_dirty``). ``force`` (after the
        Settings dialog) puts every box back to the instrument's value."""
        s = self.ctrl.status()
        if force:
            self._clear_dirty(*list(self._dirty))

        def free(spin):
            return force or (not spin.hasFocus() and spin not in self._dirty)

        self._syncing = True             # these setValue calls are not user edits
        try:
            for spin, val in ((self.int_spin, s.integration_time_s * 1e3),
                              (self.wmin_spin, s.window_min_nm), (self.wmax_spin, s.window_max_nm)):
                if isinstance(val, (int, float)) and math.isfinite(val) and free(spin):
                    spin.setValue(val)
            if free(self.avg_spin):
                self.avg_spin.setValue(int(s.averages))
        finally:
            self._syncing = False
        for chk, val in ((self.cont_chk, s.continuous), (self.sub_chk, s.dark_subtract),
                         (self.light_chk, s.light_on)):
            chk.blockSignals(True)
            chk.setChecked(bool(val))
            chk.blockSignals(False)

    def _force_fetch(self):
        self._trace_id = -1
        self._last_fetch = 0.0

    def _set_simulated(self, simulated: bool):
        """Things that depend on WHICH instrument this is, set once it is known."""
        if simulated == self._simulated:
            return
        self._simulated = simulated
        self.kind_label.setText("SIMULATED" if simulated else "CCS200/M")
        self.setWindowTitle(("Spectrometer - simulated CCS200" if simulated
                             else "Spectrometer - Thorlabs CCS200/M")
                            + ("  (remote)" if self._remote else ""))
        self.sim_card.setVisible(simulated)

    def _refresh(self):
        if self._control_bar is not None:
            self._control_bar.refresh()
        s = self.ctrl.status()
        now = time.monotonic()
        self._set_simulated(bool(getattr(s, "simulated", True)))

        # connection
        if s.hw_error:
            self.conn_dot.setText("●  error"); self.conn_dot.setToolTip(s.hw_error)
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")
        elif s.connected:
            self.conn_dot.setText("●  scanning" if s.scanning else "●  idle")
            self.conn_dot.setStyleSheet(f"color:{COLORS['ok']}; font-weight:700;")
            self.conn_dot.setToolTip(s.idn)
        else:
            self.conn_dot.setText("●  offline")
            self.conn_dot.setStyleSheet(f"color:{COLORS['danger']}; font-weight:700;")

        self.scan_time_label.setText(f"{_fmt(s.scan_time_s * 1e3, '.4g')} ms per scan, "
                                     f"{s.scans} scans")
        self._sync_inputs()

        # the analysis window, shaded on the plot
        win = (s.window_min_nm, s.window_max_nm)
        if win != self._window and all(math.isfinite(x) for x in win):
            self._window = win
            self.window_region.setRegion(win)
            # shade only a REAL restriction: a window over the whole range would
            # just tint the entire plot and say nothing
            full = (win[0] <= s.wl_min_nm + 0.5 and win[1] >= s.wl_max_nm - 0.5)
            self.window_region.setVisible(not full)

        # acquisition
        self.acq_bar.setValue(int(100 * s.acq_progress) if s.acquiring else 0)
        self.acq_btn.setEnabled(bool(s.connected) and not s.acquiring)
        self.dark_btn.setEnabled(bool(s.connected) and not s.acquiring)
        self.abort_btn.setEnabled(bool(s.acquiring))
        smp = s.sample
        if smp and smp.get("acq_id") != self._last_acq:
            self._last_acq = smp.get("acq_id")
            what = "dark" if smp.get("dark") else "acquisition"
            if smp.get("aborted"):
                self.sample_label.setText(f"#{smp.get('acq_id')}: {what} aborted")
            elif smp.get("error"):
                self.sample_label.setText(f"#{smp.get('acq_id')}: {smp.get('error')}")
            else:
                self.sample_label.setText(
                    f"#{smp.get('acq_id')} {what}: {smp.get('averages')} x "
                    f"{_fmt(smp.get('integration_time_s', math.nan) * 1e3, '.4g')} ms, peak "
                    f"{_fmt(smp.get('peak_nm'), '.2f')} nm = {_fmt(smp.get('peak_intensity'), '.3f')} FS"
                    + (", dark subtracted" if smp.get("dark_applied") else "")
                    + ("  SATURATED" if smp.get("saturated") else ""))
            if self.show_combo.currentIndex() == 1:
                self._force_fetch()

        # dark
        d = s.dark or {}
        if s.acquiring and getattr(s, "acq_is_dark", False):
            self.dark_label.setText(f"taking dark #{s.acq_id} ... (is the light blocked?)")
        elif d.get("present"):
            fits = "fits" if d.get("matches") else "does NOT fit the integration time"
            self.dark_label.setText(
                f"#{d.get('acq_id')}: {_fmt(d.get('integration_time_s', math.nan) * 1e3, '.4g')} ms, "
                f"{d.get('averages')} scans, {_age(d.get('age_s'))} -- {fits}")
        else:
            self.dark_label.setText("none" + ("  (subtraction is ON: acquire is refused)"
                                              if s.dark_subtract else ""))
        dark_id = d.get("acq_id") if d.get("present") else None
        if dark_id != self._dark_id:
            self._dark_id = dark_id
            if self.show_combo.currentIndex() == 2:
                self._force_fetch()

        # the indicator follows the LIVE scan whatever the plot shows
        self.indicator.set_state(s.exposure, s.saturated, s.peak_nm, s.scanning)

        # spectrum: fetch only when there is a new one, and not more than ~7 per second
        which = _WHICH[self.show_combo.currentIndex()]
        new = s.trace_id != self._trace_id if which == "last" else self._trace_id == -1
        if new and now - self._last_fetch > 0.14:
            self._last_fetch = now
            try:
                self._trace = self.ctrl.get_trace(which)
                self._fetch_error = ""
                self._trace_id = s.trace_id
                self._redraw()
            except Exception as exc:
                self._fetch_error = str(exc)    # nothing measured yet, or no dark
                self._trace_id = s.trace_id if which == "last" else 0
                self.trace_label.setText(self._fetch_error)

    def _redraw(self):
        t = self._trace
        if t is None:
            return
        wl = t.get("wavelengths_nm")
        y = t.get("spectrum")
        if wl is None or y is None or len(wl) != len(y):
            return
        yy = np.clip(y, 1e-5, None) if self.log_chk.isChecked() else y
        self.curve.setData(wl, yy)
        self.indicator.set_spectrum(wl, y)
        label = (f"{_fmt(t.get('integration_time_s', math.nan) * 1e3, '.4g')} ms"
                 f" x {t.get('averages', 1)}"
                 + (", dark subtracted" if t.get("dark_applied") else ", raw")
                 + ("  SATURATED" if t.get("saturated") else ""))
        self.trace_label.setText(label)
        peak = t.get("peak_nm", math.nan)
        ok = isinstance(peak, (int, float)) and math.isfinite(peak)
        self.peak_line.setVisible(ok)
        if ok:
            self.peak_line.setPos(peak)
        self.big["peak"].setText(_fmt(peak, ".2f"))
        self.big["height"].setText(_fmt(t.get("peak_intensity"), ".3f"))
        self.big["area"].setText(_fmt(t.get("integrated"), ".2f"))

    def closeEvent(self, ev: QtGui.QCloseEvent):
        self.timer.stop()
        self.ctrl.shutdown()
        super().closeEvent(ev)


def run_app(ctrl, cfg, remote: bool = False) -> int:
    """Start the Qt app with a Spectrometer-like object. The theme is chosen
    ONCE here, from cfg.ui.theme, BEFORE any widget is built."""
    set_theme(getattr(cfg.ui, "theme", "dark"))
    # '.' decimal point and no thousands separator whatever the Windows locale
    # (suite gotcha #18).
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
    """Run against the built-in simulator, in-process."""
    from ..config import Config
    from ..sim_system import build_sim_system
    cfg = Config()
    if theme:
        cfg.ui.theme = theme
    spec, _ = build_sim_system(cfg)
    return run_app(spec, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
